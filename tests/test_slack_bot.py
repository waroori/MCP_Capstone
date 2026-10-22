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
    emp = {"id": "emp_gen_052", "name": "이새봄", "dept_id": "dept_mkt", "slack_user_id": "U_FAKE"}
    def backend(method, path, token, **kw):
        calls.append((method, path))
        if path == "/confirmations":
            return [{"id": "cfm_9", "plan_id": "plan_9", "employee_id": "emp_gen_052", "question": "계속 쓰시나요?", "answer": None, "due_at": "2026-10-28T00:00:00Z", "answered_at": None}]
        if path == "/inventory":
            return [{"contract": {"id": "ctr_chatgpt_2026"}, "tool_name": "ChatGPT Enterprise", "low_usage_candidates": [{"employee_id": "emp_gen_052", "last_activity_at": None}]}]
        if path == "/plans/plan_9":
            return {"id": "plan_9", "type": "seat_reclaim", "items": [{"employee_id": "emp_gen_052", "contract_id": "ctr_chatgpt_2026"}]}
        if path.startswith("/employees/"):
            return {"employee": emp, "tasks": [{"id": "task_1", "title": "캠페인 카피 초안"}], "products": []}
        if path == "/notify-outbox":
            return [{"id": "out_1", "suggestion_id": "sug_1", "channel": "slack", "target": "U_FAKE", "text": "새 기능을 카피 작성에 쓸 수 있어요", "delivered_at": None},
                    {"id": "out_0", "suggestion_id": "sug_0", "channel": "slack", "target": "C_X", "text": "이미 보냄", "delivered_at": "2026-10-20T00:00:00Z"}]
        if path == "/suggestions":
            return {"items": [{"id": "sug_1", "update_id": "upd_1", "target_type": "employee", "target_id": "emp_gen_052", "task_id": "task_1", "body": "b", "status": "sent"}]}
        if path == "/updates/upd_1":
            return {"id": "upd_1", "vendor": "OpenAI", "url": "https://v.example/rn", "affected_plans": ["Enterprise"], "title": "t", "summary": "s"}
        if path == "/notify-outbox/out_1/delivered":
            outbox_delivered.append(path)
            return {"id": "out_1", "delivered_at": "now"}
        if path == "/updates":
            return {"items": [{"id": "upd_1"}, {"id": "upd_new"}]}
        if path.startswith("/internal/jobs/"):
            return {"result": {"stub": True}}
        raise AssertionError(f"예상 못 한 호출 {method} {path}")
    return backend


def test_worker_sends_confirmation_dm_once_and_delivers_outbox(monkeypatch, tmp_path):
    from apps.slack_bot import worker
    calls, delivered = [], []
    monkeypatch.setattr(api, "backend", _fake_backend(calls, delivered))
    monkeypatch.setattr(identity, "_user_map", lambda: {"U0REAL0001": "emp_gen_052"})  # 시드의 U_FAKE 대신 매핑표로 찾는다
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
    monkeypatch.setattr(identity, "_user_map", lambda: {"U0REAL0001": "emp_gen_052"})
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
                  "evidence": [{"source": "inventory", "ref": "ctr_chatgpt_2026", "value": {"unassigned": 0, "on_approve": "1석 추가 구매(Mock) 후 배정"}}]})
    worker.notify_plan_outcomes(slack)
    ask = slack.posted[-1]
    assert ask["channel"] == "D_U0ADMIN001" and "남은 좌석이 없어요" in ask["blocks"][0]["text"]["text"] and "1석 추가 구매" in ask["blocks"][0]["text"]["text"]
    assert [b["action_id"] for b in ask["blocks"][1]["elements"]] == ["pl_yes", "pl_no"] and ask["blocks"][1]["elements"][0]["value"] == "plan_full"


def test_outcome_messages_cover_reclaim_holder_and_requester():
    from apps.slack_bot import worker
    names = {"ctr_chatgpt_2026": "ChatGPT Enterprise", "tool_claude": "Claude API"}
    m = worker.outcome_messages({"type": "api_access", "status": "executed", "requested_by": "emp_md_01", "items": [{"tool_id": "tool_claude", "purpose": "x"}],
                                 "result": {"handoffs": [{"handoff_id": "kh_abc", "key_prefix": "sk-mock-AbCd"}]}}, names)
    assert "sk-mock-AbCd" in m["emp_md_01"] and "/key/kh_abc" in m["emp_md_01"] and "한 번" in m["emp_md_01"]  # 키 전체는 Slack에 없음
    m = worker.outcome_messages({"type": "seat_reclaim", "status": "executed", "requested_by": "emp_asset_01", "items": [{"employee_id": "emp_gen_052", "contract_id": "ctr_chatgpt_2026"}]}, names)
    assert "반납 처리됐어요" in m["emp_gen_052"] and "반납 처리가 끝났어요" in m["emp_asset_01"]
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
