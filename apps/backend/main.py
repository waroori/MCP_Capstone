"""백엔드 스텁. 8절 엔드포인트 전부가 고정 데이터로 동작한다.

B(백엔드)는 이 파일의 라우트 시그니처와 response_model을 유지한 채 내부를 DB 로직으로 바꾼다.
모든 라우트가 PostgreSQL(app 스키마)을 쓴다. fixtures.py는 시드(db/seed/seed.py)의 원본으로만 남는다.
시그니처·모델을 바꿔야 하면 packages/schemas를 먼저 고치고 PR로 리드 리뷰를 받는다.
"""

from __future__ import annotations

import csv
import io
import os
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from apps.backend import db
from apps.backend.auth import APP_ENV, authenticate, current_user, get_employee, hash_password, issue_token, require
from apps.backend.fixtures import MOCK_RELEASES
from apps.backend.fixtures import MODELS as MODEL_CATALOG  # 공식 단가표. 시드와 같은 값을 모델 고르기 목록으로 쓴다
from apps.backend.errors import ApiError, install_error_handlers, ok
from schemas.common import (
    Evidence,
    RISK_BY_PLAN_TYPE,
    BreakdownBy,
    Envelope,
    ErrorCode,
    ErrorEnvelope,
    Granularity,
    LicenseFlag,
    Money,
    PaceLevel,
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
    OpinionAsset,
    OpinionRequester,
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
    BudgetSet,
    BudgetView,
    ContractCreate,
    DepartmentCreate,
    DepartmentUpdate,
    EmployeeCreate,
    ModelApproval,
    SeatImport,
    SeatImportResult,
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
    JobSummary,
    RelevantTaskCandidate,
    SeatPace,
    SeatPaceDetail,
    TeamSeat,
    TeamUsage,
    Desk,
    DeskAssign,
    DeskCreate,
    KeyReveal,
    RelevantTasksResponse,
    SavingsSummary,
    SuggestionCreate,
    TaskCostEstimateRequest,
    TaskCostEstimateResponse,
    TaskCreate,
    TaskUpdate,
    TeamAdoption,
    UnlistedProductRequest,
    UsageResponse,
    SEAT_SHORTAGE_REASON,
    UNLISTED_REASON,
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
        # 배치·Slack 봇이 쓰는 시스템 계정(SYSTEM_TOKEN의 주인). 비밀번호가 없어 로그인할 수 없다
        c.execute(db.text("INSERT INTO employees (id, name, email, dept_id, role, job_title, status) VALUES ('sys', 'system', :e, :d, 'system', 'batch', 'active')"
                          " ON CONFLICT DO NOTHING"), dict(e="system@" + body.email.strip().lower().split("@")[1], d=dept))
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


MODEL_COLS = ("id, vendor, name, capabilities, input_usd_per_mtok::float AS input_usd_per_mtok,"
              " output_usd_per_mtok::float AS output_usd_per_mtok, approved, allowed_data_class")


def _catalog() -> dict[str, Model]:
    """고를 수 있는 모델 = 공식 단가표(MODEL_CATALOG) + 수집 배치가 새로 감지해 models에 넣은 모델(approved=false로 들어온다)."""
    db_models = {r["id"]: Model(**r) for r in db.rows(f"SELECT {MODEL_COLS} FROM models")}
    out = {mid: m.model_copy(update={"approved": mid in db_models and db_models[mid].approved}) for mid, m in MODEL_CATALOG.items()}
    return out | {mid: m for mid, m in db_models.items() if mid not in out}


@app.get("/setup/model-catalog", response_model=Envelope[list[Model]], tags=["setup"])
def model_catalog(user: Employee = Depends(require(Role.admin))):
    return ok(list(_catalog().values()))


@app.get("/departments", response_model=Envelope[list[Department]], tags=["setup"])
def departments(user: Employee = Depends(require(Role.admin, Role.system))):
    return ok([Department(**r) for r in db.rows("SELECT id, name, parent_id, slack_channel_id, coalesce(suggestions_enabled, true) AS suggestions_enabled FROM departments ORDER BY name")])


@app.post("/departments", response_model=Envelope[Department], status_code=201, tags=["setup"])
def create_department(body: DepartmentCreate, user: Employee = Depends(require(Role.admin))):
    if db.one("SELECT 1 AS x FROM departments WHERE name = :n", n=body.name.strip()):
        raise ApiError(ErrorCode.conflict, f"같은 이름의 부서가 있습니다: {body.name}")
    d = Department(id=_new_id("dept"), name=body.name.strip())
    with db.tx() as c:
        c.execute(db.text("INSERT INTO departments (id, name) VALUES (:id, :n)"), dict(id=d.id, n=d.name))
        _audit(c, user, "setup.department", d.id, None, d.model_dump())
    return ok(d)


@app.patch("/departments/{dept_id}", response_model=Envelope[Department], tags=["setup"])
def update_department(dept_id: str, body: DepartmentUpdate, user: Employee = Depends(require(Role.admin))):
    """팀별 자동 발송 설정. 끄면 send_suggestions 배치가 그 팀 제안을 발송함에 넣지 않는다(기록만). 설정 변경은 audit_log에 남긴다."""
    cols = "id, name, parent_id, slack_channel_id, coalesce(suggestions_enabled, true) AS suggestions_enabled"
    before = db.one(f"SELECT {cols} FROM departments WHERE id = :id", id=dept_id)
    if not before:
        raise ApiError(ErrorCode.not_found, "부서 없음")
    sets, params = [], {"id": dept_id}
    if body.suggestions_enabled is not None:
        sets.append("suggestions_enabled = :en"); params["en"] = body.suggestions_enabled
    if body.slack_channel_id is not None:
        sets.append("slack_channel_id = :ch"); params["ch"] = body.slack_channel_id.strip() or None
    if not sets:
        return ok(Department(**before))
    with db.tx() as c:
        c.execute(db.text(f"UPDATE departments SET {', '.join(sets)} WHERE id = :id"), params)
        after = db.one(f"SELECT {cols} FROM departments WHERE id = :id", c, id=dept_id)
        _audit(c, user, "setup.department_update", dept_id, {k: before[k] for k in ("slack_channel_id", "suggestions_enabled")},
               {k: after[k] for k in ("slack_channel_id", "suggestions_enabled")})
    return ok(Department(**after))


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
    slacks = [e.slack_user_id for e in body if e.slack_user_id]
    if dup := {x for x in slacks if slacks.count(x) > 1} | {r["slack_user_id"] for r in db.rows("SELECT slack_user_id FROM employees WHERE slack_user_id = ANY(:s)", s=slacks)}:
        raise ApiError(ErrorCode.conflict, f"이미 다른 직원에 연결된 Slack 회원 ID: {', '.join(sorted(dup))}")
    ids = [_new_id("emp") for _ in body]
    with db.tx() as c:
        for i, e in zip(ids, body):
            c.execute(db.text("INSERT INTO employees (id, name, email, dept_id, role, job_title, status, hire_date, slack_user_id) VALUES (:id, :n, :e, :d, :r, :t, 'active', :h, :s)"),
                      dict(id=i, n=e.name.strip(), e=e.email.strip().lower(), d=e.dept_id, r=e.role.value, t=e.job_title.strip(), h=e.hire_date, s=e.slack_user_id))
        _audit(c, user, "setup.employees", "employees", None, {"added": ids})
    return ok([Employee(**r) for r in db.rows(f"SELECT {EMP_COLS} FROM employees e JOIN departments d ON d.id = e.dept_id WHERE e.id = ANY(:ids) ORDER BY e.name", ids=ids)])


# ---------- 책상 (좌석 배치도, F4 신입 배치) ----------
DESK_COLS = "id, floor, dept_id, seq, employee_id"


@app.get("/desks", response_model=Envelope[list[Desk]], tags=["F4"])
def desks(user: Employee = Depends(require(Role.admin, Role.system))):
    return ok([Desk(**r) for r in db.rows(f"SELECT {DESK_COLS} FROM desks ORDER BY floor DESC, dept_id, seq")])


@app.put("/desks/{desk_id}", response_model=Envelope[Desk], tags=["F4"])
def assign_desk(desk_id: str, body: DeskAssign, user: Employee = Depends(require(Role.admin))):
    """직원을 앉힌다. 다른 자리에 있던 사람이면 옮긴다(예전 자리는 빈다). employee_id가 null이면 자리를 비운다."""
    with db.tx() as c:
        d = db.one(f"SELECT {DESK_COLS} FROM desks WHERE id = :id FOR UPDATE", c, id=desk_id)
        if not d:
            raise ApiError(ErrorCode.not_found, f"책상 없음: {desk_id}")
        e = body.employee_id
        if e:
            emp = db.one("SELECT name, role, status FROM employees WHERE id = :e", c, e=e)
            if not emp or emp["role"] == "system":
                raise ApiError(ErrorCode.not_found, f"직원 없음: {e}")
            if emp["status"] == "resigned":
                raise ApiError(ErrorCode.invalid, f"{emp['name']} 님은 퇴사 처리돼 자리를 줄 수 없습니다")
            if d["employee_id"] and d["employee_id"] != e:
                who = db.one("SELECT name FROM employees WHERE id = :e", c, e=d["employee_id"])["name"]
                raise ApiError(ErrorCode.conflict, f"{desk_id}에는 {who} 님이 앉아 있습니다. 먼저 옮기거나 비워 주세요")
            c.execute(db.text("UPDATE desks SET employee_id = NULL WHERE employee_id = :e AND id <> :id"), dict(e=e, id=desk_id))
        c.execute(db.text("UPDATE desks SET employee_id = :e WHERE id = :id"), dict(e=e, id=desk_id))
        _audit(c, user, "desk.assign", desk_id, {"employee_id": d["employee_id"]}, {"employee_id": e})
    return ok(Desk(**db.one(f"SELECT {DESK_COLS} FROM desks WHERE id = :id", id=desk_id)))


@app.post("/desks", response_model=Envelope[list[Desk]], status_code=201, tags=["F4"])
def add_desks(body: DeskCreate, user: Employee = Depends(require(Role.admin))):
    """부서 구역 끝에 책상을 붙인다. 그 층에 그 부서 구역이 없으면 새 구역(새 층)이 된다."""
    if not db.one("SELECT 1 AS x FROM departments WHERE id = :d", d=body.dept_id):
        raise ApiError(ErrorCode.invalid, f"없는 부서: {body.dept_id}")
    with db.tx() as c:
        last = db.one("SELECT coalesce(max(seq), 0) AS n FROM desks WHERE floor = :f AND dept_id = :d", c, f=body.floor, d=body.dept_id)["n"]
        ids = [f"{body.floor}-{body.dept_id.removeprefix('dept_')}-{last + k + 1:02d}" for k in range(body.count)]
        for k, i in enumerate(ids):
            c.execute(db.text("INSERT INTO desks (id, floor, dept_id, seq) VALUES (:id, :f, :d, :s)"), dict(id=i, f=body.floor, d=body.dept_id, s=last + k + 1))
        _audit(c, user, "desk.add", body.dept_id, None, {"floor": body.floor, "added": ids})
    return ok([Desk(**r) for r in db.rows(f"SELECT {DESK_COLS} FROM desks WHERE id = ANY(:ids) ORDER BY seq", ids=ids)])


@app.delete("/desks/{desk_id}", response_model=Envelope[Desk], tags=["F4"])
def remove_desk(desk_id: str, user: Employee = Depends(require(Role.admin))):
    """빈 책상 하나를 없앤다. 앉은 사람이 있으면 먼저 옮기거나 비워야 한다."""
    with db.tx() as c:
        d = db.one(f"SELECT {DESK_COLS} FROM desks WHERE id = :id FOR UPDATE", c, id=desk_id)
        if not d:
            raise ApiError(ErrorCode.not_found, f"책상 없음: {desk_id}")
        if d["employee_id"]:
            who = db.one("SELECT name FROM employees WHERE id = :e", c, e=d["employee_id"])["name"]
            raise ApiError(ErrorCode.conflict, f"{desk_id}에는 {who} 님이 앉아 있습니다. 먼저 옮기거나 비워 주세요")
        c.execute(db.text("DELETE FROM desks WHERE id = :id"), dict(id=desk_id))
        _audit(c, user, "desk.remove", desk_id, d, None)
    return ok(Desk(**d))


@app.delete("/desks", response_model=Envelope[list[Desk]], tags=["F4"])
def remove_desks(floor: str, dept_id: str | None = None, user: Employee = Depends(require(Role.admin))):
    """구역(층+부서) 또는 층 전체의 책상을 없앤다. 한 자리라도 사람이 앉아 있으면 아무것도 지우지 않는다."""
    with db.tx() as c:
        ds = db.rows(f"SELECT {DESK_COLS} FROM desks WHERE floor = :f AND (CAST(:d AS varchar) IS NULL OR dept_id = :d) ORDER BY dept_id, seq FOR UPDATE",
                     c, f=floor, d=dept_id)
        if not ds:
            raise ApiError(ErrorCode.not_found, f"책상 없음: {floor} {dept_id or ''}".strip())
        seated = [r["employee_id"] for r in ds if r["employee_id"]]
        if seated:
            names = [r["name"] for r in db.rows("SELECT name FROM employees WHERE id = ANY(:ids) ORDER BY name", c, ids=seated)]
            more = f" 외 {len(names) - 3}명" if len(names) > 3 else ""
            raise ApiError(ErrorCode.conflict, f"앉아 있는 직원 {len(names)}명({', '.join(names[:3])}{more})을 먼저 옮기거나 비워 주세요")
        c.execute(db.text("DELETE FROM desks WHERE id = ANY(:ids)"), dict(ids=[r["id"] for r in ds]))
        _audit(c, user, "desk.remove", dept_id or floor, {"floor": floor, "removed": [r["id"] for r in ds]}, None)
    return ok([Desk(**r) for r in ds])


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
        c.execute(db.text("INSERT INTO contracts (id, tool_id, plan_name, billing_type, seats_purchased, seat_price_krw, term_start, term_end, daily_token_quota)"
                          " VALUES (:id, :t, :p, CAST(:b AS billing_type), :n, :pr, :s, :e, :q)"),
                  dict(id=ctr.id, t=tool, p=ctr.plan_name, b=ctr.billing_type.value, n=ctr.seats_purchased, pr=ctr.seat_price_krw, s=body.term_start, e=body.term_end,
                       q=0 if body.billing_type == "api_usage" else body.daily_token_quota))
        _audit(c, user, "setup.contract", ctr.id, None, ctr.model_dump(mode="json"))
    return ok(ctr)


UNUSED_DAYS = 30  # ponytail: 장기 미사용 기준 하나. 제품·부서별로 다르게 하려면 계약에 기준일 칸을 둔다


@app.post("/contracts/{contract_id}/seats/import", response_model=Envelope[SeatImportResult], status_code=201, tags=["setup"])
def import_seats(contract_id: str, body: SeatImport, user: Employee = Depends(require(Role.admin))):
    """이미 쓰던 회사의 좌석 배정을 CSV로 넣는다. 한 줄이라도 틀리면 아무것도 넣지 않는다.
    좌석 수를 넘기지 않고, 이미 배정된 사람은 건너뛴다. 마지막 사용일이 없으면 미사용이 아니라 '기록 없음(확인 필요)'."""
    ctr = db.one("SELECT billing_type, seats_purchased FROM contracts WHERE id = :c", c=contract_id)
    if not ctr:
        raise ApiError(ErrorCode.not_found, f"계약 없음: {contract_id}")
    if ctr["billing_type"] == "api_usage":
        raise ApiError(ErrorCode.invalid, "API 사용량 과금 계약에는 좌석이 없습니다")
    rows = [r for r in csv.DictReader(io.StringIO(body.csv.strip().lstrip("\ufeff")))]  # 엑셀 CSV의 BOM
    if not rows or "email" not in rows[0]:
        raise ApiError(ErrorCode.invalid, "첫 줄은 머리글 email[,last_activity_at] 이어야 하고 한 줄 이상 있어야 합니다")
    emails = [(r.get("email") or "").strip().lower() for r in rows]
    people = {r["email"]: r for r in db.rows("SELECT id, lower(email) AS email, status FROM employees WHERE lower(email) = ANY(:e) AND role <> 'system'", e=emails)}
    errors = []
    for n, (r, e) in enumerate(zip(rows, emails), start=2):
        last = (r.get("last_activity_at") or "").strip()
        if e not in people:
            errors.append(f"{n}행 {e or '(빈 이메일)'}: 등록된 직원이 아닙니다")
        elif people[e]["status"] == "resigned":
            errors.append(f"{n}행 {e}: 퇴사한 직원입니다")
        elif emails.count(e) > 1:
            errors.append(f"{n}행 {e}: 파일 안에 두 번 있습니다")
        elif last and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", last):
            errors.append(f"{n}행 {e}: last_activity_at은 YYYY-MM-DD 형식이어야 합니다")
    if errors:
        raise ApiError(ErrorCode.invalid, f"CSV에 고칠 줄이 {len(errors)}개 있습니다. 아무것도 넣지 않았습니다", {"errors": errors[:50]})
    with db.tx() as c:
        c.execute(db.text("SELECT 1 FROM contracts WHERE id = :c FOR UPDATE"), dict(c=contract_id))  # 동시 배정과 좌석 수 검사를 잠근다
        have = {r["employee_id"] for r in db.rows("SELECT employee_id FROM seat_assignments WHERE contract_id = :c AND status = 'assigned'", c, c=contract_id)}
        new = [(r, people[e]) for r, e in zip(rows, emails) if people[e]["id"] not in have]
        if len(have) + len(new) > ctr["seats_purchased"]:
            raise ApiError(ErrorCode.conflict, f"구매 좌석 {ctr['seats_purchased']}석을 넘습니다 (이미 배정 {len(have)}석 + 새로 {len(new)}석)")
        for r, p in new:
            last = (r.get("last_activity_at") or "").strip() or None
            stale = last and datetime.fromisoformat(last).date() < _now().date() - timedelta(days=UNUSED_DAYS)
            flag = "on_leave" if p["status"] == "on_leave" else "no_record" if not last else "unused" if stale else None
            c.execute(db.text("INSERT INTO seat_assignments (id, contract_id, employee_id, status, assigned_at, last_activity_at, flag)"
                              " VALUES (:id, :c, :e, 'assigned', now(), CAST(:l AS timestamptz), CAST(:f AS license_flag))"),
                      dict(id=_new_id("seat"), c=contract_id, e=p["id"], l=last, f=flag))
        _audit(c, user, "setup.seats_import", contract_id, {"assigned": len(have)}, {"imported": [p["id"] for _, p in new]})
    skipped = [p["email"] for r, e in zip(rows, emails) if (p := people[e])["id"] in have]
    return ok(SeatImportResult(contract_id=contract_id, imported=len(new), skipped=skipped, assigned=len(have) + len(new), purchased=ctr["seats_purchased"]))


@app.put("/models/approved", response_model=Envelope[list[Model]], tags=["setup"])
def approve_models(body: ModelApproval, user: Employee = Depends(require(Role.admin))):
    """목록에서 고른 모델만 승인. 처음 고른 모델은 models에 넣고, 같은 공급사의 API 계약 제품에 묶는다.
    새로 감지된 API 모델은 단가가 0(미확인)이라 그대로 승인하면 비용 추정이 0원이 된다. 단가(prices)를 같이 받아야 승인한다."""
    catalog = _catalog()
    if bad := sorted(set(body.model_ids) - set(catalog)):
        raise ApiError(ErrorCode.invalid, f"목록에 없는 모델: {', '.join(bad)}")
    api_tools = {r["id"] for r in db.rows("SELECT tool_id AS id FROM contracts WHERE billing_type = 'api_usage'")}
    tool_of = {r["id"]: r["tool_id"] for r in db.rows("SELECT id, tool_id FROM models")}
    unpriced = sorted(mid for mid in body.model_ids if mid not in MODEL_CATALOG and tool_of.get(mid) in api_tools
                      and not catalog[mid].input_usd_per_mtok and mid not in body.prices)
    if unpriced:
        raise ApiError(ErrorCode.invalid, f"단가를 모르는 API 모델이라 입력·출력 단가를 넣어야 승인할 수 있습니다: {', '.join(unpriced)}", {"unpriced": unpriced})
    before = [r["id"] for r in db.rows("SELECT id FROM models WHERE approved ORDER BY id")]
    with db.tx() as c:
        for mid, price in body.prices.items():
            if mid in body.model_ids and mid not in MODEL_CATALOG:  # 공식 단가표에 있는 모델 값은 바꾸지 않는다
                c.execute(db.text("UPDATE models SET input_usd_per_mtok = :i, output_usd_per_mtok = :o WHERE id = :id"),
                          dict(i=price.input_usd_per_mtok, o=price.output_usd_per_mtok, id=mid))
        for mid in body.model_ids:
            if mid not in MODEL_CATALOG:
                continue  # 감지된 모델은 이미 models에 있다
            m = MODEL_CATALOG[mid]
            tool = db.one("SELECT t.id FROM tools t JOIN contracts k ON k.tool_id = t.id WHERE lower(t.vendor) = lower(:v)"
                          " ORDER BY (k.billing_type = 'api_usage') DESC, t.id LIMIT 1", c, v=m.vendor)  # API 계약 우선, 없으면 좌석 제품(ChatGPT 좌석 모델)
            c.execute(db.text("INSERT INTO models (id, tool_id, vendor, name, capabilities, input_usd_per_mtok, output_usd_per_mtok, approved, allowed_data_class)"
                              " VALUES (:id, :t, :v, :n, :cap, :i, :o, true, CAST(:dc AS data_class)) ON CONFLICT (id) DO NOTHING"),
                      dict(id=m.id, t=tool and tool["id"], v=m.vendor, n=m.name, cap=m.capabilities, i=m.input_usd_per_mtok, o=m.output_usd_per_mtok, dc=m.allowed_data_class.value))
        c.execute(db.text("UPDATE models SET approved = (id = ANY(:ids))"), dict(ids=body.model_ids))
        _audit(c, user, "setup.models", "models", {"approved": before}, {"approved": sorted(body.model_ids), "prices": {k: v.model_dump() for k, v in body.prices.items()}})
    return ok([Model(**r) for r in db.rows(f"SELECT {MODEL_COLS} FROM models ORDER BY id")])


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
FX = 1380.0  # 원/달러 고정 환율. usage_records.cost_krw도 이 값으로 환산해 넣는다
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
    by_job: dict[str, list[str]] = {}
    for r in rs:
        if not job_filter or r["job_title"] in job_filter:
            by_job.setdefault(r["job_title"], []).append(r["title"])
    jobs = [JobSummary(job_title=j, task_count=len(ts), task_titles=ts[:3]) for j, ts in sorted(by_job.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    return ok(RelevantTasksResponse(update=upd, candidates=cands, jobs=jobs))


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
    rs = db.rows("SELECT r.id, r.task_type, r.model_id, r.quality_score, r.threshold, r.passed, r.avg_input_tokens, r.avg_output_tokens, r.latency_ms, r.evaluated_at,"
                 " r.passed AND coalesce(m.approved, false) AS eligible FROM eval_results r LEFT JOIN models m ON m.id = r.model_id"
                 " WHERE r.task_type = :t ORDER BY r.id", t=task_type)  # 추천 후보 = 품질 기준 통과 + 사내 승인 (코드 판정)
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


ANM_COLS = ("a.id, a.scope_type, a.scope_id, e.name AS scope_name, a.rule, a.baseline::float AS baseline, a.observed::float AS observed,"
            " a.ratio::float AS ratio, a.status, a.created_at")


def _anomaly(r: dict) -> Anomaly:
    return Anomaly(**r)


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
    rs = db.rows(f"SELECT {ANM_COLS} FROM anomalies a LEFT JOIN employees e ON e.id = a.scope_id"
                 " WHERE CAST(:s AS varchar) IS NULL OR a.status::text = :s ORDER BY a.created_at DESC", s=status)
    return ok([_anomaly(r) for r in rs])


# ---------- F3 좌석별 토큰 소비 속도 ----------
WORKDAY = (9, 18)  # KST. 예상 = 오늘 사용 ÷ 업무 시간 경과 비율 (ponytail: 전사 고정. 교대·야근 팀이 생기면 부서별 설정으로)
PACE_OVER, PACE_CAUTION = 2.0, 1.5
PACE_MIN_SHARE = 0.25  # 아침 첫 몇 건이 크게 부풀지 않게: 오늘 실제 사용이 평균의 25% 미만이면 경보 대상이 아니다


def _day_start(now: datetime) -> datetime:
    k = now + KST
    return datetime(k.year, k.month, k.day, tzinfo=UTC) - KST


def _workday_fraction(now: datetime) -> float:
    k = now + KST
    return min(1.0, max(1 / (WORKDAY[1] - WORKDAY[0]), (k.hour + k.minute / 60 - WORKDAY[0]) / (WORKDAY[1] - WORKDAY[0])))


def _seat_paces(now: datetime, employee_id: str | None = None) -> list[SeatPace]:
    """사용량을 받는 좌석(contracts.daily_token_quota > 0)마다 오늘 예상량을 같은 제품 좌석 평균과 비교한다."""
    d0 = _day_start(now)
    rs = db.rows(
        "WITH hist AS (SELECT contract_id, employee_id, date_trunc('day', period_start + interval '9 hours') AS d, sum(input_tokens + output_tokens) AS t"
        "              FROM usage_records WHERE employee_id IS NOT NULL AND period_start >= :h AND period_start < :d0 GROUP BY 1, 2, 3),"
        " peer AS (SELECT contract_id, avg(t)::float AS avg FROM hist GROUP BY 1),"
        " today AS (SELECT contract_id, employee_id, sum(input_tokens + output_tokens)::bigint AS t, max(period_start) AS last"
        "           FROM usage_records WHERE employee_id IS NOT NULL AND period_start >= :d0 AND period_start <= :n GROUP BY 1, 2)"
        " SELECT s.employee_id, e.name AS employee_name, d.name AS dept_name, s.contract_id, t.product, c.daily_token_quota AS quota,"
        "        coalesce(td.t, 0) AS used, td.last, coalesce(pr.avg, 0) AS avg"
        " FROM seat_assignments s JOIN contracts c ON c.id = s.contract_id JOIN tools t ON t.id = c.tool_id"
        " JOIN employees e ON e.id = s.employee_id JOIN departments d ON d.id = e.dept_id"
        " LEFT JOIN today td ON td.contract_id = s.contract_id AND td.employee_id = s.employee_id"
        " LEFT JOIN peer pr ON pr.contract_id = s.contract_id"
        " WHERE s.status = 'assigned' AND c.daily_token_quota > 0 AND (CAST(:e AS varchar) IS NULL OR s.employee_id = :e)",
        h=d0 - timedelta(days=14), d0=d0, n=now, e=employee_id)
    frac, out = _workday_fraction(now), []
    for r in rs:
        projected = round(r["used"] / frac)
        ratio = round(projected / r["avg"], 2) if r["avg"] else 0.0
        enough = r["used"] >= r["avg"] * PACE_MIN_SHARE
        level = PaceLevel.over if ratio >= PACE_OVER and enough else PaceLevel.caution if ratio >= PACE_CAUTION and enough else PaceLevel.normal
        out.append(SeatPace(employee_id=r["employee_id"], employee_name=r["employee_name"], dept_name=r["dept_name"], contract_id=r["contract_id"],
                            product=r["product"], tokens_today=r["used"], projected_today=projected, peer_avg_daily=round(r["avg"]),
                            daily_quota=r["quota"], quota_used_pct=round(r["used"] / r["quota"] * 100, 1),
                            projected_quota_pct=round(projected / r["quota"] * 100, 1), ratio=ratio, level=level, last_activity_at=r["last"]))
    return sorted(out, key=lambda x: -x.ratio)


@app.get("/usage/seats", response_model=Envelope[list[SeatPace]], tags=["F3"])
def seat_paces(user: Employee = Depends(require(Role.admin, Role.system))):
    return ok(_seat_paces(_now()))


@app.get("/usage/teams", response_model=Envelope[list[TeamUsage]], tags=["F3"])
def team_usage(user: Employee = Depends(require(Role.admin, Role.system))):
    """팀별 오늘 사용량. 좌석 토큰은 _seat_paces를 팀으로 합치고, API 비용은 팀 프로젝트의 오늘·이번 달 사용 기록."""
    now = _now()
    d0 = _day_start(now)
    k = now + KST
    m0 = datetime(k.year, k.month, 1, tzinfo=UTC) - KST
    dept_of = {r["id"]: r["dept_id"] for r in db.rows("SELECT id, dept_id FROM employees")}
    paces: dict[str, list[SeatPace]] = {}
    for p in _seat_paces(now):
        paces.setdefault(dept_of[p.employee_id], []).append(p)
    api = {r["dept_id"]: r for r in db.rows(
        "SELECT p.dept_id, coalesce(sum(u.requests) FILTER (WHERE u.period_start >= :d0), 0)::int AS req_today,"
        " coalesce(sum(u.cost_krw) FILTER (WHERE u.period_start >= :d0), 0)::int AS today, coalesce(sum(u.cost_krw), 0)::int AS month,"
        " max(u.period_start) AS last"
        " FROM usage_records u JOIN projects p ON p.id = u.project_id WHERE u.period_start >= :m0 AND u.period_start <= :n GROUP BY 1",
        d0=d0, m0=m0, n=now)}
    out = []
    for d in db.rows("SELECT d.id, d.name, (SELECT count(*) FROM employees e WHERE e.dept_id = d.id AND e.role <> 'system' AND e.status <> 'resigned')::int AS headcount,"
                     " (SELECT coalesce(sum(monthly_budget_krw), 0) FROM projects p WHERE p.dept_id = d.id)::int AS budget FROM departments d ORDER BY d.id"):
        ps, a = paces.get(d["id"], []), api.get(d["id"], {})
        if not d["headcount"] and not ps and not a:
            continue
        projected, avg = sum(p.projected_today for p in ps), sum(p.peer_avg_daily for p in ps)
        lasts = [t for t in [*(p.last_activity_at for p in ps), a.get("last")] if t]
        out.append(TeamUsage(dept_id=d["id"], dept_name=d["name"], headcount=d["headcount"], seats_tracked=len(ps),
                             tokens_today=sum(p.tokens_today for p in ps), projected_today=projected, peer_avg_daily=avg,
                             ratio=round(projected / avg, 2) if avg else 0.0,
                             seats_over=sum(p.level == PaceLevel.over for p in ps), seats_caution=sum(p.level == PaceLevel.caution for p in ps),
                             api_requests_today=a.get("req_today", 0), api_cost_today_krw=a.get("today", 0), api_cost_month_krw=a.get("month", 0),
                             api_budget_month_krw=d["budget"],
                             alert_seats=[TeamSeat(employee_id=p.employee_id, employee_name=p.employee_name, product=p.product, level=p.level, ratio=p.ratio, projected_today=p.projected_today)
                                          for p in sorted(ps, key=lambda p: -p.ratio) if p.level != PaceLevel.normal],
                             last_activity_at=max(lasts) if lasts else None))
    return ok(sorted(out, key=lambda t: (-t.seats_over, -t.ratio, -t.api_cost_today_krw)))


@app.get("/usage/seats/{employee_id}", response_model=Envelope[SeatPaceDetail], tags=["F3"])
def seat_pace_detail(employee_id: str, user: Employee = Depends(require(Role.employee, Role.admin, Role.system))):
    if user.role == Role.employee and employee_id != user.id:
        raise ApiError(ErrorCode.forbidden, "직원은 본인 좌석만 볼 수 있습니다")
    now = _now()
    pace = _seat_paces(now, employee_id)
    if not pace:
        raise ApiError(ErrorCode.not_found, "사용량을 받는 좌석이 없습니다")
    d0 = _day_start(now)

    def rows(key_sql: str) -> list[BreakdownRow]:
        rs = _usage_rows(UsageScope.employee, employee_id, d0, now, key_sql)
        total = sum(r["input_tokens"] + r["output_tokens"] for r in rs) or 1
        return [BreakdownRow(key=str(r["key"]), requests=r["requests"], input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                             cost=Money(krw=r["krw"], usd=round(r["usd"], 4)), share=round((r["input_tokens"] + r["output_tokens"]) / total, 4)) for r in rs]
    return ok(SeatPaceDetail(pace=pace[0], by_hour=rows("to_char(u.period_start + interval '9 hours', 'HH24')"),
                             by_input_length=rows(GROUP_COL[BreakdownBy.input_length]), by_model=rows(GROUP_COL[BreakdownBy.model])))


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
        " GROUP BY c.id, t.product, t.kind ORDER BY t.kind, c.seats_purchased DESC, c.id", cid=contract_id)  # 큰 계약 먼저 (화면 첫 탭)
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
    plan, short = _plan(pid), {}
    if plan.type in SEAT_SQL:  # v2.1: 승인 대기로 들어가기 전에 한 번 검증한다 (중복 배정·휴직자·회수 대상 없음). 실행 직전에 한 번 더 한다
        short = _seat_shortage(plan)
        problem = _seat_blocker(plan, allow_short=True)  # 좌석 부족은 여기서 막지 않고 승인 대기로 둔다 (관리자가 반려)
        if problem:
            with db.tx() as c:
                c.execute(db.text("UPDATE action_plans SET status = 'failed', result = CAST(:r AS jsonb) WHERE id = :id"), dict(r=db.js({"ok": False, "reason": problem}), id=pid))
                _audit(c, user, "plan.reject_invalid", pid, {"status": status.value}, {"status": "failed", "reason": problem})
            raise ApiError(ErrorCode.conflict, problem, {"plan_id": pid})
    # 이용권 배정은 남은 좌석이 있어도 관리자 승인 뒤에 한다 (PRD S4). 좌석이 없으면 승인 대기로 두되 승인은 막고, 추가 구매는 하지 않는다 (S5)
    if short:
        note = [Evidence(source="inventory", ref=cid, value={"unassigned": 0, "shortage": n, "note": "남은 좌석 없음 · 승인 불가, 반려 대상"}) for cid, n in short.items()]
        with db.tx() as c:
            c.execute(db.text("UPDATE action_plans SET evidence = evidence || CAST(:ev AS jsonb) WHERE id = :id"), dict(ev=db.js([e.model_dump() for e in note]), id=pid))
        plan = _plan(pid)
    elif risk == RiskLevel.low:  # 자동 처리 유형(현재 없음)만 즉시 실행
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


def _opinion_facts(p: ActionPlan) -> dict:
    """승인 대기 건 의견의 사실 칸(요청자·자산·변경 내용)은 DB에서 바로 만든다. AI는 권고·요약·위험·대안만 판단한다."""
    item = (p.items or [{}])[0]
    req = get_employee(p.requested_by)
    holder = get_employee(item["employee_id"]) if item.get("employee_id") else req
    out: dict = {}
    if req:
        out["requester"] = OpinionRequester(name=req.name, dept=req.dept_name or "", job_title=req.job_title,
                                            current_products=[t.product for t in _assets(req).available_tools])
    who = [["직원", f"{holder.name} ({holder.dept_name} · {holder.job_title})"]] if holder else []
    if item.get("contract_id"):
        c = db.one("SELECT t.product, c.plan_name, c.seats_purchased, c.seat_price_krw, (SELECT count(*) FROM seat_assignments s"
                   " WHERE s.contract_id = c.id AND s.status = 'assigned') AS assigned FROM contracts c JOIN tools t ON t.id = c.tool_id WHERE c.id = :c", c=item["contract_id"])
        if c:
            out["asset"] = OpinionAsset(product=c["product"], purchased=c["seats_purchased"], assigned=c["assigned"],
                                        unassigned=c["seats_purchased"] - c["assigned"], seat_price_krw=c["seat_price_krw"], note=c["plan_name"])
            sign = "-" if p.type == PlanType.seat_reclaim else ""
            out["changes"] = who + [["제품", f"{c['product']} ({c['plan_name']})"], ["월 비용", f"{sign}{c['seat_price_krw']:,}원"]]
    elif item.get("tool_id"):
        t = db.one("SELECT product FROM tools WHERE id = :t", t=item["tool_id"])
        m = db.one("SELECT name, input_usd_per_mtok::float AS i, output_usd_per_mtok::float AS o FROM models WHERE id = :m", m=item.get("model_id") or "")
        if t:
            out["asset"] = OpinionAsset(product=t["product"], note=f"사용량 과금 · {m['name']} (100만 토큰당 입력 ${m['i']} · 출력 ${m['o']})" if m else "사용량 과금")
            out["changes"] = who + [["제품", t["product"]]] + ([["용도", item["purpose"]]] if item.get("purpose") else [])
    return out


@app.patch("/plans/{plan_id}/opinion", response_model=Envelope[ActionPlan], tags=["plans"])
def attach_opinion(plan_id: str, body: PlanOpinion, user: Employee = Depends(require(Role.system, Role.admin))):
    p = _plan(plan_id)
    if not p:
        raise ApiError(ErrorCode.not_found, "조치안 없음")
    body = body.model_copy(update={**_opinion_facts(p), "generated_at": _now()})
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
    p = _plan(plan_id)
    if p and p.status == PlanStatus.pending_approval and (short := _seat_shortage(p)):  # 승인 직전 좌석 재확인. 추가 구매는 하지 않는다
        raise ApiError(ErrorCode.conflict, "남은 좌석이 없어 승인할 수 없습니다. 반려해 주세요", {"plan_id": plan_id, "shortage": short})
    return ok(_decide(plan_id, body, user, approve=True))


@app.post("/plans/{plan_id}/reject", response_model=Envelope[ActionPlan], tags=["plans"])
def reject_plan(plan_id: str, body: PlanDecision, user: Employee = Depends(require(Role.admin))):
    """PRD S5: 남은 좌석이 없는 이용권 신청은 입력한 사유와 관계없이 '좌석 부족'으로 기록한다(직원 안내문과 같은 사유).
    좌석이 남아 있는 신청에는 '좌석 부족' 사유를 붙이지 못한다."""
    p = _plan(plan_id)
    if p and p.status == PlanStatus.pending_approval and p.type == PlanType.seat_assign:
        if _seat_shortage(p):
            body = PlanDecision(reason=SEAT_SHORTAGE_REASON)
        elif SEAT_SHORTAGE_REASON in body.reason:
            raise ApiError(ErrorCode.conflict, "남은 좌석이 있어 '좌석 부족' 사유로 반려할 수 없습니다", {"plan_id": plan_id})
    return ok(_decide(plan_id, body, user, approve=False))


@app.post("/requests/unlisted", response_model=Envelope[ActionPlan], status_code=201, tags=["plans"])
def request_unlisted(body: UnlistedProductRequest, user: Employee = Depends(require(Role.employee, Role.admin))):
    """PRD S9: 사내 등록 제품 목록에 없는 제품 신청. 신청 이력(purchase_review)에 남기고 시스템이 '사내 미도입'으로 바로 반려한다.
    구매·도입·배정으로 이어지지 않는다. 등록된 제품이면 일반 사용 신청(POST /plans)을 쓰도록 409."""
    name = body.product_name.strip()
    hit = db.one("SELECT product FROM tools WHERE lower(product) = lower(:n) OR lower(:n) LIKE '%' || lower(product) || '%'"
                 " OR lower(product) LIKE '%' || lower(:n) || '%' LIMIT 1", n=name)
    if hit:
        raise ApiError(ErrorCode.conflict, f"{hit['product']}는 사내 등록 제품입니다. 사용 신청으로 진행해 주세요", {"product": hit["product"]})
    pid = _new_id("plan")
    item = {"product_name": name, "purpose": body.purpose.strip() or None}
    with db.tx() as c:
        c.execute(db.text("INSERT INTO action_plans (id, type, risk_level, status, items, rationale, evidence, requested_by, approver_id, reject_reason, created_at)"
                          " VALUES (:id, 'purchase_review', :rk, 'rejected', CAST(:it AS jsonb), :ra, '[]'::jsonb, :by, 'sys', :rs, now())"),
                  dict(id=pid, rk=RISK_BY_PLAN_TYPE[PlanType.purchase_review].value, it=db.js([item]),
                       ra=f"사내 미도입 제품 신청: {name}" + (f" ({item['purpose']})" if item["purpose"] else ""), by=user.id, rs=UNLISTED_REASON))
        _audit(c, user, "plan.create", pid, None, {"status": "rejected", "type": "purchase_review", "product_name": name})
        _audit(c, user, "plan.reject", pid, None, {"status": "rejected", "reason": UNLISTED_REASON})
    return ok(_plan(pid))


SEAT_SQL = {
    PlanType.seat_assign: "INSERT INTO seat_assignments (id, contract_id, employee_id, status, assigned_at) VALUES (:sid, :c, :e, 'assigned', now())"
                          " ON CONFLICT (contract_id, employee_id) WHERE status = 'assigned' DO NOTHING",
    PlanType.seat_reclaim: "UPDATE seat_assignments SET status = 'unassigned', unassigned_at = now() WHERE contract_id = :c AND employee_id = :e AND status = 'assigned'",
}


def _seat_shortage(p: ActionPlan) -> dict[str, int]:
    """좌석 배정 조치안에서 계약별로 모자라는 좌석 수. 넉넉하면 빈 dict."""
    if p.type != PlanType.seat_assign:
        return {}
    out = {}
    for cid in {i["contract_id"] for i in p.items}:
        c = db.one("SELECT c.seats_purchased, count(s.id) AS used FROM contracts c LEFT JOIN seat_assignments s ON s.contract_id = c.id AND s.status = 'assigned'"
                   " WHERE c.id = :c GROUP BY c.id", c=cid)
        adding = sum(1 for i in p.items if i["contract_id"] == cid)
        if c and c["used"] + adding > c["seats_purchased"]:
            out[cid] = c["used"] + adding - c["seats_purchased"]
    return out


def _seat_blocker(p: ActionPlan, allow_short: bool = False) -> str | None:
    """좌석 조치안 재검증. 막을 이유가 없으면 None. allow_short=True면 좌석 부족은 막지 않는다(생성 때: 승인 대기로 두고 관리자가 반려).
    ponytail: 검사와 반영 사이 잠금이 없어 동시 배정 2건이 마지막 1석을 함께 통과할 수 있다. 필요하면 contracts 행을 FOR UPDATE로 잠근다."""
    if p.type not in SEAT_SQL:
        return None
    for cid in {i["contract_id"] for i in p.items}:
        if not db.one("SELECT 1 AS x FROM contracts WHERE id = :c", c=cid):
            return f"계약 없음: {cid}"
    short = _seat_shortage(p)
    if short and not allow_short:
        cid, n = next(iter(short.items()))
        adding = sum(1 for i in p.items if i["contract_id"] == cid)
        return f"{cid} 남은 좌석 {adding - n}석, 요청 {adding}석"
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
        if p.type == PlanType.seat_reclaim and i.get("confirmation_id"):  # 확인 요청을 붙였으면 반납·휴직 응답이나 기한이 지난 무응답이어야 회수
            a = db.one("SELECT answer, due_at < now() AS overdue FROM confirmations WHERE id = :id AND employee_id = :e", id=i["confirmation_id"], e=i["employee_id"])
            if not a:
                return f"{i['employee_id']}의 확인 요청이 없습니다 ({i['confirmation_id']})"
            if a["answer"] is None and not a["overdue"]:
                return f"{i['employee_id']}의 확인 응답 기한(5영업일)이 아직 지나지 않았습니다 (무응답)"
            if a["answer"] is not None and a["answer"] not in ("return_seat", "on_leave"):
                return f"{i['employee_id']}의 확인 응답이 반납·휴직이 아닙니다 ({a['answer']})"
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
    why = "승인 뒤 대상 상태가 바뀌었습니다" if any(i.get("_stale") for i in p.items) else _seat_blocker(p) or _project_blocker(p) or _access_blocker(p)
    if why:  # 재검증 실패: 조치안을 failed로 남기고 멈춘다 (좌석·절감 기록은 건드리지 않음)
        with db.tx() as c:
            c.execute(db.text("UPDATE action_plans SET status = 'failed', result = CAST(:r AS jsonb) WHERE id = :id AND status = 'approved'"),
                      dict(r=db.js({"ok": False, "reason": why}), id=p.id))
            _audit(c, user, "plan.fail", p.id, {"status": "approved"}, {"status": "failed", "reason": why})
        raise ApiError(ErrorCode.stale, f"실행 전 재검증 실패: {why}", {"plan_id": p.id})
    result = {"vendor": "mock", "ok": True, "items": p.items}
    with db.tx() as c:
        if p.type == PlanType.api_access:
            result["handoffs"] = [_grant_api_access(c, p, i) for i in p.items]
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


def _access_model(i: dict) -> str | None:
    """API 사용 신청에 쓸 모델: 요청한 모델이 그 제품의 승인 모델이면 그것, 아니면 그 제품의 첫 승인 모델."""
    rows = [r["id"] for r in db.rows("SELECT id FROM models WHERE tool_id = :t AND approved ORDER BY id", t=i["tool_id"])]
    return i.get("model_id") if i.get("model_id") in rows else (rows[0] if rows else None)


def _access_blocker(p: ActionPlan) -> str | None:
    for i in p.items if p.type == PlanType.api_access else []:
        if not _access_model(i):
            return f"{i['tool_id']}에 승인된 모델이 없습니다"
    if p.type == PlanType.api_access and not db.one("SELECT 1 AS x FROM employees WHERE id = :e AND status = 'active'", e=p.requested_by):
        return f"요청자 {p.requested_by}가 재직 중이 아닙니다"
    return None


KEY_TTL = timedelta(hours=24)


def _grant_api_access(c, p: ActionPlan, i: dict) -> dict:
    """API 사용 승인: 요청자 이름의 테스트 프로젝트와 키를 만들고, 키는 본인만 한 번 볼 수 있는 링크로 넘긴다 (Mock 벤더)."""
    emp = db.one("SELECT name, dept_id FROM employees WHERE id = :e", c, e=p.requested_by)
    product = db.one("SELECT product FROM tools WHERE id = :t", c, t=i["tool_id"])["product"]
    prj, key, hid = _new_id("prj"), _new_id("key"), "kh_" + secrets.token_urlsafe(24)
    secret = f"sk-mock-{secrets.token_urlsafe(32)}"
    c.execute(db.text("INSERT INTO projects (id, name, dept_id, owner_id, purpose, model_id, environment, monthly_budget_krw)"
                      " VALUES (:id, :n, :d, :o, :pu, :m, CAST(:env AS environment), 0)"),
              dict(id=prj, n=f"{emp['name']} {product}", d=emp["dept_id"], o=p.requested_by, pu=i["purpose"], m=_access_model(i), env=i.get("environment", "test")))
    c.execute(db.text("INSERT INTO api_keys (id, project_id, label, vendor_key_ref, created_at) VALUES (:id, :p, :l, :r, now())"),
              dict(id=key, p=prj, l=f"{emp['name']} 키", r=f"vk_{key}"))
    c.execute(db.text("INSERT INTO key_handoffs (id, api_key_id, employee_id, key_prefix, secret, expires_at) VALUES (:id, :k, :e, :px, :s, :x)"),
              dict(id=hid, k=key, e=p.requested_by, px=secret[:12], s=secret, x=_now() + KEY_TTL))
    return {"handoff_id": hid, "key_prefix": secret[:12], "project_id": prj, "tool_id": i["tool_id"]}


@app.post("/keys/handoff/{handoff_id}/reveal", response_model=Envelope[KeyReveal], tags=["F4"])
def reveal_key(handoff_id: str, user: Employee = Depends(require(Role.employee, Role.admin))):
    """본인만, 한 번만, 24시간 안에. 열면 서버에서 키 값을 지운다."""
    with db.tx() as c:
        h = db.one("SELECT h.employee_id, h.key_prefix, h.secret, h.expires_at, h.viewed_at, p.name AS project_name, t.product"
                   " FROM key_handoffs h JOIN api_keys k ON k.id = h.api_key_id JOIN projects p ON p.id = k.project_id"
                   " JOIN models m ON m.id = p.model_id JOIN tools t ON t.id = m.tool_id WHERE h.id = :id FOR UPDATE OF h", c, id=handoff_id)
        if not h or h["employee_id"] != user.id:
            raise ApiError(ErrorCode.not_found, "없는 링크이거나 본인 키가 아닙니다")
        if h["viewed_at"]:
            raise ApiError(ErrorCode.conflict, "이미 한 번 확인한 키예요. 다시 필요하면 관리자에게 재발급을 요청해 주세요")
        if h["expires_at"] <= _now():
            c.execute(db.text("UPDATE key_handoffs SET secret = NULL WHERE id = :id"), dict(id=handoff_id))
            raise ApiError(ErrorCode.stale, "링크가 만료됐어요 (24시간). 관리자에게 재발급을 요청해 주세요")
        c.execute(db.text("UPDATE key_handoffs SET secret = NULL, viewed_at = now() WHERE id = :id"), dict(id=handoff_id))
        _audit(c, user, "key.reveal", handoff_id, None, {"key_prefix": h["key_prefix"]})
    return ok(KeyReveal(product=h["product"], project_name=h["project_name"], key_prefix=h["key_prefix"], secret=h["secret"], expires_at=h["expires_at"]))


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


JOBS = {"collect_release_notes", "collect_usage", "check_thresholds", "send_suggestions", "reclaim_overdue"}
WEEKLY_CAP = 2  # 알림 정책: 팀당 주 2건


def _job_reclaim_overdue(user: Employee) -> dict:
    """알림 정책: 사용 의사 확인 응답 기한(5영업일)이 지나도 무응답이면 그 좌석을 회수한다.
    확인 요청이 붙은 승인 대기 회수 요청이 있으면 그것을, 없으면 새 회수 요청을 시스템 승인(sys)으로 실행해 이력을 남긴다.
    기준 시각은 DB의 now()다 (_seat_blocker의 기한 검사와 같은 시계). 이미 좌석이 없으면 건너뛰므로 여러 번 돌려도 같다."""
    reclaimed, skipped = [], []
    for f in db.rows("SELECT id, plan_id, employee_id, due_at FROM confirmations WHERE answer IS NULL AND due_at < now() ORDER BY due_at"):
        src = _plan(f["plan_id"])
        cid = next((i["contract_id"] for i in (src.items if src else []) if i.get("employee_id") == f["employee_id"] and i.get("contract_id")), None)
        if not cid:
            skipped.append({"confirmation_id": f["id"], "reason": "확인 요청에 연결된 계약이 없습니다"})
            continue
        if not db.one("SELECT 1 AS x FROM seat_assignments WHERE contract_id = :c AND employee_id = :e AND status = 'assigned'", c=cid, e=f["employee_id"]):
            skipped.append({"confirmation_id": f["id"], "reason": "이미 회수된 좌석입니다"})
            continue
        reason = f"사용 의사 확인 무응답, 응답 기한({f['due_at']:%Y-%m-%d}) 경과"
        with db.tx() as c:
            if src.type == PlanType.seat_reclaim and src.status == PlanStatus.pending_approval:
                pid = src.id
            else:
                pid = _new_id("plan")
                items = [{"employee_id": f["employee_id"], "contract_id": cid, "confirmation_id": f["id"]}]
                ev = [Evidence(source="confirmation", ref=f["id"], value={"answer": None, "due_at": f["due_at"].isoformat()}).model_dump()]
                c.execute(db.text("INSERT INTO action_plans (id, type, risk_level, status, items, rationale, evidence, requested_by, created_at)"
                                  " VALUES (:id, 'seat_reclaim', :rk, 'pending_approval', CAST(:it AS jsonb), :ra, CAST(:ev AS jsonb), 'sys', now())"),
                          dict(id=pid, rk=RISK_BY_PLAN_TYPE[PlanType.seat_reclaim].value, it=db.js(items), ra=reason, ev=db.js(ev)))
                _audit(c, user, "plan.create", pid, None, {"status": "pending_approval", "reason": reason})
            c.execute(db.text("UPDATE action_plans SET status = 'approved', approver_id = 'sys', approved_at = now() WHERE id = :id"), dict(id=pid))
            _audit(c, user, "plan.auto_approve", pid, {"status": "pending_approval"}, {"status": "approved", "reason": reason})
        try:
            _execute(_plan(pid), f"overdue_{f['id']}", user)
            reclaimed.append({"confirmation_id": f["id"], "plan_id": pid, "employee_id": f["employee_id"], "contract_id": cid})
        except ApiError as e:  # 실행 직전 재검증 실패는 조치안에 failed로 남는다
            skipped.append({"confirmation_id": f["id"], "plan_id": pid, "reason": e.message})
    return {"reclaimed": reclaimed, "skipped": skipped}


def _job_check_thresholds(now: datetime) -> dict:
    """좌석 토큰 소비 속도가 평균의 2배 이상(level=over)이면 경보. 같은 사람 같은 날은 한 번만."""
    made, d0 = [], _day_start(now)
    for p in _seat_paces(now):
        if p.level != PaceLevel.over:
            continue
        with db.tx() as c:
            if db.one("SELECT id FROM anomalies WHERE scope_type = 'employee' AND scope_id = :e AND rule = 'seat_pace_2x' AND created_at >= :d", c, e=p.employee_id, d=d0):
                continue
            aid = _new_id("anm")
            c.execute(db.text("INSERT INTO anomalies (id, scope_type, scope_id, rule, baseline, observed, ratio, created_at)"
                              " VALUES (:id, 'employee', :e, 'seat_pace_2x', :b, :o, :r, :n)"),
                      dict(id=aid, e=p.employee_id, b=p.peer_avg_daily, o=p.projected_today, r=p.ratio, n=now))
            made.append(aid)
    return {"new_anomalies": made}


DEMO_HEAVY = {x.strip().lower() if "@" in x else x.strip() for x in os.getenv("F3_DEMO_HEAVY", "emp_md_01,emp_cs_01").split(",") if x.strip()}  # 시연용: 늘 빠르게 쓰는 좌석 (직원 id 또는 이메일)


def _job_collect_usage(now: datetime) -> dict:
    """더미 수집: 사용량을 받는 좌석마다 빈 지난 평일(최대 14일)과 오늘 업무 시간 중 지난 시간의 사용량을 채운다.
    실제 연동 시 이 함수만 벤더 사용량 API(OpenAI Admin·Compliance 등) 호출로 바꾼다."""
    import random

    d0 = _day_start(now)
    seats = db.rows("SELECT s.employee_id, s.contract_id, lower(e.email) AS email FROM seat_assignments s JOIN contracts c ON c.id = s.contract_id"
                    " JOIN employees e ON e.id = s.employee_id WHERE s.status = 'assigned' AND c.daily_token_quota > 0 AND e.status = 'active'")
    have = {(r["employee_id"], r["d"]) for r in db.rows(
        "SELECT DISTINCT employee_id, (date_trunc('day', period_start + interval '9 hours'))::date AS d FROM usage_records"
        " WHERE employee_id IS NOT NULL AND period_start >= :h AND period_start < :d0", h=d0 - timedelta(days=14), d0=d0)}
    k = now + KST
    rows = []
    for s in seats:
        e = s["employee_id"]
        for back in range(14, 0, -1):  # 빈 지난 평일
            day = d0 - timedelta(days=back)
            local = (day + KST).date()
            if local.weekday() >= 5 or (e, local) in have:
                continue
            rnd = random.Random(f"{e}{local}")
            t = rnd.randint(15, 45) * 1000
            rows.append(dict(v=f"vr_sim_{e}_{local:%Y%m%d}", c=s["contract_id"], p=day + timedelta(hours=WORKDAY[0] + 1), e=e, m="openai:gpt-5.5-mini",
                             b="0-2k", r=t // 2000, i=t * 7 // 10, o=t * 3 // 10))
        heavy = e in DEMO_HEAVY or s["email"] in DEMO_HEAVY
        for h in range(WORKDAY[0], min(k.hour, WORKDAY[1] - 1) + 1):  # 오늘 지난 업무 시간 (지금 시간은 지난 분만큼)
            part = 1.0 if h < k.hour else k.minute / 60
            if part <= 0:
                continue
            rnd = random.Random(f"{e}{k:%Y%m%d}{h}")
            t = int(rnd.randint(15, 45) * 1000 / (WORKDAY[1] - WORKDAY[0]) * rnd.uniform(0.5, 1.5) * (3.5 if heavy else 1) * part)
            long_input = heavy and rnd.random() < 0.7
            rows.append(dict(v=f"vr_live_{e}_{k:%Y%m%d}{h:02d}", c=s["contract_id"], p=d0 + timedelta(hours=h), e=e,
                             m="openai:gpt-5.5" if long_input or rnd.random() < 0.4 else "openai:gpt-5.5-mini",
                             b="8k+" if long_input else "0-2k" if rnd.random() < 0.6 else "2k-8k", r=max(1, t // 2500), i=t * 8 // 10, o=t * 2 // 10))
    with db.tx() as c:
        for x in rows:  # 지금 시간 칸은 다음 수집 때 더 커진다
            c.execute(db.text("INSERT INTO usage_records (vendor_record_id, contract_id, period_start, employee_id, model_id, input_length_bucket,"
                              " requests, input_tokens, output_tokens, cost_usd, cost_krw) VALUES (:v, :c, :p, :e, :m, :b, :r, :i, :o, 0, 0)"
                              " ON CONFLICT (vendor_record_id) DO UPDATE SET requests = EXCLUDED.requests, input_tokens = EXCLUDED.input_tokens,"
                              " output_tokens = EXCLUDED.output_tokens"), x)
    return {"records": len(rows), "seats": len(seats)}


# 공급사 모델 목록 API: (주소, 키 환경변수, 인증 헤더). OpenAI·xAI는 created(유닉스초), Anthropic은 created_at·display_name
MODEL_LIST_APIS = {
    "OpenAI": ("https://api.openai.com/v1/models", "OPENAI_API_KEY", lambda k: {"Authorization": f"Bearer {k}"}),
    "Anthropic": ("https://api.anthropic.com/v1/models?limit=1000", "ANTHROPIC_API_KEY", lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "xAI": (os.getenv("XAI_BASE_URL", "https://api.x.ai/v1") + "/models", "XAI_API_KEY", lambda k: {"Authorization": f"Bearer {k}"}),
}
MODEL_DOCS = {"OpenAI": "https://platform.openai.com/docs/models/", "Anthropic": "https://docs.anthropic.com/en/docs/about-claude/models#", "xAI": "https://docs.x.ai/docs/models/"}
# ponytail: 이름 규칙으로 대화 모델만 고른다. 공급사가 이름 규칙을 바꾸면 여기를 고친다
CHAT_MODEL = {"OpenAI": r"gpt-|o\d", "Anthropic": r"claude-", "xAI": r"grok-"}
NOT_CHAT = re.compile(r"embed|tts|whisper|dall-e|image|audio|realtime|transcri|moderation|search")


def _parse_model_list(vendor: str, body: dict, since: datetime) -> dict[str, str]:
    """목록 응답 → since 이후 나온 대화 모델 {id: 표시 이름}. 날짜 스냅샷(-2026-10-20, -20261020, -latest)은 별칭 하나로 묶는다."""
    out: dict[str, str] = {}
    for m in body.get("data", []):
        created = datetime.fromisoformat(m["created_at"]) if "created_at" in m else datetime.fromtimestamp(m.get("created", 0), UTC)
        mid = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8}|latest)$", "", m["id"].lower())
        if created >= since and re.match(CHAT_MODEL[vendor], mid) and not NOT_CHAT.search(mid) and re.fullmatch(r"[a-z0-9.-]+", mid):
            out.setdefault(mid, m.get("display_name") or mid)
    return out


def _live_model_list(approved: dict[str, dict], known: set[str], now: datetime) -> tuple[list[dict], list[dict]]:
    """승인 공급사의 GET /v1/models를 불러 MOCK_RELEASES와 같은 모양으로 바꾼다. 처음 켤 때 옛 모델이 쏟아지지 않게
    F1_NEW_MODEL_DAYS(기본 14일) 안에 나온 것만 본다. 한 공급사가 실패해도 나머지는 계속하고 실패는 errors로 돌려준다.
    ponytail: 목록 API는 id·출시일만 준다. 기능·단가는 비워 두고(0 = 미확인) 요약도 '목록에 나타남'뿐이라 카드 뉴스가 얇다.
    다음 단계는 공급사 발표 글을 수집해 요약·단가를 채우는 것."""
    since = now - timedelta(days=float(os.getenv("F1_NEW_MODEL_DAYS", "14")))
    names = {(r["vendor"], r["name"]) for r in db.rows("SELECT vendor, name FROM models")}  # UNIQUE (vendor, name)
    rows, errors = [], []
    for vendor, tool in approved.items():
        if vendor not in MODEL_LIST_APIS:  # Canva처럼 모델 목록 API가 없는 공급사
            continue
        url, key_env, headers = MODEL_LIST_APIS[vendor]
        if not (key := os.getenv(key_env)):
            errors.append({"vendor": vendor, "error": f"{key_env} 없음"})
            continue
        try:
            r = httpx.get(url, headers=headers(key), timeout=10)
            r.raise_for_status()
            found = _parse_model_list(vendor, r.json(), since)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            errors.append({"vendor": vendor, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        for mid, name in found.items():
            if (id_ := f"{vendor.lower()}:{mid}") in known or (vendor, name) in names:
                continue
            rows.append({"model": Model(id=id_, vendor=vendor, name=name, capabilities="", input_usd_per_mtok=0, output_usd_per_mtok=0,
                                        approved=False, allowed_data_class=tool["allowed_data_class"]),
                         "tool_id": tool["id"], "url": MODEL_DOCS[vendor] + mid, "plans": [tool["product"]], "known": False,
                         "summary": f"{vendor} 모델 목록 API에 새 모델이 나타났다: {name} ({mid}). 기능·가격은 공급사 발표를 확인해야 한다."})
    return rows, errors


def _vendor_model_list(simulate: bool, now: datetime) -> tuple[list[dict], list[dict]]:
    """공급사 모델 목록. 회사가 승인한 공급사만 본다. F1_MODEL_SOURCE=live면 실제 API, 아니면(기본) Mock 목록.
    simulate면 어느 쪽이든 Mock의 아직 안 나온 새 모델 하나가 목록에 나타난다 (시연용)."""
    approved = {r["vendor"]: r for r in db.rows("SELECT DISTINCT ON (vendor) vendor, id, product, allowed_data_class FROM tools"
                                                 " WHERE approval_status = 'approved' ORDER BY vendor, id")}
    known = {r["id"] for r in db.rows("SELECT id FROM models")}
    if os.getenv("F1_MODEL_SOURCE", "mock") == "live":
        listed, errors = _live_model_list(approved, known, now)
    else:
        listed, errors = [{"model": m, "known": True} for m in MODEL_CATALOG.values() if m.vendor in approved], []
    nxt = next((r for r in MOCK_RELEASES if r["model"].vendor in approved and r["model"].id not in known), None)
    if simulate and nxt:  # Mock의 tool_id는 시드 기준이다. 셋팅으로 만든 회사는 제품 id가 달라 이 회사의 같은 공급사 제품으로 바꾼다
        t = approved[nxt["model"].vendor]
        listed.append({**nxt, "tool_id": t["id"], "plans": [t["product"]], "known": False})
    return listed, errors


def _job_collect_release_notes(now: datetime, simulate: bool = False) -> dict:
    """새 모델 감지: 공급사 모델 목록을 폴링해 DB models에 없는 모델을 찾는다 → 모델 등록(사내 승인 전) + '새 모델 출시' 업데이트.
    카드 뉴스(핵심 기능 3개 + 직무별 활용)는 에이전트 review_update가 업데이트마다 만든다."""
    known = {r["id"] for r in db.rows("SELECT id FROM models")}
    found = []
    listed, errors = _vendor_model_list(simulate, now)
    for r in listed:
        m = r["model"]
        if m.id in known or "url" not in r:
            continue
        with db.tx() as c:
            src = db.one("SELECT id FROM vendor_sources WHERE vendor = :v ORDER BY id LIMIT 1", c, v=m.vendor)
            sid = src["id"] if src else f"src_{m.vendor.lower()}_models"
            if not src:
                c.execute(db.text("INSERT INTO vendor_sources (id, vendor, url, kind) VALUES (:id, :v, :u, 'docs')"), dict(id=sid, v=m.vendor, u=r["url"].rsplit("/", 2)[0]))
            c.execute(db.text("INSERT INTO models (id, tool_id, vendor, name, capabilities, input_usd_per_mtok, output_usd_per_mtok, approved, allowed_data_class)"
                              " VALUES (:id, :t, :v, :n, :cap, :i, :o, false, CAST(:dc AS data_class))"),
                      dict(id=m.id, t=r["tool_id"], v=m.vendor, n=m.name, cap=m.capabilities, i=m.input_usd_per_mtok, o=m.output_usd_per_mtok, dc=m.allowed_data_class.value))
            uid = "upd_model_" + m.id.split(":")[1].replace(".", "_").replace("-", "_")
            c.execute(db.text("INSERT INTO vendor_updates (id, source_id, vendor, published_at, title, summary, url, affected_plans, fetched_at)"
                              " VALUES (:id, :s, :v, :p, :t, :sm, :u, :pl, :p)"),
                      dict(id=uid, s=sid, v=m.vendor, p=now, t=f"새 모델 출시: {m.name}", sm=r["summary"], u=r["url"], pl=r["plans"]))
        found.append({"model_id": m.id, "update_id": uid})
    out = {"new_models": found} if found else {"new_models": [], "note": "새 모델 없음" + (" (시뮬레이션할 모델도 모두 공개됨)" if simulate else "")}
    return {**out, "errors": errors} if errors else out


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
                 " coalesce(d.suggestions_enabled, true) AS suggestions_enabled, d.slack_channel_id, e.slack_user_id FROM suggestions g"
                 " LEFT JOIN employees e ON e.id = g.target_employee_id"
                 " LEFT JOIN departments d ON d.id = coalesce(g.target_dept_id, e.dept_id)"
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
            elif n_week >= WEEKLY_CAP and g["dept_id"]:  # 전사 새 모델 카드는 팀 상한에 넣지 않는다
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
                      dict(id=_new_id("rr"), u=upd, n=now, s=sorted({x["dept_id"] or "all" for x in xs if "to" in x}), h=db.js([x for x in xs if "reason" in x])))
    return {"sent": sent, "held": held}


@app.post("/internal/jobs/{job_name}/run", response_model=Envelope[JobRunResponse], tags=["internal"])
def run_job(job_name: str, now: datetime | None = Query(None, description="시연·테스트용 기준 시각. 비우면 현재"),
            simulate: bool = Query(False, description="collect_release_notes 전용: Mock 공급사 목록에 새 모델 하나를 공개 (출시 시뮬레이션)"),
            user: Employee = Depends(require(Role.system, Role.admin))):
    if job_name not in JOBS:
        raise ApiError(ErrorCode.not_found, f"배치 없음. 가능: {sorted(JOBS)}")
    t, now = _now(), now or _now()
    if job_name == "check_thresholds":
        result = _job_check_thresholds(now)
    elif job_name == "collect_usage":
        result = _job_collect_usage(now)
    elif job_name == "send_suggestions":
        result = _job_send_suggestions(now)
    elif job_name == "reclaim_overdue":
        result = _job_reclaim_overdue(user)
    else:
        result = _job_collect_release_notes(now, simulate)
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
