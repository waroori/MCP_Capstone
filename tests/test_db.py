"""DB 전환 확인: API 호출이 실제 테이블을 바꾸고, 제약이 잘못된 상태를 막는다."""

from fastapi.testclient import TestClient

from apps.backend import db
from apps.backend.main import app

client = TestClient(app)
ADMIN = {"Authorization": "Bearer dev.emp_asset_01"}


def unassigned() -> int:
    inv = client.get("/inventory", params={"contract_id": "ctr_chatgpt_2026"}, headers=ADMIN).json()["data"][0]
    return inv["unassigned"]


def test_reclaim_frees_seat_and_writes_audit():
    before = unassigned()
    assert client.post("/plans/plan_002/approve", json={"reason": "본인 반납 응답"}, headers=ADMIN).status_code == 200
    r = client.post("/plans/plan_002/execute", headers={**ADMIN, "Idempotency-Key": "db-k1"})
    assert r.status_code == 200 and r.json()["data"]["plan"]["status"] == "executed"
    assert unassigned() == before + 1
    actions = [a["action"] for a in db.rows("SELECT action FROM audit_log WHERE target = 'plan_002' ORDER BY created_at")]
    assert actions[-2:] == ["plan.approve", "plan.execute"]
    # 다른 키로 다시 실행하면 거절, 같은 키는 같은 결과
    assert client.post("/plans/plan_002/execute", headers={**ADMIN, "Idempotency-Key": "db-k2"}).status_code == 409
    assert client.post("/plans/plan_002/execute", headers={**ADMIN, "Idempotency-Key": "db-k1"}).json()["data"] == r.json()["data"]


def test_second_decision_conflicts():
    assert client.post("/plans/plan_002/reject", json={"reason": "아직 사용 중"}, headers=ADMIN).status_code == 200
    assert client.post("/plans/plan_002/approve", json={"reason": "번복"}, headers=ADMIN).status_code == 409


SYS = {"Authorization": "Bearer dev.sys"}
WORK_HOUR = "2026-10-20T01:00:00Z"   # 월요일 10:00 KST
NIGHT = "2026-10-20T12:00:00Z"       # 월요일 21:00 KST


def team_suggestion(dept: str) -> str:
    body = {"update_id": "upd_chatgpt_doc_compare", "target_type": "team", "target_id": dept, "relevance": "근거", "body": "제안"}
    return client.post("/suggestions", json=body, headers=SYS).json()["data"]["id"]


def run(job: str, now: str) -> dict:
    return client.post(f"/internal/jobs/{job}/run", params={"now": now}, headers=SYS).json()["data"]["result"]


def test_send_policy_hours_cap_and_switch():
    hrd = team_suggestion("dept_hrd")
    assert "deferred" in run("send_suggestions", NIGHT)                       # 업무 시간 밖이면 미룸
    prod = team_suggestion("dept_prod")                                       # 생산관리는 자동 발송 꺼짐(시드)
    extra = team_suggestion("dept_mkt")                                       # 마케팅은 이미 이번 주 1건 → 1건 더 가능
    extra2 = team_suggestion("dept_mkt")
    r = run("send_suggestions", WORK_HOUR)
    sent = {x["suggestion_id"] for x in r["sent"]}
    held = {x["suggestion_id"]: x["reason"] for x in r["held"]}
    assert hrd in sent and extra in sent
    assert "꺼짐" in held[prod] and "상한" in held[extra2]
    outbox = client.get("/notify-outbox", headers=ADMIN).json()["data"]
    assert {o["suggestion_id"] for o in outbox} == sent
    assert client.get("/review-runs", headers=ADMIN).json()["data"]


def test_two_irrelevant_holds_team():
    cs = [g["id"] for g in db.rows("SELECT id FROM employees WHERE dept_id = 'dept_cs' AND role = 'employee' ORDER BY id")]
    sid = team_suggestion("dept_cs")
    for e in cs[-2:]:
        r = client.post(f"/suggestions/{sid}/feedback", json={"reaction": "irrelevant"}, headers={"Authorization": f"Bearer dev.{e}"})
    assert r.json()["data"]["team_hold_until"]                                # 2회 연속 → 4주 보류
    held = run("send_suggestions", WORK_HOUR)["held"]
    assert any(x["dept_id"] == "dept_cs" and "보류" in x["reason"] for x in held)


def test_seat_pace_alerts_only_heavy_seats():
    now = "2026-10-21T06:00:00Z"  # 수요일 15:00 KST
    run("collect_usage", now)       # 더미 수집: 지난 평일 + 오늘 업무 시간. 시연용 과사용 좌석(emp_md_01·emp_cs_01)은 3.5배
    run("collect_usage", now)       # 다시 돌려도 같은 행을 덮어쓴다
    made = run("check_thresholds", now)["new_anomalies"]
    rows = [r for r in client.get("/anomalies", headers=ADMIN).json()["data"] if r["id"] in made]
    assert {r["scope_id"] for r in rows} == {"emp_md_01", "emp_cs_01"}
    assert all(r["rule"] == "seat_pace_2x" and r["ratio"] >= 2 and r["observed"] > r["baseline"] for r in rows)
    assert run("check_thresholds", now)["new_anomalies"] == []   # 같은 사람 같은 날 중복 경보 없음


def tools_of(emp: str) -> set[str]:
    return {t["id"] for t in client.get(f"/employees/{emp}/assets", headers=ADMIN).json()["data"]["available_tools"]}


def assign(emp: str):
    return client.post("/plans", headers=ADMIN, json={"type": "seat_assign", "items": [{"employee_id": emp, "contract_id": "ctr_chatgpt_2026"}],
                                                      "rationale": "t", "evidence": []})


def test_grant_and_revoke_change_usable_tools():
    """부여하면 그 직원의 실제 사용 가능 도구에 생기고, 회수하면 사라진다. API 도구는 팀 프로젝트가 있을 때만."""
    assert "tool_chatgpt" not in tools_of("emp_mkt_01")
    assert "tool_claude" in tools_of("emp_cs_01") and "tool_claude" not in tools_of("emp_hrd_01")  # HRD팀은 Claude API 프로젝트가 없다
    assert assign("emp_mkt_01").json()["data"]["status"] == "executed"  # F4: 남은 좌석이 있으면 바로 배정
    assert "tool_chatgpt" in tools_of("emp_mkt_01")
    plan = client.post("/plans", headers=ADMIN, json={"type": "seat_reclaim", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}],
                                                      "rationale": "t", "evidence": []}).json()["data"]
    client.post(f"/plans/{plan['id']}/approve", headers=ADMIN, json={"reason": "t"})
    assert client.post(f"/plans/{plan['id']}/execute", headers={**ADMIN, "Idempotency-Key": "rv1"}).status_code == 200
    assert "tool_chatgpt" not in tools_of("emp_mkt_01")


def test_assign_revalidates_before_execute():
    """중복 배정·휴직자는 승인 대기에 들어가기 전에 막고(409) 조치안을 failed로 남긴다.
    좌석이 없으면 승인 대기 → 관리자 승인 = 1석 추가 구매(Mock) 후 배정. 좌석 수를 넘지 않고, 구매했으니 절감 기록도 없다."""
    savings = db.one("SELECT count(*) AS n FROM savings_ledger")["n"]
    for emp, why in [("emp_gen_002", "이미"), ("emp_gen_007", "on_leave")]:
        r = assign(emp)
        assert r.status_code == 409 and why in r.json()["error"]["message"]
        assert db.one("SELECT status FROM action_plans WHERE id = :id", id=r.json()["error"]["details"]["plan_id"])["status"] == "failed"
    with db.tx() as c:  # 좌석을 꽉 채운 상태로 만든다 (구매 수 = 배정 수)
        c.execute(db.text("UPDATE contracts SET seats_purchased = 58 WHERE id = 'ctr_chatgpt_2026'"))
    r = assign("emp_mkt_01")
    plan = r.json()["data"]
    assert r.status_code == 201 and plan["status"] == "pending_approval"
    assert any(e["value"].get("on_approve") == "1석 추가 구매(Mock) 후 배정" for e in plan["evidence"] if isinstance(e["value"], dict))
    client.post(f"/plans/{plan['id']}/approve", headers=ADMIN, json={"reason": "t"})
    done = client.post(f"/plans/{plan['id']}/execute", headers={**ADMIN, "Idempotency-Key": "buy1"}).json()["data"]["plan"]
    assert done["status"] == "executed" and done["result"]["purchased_seats"] == {"ctr_chatgpt_2026": 1}
    seats = db.one("SELECT c.seats_purchased AS p, count(s.id) AS used FROM contracts c JOIN seat_assignments s ON s.contract_id = c.id AND s.status = 'assigned'"
                   " WHERE c.id = 'ctr_chatgpt_2026' GROUP BY c.id")
    assert seats["p"] == seats["used"] == 59   # 산 만큼만 배정
    assert db.one("SELECT count(*) AS n FROM savings_ledger")["n"] == savings


def test_api_access_hands_key_over_once():
    """API 사용 승인 → 개인 테스트 프로젝트·키 생성, 키는 본인만 한 번 (Slack에는 앞부분만)."""
    emp = {"Authorization": "Bearer dev.emp_md_01"}
    plan = client.post("/plans", headers=emp, json={"type": "api_access", "items": [{"tool_id": "tool_claude", "purpose": "리뷰 요약 자동화"}],
                                                    "rationale": "t", "evidence": []}).json()["data"]
    client.post(f"/plans/{plan['id']}/approve", headers=ADMIN, json={"reason": "t"})
    done = client.post(f"/plans/{plan['id']}/execute", headers={**ADMIN, "Idempotency-Key": "ak1"}).json()["data"]["plan"]
    h = done["result"]["handoffs"][0]
    assert "secret" not in h and h["key_prefix"].startswith("sk-mock-")
    url = f"/keys/handoff/{h['handoff_id']}/reveal"
    assert client.post(url, headers=ADMIN).status_code == 404      # 관리자도 남의 키는 못 본다
    got = client.post(url, headers=emp).json()["data"]
    assert got["secret"].startswith(h["key_prefix"]) and got["product"] == "Claude API"
    assert client.post(url, headers=emp).status_code == 409        # 두 번째는 안 됨
    assert db.one("SELECT secret FROM key_handoffs WHERE id = :id", id=h["handoff_id"])["secret"] is None
    assert db.one("SELECT owner_id, environment FROM projects WHERE id = :id", id=h["project_id"]) == {"owner_id": "emp_md_01", "environment": "test"}


SEED_CHECKS = {  # 시드 일관성: 모두 0건이어야 한다
    "휴직자 좌석에 on_leave 표시 없음": "SELECT count(*) AS n FROM seat_assignments s JOIN employees e ON e.id = s.employee_id"
                              " WHERE e.status <> 'active' AND s.status = 'assigned' AND s.flag IS DISTINCT FROM 'on_leave'",
    "배정일 < 입사일": "SELECT count(*) AS n FROM seat_assignments s JOIN employees e ON e.id = s.employee_id WHERE s.assigned_at::date < e.hire_date",
    "좌석 초과": "SELECT count(*) AS n FROM (SELECT 1 FROM contracts c JOIN seat_assignments s ON s.contract_id = c.id AND s.status = 'assigned'"
             " GROUP BY c.id, c.seats_purchased HAVING count(*) > c.seats_purchased) x",
    "미실행 조치안의 절감": "SELECT count(*) AS n FROM savings_ledger v JOIN action_plans p ON p.id = v.plan_id WHERE p.status <> 'executed'",
    "사용량 모델 ≠ 계약 제품": "SELECT count(*) AS n FROM usage_records u JOIN models m ON m.id = u.model_id JOIN contracts c ON c.id = u.contract_id WHERE m.tool_id <> c.tool_id",
    "미승인 모델 쓰는 프로젝트": "SELECT count(*) AS n FROM projects p JOIN models m ON m.id = p.model_id WHERE NOT m.approved",
}


def test_seed_is_consistent():
    assert {k: db.one(q)["n"] for k, q in SEED_CHECKS.items()} == dict.fromkeys(SEED_CHECKS, 0)


def test_model_change_execute_applies_and_revalidates():
    """모델 변경은 실행하면 프로젝트 모델이 실제로 바뀌고, 이미 바뀐 뒤 같은 요청은 stale(failed)로 멈춘다."""
    item = {"project_id": "prj_faq", "from_model_id": "anthropic:claude-sonnet-5-5", "to_model_id": "anthropic:claude-haiku-5-5", "environment": "test"}

    def run(key):
        p = client.post("/plans", headers=ADMIN, json={"type": "model_change", "items": [item], "rationale": "t", "evidence": []}).json()["data"]
        client.post(f"/plans/{p['id']}/approve", headers=ADMIN, json={"reason": "t"})
        return client.post(f"/plans/{p['id']}/execute", headers={**ADMIN, "Idempotency-Key": key})

    assert run("mc1").status_code == 200
    assert db.one("SELECT model_id FROM projects WHERE id = 'prj_faq'")["model_id"] == "anthropic:claude-haiku-5-5"
    r = run("mc2")
    assert r.status_code == 409 and "이미" in r.json()["error"]["message"]


def test_new_hire_registered_and_placed_on_desk():
    """신입 등록(Slack 회원 ID 포함) → 빈 책상에 앉히기 → 옮기기. 남의 자리는 막고, 모자라면 책상을 붙인다."""
    desks = client.get("/desks", headers=ADMIN).json()["data"]
    mkt = [d for d in desks if d["dept_id"] == "dept_mkt"]
    empty = [d["id"] for d in mkt if d["employee_id"] is None]
    taken = next(d for d in mkt if d["employee_id"])
    assert len(empty) >= 2 and all(d["floor"] == "4F" for d in mkt)
    new = client.post("/employees", headers=ADMIN, json=[{"name": "신입", "email": "new.hire@hanbit.example", "dept_id": "dept_mkt",
                                                         "job_title": "마케터", "slack_user_id": "U0NEWHIRE1"}]).json()["data"][0]
    assert new["slack_user_id"] == "U0NEWHIRE1"
    assert client.post("/employees", headers=ADMIN, json=[{"name": "중복", "email": "dup@hanbit.example", "dept_id": "dept_mkt",
                                                          "job_title": "x", "slack_user_id": "U0NEWHIRE1"}]).status_code == 409
    assert client.put(f"/desks/{empty[0]}", headers=ADMIN, json={"employee_id": new["id"]}).json()["data"]["employee_id"] == new["id"]
    assert client.put(f"/desks/{taken['id']}", headers=ADMIN, json={"employee_id": new["id"]}).status_code == 409   # 남의 자리
    client.put(f"/desks/{empty[1]}", headers=ADMIN, json={"employee_id": new["id"]})                                 # 옮기기
    mine = [d["id"] for d in client.get("/desks", headers=ADMIN).json()["data"] if d["employee_id"] == new["id"]]
    assert mine == [empty[1]]                                                                                         # 한 사람 한 자리
    added = client.post("/desks", headers=ADMIN, json={"floor": "4F", "dept_id": "dept_mkt", "count": 6}).json()["data"]
    assert [d["seq"] for d in added] == list(range(len(mkt) + 1, len(mkt) + 7))
    assert client.put(f"/desks/{empty[1]}", headers={"Authorization": "Bearer dev.emp_mkt_01"}, json={"employee_id": None}).status_code == 403


def test_model_release_detected_once_per_model_and_card_sent_to_all():
    """새 모델 감지: 평소 폴링은 새것 없음, 시뮬레이션마다 승인 공급사 모델 하나씩. 승인 전 공급사(xAI)는 알리지 않는다.
    전사 카드는 팀 주간 상한 없이 업무 시간에 발송함으로 간다."""
    assert run("collect_release_notes", WORK_HOUR)["new_models"] == []
    sim = lambda: client.post("/internal/jobs/collect_release_notes/run", params={"now": WORK_HOUR, "simulate": True}, headers=SYS).json()["data"]["result"]["new_models"]  # noqa: E731
    first, second, third = sim(), sim(), sim()
    assert [x["model_id"] for x in first + second] == ["openai:gpt-5.6", "anthropic:claude-sonnet-5-6"] and third == []
    m = db.one("SELECT approved, tool_id FROM models WHERE id = 'openai:gpt-5.6'")
    assert m == {"approved": False, "tool_id": "tool_chatgpt"}                     # 사내 사용 승인은 관리자가 따로
    upd = client.get(f"/updates/{first[0]['update_id']}", headers=SYS).json()["data"]
    assert upd["title"] == "새 모델 출시: GPT-5.6" and upd["affected_plans"] == ["ChatGPT Enterprise"]
    sid = client.post("/suggestions", headers=SYS, json={"update_id": upd["id"], "target_type": "all", "relevance": "새 모델",
                                                         "body": "핵심 기능\n1. a\n2. b\n3. c\n이렇게 써 보세요\n• 마케터: x"}).json()["data"]["id"]
    sent = run("send_suggestions", WORK_HOUR)["sent"]
    assert any(x["suggestion_id"] == sid and x["dept_id"] is None for x in sent)
