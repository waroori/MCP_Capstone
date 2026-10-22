"""에이전트 런타임 통합 테스트. 스크립트 모델(키 불필요)로 그래프·승인 interrupt·재개·이벤트를 확인한다."""

import json
import os
import time

os.environ["LLM_MODEL"] = "fake"
os.environ["MCP_WORK_URL"] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from apps.agent_runtime.harness import tools as tools_mod  # noqa: E402
from apps.agent_runtime.main import app as agent_app  # noqa: E402
from apps.backend.main import app as backend_app  # noqa: E402

tools_mod.TRANSPORT_OVERRIDE = httpx.ASGITransport(app=backend_app)
EMP = {"Authorization": "Bearer dev.emp_mkt_01"}
ASSET = {"Authorization": "Bearer dev.emp_asset_01"}


def events(c: TestClient, task_id: str, headers: dict, after: int = 0) -> list[dict]:
    out = []
    with c.stream("GET", f"/ai/tasks/{task_id}/stream", params={"after": after}, headers=headers, timeout=30) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:]))
    return out


def wait_status(c: TestClient, task_id: str, headers: dict, wanted: set[str], timeout: float = 10) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"/ai/tasks/{task_id}", headers=headers).json()["data"]
        if t["status"] in wanted:
            return t
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting {wanted}, last={t['status']} error={t.get('error')}")


@pytest.fixture(scope="module")
def client():
    with TestClient(agent_app) as c:
        yield c


def test_auth_required(client):
    assert client.post("/ai/tasks", json={"input": "hi"}).status_code == 401


def test_profile_question_runs_to_done_with_evidence(client):
    r = client.post("/ai/tasks", json={"input": "내 등록 업무 알려줘"}, headers=EMP)
    assert r.status_code == 202
    task_id = r.json()["data"]["task_id"]
    evs = events(client, task_id, EMP)
    types = [e["type"] for e in evs]
    assert types[:2] == ["TOOL_START", "TOOL_END"] and types[-1] == "DONE"
    assert evs[0]["tool_call"]["name"] == "get_my_profile"
    t = wait_status(client, task_id, EMP, {"done"})
    assert t["result"]["evidence"][0]["source"] == "get_my_profile"
    assert t["turns"] == 2


def test_employee_seat_request_waits_for_admin(client):
    """직원 사용 신청: 의도 분류 → create_action_plan → 좌석이 없으면 승인 대기. 에이전트는 실행하지 않고 '전달했어요'라고 답한다."""
    from apps.backend import db
    with db.tx() as c:  # 좌석을 꽉 채워 승인 대기로 들어가게 한다 (남으면 바로 배정)
        c.execute(db.text("UPDATE contracts SET seats_purchased = (SELECT count(*) FROM seat_assignments WHERE contract_id = 'ctr_chatgpt_2026' AND status = 'assigned') WHERE id = 'ctr_chatgpt_2026'"))
    task_id = client.post("/ai/tasks", json={"input": "AI-A 이용권 신청하고 싶어요", "channel": "slack"}, headers=EMP).json()["data"]["task_id"]
    evs = events(client, task_id, EMP)
    ends = [e for e in evs if e["type"] == "TOOL_END"]
    assert [e["tool_call"]["name"] for e in ends] == ["create_action_plan"] and evs[-1]["type"] == "DONE"
    assert json.loads(ends[0]["tool_call"]["output"])["status"] == "pending_approval"
    t = wait_status(client, task_id, EMP, {"done"})
    assert "전달했어요" in t["result"]["answer"] and t["result"]["plan_id"].startswith("plan_")


def test_release_and_api_access_intents_create_plans(client):
    # 반납 요청은 좌석이 있어야 승인 대기로 들어간다: 먼저 관리자가 배정을 승인·실행해 둔다
    from apps.backend.main import app as backend_app
    from fastapi.testclient import TestClient as BackendClient
    bc = BackendClient(backend_app)
    r = bc.post("/plans", json={"type": "seat_assign", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "t", "evidence": []}, headers=EMP).json()
    if "data" in r:
        pid = r["data"]["id"]
        bc.post(f"/plans/{pid}/approve", json={"reason": "t"}, headers=ASSET)
        bc.post(f"/plans/{pid}/execute", headers={**ASSET, "Idempotency-Key": f"k_{pid}"})
    for text, plan_type in [("이제 안써요, 반납할게요", "seat_reclaim"), ("세일즈 분석용으로 B API 사용해야 해요", "api_access")]:
        task_id = client.post("/ai/tasks", json={"input": text, "channel": "slack"}, headers=EMP).json()["data"]["task_id"]
        evs = events(client, task_id, EMP)
        ends = [e for e in evs if e["type"] == "TOOL_END"]
        assert ends and ends[0]["tool_call"]["name"] == "create_action_plan", text
        assert ends[0]["tool_call"]["input"]["type"] == plan_type
        assert json.loads(ends[0]["tool_call"]["output"])["status"] == "pending_approval"


def test_claim_without_plan_is_corrected(client):
    """사용 신청 의도인데 처리 요청을 만들지 않고 '전달했어요'라고 말하면 finalize가 교정한다."""
    from apps.agent_runtime.graph import finalize
    import asyncio
    from langchain_core.messages import AIMessage
    from apps.agent_runtime import graph as g
    from apps.agent_runtime.graph import Runtime
    g.RUNTIMES["t_claim"] = Runtime(trace_id="t", model=None, fallback=None, tools=[])
    state = {"task_id": "t_claim", "messages": [AIMessage(content="관리자에게 전달했어요. 결정되면 알려드릴게요.")], "intent": "access_request", "plan_created": False, "requester_role": "employee", "channel": "slack"}
    out = asyncio.run(finalize(state))
    assert "아직 관리자에게 전달되지 않았어요" in out["answer"] and "전달했어요" not in out["answer"].split("아직")[0]
    g.RUNTIMES.pop("t_claim", None)


def test_reclaim_needs_approval_then_execute_interrupts_and_resumes(client):
    """중간 위험(회수·재배정): 조치안 → 담당자 승인(백엔드) → 에이전트 실행 요청 → 실행 도구 앞 interrupt → 승인 → 실행."""
    r = client.post("/ai/tasks", json={"input": "한도윤 님 휴직 중이라 이용권 회수해서 박지후 님에게 재배정해줘"}, headers=ASSET)
    task_id, thread_id = r.json()["data"]["task_id"], r.json()["data"]["thread_id"]
    evs = events(client, task_id, ASSET)
    assert [e["type"] for e in evs] == ["TOOL_START", "TOOL_END", "TOKEN", "DONE"]
    created = json.loads(evs[1]["tool_call"]["output"])
    plan_id = created["plan_id"]
    assert created["status"] == "pending_approval" and created["risk_level"] == "medium"
    # 담당자가 화면(백엔드)에서 승인
    from apps.backend.main import app as backend_app
    from fastapi.testclient import TestClient as BackendClient
    assert BackendClient(backend_app).post(f"/plans/{plan_id}/approve", json={"reason": "본인 반납 응답 확인"}, headers=ASSET).status_code == 200
    # 같은 대화에서 실행 요청 → 변경 도구 앞에서 멈춤
    r = client.post("/ai/tasks", json={"input": f"{plan_id} 실행해줘", "thread_id": thread_id}, headers=ASSET)
    task2 = r.json()["data"]["task_id"]
    evs2 = events(client, task2, ASSET)
    assert [e["type"] for e in evs2] == ["APPROVAL_REQUIRED"]
    card = evs2[-1]["approval"]
    assert card["tool_name"] == "execute_approved_plan" and card["plan_id"] == plan_id
    t = wait_status(client, task2, ASSET, {"waiting_approval"})
    assert client.post(f"/ai/tasks/{task2}/approval", json={"approved": True, "reason": "ok"}, headers=EMP).status_code == 403
    assert client.post(f"/ai/tasks/{task2}/approval", json={"approved": True, "reason": ""}, headers=ASSET).status_code == 422
    assert client.post(f"/ai/tasks/{task2}/approval", json={"approved": True, "reason": "재배정 실행"}, headers=ASSET).status_code == 202
    evs3 = events(client, task2, ASSET, after=len(evs2))
    assert evs3[0]["type"] == "TOOL_START" and evs3[0]["tool_call"]["name"] == "execute_approved_plan" and evs3[-1]["type"] == "DONE"
    assert json.loads(evs3[1]["tool_call"]["output"])["status"] == "executed"
    t = wait_status(client, task2, ASSET, {"done"})
    assert t["result"]["plan_id"] == plan_id
    assert client.post(f"/ai/tasks/{task2}/approval", json={"approved": True, "reason": "x"}, headers=ASSET).status_code == 409


def test_rejection_ends_without_execution(client):
    task_id = client.post("/ai/tasks", json={"input": "plan_002 실행해줘"}, headers=ASSET).json()["data"]["task_id"]
    evs = events(client, task_id, ASSET)
    assert evs[-1]["type"] == "APPROVAL_REQUIRED"
    wait_status(client, task_id, ASSET, {"waiting_approval"})
    client.post(f"/ai/tasks/{task_id}/approval", json={"approved": False, "reason": "예산 검토 먼저"}, headers=ASSET)
    t = wait_status(client, task_id, ASSET, {"done", "failed"})
    assert t["status"] == "done" and "거절" in t["result"]["answer"]
    assert all(tc["error"] or tc["name"] != "execute_approved_plan" for tc in t["tool_calls"])


def test_task_list_scoping(client):
    mine = client.get("/ai/tasks", params={"mine": True}, headers=EMP).json()["data"]
    assert mine and all(t["requester_id"] == "emp_mkt_01" for t in mine)
    assert client.get("/ai/tasks", params={"mine": False}, headers=EMP).status_code == 403
    assert client.get("/ai/tasks", params={"mine": False}, headers=ASSET).status_code == 200


def test_public_answer_has_no_internal_identifiers(client):
    """직원(또는 Slack 채널) 답변에는 도구 이름·id·근거 줄이 없다. 참고 데이터는 evidence에 남는다."""
    task_id = client.post("/ai/tasks", json={"input": "AI-A 이용권 신청하고 싶어요", "channel": "slack"}, headers=EMP).json()["data"]["task_id"]
    events(client, task_id, EMP)
    t = wait_status(client, task_id, EMP, {"done"})
    ans = t["result"]["answer"]
    assert "근거" not in ans and "create_action_plan" not in ans and "plan_" not in ans and "emp_" not in ans, ans
    assert t["result"]["evidence"] and t["result"]["plan_id"].startswith("plan_")  # 구조화된 참고 데이터는 유지


def test_admin_web_answer_keeps_evidence_line(client):
    task_id = client.post("/ai/tasks", json={"input": "내 등록 업무 알려줘", "channel": "admin_ui"}, headers=ASSET).json()["data"]["task_id"]
    events(client, task_id, ASSET)
    t = wait_status(client, task_id, ASSET, {"done"})
    assert "get_my_profile" in t["result"]["answer"]  # 관리자 웹에서는 상세 유지 (스크립트 모델은 도구 이름을 답에 넣는다)


def test_plan_creation_triggers_review_with_opinion(client):
    """직원 요청으로 처리 요청이 생기면 시스템 계정 검토가 돌아 ai_opinion이 붙는다 (관리자 승인 전)."""
    from apps.backend import db
    with db.tx() as c:  # 좌석을 꽉 채워 승인 대기로 들어가게 한다 (남으면 바로 배정)
        c.execute(db.text("UPDATE contracts SET seats_purchased = (SELECT count(*) FROM seat_assignments WHERE contract_id = 'ctr_chatgpt_2026' AND status = 'assigned') WHERE id = 'ctr_chatgpt_2026'"))
    from apps.backend.main import app as backend_app
    from fastapi.testclient import TestClient as BackendClient
    task_id = client.post("/ai/tasks", json={"input": "AI-A 이용권 신청하고 싶어요", "channel": "slack"}, headers=EMP).json()["data"]["task_id"]
    events(client, task_id, EMP)
    t = wait_status(client, task_id, EMP, {"done"})
    pid = t["result"]["plan_id"]
    bc = BackendClient(backend_app)
    for _ in range(80):
        p = bc.get(f"/plans/{pid}", headers=ASSET).json()["data"]
        if p.get("ai_opinion"):
            break
        time.sleep(0.1)
    assert p["status"] == "pending_approval" and p["ai_opinion"]["recommendation"] == "approve"
    assert p["ai_opinion"]["changes"][0][0] == "직원" and "plan_" not in p["ai_opinion"]["summary"]
    reviews = [x for x in client.get("/ai/tasks", params={"mine": False}, headers=ASSET).json()["data"] if x["requester_id"] == "sys"]
    assert reviews and reviews[0]["channel"] == "system"


def test_review_update_prompt_saves_suggestion(client):
    """F1: 시스템 계정 review_update 작업이 업데이트를 읽고 관련 업무에 제안을 저장한다 (워커가 자동으로 돌리는 단위)."""
    from fastapi.testclient import TestClient as BackendClient
    SYS = {"Authorization": "Bearer dev.sys"}
    bc = BackendClient(backend_app)
    done = {s["update_id"] for s in bc.get("/suggestions", headers=SYS).json()["data"]["items"]}
    upd = next((u for u in bc.get("/updates", headers=SYS).json()["data"]["items"] if u["id"] not in done), None)
    if upd is None:
        pytest.skip("제안 없는 업데이트가 시드에 없음")
    r = client.post("/ai/tasks", json={"input": f"업데이트 {upd['id']}를 검토해 관련 업무에 활용 제안을 만들어줘", "prompt_name": "review_update",
                                       "prompt_args": {"update_id": upd["id"]}, "channel": "system"}, headers=SYS)
    assert r.status_code == 202, r.text
    t = wait_status(client, r.json()["data"]["task_id"], SYS, {"done", "failed"})
    assert t["status"] == "done" and [c["name"] for c in t["tool_calls"]][:2] == ["get_vendor_update", "find_relevant_tasks"]
    sugs = bc.get("/suggestions", params={"update_id": upd["id"]}, headers=SYS).json()["data"]["items"]
    assert sugs and sugs[0]["relevance"]


def test_model_release_simulation_makes_one_card(client):
    """F1: 출시 시뮬레이션 → 새 모델 감지 → review_update가 전사 카드 뉴스 한 장(핵심 기능 + 직무별 활용)을 저장한다."""
    from fastapi.testclient import TestClient as BackendClient
    SYS = {"Authorization": "Bearer dev.sys"}
    bc = BackendClient(backend_app)
    found = bc.post("/internal/jobs/collect_release_notes/run", params={"simulate": True}, headers=SYS).json()["data"]["result"]["new_models"][0]
    r = client.post("/ai/tasks", json={"input": f"업데이트 {found['update_id']}를 검토해 관련 업무에 활용 제안을 만들어줘", "prompt_name": "review_update",
                                       "prompt_args": {"update_id": found["update_id"]}, "channel": "system"}, headers=SYS)
    t = wait_status(client, r.json()["data"]["task_id"], SYS, {"done", "failed"})
    assert t["status"] == "done"
    sugs = bc.get("/suggestions", params={"update_id": found["update_id"]}, headers=SYS).json()["data"]["items"]
    assert len(sugs) == 1 and sugs[0]["target_type"] == "all"
    assert "핵심 기능" in sugs[0]["body"] and "이렇게 써 보세요" in sugs[0]["body"] and "• " in sugs[0]["body"]
