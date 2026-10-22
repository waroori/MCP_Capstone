"""백엔드 스텁. 8절 엔드포인트 전부가 고정 데이터로 동작한다.

B(백엔드)는 이 파일의 라우트 시그니처와 response_model을 유지한 채 내부를 DB 로직으로 바꾼다.
모든 라우트가 PostgreSQL(app 스키마)을 쓴다. fixtures.py는 시드(db/seed/seed.py)의 원본으로만 남는다.
시그니처·모델을 바꿔야 하면 packages/schemas를 먼저 고치고 PR로 리드 리뷰를 받는다.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from apps.backend import db
from apps.backend.auth import APP_ENV, authenticate, current_user, get_employee, hash_password, issue_token, require
from apps.backend.fixtures import MODELS as MODEL_CATALOG  # 공식 단가표. 시드와 같은 값을 모델 고르기 목록으로 쓴다
from apps.backend.errors import ApiError, install_error_handlers, ok
from schemas.common import (
    RISK_BY_PLAN_TYPE,
    BreakdownBy,
    Envelope,
    ErrorCode,
    ErrorEnvelope,
    Granularity,
    LicenseFlag,
    Money,
    Page,
    PlanStatus,
    PlanType,
    RiskLevel,
    Role,
    SuggestionStatus,
    UsageScope,
)
from schemas.entities import (
    ActionPlan,
    EmployeeProfile,
    PlanOpinion,
    Contract,
    Department,
    Project,
    Anomaly,
    AuditEntry,
    BreakdownRow,
    Confirmation,
    Employee,
    EvalResult,
    Model,
    RegisteredTask,
    Suggestion,
    ToolWithPlan,
    UsagePoint,
    UsageSummary,
    VendorUpdate,
)
from pydantic import ValidationError

from schemas.plan_items import validate_items
from schemas.rest import (
    AnomalyDetail,
    BudgetSet,
    BudgetView,
    ContractCreate,
    DepartmentCreate,
    EmployeeCreate,
    ModelApproval,
    ProjectCreate,
    SetupStatus,
    SignupRequest,
    ConfirmationAnswerRequest,
    ConfirmationCreate,
    ContractInventory,
    DevLoginRequest,
    SlackExchangeRequest,
    DevLoginResponse,
    LoginRequest,
    FeedbackCreate,
    JobRunResponse,
    LowUsageCandidate,
    MemberTool,
    OrgMember,
    MeResponse,
    PlanCreate,
    PlanDecision,
    PlanExecuteResponse,
    RelevantTaskCandidate,
    RelevantTasksResponse,
    SavingsSummary,
    SuggestionCreate,
    TaskCostEstimateRequest,
    TaskCostEstimateResponse,
    TaskCreate,
    TaskUpdate,
    TeamAdoption,
    UnmetNeedAggregate,
    UnmetNeedCreate,
    UnmetNeedCreateResponse,
    UsageResponse,
)

ALL = (Role.employee, Role.admin, Role.system)
ERR = {401: {"model": ErrorEnvelope}, 403: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}}

app = FastAPI(title="AI 활용·운영 최적화 에이전트 — Backend API", version="0.1.0", responses=ERR)
install_error_handlers(app)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- auth / me ----------
@app.post("/auth/login", response_model=Envelope[DevLoginResponse], tags=["auth"])
def login(body: LoginRequest):
    emp = authenticate(body.login_id, body.password)
    return ok(DevLoginResponse(access_token=issue_token(emp), employee=emp))


@app.post("/auth/dev-login", response_model=Envelope[DevLoginResponse], tags=["auth"])
def dev_login(body: DevLoginRequest):
    """개발·시연용. APP_ENV=prod에서는 막힌다."""
    if APP_ENV == "prod":
        raise ApiError(ErrorCode.not_found, "운영 환경에서는 쓸 수 없습니다")
    emp = get_employee(body.employee_id)
    if not emp:
        raise ApiError(ErrorCode.not_found, f"직원 {body.employee_id} 없음")
    return ok(DevLoginResponse(access_token=issue_token(emp), employee=emp))


@app.post("/auth/slack-exchange", response_model=Envelope[DevLoginResponse], tags=["auth"])
def slack_exchange(body: SlackExchangeRequest, user: Employee = Depends(require(Role.system))):
    """Slack 봇(시스템 토큰)이 Slack 사용자 대신 직원 토큰을 받는다. APP_ENV=prod에서도 열려 있다(dev-login 대신)."""
    r = None if body.employee_id else db.one("SELECT id FROM employees WHERE slack_user_id = :s", s=body.slack_user_id)
    emp = get_employee(body.employee_id or (r and r["id"]) or "")
    if not emp:
        raise ApiError(ErrorCode.not_found, f"Slack 사용자 {body.slack_user_id} 에 연결된 직원이 없습니다. SLACK_USER_MAP을 확인하세요.")
    if emp.status == "resigned":
        raise ApiError(ErrorCode.forbidden, "퇴사 처리된 계정입니다")
    return ok(DevLoginResponse(access_token=issue_token(emp), employee=emp))


# ---------- 초기 설정 (회원가입 → 마법사) ----------
# 회사는 한 곳(company 테이블 한 행). 설정 변경은 처리 요청 없이 관리자가 바로 하고 모두 audit_log에 남긴다.
EMP_COLS = "e.id, e.name, e.email, e.dept_id, d.name AS dept_name, e.role, e.job_title, e.status, e.hire_date, e.slack_user_id"


def _this_year() -> int:
    return (_now() + timedelta(hours=9)).year


def _setup_status() -> SetupStatus:
    c = db.one("SELECT name, setup_completed_at FROM company")
    n = db.one("SELECT (SELECT count(*) FROM departments) AS departments, (SELECT count(*) FROM employees WHERE role <> 'system') AS employees,"
               " (SELECT count(*) FROM contracts) AS contracts, (SELECT count(*) FROM models WHERE approved) AS models,"
               " (SELECT count(*) FROM projects) AS projects, (SELECT count(*) FROM budgets WHERE fiscal_year = :y) AS budgets,"
               " (SELECT count(*) FROM employees WHERE role = 'admin' AND password_hash IS NOT NULL) AS admins", y=_this_year())
    return SetupStatus(company_name=c and c["name"], has_admin=n.pop("admins") > 0, setup_completed=bool(c and c["setup_completed_at"]), steps=n, dev_login=APP_ENV != "prod")


@app.get("/setup/status", response_model=Envelope[SetupStatus], tags=["setup"])
def setup_status():
    return ok(_setup_status())


@app.post("/setup/signup", response_model=Envelope[DevLoginResponse], status_code=201, tags=["setup"])
def setup_signup(body: SignupRequest):
    """첫 관리자 가입. 회사 행을 먼저 잡아(ON CONFLICT) 두 사람이 동시에 가입해도 한 명만 성공한다."""
    with db.tx() as c:
        if not c.execute(db.text("INSERT INTO company (name) VALUES (:n) ON CONFLICT DO NOTHING RETURNING id"), dict(n=body.company_name.strip())).first():
            raise ApiError(ErrorCode.conflict, "이미 가입한 회사가 있습니다. 관리자 계정으로 로그인하세요")
        if db.one("SELECT 1 AS x FROM employees WHERE lower(email) = lower(:e)", c, e=body.email):
            raise ApiError(ErrorCode.conflict, "이미 쓰는 이메일입니다")
        dept = _new_id("dept")
        emp = _new_id("emp")
        c.execute(db.text("INSERT INTO departments (id, name) VALUES (:id, :n)"), dict(id=dept, n=body.dept_name.strip()))
        c.execute(db.text("INSERT INTO employees (id, name, email, dept_id, role, job_title, status, hire_date, password_hash)"
                          " VALUES (:id, :n, :e, :d, 'admin', :t, 'active', current_date, :pw)"),
                  dict(id=emp, n=body.admin_name.strip(), e=body.email.strip().lower(), d=dept, t=body.job_title.strip(), pw=hash_password(body.password)))
    admin = get_employee(emp)
    with db.tx() as c:
        _audit(c, admin, "setup.signup", "company", None, {"company": body.company_name, "admin": emp, "dept": dept})
    return ok(DevLoginResponse(access_token=issue_token(admin), employee=admin))


@app.post("/setup/complete", response_model=Envelope[SetupStatus], tags=["setup"])
def setup_complete(user: Employee = Depends(require(Role.admin))):
    st = _setup_status()
    missing = {k: label for k, label in (("contracts", "구독 제품"), ("budgets", "올해 예산")) if not st.steps[k]}
    if missing:
        raise ApiError(ErrorCode.invalid, f"아직 입력하지 않은 단계가 있습니다: {', '.join(missing.values())}", {"missing": list(missing)})
    with db.tx() as c:
        c.execute(db.text("UPDATE company SET setup_completed_at = coalesce(setup_completed_at, now())"))
        _audit(c, user, "setup.complete", "company", None, st.steps)
    return ok(_setup_status())


@app.get("/setup/model-catalog", response_model=Envelope[list[Model]], tags=["setup"])
def model_catalog(user: Employee = Depends(require(Role.admin))):
    approved = {r["id"] for r in db.rows("SELECT id FROM models WHERE approved")}
    return ok([m.model_copy(update={"approved": m.id in approved}) for m in MODEL_CATALOG.values()])


@app.get("/departments", response_model=Envelope[list[Department]], tags=["setup"])
def departments(user: Employee = Depends(require(Role.admin, Role.system))):
    return ok([Department(**r) for r in db.rows("SELECT id, name, parent_id FROM departments ORDER BY name")])


@app.post("/departments", response_model=Envelope[Department], status_code=201, tags=["setup"])
def create_department(body: DepartmentCreate, user: Employee = Depends(require(Role.admin))):
    if db.one("SELECT 1 AS x FROM departments WHERE name = :n", n=body.name.strip()):
        raise ApiError(ErrorCode.conflict, f"같은 이름의 부서가 있습니다: {body.name}")
    d = Department(id=_new_id("dept"), name=body.name.strip())
    with db.tx() as c:
        c.execute(db.text("INSERT INTO departments (id, name) VALUES (:id, :n)"), dict(id=d.id, n=d.name))
        _audit(c, user, "setup.department", d.id, None, d.model_dump())
    return ok(d)


@app.post("/employees", response_model=Envelope[list[Employee]], status_code=201, tags=["setup"])
def create_employees(body: list[EmployeeCreate], user: Employee = Depends(require(Role.admin))):
    """여러 명을 한 번에. 하나라도 틀리면 아무도 넣지 않는다. 비밀번호는 없음(로그인은 본인 설정 후)."""
    if not body or len(body) > 500:
        raise ApiError(ErrorCode.invalid, "직원은 한 번에 1~500명까지 넣을 수 있습니다")
    if any(e.role == Role.system for e in body):
        raise ApiError(ErrorCode.invalid, "system 역할은 만들 수 없습니다")
    emails = [e.email.strip().lower() for e in body]
    dup = {e for e in emails if emails.count(e) > 1} | {r["email"] for r in db.rows("SELECT email FROM employees WHERE lower(email) = ANY(:e)", e=emails)}
    if dup:
        raise ApiError(ErrorCode.conflict, f"이미 있거나 겹치는 이메일: {', '.join(sorted(dup))}")
    depts = {r["id"] for r in db.rows("SELECT id FROM departments")}
    if bad := sorted({e.dept_id for e in body} - depts):
        raise ApiError(ErrorCode.invalid, f"없는 부서: {', '.join(bad)}")
    ids = [_new_id("emp") for _ in body]
    with db.tx() as c:
        for i, e in zip(ids, body):
            c.execute(db.text("INSERT INTO employees (id, name, email, dept_id, role, job_title, status, hire_date) VALUES (:id, :n, :e, :d, :r, :t, 'active', :h)"),
                      dict(id=i, n=e.name.strip(), e=e.email.strip().lower(), d=e.dept_id, r=e.role.value, t=e.job_title.strip(), h=e.hire_date))
        _audit(c, user, "setup.employees", "employees", None, {"added": ids})
    return ok([Employee(**r) for r in db.rows(f"SELECT {EMP_COLS} FROM employees e JOIN departments d ON d.id = e.dept_id WHERE e.id = ANY(:ids) ORDER BY e.name", ids=ids)])


@app.post("/contracts", response_model=Envelope[Contract], status_code=201, tags=["setup"])
def create_contract(body: ContractCreate, user: Employee = Depends(require(Role.admin))):
    if body.term_end <= body.term_start:
        raise ApiError(ErrorCode.invalid, "계약 종료일은 시작일보다 뒤여야 합니다")
    if body.billing_type != "api_usage" and body.seats_purchased == 0:
        raise ApiError(ErrorCode.invalid, "좌석 과금 제품은 구매 좌석 수가 1 이상이어야 합니다")
    with db.tx() as c:
        tool = body.tool_id
        if tool and not db.one("SELECT 1 AS x FROM tools WHERE id = :t", c, t=tool):
            raise ApiError(ErrorCode.not_found, f"제품 없음: {tool}")
        if not tool:
            tool = _new_id("tool")
            c.execute(db.text("INSERT INTO tools (id, kind, vendor, product, category, approval_status, allowed_data_class)"
                              " VALUES (:id, CAST(:k AS tool_kind), :v, :p, :c, 'approved', CAST(:dc AS data_class))"),
                      dict(id=tool, k=body.kind, v=body.vendor.strip(), p=body.product.strip(), c=body.category, dc=body.allowed_data_class.value))
        ctr = Contract(id=_new_id("ctr"), tool_id=tool, plan_name=body.plan_name.strip(), billing_type=body.billing_type,
                       seats_purchased=0 if body.billing_type == "api_usage" else body.seats_purchased, seat_price_krw=body.seat_price_krw,
                       term_start=datetime.combine(body.term_start, datetime.min.time(), UTC), term_end=datetime.combine(body.term_end, datetime.min.time(), UTC))
        c.execute(db.text("INSERT INTO contracts (id, tool_id, plan_name, billing_type, seats_purchased, seat_price_krw, term_start, term_end)"
                          " VALUES (:id, :t, :p, CAST(:b AS billing_type), :n, :pr, :s, :e)"),
                  dict(id=ctr.id, t=tool, p=ctr.plan_name, b=ctr.billing_type.value, n=ctr.seats_purchased, pr=ctr.seat_price_krw, s=body.term_start, e=body.term_end))
        _audit(c, user, "setup.contract", ctr.id, None, ctr.model_dump(mode="json"))
    return ok(ctr)


@app.put("/models/approved", response_model=Envelope[list[Model]], tags=["setup"])
def approve_models(body: ModelApproval, user: Employee = Depends(require(Role.admin))):
    """목록에서 고른 모델만 승인. 처음 고른 모델은 models에 넣고, 같은 공급사의 API 계약 제품에 묶는다."""
    if bad := sorted(set(body.model_ids) - set(MODEL_CATALOG)):
        raise ApiError(ErrorCode.invalid, f"목록에 없는 모델: {', '.join(bad)}")
    before = [r["id"] for r in db.rows("SELECT id FROM models WHERE approved ORDER BY id")]
    with db.tx() as c:
        for mid in body.model_ids:
            m = MODEL_CATALOG[mid]
            tool = db.one("SELECT t.id FROM tools t JOIN contracts k ON k.tool_id = t.id WHERE lower(t.vendor) = lower(:v) AND k.billing_type = 'api_usage' LIMIT 1", c, v=m.vendor)
            c.execute(db.text("INSERT INTO models (id, tool_id, vendor, name, capabilities, input_usd_per_mtok, output_usd_per_mtok, approved, allowed_data_class)"
                              " VALUES (:id, :t, :v, :n, :cap, :i, :o, true, CAST(:dc AS data_class)) ON CONFLICT (id) DO NOTHING"),
                      dict(id=m.id, t=tool and tool["id"], v=m.vendor, n=m.name, cap=m.capabilities, i=m.input_usd_per_mtok, o=m.output_usd_per_mtok, dc=m.allowed_data_class.value))
        c.execute(db.text("UPDATE models SET approved = (id = ANY(:ids))"), dict(ids=body.model_ids))
        _audit(c, user, "setup.models", "models", {"approved": before}, {"approved": sorted(body.model_ids)})
    return ok([Model(**r) for r in db.rows("SELECT id, vendor, name, capabilities, input_usd_per_mtok::float AS input_usd_per_mtok,"
                                           " output_usd_per_mtok::float AS output_usd_per_mtok, approved, allowed_data_class FROM models ORDER BY id")])


@app.post("/projects", response_model=Envelope[Project], status_code=201, tags=["setup"])
def create_project(body: ProjectCreate, user: Employee = Depends(require(Role.admin))):
    if not db.one("SELECT 1 AS x FROM models WHERE id = :m AND approved", m=body.model_id):
        raise ApiError(ErrorCode.invalid, f"승인된 모델이 아닙니다: {body.model_id}")
    owner = db.one("SELECT dept_id FROM employees WHERE id = :e AND status = 'active'", e=body.owner_id)
    if not owner:
        raise ApiError(ErrorCode.invalid, f"재직 중인 담당자가 아닙니다: {body.owner_id}")
    if not db.one("SELECT 1 AS x FROM departments WHERE id = :d", d=body.dept_id):
        raise ApiError(ErrorCode.invalid, f"없는 부서: {body.dept_id}")
    prj = Project(id=_new_id("prj"), **body.model_dump())
    with db.tx() as c:
        c.execute(db.text("INSERT INTO projects (id, name, dept_id, owner_id, purpose, model_id, environment, monthly_budget_krw)"
                          " VALUES (:id, :name, :dept_id, :owner_id, :purpose, :model_id, CAST(:environment AS environment), :monthly_budget_krw)"),
                  prj.model_dump(mode="json"))
        c.execute(db.text("INSERT INTO api_keys (id, project_id, label, vendor_key_ref, created_at) VALUES (:id, :p, :l, :r, now())"),
                  dict(id=f"key_{prj.id}", p=prj.id, l=f"{prj.name} 키", r=f"vk_{prj.id}"))
        _audit(c, user, "setup.project", prj.id, None, prj.model_dump(mode="json"))
    return ok(prj)


def _budget_view(year: int) -> BudgetView:
    b = {r["category"]: r["annual_krw"] for r in db.rows("SELECT category, annual_krw FROM budgets WHERE fiscal_year = :y", y=year)}
    # 좌석료는 배정 여부와 상관없이 구매한 좌석 수만큼 낸다
    seat_cost = db.one("SELECT coalesce(sum(seats_purchased * seat_price_krw), 0)::bigint AS v FROM contracts WHERE billing_type <> 'api_usage'")["v"]
    prj = db.one("SELECT coalesce(sum(monthly_budget_krw), 0)::bigint AS v FROM projects")["v"]
    seat, api = b.get("seat", 0), b.get("api", 0)
    return BudgetView(fiscal_year=year, seat_annual_krw=seat, api_annual_krw=api, seat_monthly_krw=seat // 12, api_monthly_krw=api // 12,
                      seat_monthly_cost_krw=seat_cost, project_budget_sum_krw=prj)


@app.get("/budgets", response_model=Envelope[BudgetView], tags=["setup"])
def get_budgets(fiscal_year: int | None = None, user: Employee = Depends(require(Role.admin, Role.system))):
    return ok(_budget_view(fiscal_year or _this_year()))


@app.put("/budgets", response_model=Envelope[BudgetView], tags=["setup"])
def set_budgets(body: BudgetSet, user: Employee = Depends(require(Role.admin))):
    before = _budget_view(body.fiscal_year)
    with db.tx() as c:
        for cat, v in (("seat", body.seat_annual_krw), ("api", body.api_annual_krw)):
            c.execute(db.text("INSERT INTO budgets (fiscal_year, category, annual_krw) VALUES (:y, :c, :v)"
                              " ON CONFLICT (fiscal_year, category) DO UPDATE SET annual_krw = EXCLUDED.annual_krw"), dict(y=body.fiscal_year, c=cat, v=v))
        _audit(c, user, "setup.budgets", f"budgets:{body.fiscal_year}", before.model_dump(), body.model_dump())
    return ok(_budget_view(body.fiscal_year))


TASK_COLS = "id, employee_id, title, description, frequency, tools_used, source, updated_at"
FX = 1380.0  # 원/달러. 수집 배치가 붙으면 usage_records.fx_rate를 쓴다
KST = timedelta(hours=9)


def _krw(usd: float) -> Money:
    return Money(krw=round(usd * FX), usd=round(usd, 4))


def _tools_with_plan(approved_only: bool = False) -> list[ToolWithPlan]:
    rs = db.rows("SELECT t.*, c.id AS c_id, c.plan_name, c.billing_type, c.seats_purchased, c.seat_price_krw,"
                 " c.term_start::timestamptz AS term_start, c.term_end::timestamptz AS term_end, c.mid_term_reduction"
                 " FROM tools t LEFT JOIN contracts c ON c.tool_id = t.id"
                 " WHERE NOT :ap OR t.approval_status = 'approved' ORDER BY t.kind, t.id", ap=approved_only)
    out = []
    for r in rs:
        c = None
        if r["c_id"]:
            c = Contract(id=r["c_id"], tool_id=r["id"], **{k: r[k] for k in ("plan_name", "billing_type", "seats_purchased", "seat_price_krw", "term_start", "term_end", "mid_term_reduction")})
        out.append(ToolWithPlan(**{k: r[k] for k in ("id", "vendor", "product", "category", "approval_status", "allowed_data_class", "policy_note")}, contract=c))
    return out


def _assets(emp: Employee) -> MeResponse:
    """실제로 쓸 수 있는 도구만: 좌석 계약은 본인 배정 좌석이 있을 때, API 계약은 소속 팀 프로젝트가 그 제품 모델을 쓸 때."""
    usable = {r["tool_id"] for r in db.rows(
        "SELECT c.tool_id FROM seat_assignments s JOIN contracts c ON c.id = s.contract_id WHERE s.employee_id = :e AND s.status = 'assigned'"
        " UNION SELECT m.tool_id FROM projects p JOIN models m ON m.id = p.model_id WHERE p.dept_id = :d", e=emp.id, d=emp.dept_id)}
    tasks = [RegisteredTask(**r) for r in db.rows(f"SELECT {TASK_COLS} FROM registered_tasks WHERE employee_id = :e ORDER BY updated_at DESC", e=emp.id)]
    return MeResponse(employee=emp, tasks=tasks, available_tools=[t for t in _tools_with_plan(True) if t.contract and t.id in usable])


@app.get("/me", response_model=Envelope[MeResponse], tags=["me"])
def me(user: Employee = Depends(current_user)):
    return ok(_assets(user))


@app.get("/employees/{employee_id}/assets", response_model=Envelope[MeResponse], tags=["F4"])
def employee_assets(employee_id: str, user: Employee = Depends(require(Role.admin, Role.system))):
    emp = get_employee(employee_id)
    if not emp:
        raise ApiError(ErrorCode.not_found, "직원 없음")
    return ok(_assets(emp))


@app.get("/me/tasks", response_model=Envelope[list[RegisteredTask]], tags=["me"])
def list_my_tasks(user: Employee = Depends(require(Role.employee))):
    return ok([RegisteredTask(**r) for r in db.rows(f"SELECT {TASK_COLS} FROM registered_tasks WHERE employee_id = :e ORDER BY updated_at DESC", e=user.id)])


@app.post("/me/tasks", response_model=Envelope[RegisteredTask], status_code=201, tags=["me"])
def create_my_task(body: TaskCreate, user: Employee = Depends(require(Role.employee))):
    with db.tx() as c:
        r = db.one(f"INSERT INTO registered_tasks ({TASK_COLS}) VALUES (:id, :e, :t, :d, :f, :tu, :s, now()) RETURNING {TASK_COLS}", c,
                   id=_new_id("task"), e=user.id, t=body.title, d=body.description, f=body.frequency, tu=body.tools_used, s=body.source.value)
    return ok(RegisteredTask(**r))


@app.patch("/me/tasks/{task_id}", response_model=Envelope[RegisteredTask], tags=["me"])
def update_my_task(task_id: str, body: TaskUpdate, user: Employee = Depends(require(Role.employee))):
    ch = body.model_dump(exclude_none=True)
    sets = "".join(f"{k} = :{k}, " for k in ch)
    with db.tx() as c:
        r = db.one(f"UPDATE registered_tasks SET {sets}updated_at = now() WHERE id = :id AND employee_id = :e RETURNING {TASK_COLS}", c, id=task_id, e=user.id, **ch)
    if not r:
        raise ApiError(ErrorCode.not_found, "본인 업무가 아니거나 없음")
    return ok(RegisteredTask(**r))


# ---------- catalog ----------
@app.get("/tools", response_model=Envelope[list[ToolWithPlan]], tags=["catalog"])
def list_tools(category: str | None = None, user: Employee = Depends(current_user)):
    return ok([t for t in _tools_with_plan() if not category or t.category == category])


@app.get("/models", response_model=Envelope[list[Model]], tags=["catalog"])
def list_models(user: Employee = Depends(current_user)):
    return ok([Model(**r) for r in db.rows("SELECT id, vendor, name, capabilities, input_usd_per_mtok, output_usd_per_mtok, approved, allowed_data_class FROM models WHERE approved ORDER BY input_usd_per_mtok DESC")])


# ---------- updates / suggestions (F1) ----------
UPD_COLS = "id, source_id, vendor, published_at, title, summary, url, affected_plans, fetched_at"
SUG_COLS = "id, update_id, target_type, coalesce(target_employee_id, target_dept_id) AS target_id, task_id, relevance, body, status, hold_reason, sent_at"


def _update(update_id: str) -> VendorUpdate:
    r = db.one(f"SELECT {UPD_COLS} FROM vendor_updates WHERE id = :id", id=update_id)
    if not r:
        raise ApiError(ErrorCode.not_found, "업데이트 없음")
    return VendorUpdate(**r)


@app.get("/updates", response_model=Envelope[Page[VendorUpdate]], tags=["F1"])
def list_updates(limit: int = 50, offset: int = 0, user: Employee = Depends(current_user)):
    rs = db.rows(f"SELECT {UPD_COLS} FROM vendor_updates ORDER BY published_at DESC LIMIT :l OFFSET :o", l=limit, o=offset)
    return ok(Page(items=[VendorUpdate(**r) for r in rs], total=db.one("SELECT count(*) AS n FROM vendor_updates")["n"], limit=limit, offset=offset))


@app.get("/updates/{update_id}", response_model=Envelope[VendorUpdate], tags=["F1"])
def get_update(update_id: str, user: Employee = Depends(current_user)):
    return ok(_update(update_id))


@app.get("/updates/{update_id}/relevant-tasks", response_model=Envelope[RelevantTasksResponse], tags=["F1"])
def relevant_tasks(update_id: str, job_filter: list[str] | None = Query(None), user: Employee = Depends(require(Role.system, Role.admin))):
    upd = _update(update_id)
    rs = db.rows("SELECT t.id, t.title, t.description, e.id AS employee_id, e.job_title, e.dept_id FROM registered_tasks t"
                 " JOIN employees e ON e.id = t.employee_id WHERE e.status = 'active' ORDER BY t.id")
    cands = [RelevantTaskCandidate(task_id=r["id"], job_title=r["job_title"], team_id=r["dept_id"],
                                   employee_id=r["employee_id"] if user.role == Role.system else None,  # 관리자는 팀 단위만
                                   summary=f"{r['title']}: {r['description']}")
             for r in rs if not job_filter or r["job_title"] in job_filter]
    return ok(RelevantTasksResponse(update=upd, candidates=cands))


@app.get("/suggestions", response_model=Envelope[Page[Suggestion]], tags=["F1"])
def list_suggestions(status: SuggestionStatus | None = None, update_id: str | None = None, user: Employee = Depends(require(Role.system, Role.admin))):
    rs = db.rows(f"SELECT {SUG_COLS} FROM suggestions WHERE (CAST(:s AS suggestion_status) IS NULL OR status = CAST(:s AS suggestion_status))"
                 " AND (CAST(:u AS varchar) IS NULL OR update_id = :u) ORDER BY created_at DESC", s=status and status.value, u=update_id)
    return ok(Page(items=[Suggestion(**r) for r in rs], total=len(rs)))


@app.post("/suggestions", response_model=Envelope[Suggestion], status_code=201, tags=["F1"])
def create_suggestion(body: SuggestionCreate, user: Employee = Depends(require(Role.system, Role.admin))):
    if body.status == SuggestionStatus.on_hold:
        if not (body.hold_reason or "").strip():
            raise ApiError(ErrorCode.invalid, "보류(on_hold)에는 hold_reason이 필요합니다")
        status = SuggestionStatus.on_hold
    else:
        if not body.relevance.strip():
            raise ApiError(ErrorCode.invalid, "관련성 근거(relevance) 없이는 제안을 보낼 수 없습니다. 보내지 않으려면 status=on_hold와 hold_reason을 쓰세요")
        if not body.body.strip():
            raise ApiError(ErrorCode.invalid, "제안 본문(body)이 비어 있습니다")
        status = SuggestionStatus.draft  # v2: 사람 승인 없음. send_suggestions 배치가 알림 정책을 검사해 보낸다
    _update(body.update_id)
    emp_id = body.target_id if body.target_type.value == "employee" else None
    dept_id = body.target_id if body.target_type.value == "team" else None
    if emp_id and not get_employee(emp_id):
        raise ApiError(ErrorCode.not_found, "대상 직원 없음")
    with db.tx() as c:
        r = db.one("INSERT INTO suggestions (id, update_id, target_type, target_employee_id, target_dept_id, task_id, relevance, body, status, hold_reason)"
                   f" VALUES (:id, :u, :tt, :e, :d, :t, :rel, :b, :s, :h) RETURNING {SUG_COLS}", c,
                   id=_new_id("sug"), u=body.update_id, tt=body.target_type.value, e=emp_id, d=dept_id, t=body.task_id,
                   rel=body.relevance, b=body.body, s=status.value, h=body.hold_reason)
    return ok(Suggestion(**r))


def _tool_category(update_id: str) -> str:
    r = db.one("SELECT t.category FROM vendor_updates u JOIN tools t ON t.vendor = u.vendor WHERE u.id = :id LIMIT 1", id=update_id)
    return r["category"] if r else "unknown"


@app.post("/suggestions/{suggestion_id}/feedback", response_model=Envelope[dict], status_code=201, tags=["F1"])
def feedback(suggestion_id: str, body: FeedbackCreate, user: Employee = Depends(require(Role.employee, Role.admin))):
    s = db.one("SELECT id, update_id FROM suggestions WHERE id = :id", id=suggestion_id)
    if not s:
        raise ApiError(ErrorCode.not_found, "제안 없음")
    fid, held = _new_id("fb"), None
    with db.tx() as c:
        c.execute(db.text("INSERT INTO feedback (id, suggestion_id, employee_id, reaction, created_at) VALUES (:id, :s, :e, :r, now())"
                          " ON CONFLICT (suggestion_id, employee_id) DO UPDATE SET reaction = EXCLUDED.reaction, created_at = now()"),
                  dict(id=fid, s=suggestion_id, e=user.id, r=body.reaction.value))
        # 알림 정책: 같은 팀이 같은 제품 분야 제안에 '관련 없음'을 2번 연속 → 그 팀에 4주 보류
        cat = _tool_category(s["update_id"])
        last2 = db.rows("SELECT f.reaction FROM feedback f JOIN employees e ON e.id = f.employee_id JOIN suggestions g ON g.id = f.suggestion_id"
                        " JOIN vendor_updates u ON u.id = g.update_id JOIN tools t ON t.vendor = u.vendor"
                        " WHERE e.dept_id = :d AND t.category = :cat ORDER BY f.created_at DESC LIMIT 2", c, d=user.dept_id, cat=cat)
        if len(last2) == 2 and all(r["reaction"] == "irrelevant" for r in last2):
            held = (_now() + timedelta(weeks=4)).isoformat()
            c.execute(db.text("INSERT INTO suggestion_holds (id, dept_id, tool_category, hold_until, reason) VALUES (:id, :d, :cat, :h, '관련 없음 2회 연속')"
                              " ON CONFLICT (dept_id, tool_category) DO UPDATE SET hold_until = EXCLUDED.hold_until"),
                      dict(id=_new_id("hold"), d=user.dept_id, cat=cat, h=held))
    return ok({"feedback_id": fid, "suggestion_id": suggestion_id, "reaction": body.reaction.value, "team_hold_until": held})


@app.get("/adoption/teams", response_model=Envelope[list[TeamAdoption]], tags=["F1"])
def adoption(team_id: str | None = None, user: Employee = Depends(require(Role.admin))):
    # 팀 단위만, 5명 이상 팀만 (설계 원칙 3). 개인 제안은 받은 사람의 팀으로 센다
    rs = db.rows(
        "WITH s AS (SELECT g.id, coalesce(g.target_dept_id, e.dept_id) AS dept_id FROM suggestions g"
        "  LEFT JOIN employees e ON e.id = g.target_employee_id WHERE g.status = 'sent'),"
        " f AS (SELECT s.dept_id, f.reaction, f.employee_id FROM feedback f JOIN s ON s.id = f.suggestion_id)"
        " SELECT d.id AS team_id, d.name AS team_name,"
        "  (SELECT count(*) FROM employees e WHERE e.dept_id = d.id AND e.role = 'employee') AS headcount,"
        "  (SELECT count(*) FROM s WHERE s.dept_id = d.id) AS suggestions_sent,"
        "  (SELECT count(*) FROM f WHERE f.dept_id = d.id AND f.reaction = 'useful') AS useful,"
        "  (SELECT count(*) FROM f WHERE f.dept_id = d.id AND f.reaction = 'irrelevant') AS irrelevant,"
        "  (SELECT count(*) FROM f WHERE f.dept_id = d.id AND f.reaction = 'question') AS questions,"
        "  (SELECT count(DISTINCT f.employee_id) FROM f WHERE f.dept_id = d.id) AS responders"
        " FROM departments d WHERE CAST(:t AS varchar) IS NULL OR d.id = :t ORDER BY d.id", t=team_id)
    return ok([TeamAdoption(**{k: v for k, v in r.items() if k != "responders"}, active_ratio=round(r["responders"] / r["headcount"], 2))
               for r in rs if r["headcount"] >= 5 and r["suggestions_sent"] > 0])


# ---------- F2 ----------
@app.get("/eval-results", response_model=Envelope[list[EvalResult]], tags=["F2"])
def eval_results(task_type: str, model_ids: list[str] | None = Query(None), user: Employee = Depends(require(Role.employee, Role.admin, Role.system))):
    rs = db.rows("SELECT id, task_type, model_id, quality_score, threshold, passed, avg_input_tokens, avg_output_tokens, latency_ms, evaluated_at"
                 " FROM eval_results WHERE task_type = :t ORDER BY id", t=task_type)
    return ok([EvalResult(**r) for r in rs if not model_ids or r["model_id"] in model_ids])


@app.post("/estimates/task-cost", response_model=Envelope[TaskCostEstimateResponse], tags=["F2"])
def estimate(body: TaskCostEstimateRequest, user: Employee = Depends(current_user)):
    r = db.one("SELECT id, vendor, name, capabilities, input_usd_per_mtok, output_usd_per_mtok, approved, allowed_data_class FROM models WHERE id = :id", id=body.model_id)
    if not r:
        raise ApiError(ErrorCode.not_found, "모델 없음")
    m = Model(**r)
    daily_usd = body.requests_per_day * (body.avg_input_tokens * m.input_usd_per_mtok + body.avg_output_tokens * m.output_usd_per_mtok) / 1_000_000
    monthly_usd = daily_usd * body.working_days_per_month
    formula = (f"{body.requests_per_day}건 × ({body.avg_input_tokens}×${m.input_usd_per_mtok}/M + "
               f"{body.avg_output_tokens}×${m.output_usd_per_mtok}/M) = ${daily_usd:.2f}/일 × {body.working_days_per_month}일, 환율 {FX:.0f}원")
    return ok(TaskCostEstimateResponse(model=m, daily=_krw(daily_usd), monthly=_krw(monthly_usd), fx_rate_krw_per_usd=FX, formula=formula))


# ---------- F5 ----------
def _unmet_aggs(tag: str | None = None) -> list[UnmetNeedAggregate]:
    rs = db.rows("SELECT capability_tag, count(*) AS count, count(DISTINCT dept_id) AS dept_count, (array_agg(summary ORDER BY created_at))[1:3] AS sample_summaries"
                 " FROM unmet_needs WHERE CAST(:t AS varchar) IS NULL OR capability_tag = :t GROUP BY capability_tag ORDER BY count(*) DESC", t=tag)
    return [UnmetNeedAggregate(**r, threshold_met=r["count"] >= 3 and r["dept_count"] >= 2) for r in rs]


@app.get("/unmet-needs", response_model=Envelope[list[UnmetNeedAggregate]], tags=["F5"])
def list_unmet(min_count: int = 1, capability_tag: str | None = None, user: Employee = Depends(require(Role.admin))):
    return ok([a for a in _unmet_aggs(capability_tag) if a.count >= min_count])


@app.post("/unmet-needs", response_model=Envelope[UnmetNeedCreateResponse], status_code=201, tags=["F5"])
def record_unmet(body: UnmetNeedCreate, user: Employee = Depends(require(Role.employee, Role.admin, Role.system))):
    from schemas.entities import UnmetNeed
    with db.tx() as c:
        r = db.one("INSERT INTO unmet_needs (id, employee_id, dept_id, summary, capability_tag, created_at) VALUES (:id, :e, :d, :s, :t, now())"
                   " RETURNING id, employee_id, dept_id, summary, capability_tag, status, created_at", c,
                   id=_new_id("need"), e=user.id, d=user.dept_id, s=body.summary, t=body.capability_tag)
    agg = _unmet_aggs(body.capability_tag)[0]
    return ok(UnmetNeedCreateResponse(need=UnmetNeed(**r), same_tag_count=agg.count, same_tag_dept_count=agg.dept_count, threshold_met=agg.threshold_met))


# ---------- usage / anomalies (F3) ----------
SCOPE_COL = {UsageScope.project: "u.project_id", UsageScope.employee: "u.employee_id", UsageScope.contract: "u.contract_id", UsageScope.dept: "p.dept_id"}
GROUP_COL = {BreakdownBy.model: "coalesce(m.name, u.model_id)", BreakdownBy.api_key: "u.api_key_id", BreakdownBy.input_length: "u.input_length_bucket"}
USAGE_FROM = " FROM usage_records u LEFT JOIN projects p ON p.id = u.project_id LEFT JOIN models m ON m.id = u.model_id"


def _usage_rows(scope: UsageScope, scope_id: str, start: datetime, end: datetime, key_sql: str) -> list[dict]:
    return db.rows(f"SELECT {key_sql} AS key, sum(u.requests)::int AS requests, sum(u.input_tokens)::bigint AS input_tokens,"
                   " sum(u.output_tokens)::bigint AS output_tokens, sum(u.cost_usd)::float AS usd, sum(u.cost_krw)::int AS krw, max(u.period_start) AS last"
                   f"{USAGE_FROM} WHERE {SCOPE_COL[scope]} = :sid AND u.period_start BETWEEN :s AND :e GROUP BY 1 ORDER BY 1",
                   sid=scope_id, s=start, e=end)


def _series_rows(scope, scope_id, start, end, granularity=Granularity.day) -> list[UsagePoint]:
    rs = _usage_rows(scope, scope_id, start, end, f"date_trunc('{granularity.value}', u.period_start)")
    return [UsagePoint(period_start=r["key"], requests=r["requests"], input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                       cost=Money(krw=r["krw"], usd=round(r["usd"], 4))) for r in rs]


def _breakdown(scope, scope_id, start, end, by: BreakdownBy) -> list[BreakdownRow]:
    rs = _usage_rows(scope, scope_id, start, end, GROUP_COL[by])
    total = sum(r["usd"] for r in rs) or 1
    return sorted([BreakdownRow(key=str(r["key"]), requests=r["requests"], input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                                cost=Money(krw=r["krw"], usd=round(r["usd"], 4)), share=round(r["usd"] / total, 4)) for r in rs], key=lambda b: -b.share)


@app.get("/usage", response_model=Envelope[UsageResponse], tags=["F3"])
def usage(scope: UsageScope, scope_id: str, start: datetime, end: datetime, granularity: Granularity = Granularity.day, group_by: BreakdownBy | None = None, user: Employee = Depends(require(Role.employee, Role.admin, Role.system))):
    if user.role == Role.employee and not (scope == UsageScope.employee and scope_id == user.id):
        raise ApiError(ErrorCode.forbidden, "직원은 본인 사용량만 조회할 수 있습니다")
    series = _series_rows(scope, scope_id, start, end, granularity)
    last = db.one(f"SELECT max(u.period_start) AS t{USAGE_FROM} WHERE {SCOPE_COL[scope]} = :sid", sid=scope_id)["t"]
    summary = UsageSummary(scope=scope.value, scope_id=scope_id, period_start=start, period_end=end,
                           requests=sum(p.requests for p in series), input_tokens=sum(p.input_tokens for p in series),
                           output_tokens=sum(p.output_tokens for p in series),
                           cost=Money(krw=sum(p.cost.krw for p in series), usd=round(sum(p.cost.usd for p in series), 4)), last_activity_at=last)
    breakdown = _breakdown(scope, scope_id, start, end, group_by) if group_by else []
    return ok(UsageResponse(summary=summary, series=series, breakdown=breakdown))


ANM_COLS = ("a.id, a.scope_type, a.scope_id, p.name AS scope_name, a.rule, a.baseline::float AS baseline, a.observed::float AS observed,"
            " a.ratio::float AS ratio, a.projected_monthly_krw, a.projected_monthly_usd::float AS projected_monthly_usd, a.status, a.created_at")


def _anomaly(r: dict) -> Anomaly:
    krw, usd = r.pop("projected_monthly_krw"), r.pop("projected_monthly_usd")
    return Anomaly(**r, projected_monthly_cost=Money(krw=krw, usd=usd) if krw is not None else None)


@app.get("/projects", response_model=Envelope[list[Project]], tags=["F3"])
def projects(user: Employee = Depends(require(Role.admin, Role.system))):
    return ok([Project(**r) for r in db.rows("SELECT id, name, dept_id, owner_id, purpose, model_id, environment, monthly_budget_krw FROM projects ORDER BY id")])


@app.get("/org/members", response_model=Envelope[list[OrgMember]], tags=["F4"])
def org_members(user: Employee = Depends(require(Role.admin, Role.system))):
    """좌석 배치도용. 도구 판정은 _assets와 같다: 배정 좌석 + 소속 팀 프로젝트가 쓰는 API 모델."""
    tools: dict[str, list[MemberTool]] = {}
    db_has_usage = {r["tool_id"]: True for r in db.rows("SELECT DISTINCT c.tool_id FROM usage_records u JOIN contracts c ON c.id = u.contract_id WHERE u.employee_id IS NOT NULL")}
    # 최근 30일 사용: 사용 기록(usage_records.employee_id)을 주는 좌석 제품만 숫자가 있고, 나머지(기록 미제공)는 None
    for r in db.rows("SELECT s.employee_id, c.tool_id, t.product, c.plan_name AS detail, s.flag, s.last_activity_at, u.days AS active_days_30, u.reqs AS requests_30"
                     " FROM seat_assignments s JOIN contracts c ON c.id = s.contract_id JOIN tools t ON t.id = c.tool_id"
                     " LEFT JOIN (SELECT contract_id, employee_id, count(DISTINCT date_trunc('day', period_start))::int AS days, sum(requests)::int AS reqs"
                     "            FROM usage_records WHERE employee_id IS NOT NULL AND period_start > now() - interval '30 days' GROUP BY 1, 2) u"
                     "   ON u.contract_id = s.contract_id AND u.employee_id = s.employee_id"
                     " WHERE s.status = 'assigned' ORDER BY t.product"):
        if r["active_days_30"] is None and db_has_usage.get(r["tool_id"]):
            r["active_days_30"], r["requests_30"] = 0, 0  # 기록을 주는 제품인데 30일간 0건
        tools.setdefault(r.pop("employee_id"), []).append(MemberTool(via="seat", **r))
    by_dept: dict[str, list[MemberTool]] = {}
    for r in db.rows("SELECT p.dept_id, m.tool_id, t.product, p.name || ' · ' || m.name AS detail FROM projects p"
                     " JOIN models m ON m.id = p.model_id JOIN tools t ON t.id = m.tool_id ORDER BY p.id"):
        by_dept.setdefault(r.pop("dept_id"), []).append(MemberTool(via="project", **r))
    emps = db.rows("SELECT e.id, e.name, e.email, e.dept_id, d.name AS dept_name, e.role, e.job_title, e.status, e.hire_date, e.slack_user_id"
                   " FROM employees e JOIN departments d ON d.id = e.dept_id WHERE e.role <> 'system' ORDER BY e.dept_id, e.id")
    return ok([OrgMember(employee=Employee(**e), tools=tools.get(e["id"], []) + by_dept.get(e["dept_id"], [])) for e in emps])


@app.get("/anomalies", response_model=Envelope[list[Anomaly]], tags=["F3"])
def anomalies(status: str | None = None, user: Employee = Depends(require(Role.admin, Role.system))):
    rs = db.rows(f"SELECT {ANM_COLS} FROM anomalies a LEFT JOIN projects p ON p.id = a.scope_id"
                 " WHERE CAST(:s AS varchar) IS NULL OR a.status::text = :s ORDER BY a.created_at DESC", s=status)
    return ok([_anomaly(r) for r in rs])


@app.get("/anomalies/{anomaly_id}", response_model=Envelope[AnomalyDetail], tags=["F3"])
def anomaly_detail(anomaly_id: str, user: Employee = Depends(require(Role.admin, Role.system))):
    r = db.one(f"SELECT {ANM_COLS} FROM anomalies a LEFT JOIN projects p ON p.id = a.scope_id WHERE a.id = :id", id=anomaly_id)
    if not r:
        raise ApiError(ErrorCode.not_found, "경보 없음")
    a = _anomaly(r)
    prj = db.one("SELECT id, name, dept_id, owner_id, purpose, model_id, environment, monthly_budget_krw FROM projects WHERE id = :id", id=a.scope_id)
    end = a.created_at
    start = end - timedelta(days=14)
    scope = UsageScope.project
    return ok(AnomalyDetail(anomaly=a, project=Project(**prj) if prj else None, series=_series_rows(scope, a.scope_id, start, end),
                            breakdown_by_model=_breakdown(scope, a.scope_id, start, end, BreakdownBy.model),
                            breakdown_by_input_length=_breakdown(scope, a.scope_id, start, end, BreakdownBy.input_length)))


# ---------- F4 ----------
@app.get("/inventory", response_model=Envelope[list[ContractInventory]], tags=["F4"])
def inventory(contract_id: str | None = None, user: Employee = Depends(require(Role.admin, Role.system))):
    contracts = db.rows(
        "SELECT c.id, c.tool_id, c.plan_name, c.billing_type, c.seats_purchased, c.seat_price_krw,"
        " c.term_start::timestamptz AS term_start, c.term_end::timestamptz AS term_end, c.mid_term_reduction,"
        " t.product AS tool_name, count(s.id) AS assigned"
        " FROM contracts c JOIN tools t ON t.id = c.tool_id"
        " LEFT JOIN seat_assignments s ON s.contract_id = c.id AND s.status = 'assigned'"
        " WHERE c.seats_purchased > 0 AND (CAST(:cid AS varchar) IS NULL OR c.id = :cid)"
        " GROUP BY c.id, t.product, t.kind ORDER BY t.kind, c.id", cid=contract_id)
    cands = db.rows(
        "SELECT s.contract_id, s.employee_id, e.name AS employee_name, d.name AS dept_name, s.last_activity_at, s.flag"
        " FROM seat_assignments s JOIN employees e ON e.id = s.employee_id JOIN departments d ON d.id = e.dept_id"
        " WHERE s.status = 'assigned' AND s.flag IS NOT NULL ORDER BY s.last_activity_at NULLS FIRST")
    out = []
    for c in contracts:
        low = [LowUsageCandidate(**{k: v for k, v in x.items() if k != "contract_id"}, needs_confirmation=x["flag"] != LicenseFlag.recent_hire.value)
               for x in cands if x["contract_id"] == c["id"]]
        out.append(ContractInventory(contract=Contract(**c), tool_name=c["tool_name"], purchased=c["seats_purchased"],
                                     assigned=c["assigned"], unassigned=c["seats_purchased"] - c["assigned"], low_usage_candidates=low))
    return ok(out)


# ---------- plans (공통 실행 게이트) ----------
PLAN_COLS = "id, type, risk_level, status, items, rationale, evidence, ai_opinion, requested_by, approver_id, approved_at, reject_reason, execution_key, executed_at, result, created_at"


def _plan(plan_id: str, conn=None) -> ActionPlan | None:
    r = db.one(f"SELECT {PLAN_COLS} FROM action_plans WHERE id = :id", conn, id=plan_id)
    return ActionPlan(**r) if r else None


def _audit(conn, user: Employee, action: str, target: str, before: dict | None, after: dict | None) -> None:
    conn.execute(db.text("INSERT INTO audit_log (id, actor_id, actor_role, action, target, before, after, created_at)"
                         " VALUES (:id, :a, :r, :act, :t, CAST(:b AS jsonb), CAST(:af AS jsonb), now())"),
                 dict(id=_new_id("aud"), a=user.id, r=user.role.value, act=action, t=target, b=db.js(before), af=db.js(after)))


@app.get("/plans", response_model=Envelope[Page[ActionPlan]], tags=["plans"])
def list_plans(status: PlanStatus | None = None, type: PlanType | None = None, user: Employee = Depends(current_user)):
    rs = db.rows(f"SELECT {PLAN_COLS} FROM action_plans WHERE (CAST(:s AS plan_status) IS NULL OR status = CAST(:s AS plan_status))"
                 " AND (CAST(:t AS plan_type) IS NULL OR type = CAST(:t AS plan_type))"
                 " AND (CAST(:me AS varchar) IS NULL OR requested_by = :me) ORDER BY created_at DESC",
                 s=status and status.value, t=type and type.value, me=user.id if user.role == Role.employee else None)
    items = [ActionPlan(**r) for r in rs]
    return ok(Page(items=items, total=len(items)))


@app.post("/plans", response_model=Envelope[ActionPlan], status_code=201, tags=["plans"])
def create_plan(body: PlanCreate, user: Employee = Depends(require(Role.employee, Role.admin, Role.system))):
    allowed = {Role.employee: {PlanType.seat_assign, PlanType.seat_reclaim, PlanType.api_access, PlanType.model_change}}  # v2.1
    if user.role in allowed and body.type not in allowed[user.role]:
        raise ApiError(ErrorCode.forbidden, f"{user.role.value} 역할은 {body.type.value} 조치안을 만들 수 없습니다")
    if user.role == Role.employee and body.type in (PlanType.seat_assign, PlanType.seat_reclaim) and any(i.get("employee_id") != user.id for i in body.items):
        raise ApiError(ErrorCode.forbidden, "직원은 본인 이용권만 신청·반납할 수 있습니다")
    if user.role == Role.employee and body.type == PlanType.model_change:  # 시나리오 B: 본인 프로젝트의 테스트 환경만
        for i in body.items:
            prj = db.one("SELECT owner_id FROM projects WHERE id = :id", id=i.get("project_id", ""))
            if not prj or prj["owner_id"] != user.id:
                raise ApiError(ErrorCode.forbidden, "직원은 본인이 오너인 프로젝트의 모델 변경만 요청할 수 있습니다", {"project_id": i.get("project_id")})
            if i.get("environment", "test") != "test":
                raise ApiError(ErrorCode.forbidden, "직원은 테스트 환경 변경만 요청할 수 있습니다. 운영 환경은 자산관리가 요청합니다")
    try:
        items = validate_items(body.type, body.items)
    except ValidationError as e:
        raise ApiError(ErrorCode.invalid, f"{body.type.value} 조치안의 items 형식이 맞지 않습니다", {"errors": e.errors()})
    risk = RISK_BY_PLAN_TYPE[body.type]
    if body.type == PlanType.model_change and any(i.get("environment") == "prod" for i in items):
        risk = RiskLevel.high
    status = PlanStatus.approved if risk == RiskLevel.low else PlanStatus.pending_approval
    pid = _new_id("plan")
    with db.tx() as c:
        c.execute(db.text("INSERT INTO action_plans (id, type, risk_level, status, items, rationale, evidence, requested_by, created_at)"
                          " VALUES (:id, :ty, :rk, :st, CAST(:it AS jsonb), :ra, CAST(:ev AS jsonb), :by, now())"),
                  dict(id=pid, ty=body.type.value, rk=risk.value, st=status.value, it=db.js(items), ra=body.rationale,
                       ev=db.js([e.model_dump() for e in body.evidence]), by=user.id))
        _audit(c, user, "plan.create", pid, None, {"status": status.value, "risk_level": risk.value})
    plan = _plan(pid)
    if plan.type in SEAT_SQL:  # v2.1: 승인 대기로 들어가기 전에 한 번 검증한다 (중복 배정·휴직자·좌석 초과·회수 대상 없음). 실행 직전에 한 번 더 한다
        problem = _seat_blocker(plan)
        if problem:
            with db.tx() as c:
                c.execute(db.text("UPDATE action_plans SET status = 'failed', result = CAST(:r AS jsonb) WHERE id = :id"), dict(r=db.js({"ok": False, "reason": problem}), id=pid))
                _audit(c, user, "plan.reject_invalid", pid, {"status": status.value}, {"status": "failed", "reason": problem})
            raise ApiError(ErrorCode.conflict, problem, {"plan_id": pid})
    if risk == RiskLevel.low:  # 자동 처리 유형(현재 없음)만 즉시 실행
        plan = _execute(plan, f"auto_{pid}", user).plan
    return ok(plan)


@app.get("/plans/{plan_id}", response_model=Envelope[ActionPlan], tags=["plans"])
def get_plan(plan_id: str, user: Employee = Depends(current_user)):
    p = _plan(plan_id)
    if not p or (user.role == Role.employee and p.requested_by != user.id):
        raise ApiError(ErrorCode.not_found, "조치안 없음")
    return ok(p)


@app.get("/employees/{employee_id}", response_model=Envelope[EmployeeProfile], tags=["plans"])
def get_employee_profile(employee_id: str, user: Employee = Depends(require(Role.system, Role.admin))):
    """검토용 직원 프로필. 비밀번호·개인 사용량은 주지 않는다."""
    r = db.one("SELECT e.id, e.name, e.email, e.dept_id, d.name AS dept_name, e.role, e.job_title, e.status, e.hire_date, e.slack_user_id"
               " FROM employees e LEFT JOIN departments d ON d.id = e.dept_id WHERE e.id = :id", id=employee_id)
    if not r:
        raise ApiError(ErrorCode.not_found, "직원 없음")
    tasks = [RegisteredTask(**t) for t in db.rows(f"SELECT {TASK_COLS} FROM registered_tasks WHERE employee_id = :e ORDER BY updated_at DESC", e=employee_id)]
    products = [p["product"] for p in db.rows("SELECT t.product FROM seat_assignments s JOIN contracts c ON c.id = s.contract_id JOIN tools t ON t.id = c.tool_id"
                                               " WHERE s.employee_id = :e AND s.status = 'assigned'", e=employee_id)]
    return ok(EmployeeProfile(employee=Employee(**r), tasks=tasks, products=products))


@app.patch("/plans/{plan_id}/opinion", response_model=Envelope[ActionPlan], tags=["plans"])
def attach_opinion(plan_id: str, body: PlanOpinion, user: Employee = Depends(require(Role.system, Role.admin))):
    p = _plan(plan_id)
    if not p:
        raise ApiError(ErrorCode.not_found, "조치안 없음")
    body.generated_at = _now()
    with db.tx() as c:
        c.execute(db.text("UPDATE action_plans SET ai_opinion = :o WHERE id = :id"), dict(o=body.model_dump_json(), id=plan_id))
        _audit(c, user, "plan.opinion", plan_id, None, {"recommendation": body.recommendation})
    return ok(_plan(plan_id))


def _decide(plan_id: str, body: PlanDecision, user: Employee, approve: bool) -> ActionPlan:
    with db.tx() as c:
        # 상태 전이는 조건부 UPDATE 한 번. 동시에 두 명이 눌러도 한 명만 성공한다
        n = c.execute(db.text("UPDATE action_plans SET status = CAST(:st AS plan_status), approver_id = :u,"
                              " approved_at = CASE WHEN :ap THEN now() END, reject_reason = CASE WHEN :ap THEN NULL ELSE :rs END"
                              " WHERE id = :id AND status = 'pending_approval'"),
                      dict(st="approved" if approve else "rejected", u=user.id, ap=approve, rs=body.reason, id=plan_id)).rowcount
        if n == 0:
            p = _plan(plan_id, c)
            if not p:
                raise ApiError(ErrorCode.not_found, "조치안 없음")
            raise ApiError(ErrorCode.conflict, f"승인 대기 상태가 아님: {p.status.value}")
        _audit(c, user, "plan.approve" if approve else "plan.reject", plan_id, {"status": "pending_approval"},
               {"status": "approved" if approve else "rejected", "reason": body.reason})
    return _plan(plan_id)


@app.post("/plans/{plan_id}/approve", response_model=Envelope[ActionPlan], tags=["plans"])
def approve_plan(plan_id: str, body: PlanDecision, user: Employee = Depends(require(Role.admin))):
    return ok(_decide(plan_id, body, user, approve=True))


@app.post("/plans/{plan_id}/reject", response_model=Envelope[ActionPlan], tags=["plans"])
def reject_plan(plan_id: str, body: PlanDecision, user: Employee = Depends(require(Role.admin))):
    return ok(_decide(plan_id, body, user, approve=False))


SEAT_SQL = {
    PlanType.seat_assign: "INSERT INTO seat_assignments (id, contract_id, employee_id, status, assigned_at) VALUES (:sid, :c, :e, 'assigned', now())"
                          " ON CONFLICT (contract_id, employee_id) WHERE status = 'assigned' DO NOTHING",
    PlanType.seat_reclaim: "UPDATE seat_assignments SET status = 'unassigned', unassigned_at = now() WHERE contract_id = :c AND employee_id = :e AND status = 'assigned'",
}


def _seat_blocker(p: ActionPlan) -> str | None:
    """좌석 조치안 실행 직전 재검증. 막을 이유가 없으면 None.
    ponytail: 검사와 반영 사이 잠금이 없어 동시 배정 2건이 마지막 1석을 함께 통과할 수 있다. 필요하면 contracts 행을 FOR UPDATE로 잠근다."""
    if p.type not in SEAT_SQL:
        return None
    for cid in {i["contract_id"] for i in p.items}:
        c = db.one("SELECT c.seats_purchased, count(s.id) AS used FROM contracts c LEFT JOIN seat_assignments s ON s.contract_id = c.id AND s.status = 'assigned'"
                   " WHERE c.id = :c GROUP BY c.id", c=cid)
        if not c:
            return f"계약 없음: {cid}"
        adding = sum(1 for i in p.items if i["contract_id"] == cid) if p.type == PlanType.seat_assign else 0
        if c["used"] + adding > c["seats_purchased"]:
            return f"{cid} 남은 좌석 {c['seats_purchased'] - c['used']}석, 요청 {adding}석"
    for i in p.items:
        e = db.one("SELECT status FROM employees WHERE id = :e", e=i["employee_id"])
        has = db.one("SELECT 1 AS x FROM seat_assignments WHERE contract_id = :c AND employee_id = :e AND status = 'assigned'", c=i["contract_id"], e=i["employee_id"])
        if not e:
            return f"직원 없음: {i['employee_id']}"
        if p.type == PlanType.seat_assign and e["status"] != "active":
            return f"{i['employee_id']}는 {e['status']} 상태라 배정할 수 없습니다"
        if p.type == PlanType.seat_assign and has:
            return f"{i['employee_id']}는 이미 {i['contract_id']} 좌석이 있습니다"
        if p.type == PlanType.seat_reclaim and not has:
            return f"{i['employee_id']}에게 회수할 {i['contract_id']} 좌석이 없습니다"
        if p.type == PlanType.seat_reclaim and i.get("confirmation_id"):  # 확인 요청을 붙였으면 반납·휴직 응답이 있어야 회수
            a = db.one("SELECT answer FROM confirmations WHERE id = :id AND employee_id = :e", id=i["confirmation_id"], e=i["employee_id"])
            if not a or a["answer"] not in ("return_seat", "on_leave"):
                return f"{i['employee_id']}의 확인 응답이 반납·휴직이 아닙니다 ({a and a['answer'] or '무응답'})"
        r = i.get("reassign_to_employee_id")
        if r and (db.one("SELECT status FROM employees WHERE id = :e", e=r) or {}).get("status") != "active":
            return f"재배정 대상 {r}이 재직 중이 아닙니다"
        if r and db.one("SELECT 1 AS x FROM seat_assignments WHERE contract_id = :c AND employee_id = :e AND status = 'assigned'", c=i["contract_id"], e=r):
            return f"재배정 대상 {r}는 이미 좌석이 있습니다"
    return None


def _project_blocker(p: ActionPlan) -> str | None:
    """모델 변경·예산 조정 실행 직전 재검증: 프로젝트가 승인 때 본 값 그대로인지, 바꿀 모델이 승인됐는지."""
    for i in p.items if p.type in (PlanType.model_change, PlanType.budget_review) else []:
        prj = db.one("SELECT model_id, environment, monthly_budget_krw FROM projects WHERE id = :id", id=i["project_id"])
        if not prj:
            return f"프로젝트 없음: {i['project_id']}"
        if p.type == PlanType.budget_review:
            if i.get("proposed_budget_krw") is None:
                return "제안 예산(proposed_budget_krw)이 없어 반영할 값이 없습니다"
            if prj["monthly_budget_krw"] != i["current_budget_krw"]:
                return f"{i['project_id']} 예산이 {prj['monthly_budget_krw']:,}원으로 바뀌었습니다"
            continue
        if prj["model_id"] != i["from_model_id"]:
            return f"{i['project_id']} 모델이 이미 {prj['model_id']}입니다"
        if prj["environment"] != i.get("environment", "test"):
            return f"{i['project_id']}는 {prj['environment']} 환경입니다. 요청 환경({i.get('environment', 'test')})과 다릅니다"
        to = db.one("SELECT approved FROM models WHERE id = :id", id=i["to_model_id"])
        if not to or not to["approved"]:
            return f"{i['to_model_id']}는 승인된 모델이 아닙니다"
    return None


def _exec_response(p: ActionPlan) -> PlanExecuteResponse:
    sv = db.rows("SELECT id, plan_id, kind, amount_krw, basis FROM savings_ledger WHERE plan_id = :id ORDER BY id", id=p.id)
    return PlanExecuteResponse(plan=p, result=p.result, verified={"items": p.items, "state": "applied"}, savings=sv)


def _execute(p: ActionPlan, idempotency_key: str, user: Employee) -> PlanExecuteResponse:
    """실행기. 1) 멱등 키 2) 승인 상태 3) 대상 재검증 4) 좌석 반영 5) 절감 기록. 한 트랜잭션."""
    if p.status == PlanStatus.executed:
        if db.one("SELECT execution_key FROM action_plans WHERE id = :id", id=p.id)["execution_key"] == idempotency_key:  # 같은 키면 첫 결과 그대로
            return _exec_response(p)
        raise ApiError(ErrorCode.conflict, "이미 다른 실행 키로 실행된 조치안입니다", {"plan_id": p.id})
    if p.status != PlanStatus.approved:
        raise ApiError(ErrorCode.forbidden, f"승인 상태가 아님: {p.status.value}")
    if p.risk_level == RiskLevel.high:
        raise ApiError(ErrorCode.forbidden, "높은 위험도 조치는 MVP에서 실행하지 않습니다 (요청만 생성)")
    why = "승인 뒤 대상 상태가 바뀌었습니다" if any(i.get("_stale") for i in p.items) else _seat_blocker(p) or _project_blocker(p)
    if why:  # 재검증 실패: 조치안을 failed로 남기고 멈춘다 (좌석·절감 기록은 건드리지 않음)
        with db.tx() as c:
            c.execute(db.text("UPDATE action_plans SET status = 'failed', result = CAST(:r AS jsonb) WHERE id = :id AND status = 'approved'"),
                      dict(r=db.js({"ok": False, "reason": why}), id=p.id))
            _audit(c, user, "plan.fail", p.id, {"status": "approved"}, {"status": "failed", "reason": why})
        raise ApiError(ErrorCode.stale, f"실행 전 재검증 실패: {why}", {"plan_id": p.id})
    result = {"vendor": "mock", "ok": True, "items": p.items}
    with db.tx() as c:
        n = c.execute(db.text("UPDATE action_plans SET status = 'executed', executed_at = now(), execution_key = :k, result = CAST(:r AS jsonb)"
                              " WHERE id = :id AND status = 'approved'"), dict(k=idempotency_key, r=db.js(result), id=p.id)).rowcount
        if n == 0:  # 그 사이 다른 요청이 실행했다
            raise ApiError(ErrorCode.stale, "실행 직전에 상태가 바뀌었습니다", {"plan_id": p.id})
        for i in p.items if p.type in SEAT_SQL else []:
            c.execute(db.text(SEAT_SQL[p.type]), dict(sid=_new_id("seat"), c=i["contract_id"], e=i["employee_id"]))
            if i.get("reassign_to_employee_id"):  # 회수한 좌석을 바로 다른 사람에게
                c.execute(db.text(SEAT_SQL[PlanType.seat_assign]), dict(sid=_new_id("seat"), c=i["contract_id"], e=i["reassign_to_employee_id"]))
        for i in p.items if p.type == PlanType.model_change else []:
            c.execute(db.text("UPDATE projects SET model_id = :to WHERE id = :p AND model_id = :fr"), dict(to=i["to_model_id"], p=i["project_id"], fr=i["from_model_id"]))
        for i in p.items if p.type == PlanType.budget_review else []:
            c.execute(db.text("UPDATE projects SET monthly_budget_krw = :to WHERE id = :p AND monthly_budget_krw = :fr"),
                      dict(to=i["proposed_budget_krw"], p=i["project_id"], fr=i["current_budget_krw"]))
        if p.type == PlanType.seat_assign:
            c.execute(db.text("INSERT INTO savings_ledger (id, plan_id, kind, amount_krw, basis) VALUES (:id, :p, 'purchase_avoided', 90000,"
                              " '미배정 좌석 배정으로 추가 구매 회피 (월 45,000원 × 남은 2개월)')"), dict(id=_new_id("sav"), p=p.id))
        _audit(c, user, "plan.execute", p.id, {"status": "approved"}, {"status": "executed", "key": idempotency_key})
    return _exec_response(_plan(p.id))


@app.post("/plans/{plan_id}/execute", response_model=Envelope[PlanExecuteResponse], tags=["plans"])
def execute_plan(plan_id: str, idempotency_key: str = Header(alias="Idempotency-Key"), user: Employee = Depends(require(Role.admin, Role.system))):
    p = _plan(plan_id)
    if not p:
        raise ApiError(ErrorCode.not_found, "조치안 없음")
    return ok(_execute(p, idempotency_key, user))


def _add_business_days(start: datetime, days: int) -> datetime:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


CFM_COLS = "id, plan_id, employee_id, question, answer, due_at, answered_at"


@app.get("/confirmations", response_model=Envelope[list[Confirmation]], tags=["F4"])
def list_confirmations(answered: bool | None = Query(None, description="false면 아직 응답 없는 요청만"), user: Employee = Depends(require(Role.admin, Role.system))):
    """사용 의사 확인 요청 목록. Slack 봇 워커가 answered=false로 읽어 직원에게 DM을 보낸다."""
    rs = db.rows(f"SELECT {CFM_COLS} FROM confirmations WHERE (CAST(:a AS boolean) IS NULL OR (answer IS NOT NULL) = CAST(:a AS boolean)) ORDER BY due_at", a=answered)
    return ok([Confirmation(**r) for r in rs])


@app.post("/confirmations", response_model=Envelope[Confirmation], status_code=201, tags=["F4"])
def create_confirmation(body: ConfirmationCreate, user: Employee = Depends(require(Role.admin, Role.system))):
    if not get_employee(body.employee_id):
        raise ApiError(ErrorCode.not_found, "직원 없음")
    if not _plan(body.plan_id):
        raise ApiError(ErrorCode.not_found, "조치안 없음")
    # 같은 조치안·직원에 이미 보낸 확인 요청이 있으면 그것을 돌려준다 (UNIQUE plan_id, employee_id)
    r = db.one(f"SELECT {CFM_COLS} FROM confirmations WHERE plan_id = :p AND employee_id = :e", p=body.plan_id, e=body.employee_id)
    if not r:
        with db.tx() as c:
            r = db.one(f"INSERT INTO confirmations (id, plan_id, employee_id, question, due_at) VALUES (:id, :p, :e, :q, :due) RETURNING {CFM_COLS}", c,
                       id=_new_id("cfm"), p=body.plan_id, e=body.employee_id, q=body.question, due=_add_business_days(_now(), 5))
    return ok(Confirmation(**r))


@app.post("/confirmations/{confirmation_id}/answer", response_model=Envelope[Confirmation], tags=["F4"])
def answer_confirmation(confirmation_id: str, body: ConfirmationAnswerRequest, user: Employee = Depends(require(Role.employee))):
    r = db.one(f"SELECT {CFM_COLS} FROM confirmations WHERE id = :id", id=confirmation_id)
    if not r:
        raise ApiError(ErrorCode.not_found, "확인 요청 없음")
    if r["employee_id"] != user.id:
        raise ApiError(ErrorCode.forbidden, "본인에게 온 확인 요청만 답할 수 있습니다")
    with db.tx() as c:
        r = db.one(f"UPDATE confirmations SET answer = :a, answered_at = now() WHERE id = :id RETURNING {CFM_COLS}", c, a=body.answer.value, id=confirmation_id)
    return ok(Confirmation(**r))


# ---------- savings / audit / jobs ----------
@app.get("/savings", response_model=Envelope[SavingsSummary], tags=["F4"])
def savings(user: Employee = Depends(require(Role.admin))):
    by = {r["kind"]: r["total"] for r in db.rows("SELECT kind, sum(amount_krw)::int AS total FROM savings_ledger GROUP BY kind")}
    return ok(SavingsSummary(by_kind=by, total_krw=sum(by.values())))


@app.get("/audit-log", response_model=Envelope[Page[AuditEntry]], tags=["audit"])
def audit_log(limit: int = 50, offset: int = 0, user: Employee = Depends(require(Role.admin))):
    rs = db.rows("SELECT id, actor_id, actor_role, action, target, before, after, created_at FROM audit_log ORDER BY created_at DESC LIMIT :l OFFSET :o", l=limit, o=offset)
    total = db.one("SELECT count(*) AS n FROM audit_log")["n"]
    return ok(Page(items=[AuditEntry(**r) for r in rs], total=total, limit=limit, offset=offset))


JOBS = {"collect_release_notes", "collect_usage", "check_thresholds", "aggregate_unmet_needs", "send_suggestions"}
WEEKLY_CAP = 2  # 알림 정책: 팀당 주 2건


def _job_check_thresholds(now: datetime) -> dict:
    """프로젝트별 마지막 날 비용이 직전 14일 중앙값의 3배를 넘으면 경보. 같은 날 같은 경보는 한 번만."""
    made = []
    for p in db.rows("SELECT id, monthly_budget_krw FROM projects ORDER BY id"):
        days = db.rows("SELECT date_trunc('day', period_start) AS d, sum(cost_krw)::float AS krw, sum(cost_usd)::float AS usd FROM usage_records"
                       " WHERE project_id = :p AND period_start <= :n GROUP BY 1 ORDER BY 1 DESC LIMIT 15", p=p["id"], n=now)
        if len(days) < 8:
            continue
        prior = sorted(d["krw"] for d in days[1:])
        base = prior[len(prior) // 2]
        last = days[0]
        if base and last["krw"] / base > 3:
            with db.tx() as c:
                dup = db.one("SELECT id FROM anomalies WHERE scope_id = :p AND rule = 'daily_cost_gt_3x_baseline' AND created_at >= :d", c, p=p["id"], d=last["d"])
                if not dup:
                    aid = _new_id("anm")
                    c.execute(db.text("INSERT INTO anomalies (id, scope_type, scope_id, rule, baseline, observed, ratio, projected_monthly_krw, projected_monthly_usd, created_at)"
                                      " VALUES (:id, 'project', :p, 'daily_cost_gt_3x_baseline', :b, :o, :r, :pk, :pu, :n)"),
                              dict(id=aid, p=p["id"], b=base, o=last["krw"], r=round(last["krw"] / base, 2), pk=round(last["krw"] * 30), pu=round(last["usd"] * 30, 4), n=now))
                    made.append(aid)
    return {"new_anomalies": made}


def _in_business_hours(now: datetime) -> bool:
    k = now + KST
    return k.weekday() < 5 and 9 <= k.hour < 18


def _job_send_suggestions(now: datetime) -> dict:
    """알림 정책을 코드로 검사하고 통과한 제안을 발송함(notify_outbox)에 넣는다. Slack 연결 전에는 발송함이 곧 결과다."""
    if not _in_business_hours(now):
        return {"sent": [], "deferred": "업무 시간(평일 09~18시 KST) 밖이라 다음 업무 시간으로 미룸"}
    week_ago = now - timedelta(days=7)
    sent, held = [], []
    rs = db.rows("SELECT g.id, g.update_id, g.body, coalesce(g.target_dept_id, e.dept_id) AS dept_id, g.target_employee_id,"
                 " d.suggestions_enabled, d.slack_channel_id, e.slack_user_id FROM suggestions g"
                 " LEFT JOIN employees e ON e.id = g.target_employee_id"
                 " JOIN departments d ON d.id = coalesce(g.target_dept_id, e.dept_id)"
                 " WHERE g.status IN ('draft', 'pending_approval') ORDER BY g.created_at")
    with db.tx() as c:
        for g in rs:
            reason = None
            hold = db.one("SELECT hold_until FROM suggestion_holds WHERE dept_id = :d AND tool_category = :cat AND hold_until > :n", c,
                          d=g["dept_id"], cat=_tool_category(g["update_id"]), n=now)
            n_week = db.one("SELECT count(*) AS n FROM suggestions x LEFT JOIN employees e ON e.id = x.target_employee_id"
                            " WHERE x.status = 'sent' AND x.sent_at >= :w AND coalesce(x.target_dept_id, e.dept_id) = :d", c, w=week_ago, d=g["dept_id"])["n"]
            if not g["suggestions_enabled"]:
                reason = "팀 자동 발송 꺼짐"
            elif hold:
                reason = f"관련 없음 2회 연속으로 {hold['hold_until']:%Y-%m-%d}까지 보류"
            elif n_week >= WEEKLY_CAP:
                reason = f"팀당 주 {WEEKLY_CAP}건 상한 도달"
            if reason:
                c.execute(db.text("UPDATE suggestions SET status = 'on_hold', hold_reason = :r WHERE id = :id"), dict(r=reason, id=g["id"]))
                held.append({"suggestion_id": g["id"], "dept_id": g["dept_id"], "reason": reason})
                continue
            to = g["slack_user_id"] if g["target_employee_id"] else g["slack_channel_id"]
            c.execute(db.text("INSERT INTO notify_outbox (id, suggestion_id, channel, target, text, created_at) VALUES (:id, :s, 'slack', :t, :x, :n)"),
                      dict(id=_new_id("out"), s=g["id"], t=to, x=g["body"], n=now))
            c.execute(db.text("UPDATE suggestions SET status = 'sent', sent_at = :n WHERE id = :id"), dict(n=now, id=g["id"]))
            sent.append({"suggestion_id": g["id"], "dept_id": g["dept_id"], "to": to})
        by_update: dict = {}
        for x in sent + held:
            by_update.setdefault(db.one("SELECT update_id FROM suggestions WHERE id = :id", c, id=x["suggestion_id"])["update_id"], []).append(x)
        for upd, xs in by_update.items():
            c.execute(db.text("INSERT INTO review_runs (id, update_id, ran_at, sent_dept_ids, held) VALUES (:id, :u, :n, :s, CAST(:h AS jsonb))"),
                      dict(id=_new_id("rr"), u=upd, n=now, s=sorted({x["dept_id"] for x in xs if "to" in x}), h=db.js([x for x in xs if "reason" in x])))
    return {"sent": sent, "held": held}


@app.post("/internal/jobs/{job_name}/run", response_model=Envelope[JobRunResponse], tags=["internal"])
def run_job(job_name: str, now: datetime | None = Query(None, description="시연·테스트용 기준 시각. 비우면 현재"), user: Employee = Depends(require(Role.system, Role.admin))):
    if job_name not in JOBS:
        raise ApiError(ErrorCode.not_found, f"배치 없음. 가능: {sorted(JOBS)}")
    t, now = _now(), now or _now()
    if job_name == "check_thresholds":
        result = _job_check_thresholds(now)
    elif job_name == "send_suggestions":
        result = _job_send_suggestions(now)
    elif job_name == "aggregate_unmet_needs":
        result = {"aggregates": [a.model_dump() for a in _unmet_aggs()]}
    else:  # 수집 배치는 mock_vendor가 생기면 연결한다
        result = {"stub": True, "reason": "mock_vendor 미구현. 시드 데이터로 대신함"}
    with db.tx() as c:
        c.execute(db.text("INSERT INTO job_runs (id, name, queue, status, attempt, idempotency_key, payload, created_at, finished_at)"
                          " VALUES (:id, :n, 'batch', 'done', 1, :k, CAST(:p AS jsonb), :t, now())"),
                  dict(id=_new_id("job"), n=job_name, k=f"{job_name}:{uuid.uuid4().hex[:12]}", p=db.js(result), t=t))
    return ok(JobRunResponse(job=job_name, started_at=t, finished_at=_now(), result=result))


@app.get("/review-runs", tags=["F1"])
def review_runs(user: Employee = Depends(require(Role.admin))):
    """자동 제안 판단 기록 (v2). 응답 모델은 [schema] PR에서 고정한다."""
    return ok(db.rows("SELECT r.id, r.update_id, u.title, r.ran_at, r.sent_dept_ids, r.held FROM review_runs r JOIN vendor_updates u ON u.id = r.update_id ORDER BY r.ran_at DESC"))


@app.get("/notify-outbox", tags=["F1"])
def notify_outbox(user: Employee = Depends(require(Role.admin, Role.system))):
    """발송함. 발송 배치가 넣고, Slack 봇 워커가 읽어 보낸 뒤 delivered_at을 채운다."""
    return ok(db.rows("SELECT id, suggestion_id, channel, target, text, created_at, delivered_at FROM notify_outbox ORDER BY created_at DESC"))


@app.patch("/notify-outbox/{outbox_id}/delivered", tags=["F1"])
def notify_outbox_delivered(outbox_id: str, user: Employee = Depends(require(Role.admin, Role.system))):
    """Slack 봇 워커가 실제로 보낸 뒤 전달 표시. 이미 표시된 건은 그대로 돌려준다 (멱등)."""
    with db.tx() as c:
        r = db.one("UPDATE notify_outbox SET delivered_at = coalesce(delivered_at, now()) WHERE id = :id"
                   " RETURNING id, suggestion_id, channel, target, text, created_at, delivered_at", c, id=outbox_id)
    if not r:
        raise ApiError(ErrorCode.not_found, "발송함 항목 없음")
    return ok(r)


# ---------- AI 작업 중계 (docs/아키텍처.md 5-3) ----------
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8002")
AGENT_TRANSPORT: httpx.AsyncBaseTransport | None = None  # 테스트가 가짜 에이전트를 끼운다


async def _relay(request: Request) -> StreamingResponse:
    """/ai/tasks* 를 에이전트로 그대로 넘긴다(SSE 포함). 화면은 에이전트 주소를 모른다. 계약은 specs/openapi.agent.json."""
    client = httpx.AsyncClient(base_url=AGENT_URL, transport=AGENT_TRANSPORT, timeout=httpx.Timeout(10, read=60))  # SSE keep-alive는 15초마다
    headers = {k: v for k in ("authorization", "content-type", "x-trace-id") if (v := request.headers.get(k))}
    req = client.build_request(request.method, request.url.path, params=request.query_params, headers=headers, content=await request.body())
    try:
        r = await client.send(req, stream=True)
    except httpx.TransportError as e:
        await client.aclose()
        raise ApiError(ErrorCode.agent_unavailable, f"AI 비서 서버에 연결할 수 없습니다 ({type(e).__name__})")

    async def close():
        await r.aclose()
        await client.aclose()

    passthrough = {k: v for k in ("cache-control", "x-accel-buffering") if (v := r.headers.get(k))}
    return StreamingResponse(r.aiter_bytes(), r.status_code, passthrough, r.headers.get("content-type"), BackgroundTask(close))


for _method, _path in [("POST", "/ai/tasks"), ("GET", "/ai/tasks"), ("GET", "/ai/tasks/{task_id}"),
                       ("GET", "/ai/tasks/{task_id}/stream"), ("POST", "/ai/tasks/{task_id}/approval")]:
    app.add_api_route(_path, _relay, methods=[_method], dependencies=[Depends(current_user)], tags=["ai"])
