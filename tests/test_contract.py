"""계약 테스트. 모든 엔드포인트가 봉투 형식과 response_model을 지키는지 확인한다.
백엔드가 DB로 바뀌어도 이 테스트는 그대로 통과해야 한다.
"""

import pytest
from fastapi.testclient import TestClient

from apps.backend import fixtures as fx
from apps.backend.main import app
from schemas.common import Role
from schemas.mcp_tools import TOOLS, tools_for_role
from schemas.rest import ROUTES

client = TestClient(app, raise_server_exceptions=True)


def login(employee_id: str) -> dict:
    r = client.post("/auth/dev-login", json={"employee_id": employee_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


EMP, ASSET, SYS = (login(i) for i in ["emp_mkt_01", "emp_asset_01", "sys"])
HRD = login("emp_hrd_01")  # v2: HRD 담당도 일반 직원 역할
APPROVER = ASSET  # v2: 책임자 결정도 관리자


def test_envelope_success_and_error():
    assert set(client.get("/me", headers=EMP).json()) == {"data", "meta"}
    r = client.get("/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = client.get("/inventory", headers=EMP)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


def test_routes_table_matches_app():
    """schemas.rest.ROUTES 표와 실제 라우터가 같아야 한다."""
    app_routes = {(m, r.path) for r in app.routes for m in getattr(r, "methods", []) if m != "HEAD"}
    spec_routes = {(m, p) for m, p, _, _ in ROUTES}
    assert spec_routes == {x for x in app_routes if not x[1].startswith(("/openapi", "/docs", "/redoc"))}


def test_hrd_sees_team_only_candidates():
    data = client.get("/updates/upd_chatgpt_doc_compare/relevant-tasks", headers=ASSET).json()["data"]
    assert all(c["employee_id"] is None for c in data["candidates"])  # 관리자는 팀 단위만
    data = client.get("/updates/upd_chatgpt_doc_compare/relevant-tasks", headers=SYS).json()["data"]
    assert all(c["employee_id"] for c in data["candidates"])


def test_suggestion_requires_relevance_unless_on_hold():
    body = {"update_id": "upd_chatgpt_doc_compare", "target_type": "employee", "target_id": "emp_hrd_01", "relevance": " ", "body": "x"}
    assert client.post("/suggestions", json=body, headers=SYS).status_code == 422
    hold = {"update_id": "upd_claude_price", "target_type": "all", "status": "on_hold", "hold_reason": "회사 플랜에 없는 기능"}
    r = client.post("/suggestions", json=hold, headers=SYS)
    assert r.status_code == 201 and r.json()["data"]["status"] == "on_hold" and r.json()["data"]["hold_reason"]
    assert client.post("/suggestions", json={**hold, "hold_reason": ""}, headers=SYS).status_code == 422


def test_seat_request_auto_when_seats_left_else_admin():
    """F4: 좌석이 남으면 시스템이 바로 배정, API 사용·반납은 승인 대기."""
    body = {"type": "seat_assign", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "사용 신청", "evidence": []}
    d = client.post("/plans", json=body, headers=EMP).json()["data"]
    assert d["status"] == "executed" and d["approver_id"] == "sys" and d["executed_at"]
    api = {"type": "api_access", "items": [{"tool_id": "tool_claude", "purpose": "세일즈 데이터 자동 분석"}], "rationale": "API 사용 신청", "evidence": []}
    d = client.post("/plans", json=api, headers=EMP).json()["data"]
    assert d["status"] == "pending_approval"
    mine = {"type": "seat_reclaim", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "본인 반납", "evidence": []}
    assert client.post("/plans", json=mine, headers=EMP).status_code == 201
    other = {"type": "seat_reclaim", "items": [{"employee_id": "emp_cs_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "x", "evidence": []}
    assert client.post("/plans", json=other, headers=EMP).status_code == 403  # 남의 좌석 반납 불가


def test_seat_usage_is_admin_or_self():
    assert client.get("/usage/seats", headers=EMP).status_code == 403
    paces = client.get("/usage/seats", headers=ASSET).json()["data"]
    assert paces and all(p["product"] == "ChatGPT Enterprise" and p["daily_quota"] > 0 for p in paces)
    other = next(p["employee_id"] for p in paces if p["employee_id"] != EMP["Authorization"].removeprefix("Bearer dev."))
    assert client.get(f"/usage/seats/{other}", headers=EMP).status_code == 403
    assert client.get(f"/usage/seats/{other}", headers=ASSET).json()["data"]["pace"]["employee_id"] == other


def test_projects_list_has_budgets():
    data = client.get("/projects", headers=ASSET).json()["data"]
    got = {p["id"]: p["monthly_budget_krw"] for p in data}
    assert {k: v.monthly_budget_krw for k, v in fx.PROJECTS.items()}.items() <= got.items()  # 시드가 프로젝트를 더 넣는다
    assert client.get("/projects", headers=EMP).status_code == 403


def test_org_members_tools_match_assets():
    """좌석 배치도 도구 = 직원별 assets 판정과 같아야 한다 (같은 규칙을 두 곳에서 쓰므로)."""
    data = client.get("/org/members", headers=ASSET).json()["data"]
    assert data and all(m["employee"]["role"] != "system" for m in data)
    for m in data[:5] + [x for x in data if x["employee"]["id"] in ("emp_cs_01", "emp_mkt_01")]:
        assets = client.get(f"/employees/{m['employee']['id']}/assets", headers=ASSET).json()["data"]["available_tools"]
        assert {t["tool_id"] for t in m["tools"]} == {t["id"] for t in assets}, m["employee"]["id"]


def test_setup_status_and_signup_once():
    st = client.get("/setup/status").json()["data"]
    assert st["company_name"] and st["has_admin"] and st["setup_completed"]
    r = client.post("/setup/signup", json={"company_name": "다른 회사", "admin_name": "a", "email": "new@x.co", "password": "12345678"})
    assert r.status_code == 409  # 회사는 한 곳
    assert client.post("/departments", json={"name": "새 부서"}, headers=EMP).status_code == 403


def test_budgets_roundtrip_and_monthly_split():
    before = client.get("/budgets", headers=ASSET).json()["data"]
    try:
        b = client.put("/budgets", json={"fiscal_year": before["fiscal_year"], "seat_annual_krw": 12_000_000, "api_annual_krw": 1_200_000}, headers=ASSET).json()["data"]
        assert (b["seat_monthly_krw"], b["api_monthly_krw"]) == (1_000_000, 100_000)
    finally:
        client.put("/budgets", json={k: before[k] for k in ("fiscal_year", "seat_annual_krw", "api_annual_krw")}, headers=ASSET)


def test_setup_inputs_validated():
    bad_term = {"vendor": "V", "product": "P", "plan_name": "x", "billing_type": "seat_flat", "seats_purchased": 5, "seat_price_krw": 1000,
                "term_start": "2026-12-31", "term_end": "2026-01-01"}
    assert client.post("/contracts", json=bad_term, headers=ASSET).status_code == 422
    assert client.put("/models/approved", json={"model_ids": ["openai:not-a-model"]}, headers=ASSET).status_code == 422
    owner = "emp_cs_01"
    r = client.post("/projects", json={"name": "x", "dept_id": "dept_cs", "owner_id": owner, "purpose": "x", "model_id": "xai:grok-4.3", "monthly_budget_krw": 1}, headers=ASSET)
    assert r.status_code == 422  # 승인 안 된 모델
    dup = client.post("/employees", json=[{"name": "a", "email": "dup@x.co", "dept_id": "dept_cs", "job_title": "t"}] * 2, headers=ASSET)
    assert dup.status_code == 409


def test_org_members_usage_matches_flags():
    """'기록 없음' 좌석은 30일 사용 0일, 사용 기록을 안 주는 제품(Acrobat)은 None."""
    for m in client.get("/org/members", headers=ASSET).json()["data"]:
        for t in m["tools"]:
            if t["flag"] == "no_record":
                assert t["active_days_30"] == 0
            if t["tool_id"] == "tool_adobe":
                assert t["active_days_30"] is None


def test_estimate_formula():
    body = {"model_id": "anthropic:claude-sonnet-5-5", "requests_per_day": 1000, "avg_input_tokens": 600, "avg_output_tokens": 20}
    d = client.post("/estimates/task-cost", json=body, headers=EMP).json()["data"]
    assert d["monthly"]["krw"] > 0 and "formula" in d


def test_plan_lifecycle_and_idempotency():
    plan = client.post("/plans", json={"type": "seat_reclaim", "items": [{"employee_id": "emp_sales_02", "contract_id": "ctr_chatgpt_2026"}], "rationale": "휴직", "evidence": [{"source": "confirmations", "ref": "cfm_001"}]}, headers=ASSET).json()["data"]
    assert plan["status"] == "pending_approval" and plan["risk_level"] == "medium"
    pid = plan["id"]
    # 승인 전 실행 → 403
    assert client.post(f"/plans/{pid}/execute", headers={**ASSET, "Idempotency-Key": "k1"}).status_code == 403
    # 사유 없는 승인 → 422
    assert client.post(f"/plans/{pid}/approve", json={"reason": ""}, headers=ASSET).status_code == 422
    assert client.post(f"/plans/{pid}/approve", json={"reason": "본인 반납 응답"}, headers=ASSET).json()["data"]["status"] == "approved"
    r1 = client.post(f"/plans/{pid}/execute", headers={**ASSET, "Idempotency-Key": "k1"}).json()["data"]
    r2 = client.post(f"/plans/{pid}/execute", headers={**ASSET, "Idempotency-Key": "k1"}).json()["data"]
    assert r1 == r2 and r1["plan"]["status"] == "executed"


def test_high_risk_only_approver_and_never_executed():
    plan = client.post("/plans", json={"type": "purchase_review", "items": [{"tool_id": "tool_grok"}], "rationale": "수요 3건", "evidence": []}, headers=ASSET).json()["data"]
    pid = plan["id"]
    assert plan["risk_level"] == "high"
    assert client.post(f"/plans/{pid}/approve", json={"reason": "ok"}, headers=EMP).status_code == 403
    assert client.post(f"/plans/{pid}/approve", json={"reason": "ok"}, headers=ASSET).status_code == 200  # v2: 관리자가 책임자 결정
    assert client.post(f"/plans/{pid}/execute", headers={**ASSET, "Idempotency-Key": "k2"}).status_code == 403


def test_employee_usage_self_only():
    q = {"scope": "project", "scope_id": "prj_quote_summary", "start": "2026-10-01T00:00:00Z", "end": "2026-10-20T00:00:00Z"}
    assert client.get("/usage", params=q, headers=EMP).status_code == 403
    assert client.get("/usage", params={**q, "group_by": "input_length"}, headers=ASSET).status_code == 200


def test_mcp_registry_roles_and_schemas():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names)) == 24
    assert "execute_approved_plan" not in {t.name for t in tools_for_role(Role.employee)}
    assert "get_usage_breakdown" in {t.name for t in tools_for_role(Role.admin)}
    for t in TOOLS:
        t.input_model.model_json_schema()
        t.output_model.model_json_schema()


@pytest.mark.parametrize("method,path", [(m, p) for m, p, _, _ in ROUTES if "{" not in p and m == "GET" and not p.startswith("/ai/") and p not in ("/usage", "/eval-results")])
def test_every_simple_get_responds_in_envelope(method, path):
    r = client.get(path, headers=SYS if path not in ("/adoption/teams", "/me/tasks", "/savings", "/audit-log") else ASSET)
    assert r.status_code in (200, 403), (path, r.text)
    assert ("data" in r.json()) or ("error" in r.json())


def test_plan_items_validated_per_type():
    bad = {"type": "model_change", "items": [{"project_id": "prj_faq"}], "rationale": "x", "evidence": []}
    r = client.post("/plans", json=bad, headers=ASSET)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"
    good = {"type": "model_change", "items": [{"project_id": "prj_faq", "from_model_id": "anthropic:claude-opus-5-5", "to_model_id": "anthropic:claude-sonnet-5-5", "environment": "prod"}], "rationale": "x", "evidence": []}
    assert client.post("/plans", json=good, headers=ASSET).json()["data"]["risk_level"] == "high"


def test_confirmation_create_and_answer():
    # cfm_001(emp_sales_02)은 이미 반납 응답이 있으므로 아직 묻지 않은 휴직자로 새 요청을 만든다
    r = client.post("/confirmations", json={"employee_id": "emp_mgmt_02", "plan_id": "plan_002", "question": "계속 쓰시나요?"}, headers=ASSET)
    assert r.status_code == 201, r.text
    c = r.json()["data"]
    assert c["id"].startswith("cfm_") and c["due_at"] and c["answer"] is None and c["answered_at"] is None
    assert client.post("/confirmations", json={"employee_id": "emp_sales_02", "plan_id": "plan_002", "question": "?"}, headers=EMP).status_code == 403
    assert client.post("/confirmations", json={"employee_id": "nobody", "plan_id": "plan_002", "question": "?"}, headers=ASSET).status_code == 404


def test_employee_model_change_only_own_project_test_env():
    CS = login("emp_cs_01")  # prj_cs_classify 오너
    ok_body = {"type": "model_change", "items": [{"project_id": "prj_cs_classify", "from_model_id": "anthropic:claude-opus-5-5", "to_model_id": "anthropic:claude-sonnet-5-5", "environment": "test"}], "rationale": "평가 통과 후보 중 저비용", "evidence": []}
    r = client.post("/plans", json=ok_body, headers=CS)
    assert r.status_code == 201 and r.json()["data"]["risk_level"] == "medium" and r.json()["data"]["status"] == "pending_approval"
    prod = {**ok_body, "items": [{**ok_body["items"][0], "environment": "prod"}]}
    assert client.post("/plans", json=prod, headers=CS).status_code == 403
    other = {**ok_body, "items": [{**ok_body["items"][0], "project_id": "prj_faq"}]}
    assert client.post("/plans", json=other, headers=CS).status_code == 403
    assert client.post("/plans", json=ok_body, headers=EMP).status_code == 403  # 마케팅 직원은 오너가 아님
    not_mine = {"type": "seat_assign", "items": [{"employee_id": "emp_cs_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "x", "evidence": []}
    assert client.post("/plans", json=not_mine, headers=EMP).status_code == 403


def test_employee_profile_and_plan_opinion():
    """검토용 직원 프로필과 AI 의견 붙이기. 의견은 JSON으로 저장되고 조회 시 구조화돼 돌아온다."""
    prof = client.get("/employees/emp_mkt_01", headers=SYS).json()["data"]
    assert prof["employee"]["name"] == "박지후" and "password" not in str(prof)
    assert client.get("/employees/emp_mkt_01", headers=EMP).status_code == 403
    body = {"type": "seat_assign", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "사용 신청", "evidence": []}
    pid = client.post("/plans", json=body, headers=EMP).json()["data"]["id"]
    op = {"recommendation": "approve", "summary": "미배정 좌석이 있어 바로 배정해도 됩니다.", "requester": {"name": "박서준", "dept": "마케팅", "job_title": "콘텐츠 마케터", "current_products": []},
          "asset": {"product": "ChatGPT Enterprise", "purchased": 60, "assigned": 59, "unassigned": 1, "seat_price_krw": 45000}, "facts": ["60석 중 1석 남음"], "changes": [["직원", "박서준 (마케팅)"]], "evidence_text": ["이용권 현황: 미배정 1석"]}
    r = client.patch(f"/plans/{pid}/opinion", json=op, headers=SYS)
    assert r.status_code == 200 and r.json()["data"]["ai_opinion"]["recommendation"] == "approve"
    got = client.get(f"/plans/{pid}", headers=ASSET).json()["data"]
    assert got["ai_opinion"]["summary"].startswith("미배정") and got["ai_opinion"]["generated_at"]
    assert client.patch(f"/plans/{pid}/opinion", json=op, headers=EMP).status_code == 403


def test_confirmations_list_and_outbox_delivered_mark():
    """Slack 봇 워커가 쓰는 두 경로: 미응답 확인 요청 목록, 발송함 전달 표시(멱등)."""
    c = client.post("/confirmations", json={"employee_id": "emp_hrd_04", "plan_id": "plan_002", "question": "계속 쓰시나요?"}, headers=ASSET).json()["data"]
    pending = client.get("/confirmations", params={"answered": "false"}, headers=SYS).json()["data"]
    assert c["id"] in {x["id"] for x in pending} and all(x["answer"] is None for x in pending)
    assert client.get("/confirmations", headers=EMP).status_code == 403
    assert client.get("/notify-outbox", headers=SYS).status_code == 200  # 워커는 시스템 계정으로 읽는다
    client.post("/internal/jobs/send_suggestions/run", params={"now": "2026-10-21T01:00:00Z"}, headers=SYS)  # 평일 10시 KST → 발송함 채움
    rows = client.get("/notify-outbox", headers=SYS).json()["data"]
    if rows:
        r = client.patch(f"/notify-outbox/{rows[0]['id']}/delivered", headers=SYS).json()["data"]
        again = client.patch(f"/notify-outbox/{rows[0]['id']}/delivered", headers=SYS).json()["data"]
        assert r["delivered_at"] and again["delivered_at"] == r["delivered_at"]
    assert client.patch("/notify-outbox/out_nope/delivered", headers=SYS).status_code == 404
