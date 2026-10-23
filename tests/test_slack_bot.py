"""Slack 봇: 실제 Slack 없이 메시지 빌더, 진행 갱신, 승인 흐름, 버튼 처리를 가짜 Slack 클라이언트로 확인한다."""

from __future__ import annotations

import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

from apps.slack_bot import app as bot  # noqa: E402
from apps.slack_bot import client as api  # noqa: E402
from apps.slack_bot import identity, send  # noqa: E402


class FakeSlack:
    def __init__(self):
        self.posted, self.updated, self.ephemeral, self.opened = [], [], [], []

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": f"ts{len(self.posted)}", "thread_ts": kw.get("thread_ts")}

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)

    def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": f"D_{users}"}}


def ev(type_, seq, **f):
    return {"type": type_, "task_id": "task_1", "seq": seq, "at": "2026-10-21T09:00:00Z", **f}


def test_builders_have_buttons_and_links():
    b = send.build_suggestion({"id": "sug_1", "body": "문서 비교 기능을 설문 비교에 쓸 수 있어요"}, "교육 과정별 설문 결과 비교", {"vendor": "AI-A", "url": "https://v.example/rn", "affected_plans": ["Enterprise"]})
    assert b[-1]["type"] == "actions" and [e["action_id"] for e in b[-1]["elements"]] == ["fb_useful", "fb_irrelevant", "fb_question"]
    assert "원문 보기" in b[2]["elements"][0]["text"]
    c = send.build_confirmation({"id": "cfm_1"}, "AI-A Enterprise Chat", None)
    assert "응답이 없어도 회수되지 않습니다" in c[0]["text"]["text"]
    a = send.build_approval_request({"task_id": "task_1", "input": "회수해줘", "requester_id": "emp_asset_01", "approval": {"summary": "승인된 조치안 plan_9 을(를) 실행합니다.", "evidence": [{"source": "confirmations", "ref": "cfm_001"}]}})
    assert [e["action_id"] for e in a[-1]["elements"]] == ["ap_yes", "ap_no"]
    blocks = send.build_answer({"answer": "추천: b-mid", "evidence": [{"source": "get_eval_results", "ref": "customer_inquiry_classification"}]})
    assert len(blocks) == 1 and "get_eval_results" not in blocks[0]["text"]["text"]  # Slack에는 참고 데이터를 보여 주지 않는다


def test_follow_updates_one_message_then_answers(monkeypatch):
    slack = FakeSlack()
    events = [ev("TOOL_START", 1, tool_call={"id": "c1", "name": "get_my_profile", "input": {}}),
              ev("TOOL_END", 2, tool_call={"id": "c1", "name": "get_my_profile", "input": {}, "output": "{}", "error": None}),
              ev("TOKEN", 3, text="등록 업무는"), ev("DONE", 4, result={"answer": "등록 업무는 2개입니다.", "evidence": [{"source": "get_my_profile", "ref": "emp_mkt_01"}]})]
    monkeypatch.setattr(api, "stream_events", lambda token, task_id, after=0, timeout=180: iter(events))
    bot._follow(slack, "dev.emp_mkt_01", "task_1", "D1", "ts1", None, 0)
    final = slack.updated[-1]
    assert final["text"].startswith("등록 업무는 2개") and final["blocks"][0]["text"]["text"].startswith("등록 업무는 2개")
    assert all(u["ts"] == "ts1" for u in slack.updated)  # 같은 메시지만 갱신
    assert all("get_my_profile" not in u["text"] for u in slack.updated)  # 진행 표시에 도구 이름 없음


def test_follow_approval_notifies_admin_and_resumes_after_decision(monkeypatch):
    slack = FakeSlack()
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_ADMIN")
    first = [ev("APPROVAL_REQUIRED", 1, approval={"tool_name": "execute_approved_plan", "tool_input": {"plan_id": "plan_9"}, "plan_id": "plan_9", "summary": "승인된 조치안 plan_9 을(를) 실행합니다.", "evidence": []})]
    monkeypatch.setattr(api, "stream_events", lambda token, task_id, after=0, timeout=180: iter(first))
    monkeypatch.setattr(api, "agent", lambda m, p, t, **kw: {"task_id": "task_1", "input": "plan_9 실행해줘", "requester_id": "emp_asset_01",
                                                               "approval": first[0]["approval"]})
    bot._follow(slack, "dev.emp_asset_01", "task_1", "D1", "ts1", None, 0)
    assert "관리자 확인" in slack.updated[-1]["text"] and slack.opened == ["U_ADMIN"]
    assert slack.posted[-1]["channel"] == "D_U_ADMIN" and bot.PENDING["task_1"]["after"] == 1
    # 관리자가 버튼으로 확인 → 승인 API 호출 → 요청자 메시지 이어서 갱신
    approved = {}
    monkeypatch.setattr(identity, "resolve", lambda uid: ("dev.emp_asset_01", {"id": "emp_asset_01", "role": "admin"}))
    monkeypatch.setattr(api, "approve_task", lambda token, task_id, ok, reason: approved.update(task_id=task_id, ok=ok))
    second = [ev("TOOL_START", 2, tool_call={"id": "c2", "name": "execute_approved_plan", "input": {}}),
              ev("TOOL_END", 3, tool_call={"id": "c2", "name": "execute_approved_plan", "input": {}, "output": "{}", "error": None}),
              ev("DONE", 4, result={"answer": "처리했습니다.", "evidence": []})]
    monkeypatch.setattr(api, "stream_events", lambda token, task_id, after=0, timeout=180: iter(second) if after == 1 else iter([]))
    body = {"user": {"id": "U_ADMIN"}, "channel": {"id": "D_U_ADMIN"}, "message": {"ts": "ts9"}}
    bot.on_approval(ack=lambda: None, body=body, action={"action_id": "ap_yes", "value": "task_1"}, client=slack)
    import time
    time.sleep(0.3)  # 백그라운드 스레드 완료 대기
    assert approved == {"task_id": "task_1", "ok": True}
    assert slack.updated[-1]["text"].startswith("처리했습니다.") and "task_1" not in bot.PENDING


def test_poll_pending_resumes_after_web_approval(monkeypatch):
    """웹 콘솔에서 승인되면(Slack 버튼이 아니어도) 폴링이 감지해 요청자 메시지를 이어서 갱신한다."""
    slack = FakeSlack()
    bot.PENDING.clear()
    bot.PENDING["task_9"] = {"token": "dev.emp_mkt_01", "channel": "D1", "ts": "ts1", "thread_ts": None, "after": 1, "is_admin": False}
    monkeypatch.setattr(api, "agent", lambda m, p, t, **kw: {"task_id": "task_9", "status": "done"})
    done = [ev("DONE", 2, result={"answer": "배정됐어요. 지금 바로 쓸 수 있어요.", "evidence": []})]
    monkeypatch.setattr(api, "stream_events", lambda token, task_id, after=0, timeout=180: iter(done))
    bot.poll_pending(slack, once=True)
    import time as _t
    _t.sleep(0.3)
    assert "task_9" not in bot.PENDING and slack.updated[-1]["text"].startswith("배정됐어요")


def test_errors_are_friendly_to_employees(monkeypatch):
    slack = FakeSlack()
    monkeypatch.setattr(identity, "resolve", lambda uid: (_ for _ in ()).throw(api.ApiError("not_found", "Slack 사용자 U9 에 연결된 직원이 없습니다. SLACK_USER_MAP을 확인하세요.")))
    bot._handle_request(slack, {"channel": "D1", "user": "U9", "ts": "1.0", "channel_type": "im"}, "이용권 주세요")
    assert "SLACK_USER_MAP" not in slack.posted[-1]["text"] and "찾을 수 없는" in slack.posted[-1]["text"]


def test_feedback_button_calls_backend(monkeypatch):
    slack = FakeSlack()
    calls = {}
    monkeypatch.setattr(identity, "resolve", lambda uid: ("dev.emp_mkt_01", {"id": "emp_mkt_01", "role": "employee"}))
    monkeypatch.setattr(api, "feedback", lambda token, sid, reaction: calls.update(sid=sid, reaction=reaction))
    bot.on_feedback(ack=lambda: None, body={"user": {"id": "U1"}, "channel": {"id": "C1"}}, action={"action_id": "fb_irrelevant", "value": "sug_1"}, client=slack)
    assert calls == {"sid": "sug_1", "reaction": "irrelevant"} and "관련 없음" in slack.ephemeral[-1]["text"]


def test_identity_exchanges_via_backend(monkeypatch):
    """SLACK_USER_MAP의 직원 id를 실어 백엔드 /auth/slack-exchange로 토큰을 받고, 백엔드 not_found는 그대로 올린다."""
    identity._cache.clear()
    monkeypatch.setenv("SLACK_USER_MAP", '{"U_MKT01": "emp_mkt_01"}')
    calls = []

    def fake_backend(method, path, token, json):
        calls.append((path, json))
        if json["employee_id"] is None:
            raise api.ApiError("not_found", "연결된 직원 없음")
        return {"access_token": "jwt-mkt", "employee": {"id": json["employee_id"], "role": "employee"}}

    monkeypatch.setattr(identity, "backend", fake_backend)
    token, emp = identity.resolve("U_MKT01")
    assert token == "jwt-mkt" and emp["id"] == "emp_mkt_01"
    assert calls == [("/auth/slack-exchange", {"slack_user_id": "U_MKT01", "employee_id": "emp_mkt_01"})]
    try:
        identity.resolve("U_UNKNOWN")
        raise AssertionError("매핑 없는 사용자는 실패해야 함")
    except api.ApiError as exc:
        assert exc.code == "not_found"


def test_create_app_registers_listeners_without_network():
    app = bot.create_app(verify_token=False)
    assert len(app._listeners) >= 10  # 이벤트 2 + 버튼 8


# ---------- 발신 워커 (확인 요청 DM · 발송함 · F1 체인) ----------
def _fake_backend(calls, outbox_delivered):
    emp = {"id": "emp_sales_09", "name": "이새봄", "dept_id": "dept_mkt", "slack_user_id": "U_FAKE"}
    def backend(method, path, token, **kw):
        calls.append((method, path))
        if path == "/confirmations":
            return [{"id": "cfm_9", "plan_id": "plan_9", "employee_id": "emp_sales_09", "question": "계속 쓰시나요?", "answer": None, "due_at": "2026-10-28T00:00:00Z", "answered_at": None}]
        if path == "/inventory":
            return [{"contract": {"id": "ctr_chatgpt_2026"}, "tool_name": "ChatGPT Enterprise", "low_usage_candidates": [{"employee_id": "emp_sales_09", "last_activity_at": None}]}]
        if path == "/plans/plan_9":
            return {"id": "plan_9", "type": "seat_reclaim", "items": [{"employee_id": "emp_sales_09", "contract_id": "ctr_chatgpt_2026"}]}
        if path.startswith("/employees/"):
            return {"employee": emp, "tasks": [{"id": "task_1", "title": "캠페인 카피 초안"}], "products": []}
        if path == "/notify-outbox":
            return [{"id": "out_1", "suggestion_id": "sug_1", "channel": "slack", "target": "U_FAKE", "text": "새 기능을 카피 작성에 쓸 수 있어요", "delivered_at": None},
                    {"id": "out_0", "suggestion_id": "sug_0", "channel": "slack", "target": "C_X", "text": "이미 보냄", "delivered_at": "2026-10-20T00:00:00Z"}]
        if path == "/suggestions":
            return {"items": [{"id": "sug_1", "update_id": "upd_1", "target_type": "employee", "target_id": "emp_sales_09", "task_id": "task_1", "body": "b", "status": "sent"}]}
        if path == "/updates/upd_1":
            return {"id": "upd_1", "vendor": "OpenAI", "url": "https://v.example/rn", "affected_plans": ["Enterprise"], "title": "t", "summary": "s"}
        if path == "/notify-outbox/out_1/delivered":
            outbox_delivered.append(path)
            return {"id": "out_1", "delivered_at": "now"}
        if path == "/updates":
            return {"items": [{"id": "upd_1"}, {"id": "upd_new", "vendor": "OpenAI", "title": "문서 비교", "affected_plans": ["Enterprise"]}]}
        if path == "/tools":
            return [{"id": "tool_chatgpt", "vendor": "OpenAI", "product": "ChatGPT Enterprise", "contract": {"id": "ctr_chatgpt_2026", "plan_name": "Enterprise (seat + usage)"}}]
        if path.startswith("/internal/jobs/"):
            return {"result": {"stub": True}}
        if path == "/org/members":  # 전사·부서 대상: DB에 실제 Slack ID가 있는 재직 직원. 가짜 ID(U_FAKE)·시스템·퇴사자는 빠진다
            return [{"employee": emp, "tools": []},
                    {"employee": {"id": "emp_cs_02", "name": "정서현", "dept_id": "dept_cs", "role": "employee", "status": "active", "job_title": "CS 상담", "slack_user_id": "U0CAF7Z7PRD"}, "tools": []},
                    {"employee": {"id": "emp_md_02", "name": "정윤지", "dept_id": "dept_md", "role": "employee", "status": "active", "job_title": "상품기획", "slack_user_id": "U0CARE0JF29"}, "tools": []},
                    {"employee": {"id": "sys", "name": "system", "dept_id": "dept_mgmt", "role": "system", "status": "active", "slack_user_id": "U0SYS000001"}, "tools": []},
                    {"employee": {"id": "emp_old", "name": "퇴사자", "dept_id": "dept_cs", "role": "employee", "status": "resigned", "slack_user_id": "U0OLD000001"}, "tools": []}]
        raise AssertionError(f"예상 못 한 호출 {method} {path}")
    return backend


def test_broadcast_targets_include_db_slack_ids(monkeypatch):
    """전사(all)·부서(team) 카드의 받는 사람: .env 매핑표에 없어도 DB에 실제 Slack ID가 있는 재직 직원이면 받는다.
    (seed_team 회사는 매핑표가 비어 있어 두어 명에게만 가던 문제.) 시스템 계정·퇴사자·가짜 ID는 제외."""
    from apps.slack_bot import worker
    monkeypatch.setattr(api, "backend", _fake_backend([], []))
    monkeypatch.setattr(identity, "_user_map", lambda: {"U0REAL0001": "emp_sales_09"})
    worker._emp_cache.clear()
    ids = [e["id"] for e in worker._targets({"target_type": "all"})]
    assert ids == ["emp_sales_09", "emp_cs_02", "emp_md_02"]  # 매핑표 1명 + DB 2명, sys·퇴사자 제외
    assert [e["id"] for e in worker._targets({"target_type": "team", "target_id": "dept_cs"})] == ["emp_cs_02"]
    monkeypatch.setattr(identity, "_user_map", lambda: {})  # 매핑표가 비어도 DB로 간다
    assert [e["id"] for e in worker._targets({"target_type": "all"})] == ["emp_cs_02", "emp_md_02"]


def test_worker_sends_confirmation_dm_once_and_delivers_outbox(monkeypatch, tmp_path):
    from apps.slack_bot import worker
    calls, delivered = [], []
    monkeypatch.setattr(api, "backend", _fake_backend(calls, delivered))
    monkeypatch.setattr(identity, "_user_map", lambda: {"U0REAL0001": "emp_sales_09"})  # 시드의 U_FAKE 대신 매핑표로 찾는다
    monkeypatch.setattr(worker, "STATE_PATH", tmp_path / "state.json")
    worker._emp_cache.clear()
    slack = FakeSlack()
    state = worker.load_state()
    assert worker.deliver_confirmations(slack, state) == ["cfm_9"]
    assert slack.opened == ["U0REAL0001"] and slack.posted[-1]["channel"] == "D_U0REAL0001"
    assert [e["action_id"] for e in slack.posted[-1]["blocks"][-1]["elements"]] == ["cf_still_needed", "cf_return_seat", "cf_on_leave"]
    assert "ChatGPT Enterprise" in slack.posted[-1]["blocks"][0]["text"]["text"]
    assert worker.deliver_confirmations(slack, worker.load_state()) == []  # 재시작해도 두 번 보내지 않는다
    assert worker.deliver_outbox(slack) == ["out_1"] and delivered == ["/notify-outbox/out_1/delivered"]
    sug = slack.posted[-1]
    assert "캠페인 카피 초안" in sug["blocks"][0]["text"]["text"] and "원문 보기" in sug["blocks"][2]["elements"][0]["text"]
    assert [e["action_id"] for e in sug["blocks"][-1]["elements"]] == ["fb_useful", "fb_irrelevant", "fb_question"]


def test_worker_skips_unmapped_employee(monkeypatch, tmp_path):
    from apps.slack_bot import worker
    monkeypatch.setattr(api, "backend", _fake_backend([], []))
    monkeypatch.setattr(identity, "_user_map", lambda: {})
    monkeypatch.setattr(worker, "STATE_PATH", tmp_path / "state.json")
    worker._emp_cache.clear()
    slack = FakeSlack()
    assert worker.deliver_confirmations(slack, worker.load_state()) == [] and slack.posted == []  # U_FAKE는 가짜 ID라 보내지 않음
    assert worker.deliver_outbox(slack) == []


def test_run_f1_reviews_only_unreviewed_updates_then_sends(monkeypatch, tmp_path):
    from apps.slack_bot import worker
    calls, delivered = [], []
    monkeypatch.setattr(api, "backend", _fake_backend(calls, delivered))
    monkeypatch.setattr(identity, "_user_map", lambda: {"U0REAL0001": "emp_sales_09"})
    created = []
    def agent(method, path, token, **kw):
        if method == "POST":
            created.append(kw["json"])
            return {"task_id": "task_r1", "thread_id": "th", "status": "queued"}
        return {"task_id": "task_r1", "status": "done"}
    monkeypatch.setattr(api, "agent", agent)
    worker._emp_cache.clear()
    out = worker.run_f1(FakeSlack(), wait=5)
    assert [c["prompt_args"]["update_id"] for c in created] == ["upd_new"] and created[0]["prompt_name"] == "review_update" and created[0]["channel"] == "system"
    assert out["reviews"] == [{"update_id": "upd_new", "task_id": "task_r1", "status": "done"}] and out["delivered"] == ["out_1"]
    assert ("POST", "/internal/jobs/collect_release_notes/run") in calls and ("POST", "/internal/jobs/send_suggestions/run") in calls


def test_worker_notifies_requester_after_web_decision(monkeypatch):
    """직원이 낸 처리 요청이 웹에서 승인·실행/반려되면 요청자 DM. 봇 기동 전 건은 알리지 않고, 같은 건은 한 번만."""
    from apps.slack_bot import worker
    plans = [{"id": "plan_old", "type": "seat_assign", "status": "executed", "requested_by": "emp_md_01", "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}]},
             {"id": "plan_new", "type": "seat_assign", "status": "pending_approval", "requested_by": "emp_md_01", "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}]},
             {"id": "plan_rej", "type": "api_access", "status": "pending_approval", "requested_by": "emp_md_01", "items": [{"tool_id": "tool_claude", "purpose": "x"}]}]
    def backend(method, path, token, **kw):
        if path == "/plans":
            return {"items": [dict(p) for p in plans]}
        if path == "/tools":
            return [{"id": "tool_chatgpt", "product": "ChatGPT Enterprise", "contract": {"id": "ctr_chatgpt_2026"}}, {"id": "tool_claude", "product": "Claude API", "contract": None}]
        if path == "/employees/emp_md_01":
            return {"employee": {"id": "emp_md_01", "name": "정서진", "dept_name": "상품기획·MD", "role": "employee", "slack_user_id": "U_MD01", "email": "seojin.jung@hanbit.example"}, "tasks": [], "products": []}
        raise AssertionError(path)
    monkeypatch.setattr(api, "backend", backend)
    monkeypatch.setattr(identity, "_user_map", lambda: {"U0REAL0002": "emp_md_01"})
    monkeypatch.setattr(identity, "admin_slack_ids", lambda: ["U0ADMIN001"])
    worker._plan_status.clear(); worker._tool_cache.clear(); worker._emp_cache.clear()
    slack = FakeSlack()
    assert worker.notify_plan_outcomes(slack, baseline=True) == [] and slack.posted == []  # 기동 시점: 이미 끝난 plan_old는 알리지 않음
    assert worker.notify_plan_outcomes(slack) == []  # 변화 없음
    plans[1]["status"] = "executed"
    plans[2]["status"], plans[2]["reject_reason"] = "rejected", "팀 프로젝트로 이미 쓸 수 있음"
    assert worker.notify_plan_outcomes(slack) == ["plan_new", "plan_rej"]
    texts = [m["text"] for m in slack.posted]
    assert texts[0].startswith("요청하신 ChatGPT Enterprise 이용권이 배정됐어요. 지금부터 쓸 수 있어요.") and all(c == "D_U0REAL0002" for c in [m["channel"] for m in slack.posted])
    assert "seojin.jung@hanbit.example" in texts[0] and "비밀번호: 따로 없어요" in texts[0] and "https://chatgpt.com" in texts[0]  # SSO 안내, 비밀번호 없음
    assert "Claude API 요청이 반려됐어요" in texts[1] and "팀 프로젝트로 이미 쓸 수 있음" in texts[1]
    assert worker.notify_plan_outcomes(slack) == [] and len(slack.posted) == 2  # 두 번 보내지 않음
    # 기동 뒤 새로 들어온 승인 대기 → 관리자 DM에 [승인]/[반려]
    plans.append({"id": "plan_full", "type": "seat_assign", "status": "pending_approval", "requested_by": "emp_md_01", "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}],
                  "evidence": [{"source": "inventory", "ref": "ctr_chatgpt_2026", "value": {"unassigned": 0, "shortage": 1, "note": "남은 좌석 없음 · 승인 불가, 반려 대상"}}]})
    plans.append({"id": "plan_ok", "type": "seat_assign", "status": "pending_approval", "requested_by": "emp_md_01", "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}]})
    worker.notify_plan_outcomes(slack)
    full, ok = slack.posted[-2], slack.posted[-1]
    assert full["channel"] == "D_U0ADMIN001" and "남은 좌석이 없어 승인할 수 없어요" in full["blocks"][0]["text"]["text"] and "추가 구매" in full["blocks"][0]["text"]["text"]
    assert [b["action_id"] for b in full["blocks"][1]["elements"]] == ["pl_no"]  # 좌석이 없으면 [반려]만
    assert "승인하면 남은 좌석에서 배정합니다" in ok["blocks"][0]["text"]["text"]
    assert [b["action_id"] for b in ok["blocks"][1]["elements"]] == ["pl_yes", "pl_no"] and ok["blocks"][1]["elements"][0]["value"] == "plan_ok"


def test_outcome_messages_cover_reclaim_holder_and_requester():
    from apps.slack_bot import worker
    names = {"ctr_chatgpt_2026": "ChatGPT Enterprise", "tool_claude": "Claude API"}
    m = worker.outcome_messages({"type": "api_access", "status": "executed", "requested_by": "emp_md_01", "items": [{"tool_id": "tool_claude", "purpose": "x"}],
                                 "result": {"handoffs": [{"handoff_id": "kh_abc", "key_prefix": "sk-mock-AbCd"}]}}, names)
    assert "sk-mock-AbCd" in m["emp_md_01"] and "/key/kh_abc" in m["emp_md_01"] and "한 번" in m["emp_md_01"]  # 키 전체는 Slack에 없음
    m = worker.outcome_messages({"type": "seat_reclaim", "status": "executed", "requested_by": "emp_asset_01", "items": [{"employee_id": "emp_sales_09", "contract_id": "ctr_chatgpt_2026"}]}, names)
    assert "반납 처리됐어요" in m["emp_sales_09"] and "반납 처리가 끝났어요" in m["emp_asset_01"]
    m = worker.outcome_messages({"type": "seat_assign", "status": "failed", "requested_by": "emp_md_01", "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}]}, names)
    assert "관리자가 다시 확인" in m["emp_md_01"]


def test_identity_single_user_fallback(monkeypatch):
    """SLACK_USER_MAP이 비어 있으면 SLACK_USER_ID → SLACK_EMPLOYEE_ID 한 명으로 연결한다 (scripts/run_public.sh)."""
    monkeypatch.setenv("SLACK_USER_MAP", "")
    monkeypatch.setenv("SLACK_USER_ID", "U_ME")
    monkeypatch.setenv("SLACK_EMPLOYEE_ID", "emp_cs_01")
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "")
    assert identity._user_map() == {"U_ME": "emp_cs_01"} and identity.admin_slack_ids() == ["U_ME"]
    monkeypatch.setenv("SLACK_USER_MAP", '{"U_X": "emp_mkt_01"}')
    assert identity._user_map() == {"U_X": "emp_mkt_01"}  # 매핑이 있으면 매핑이 우선


def test_seat_alert_dm_has_analyze_button():
    blocks = send.build_seat_alert({"scope_id": "emp_md_01", "observed": 312000.4, "baseline": 30000, "ratio": 10.4})
    text = blocks[0]["text"]["text"]
    assert "토큰 사용량이 많은 작업 중이에요" in text and "312,000" in text and "10.4배" in text
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == "seat_analyze" and button["value"] == "emp_md_01"


def test_model_card_sends_only_my_job_line():
    """카드 뉴스는 핵심 기능은 그대로, '이렇게 써 보세요'는 받는 사람 직무 줄만 보낸다. 다른 직무 줄은 Slack에 나가지 않는다(관리자 화면에서만)."""
    upd = {"title": "새 모델 출시: GPT-5.6", "affected_plans": ["ChatGPT Enterprise"], "url": "https://x"}
    body = "핵심 기능\n1. 긴 문서\n2. 표\n3. 이미지\n이렇게 써 보세요\n• 마케터: 카피 비교\n• 영업 담당: 견적 요약"
    blocks = send.build_model_card({"id": "sug_x", "body": body}, upd, "마케터")
    text = blocks[1]["text"]["text"]
    assert blocks[0]["type"] == "header" and "*핵심 기능*" in text and "3. 이미지" in text
    assert "*• 마케터: 카피 비교*  ← 내 직무" in text and "영업 담당" not in text
    assert [b["action_id"] for b in blocks[-1]["elements"]] == ["fb_useful", "fb_irrelevant", "fb_question"]
    other = send.build_model_card({"id": "sug_x", "body": body}, upd, "생산 계획 · 대리")[1]["text"]["text"]
    assert "마케터" not in other and "영업 담당" not in other and "내 직무 활용 예는 아직 없어요" in other  # 카드에 없는 직무: 공통 한 줄


def test_menu_word_shows_buttons_without_agent(monkeypatch):
    """규칙 경로: '메뉴' 한 단어면 에이전트를 부르지 않고 제품별 버튼. 쓰는 좌석은 [반납], 아니면 [신청], API는 [사용 신청]."""
    slack = FakeSlack()
    monkeypatch.setattr(identity, "resolve", lambda uid: ("dev.emp_mkt_01", {"id": "emp_mkt_01", "role": "employee"}))
    monkeypatch.setattr(api, "create_task", lambda *a, **k: (_ for _ in ()).throw(AssertionError("에이전트를 부르면 안 된다")))
    tools = [{"id": "tool_chatgpt", "product": "ChatGPT Enterprise", "contract": {"id": "ctr_chatgpt_2026", "billing_type": "seat_plus_usage"}},
             {"id": "tool_canva", "product": "Canva Enterprise", "contract": {"id": "ctr_canva_2026", "billing_type": "seat_flat"}},
             {"id": "tool_claude", "product": "Claude API", "contract": {"id": "ctr_claude_2026", "billing_type": "api_usage"}},
             {"id": "tool_grok", "product": "Grok API", "approval_status": "under_review", "contract": None}]
    monkeypatch.setattr(api, "backend", lambda m, p, t, **kw: tools if p == "/tools" else {"available_tools": [{"id": "tool_canva"}]})
    bot._handle_request(slack, {"channel": "D1", "user": "U1", "ts": "1.0", "channel_type": "im"}, " 메뉴 ")
    buttons = [(b["text"]["text"].split("\n")[0], b["accessory"]["action_id"], b["accessory"]["value"]) for b in slack.posted[-1]["blocks"] if "accessory" in b]
    assert buttons == [("*ChatGPT Enterprise*", "menu_seat_assign", "ctr_chatgpt_2026"), ("*Canva Enterprise*", "menu_seat_reclaim", "ctr_canva_2026"),
                       ("*Claude API*", "menu_api_access", "tool_claude")]


def test_menu_buttons_create_plans_directly(monkeypatch):
    slack = FakeSlack()
    slack.views = []
    slack.views_open = lambda **kw: slack.views.append(kw)
    calls = []
    monkeypatch.setattr(identity, "resolve", lambda uid: ("dev.emp_mkt_01", {"id": "emp_mkt_01", "role": "employee"}))
    monkeypatch.setattr(api, "backend", lambda m, p, t, **kw: calls.append((m, p, kw["json"])) or {"status": "pending_approval", "evidence": []})
    body = {"user": {"id": "U1"}, "channel": {"id": "D1"}, "message": {"ts": "9.0", "blocks": [{"text": {"text": "*Claude API*\n…"}, "accessory": {"value": "tool_claude"}}]}, "trigger_id": "tr1"}
    bot.on_menu_seat(ack=lambda: None, body=body, action={"action_id": "menu_seat_assign", "value": "ctr_chatgpt_2026"}, client=slack)
    assert calls[-1] == ("POST", "/plans", {"type": "seat_assign", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}],
                                            "rationale": "Slack 메뉴에서 본인 신청", "evidence": []})
    assert "관리자에게 신청을 전달했어요" in slack.posted[-1]["text"]  # 좌석이 있어도 승인 대기 (S4)
    bot.on_menu_api(ack=lambda: None, body=body, action={"value": "tool_claude"}, client=slack)
    view = slack.views[-1]["view"]
    assert view["private_metadata"] == "tool_claude|D1" and "Claude API" in view["blocks"][0]["label"]["text"]
    bot.on_menu_api_submit(ack=lambda: None, body={"user": {"id": "U1"}}, client=slack,
                           view={"private_metadata": "tool_claude|D1", "state": {"values": {"purpose": {"v": {"value": " 문의 자동 분류 "}}}}})
    assert calls[-1][2]["items"] == [{"tool_id": "tool_claude", "purpose": "문의 자동 분류"}] and "관리자에게 전달했어요" in slack.posted[-1]["text"]


def test_plan_mismatch_is_rule_not_ai(monkeypatch):
    """F1: 회사 플랜에 없는 업데이트는 코드가 판정해 보류만 남기고 에이전트를 부르지 않는다."""
    from apps.slack_bot import worker
    tools = [{"id": "tool_chatgpt", "vendor": "OpenAI", "product": "ChatGPT Enterprise", "contract": {"plan_name": "Enterprise (seat + usage)"}},
             {"id": "tool_claude", "vendor": "Anthropic", "product": "Claude API", "contract": {"plan_name": "API pay-as-you-go"}},
             {"id": "tool_grok", "vendor": "xAI", "product": "Grok API", "contract": None}]
    assert worker.plan_mismatch({"vendor": "OpenAI", "affected_plans": ["Enterprise"]}, tools) is None
    assert worker.plan_mismatch({"vendor": "OpenAI", "affected_plans": ["ChatGPT Enterprise"]}, tools) is None
    assert worker.plan_mismatch({"vendor": "Anthropic", "affected_plans": ["API"]}, tools) is None
    assert worker.plan_mismatch({"vendor": "Anthropic", "affected_plans": []}, tools) is None
    assert worker.plan_mismatch({"vendor": "OpenAI", "affected_plans": ["Edu"]}, tools) == "회사 플랜에 없는 기능: Edu"
    assert "계약한 xAI 제품이 없음" in worker.plan_mismatch({"vendor": "xAI", "affected_plans": []}, tools)
    posted = []
    def backend(method, path, token, **kw):
        if method == "POST":
            posted.append(kw["json"])
            return {}
        return {"/updates": {"items": [{"id": "upd_edu", "vendor": "OpenAI", "title": "교육 플랜 기능", "affected_plans": ["Edu"]}]},
                "/suggestions": {"items": []}, "/tools": tools}[path]
    monkeypatch.setattr(api, "backend", backend)
    monkeypatch.setattr(api, "agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("에이전트를 부르면 안 된다")))
    assert worker.review_updates(wait=1) == [{"update_id": "upd_edu", "status": "on_hold", "reason": "회사 플랜에 없는 기능: Edu"}]
    assert posted == [{"update_id": "upd_edu", "target_type": "all", "status": "on_hold", "hold_reason": "회사 플랜에 없는 기능: Edu"}]


def test_fixed_reject_messages_and_checklist():
    """PRD S5·S9 정형 반려 안내문, S2 점검 체크리스트(완료 요구 없음), S3 관리자 팀별 비용 알림."""
    from apps.slack_bot import worker
    names = {"ctr_chatgpt_2026": "ChatGPT Enterprise"}
    short = worker.outcome_messages({"type": "seat_assign", "status": "rejected", "requested_by": "emp_md_01", "reject_reason": "좌석 부족",
                                     "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}]}, names)
    assert short["emp_md_01"] == "ChatGPT Enterprise 사용 신청이 반려되었습니다. 현재 배정 가능한 이용권이 부족합니다."
    unlisted = worker.outcome_messages({"type": "purchase_review", "status": "rejected", "requested_by": "emp_md_01", "reject_reason": "사내 미도입",
                                        "items": [{"product_name": "Notion AI", "purpose": "회의록 정리"}]}, names)
    assert unlisted["emp_md_01"] == ("*[미도입 제품 신청]* Notion AI (회의록 정리)\n"
                                     "Notion AI은 현재 사내 제공 대상이 아니므로 사용 신청이 반려되었습니다. 요청 내용은 신청 이력에 기록되었습니다.")
    text = send.build_seat_alert({"scope_id": "emp_md_01", "observed": 1, "baseline": 1, "ratio": 2.0})[0]["text"]["text"]
    assert text.count("점검해 보세요") == 3 and "알려 주실 필요는 없어요" in text and "모델 변경을 권해" not in text
    cost = send.build_cost_alert({"마케팅": 2, "영업": 1}, "http://web/cost/teams")[0]["text"]["text"]
    assert cost.index("마케팅: 2석") < cost.index("영업: 1석") and "<http://web/cost/teams|비용 관리 대시보드>" in cost


def test_run_f3_sends_team_summary_to_admins(monkeypatch):
    """S3: 새 경보가 있으면 본인 DM과 별개로 관리자에게 팀별 요약 + 대시보드 링크를 한 번 보낸다."""
    from apps.slack_bot import worker
    anomalies = [{"id": "anm_1", "scope_type": "employee", "scope_id": "emp_mkt_02", "scope_name": "a", "observed": 2, "baseline": 1, "ratio": 2.0},
                 {"id": "anm_2", "scope_type": "employee", "scope_id": "emp_mkt_03", "scope_name": "b", "observed": 2, "baseline": 1, "ratio": 2.0}]
    def backend(method, path, token, **kw):
        if path == "/anomalies":
            return anomalies
        if path.startswith("/employees/"):
            return {"employee": {"id": path.rsplit("/", 1)[1], "name": "x", "dept_name": "마케팅", "role": "employee", "slack_user_id": None}}
        return {}
    monkeypatch.setattr(api, "backend", backend)
    monkeypatch.setattr(identity, "admin_slack_ids", lambda: ["U0ADMIN001"])
    monkeypatch.setattr(worker, "save_state", lambda state: None)
    worker._emp_cache.clear()
    slack = FakeSlack()
    assert worker.run_f3(slack, {"anomalies": []}) == ["anm_1", "anm_2"]
    admin = [m for m in slack.posted if m["channel"] == "D_U0ADMIN001"]
    assert len(admin) == 1 and "마케팅: 2석" in admin[0]["blocks"][0]["text"]["text"]
    assert worker.run_f3(slack, {"anomalies": ["anm_1", "anm_2"]}) == [] and len(slack.posted) == 1  # 이미 보낸 경보는 다시 안 보냄


def test_self_return_is_not_announced_twice():
    """직원 본인 반납(시스템 자동 승인)은 대화에서 이미 답했으므로 워커가 결과 DM을 다시 보내지 않는다. 관리자 회수는 알린다."""
    from apps.slack_bot import worker
    names = {"ctr_chatgpt_2026": "ChatGPT Enterprise"}
    self_return = {"type": "seat_reclaim", "status": "executed", "requested_by": "emp_md_01", "approver_id": "sys",
                   "items": [{"employee_id": "emp_md_01", "contract_id": "ctr_chatgpt_2026"}]}
    assert worker.outcome_messages(self_return, names) == {}
    by_admin = {**self_return, "requested_by": "emp_asset_01", "approver_id": "emp_asset_01"}
    assert "반납 처리됐어요" in worker.outcome_messages(by_admin, names)["emp_md_01"]
