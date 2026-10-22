"""8절 백엔드 REST API 요청·응답 모델.

경로별 매핑은 파일 끝의 ROUTES 표를 본다. 백엔드는 이 모델로 response_model을 걸고,
MCP 서버·UI·Slack은 이 모델로 응답을 파싱한다.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from schemas.common import (
    BillingType,
    BreakdownBy,
    PaceLevel,
    ConfirmationAnswer,
    DataClass,
    Environment,
    Evidence,
    Granularity,
    LicenseFlag,
    Money,
    PlanStatus,
    PlanType,
    Reaction,
    RiskLevel,
    Role,
    SavingsKind,
    SuggestionStatus,
    TargetType,
    TaskSource,
    UsageScope,
)
from schemas.entities import (
    ActionPlan,
    Anomaly,
    AuditEntry,
    BreakdownRow,
    Confirmation,
    Contract,
    Department,
    Employee,
    EvalResult,
    Model,
    Project,
    RegisteredTask,
    Suggestion,
    ToolWithPlan,
    UsagePoint,
    UsageSummary,
    VendorUpdate,
)


# ---------- /auth ----------
class DevLoginRequest(BaseModel):
    employee_id: str


class SlackExchangeRequest(BaseModel):
    slack_user_id: str
    employee_id: str | None = None  # Slack 봇의 SLACK_USER_MAP으로 찾은 직원. 없으면 직원의 slack_user_id로 찾는다


class LoginRequest(BaseModel):
    login_id: str = Field(description="사번(예: emp_mkt_01) 또는 회사 이메일")
    password: str = Field(min_length=1)


class DevLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    employee: Employee


# ---------- /me ----------
class MeResponse(BaseModel):
    employee: Employee
    tasks: list[RegisteredTask]
    available_tools: list[ToolWithPlan]


class TaskCreate(BaseModel):
    title: str
    description: str
    frequency: str
    tools_used: list[str] = []
    source: TaskSource = TaskSource.manual


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    frequency: str | None = None
    tools_used: list[str] | None = None


# ---------- /updates ----------
class RelevantTaskCandidate(BaseModel):
    """관리자에게는 employee_id 없이 팀 단위(team_id, job_title)로만 내려간다. 시스템 계정만 개인을 본다."""

    task_id: str
    job_title: str
    team_id: str
    employee_id: str | None = None
    summary: str


class RelevantTasksResponse(BaseModel):
    update: VendorUpdate
    candidates: list[RelevantTaskCandidate]


# ---------- /suggestions ----------
class SuggestionCreate(BaseModel):
    update_id: str
    target_type: TargetType
    target_id: str | None = None
    task_id: str | None = None
    relevance: str = Field("", description="관련성 근거. 보류(on_hold)가 아니면 필수")
    body: str = ""
    status: SuggestionStatus | None = Field(None, description="생략하면 개인은 draft, 팀·전사는 pending_approval. on_hold면 보내지 않고 사유만 남긴다")
    hold_reason: str | None = None


class FeedbackCreate(BaseModel):
    reaction: Reaction


class SuggestionListQuery(BaseModel):
    status: SuggestionStatus | None = None
    update_id: str | None = None


# ---------- /adoption ----------
class TeamAdoption(BaseModel):
    team_id: str
    team_name: str
    headcount: int = Field(description="5명 미만 팀은 목록에서 제외")
    suggestions_sent: int
    useful: int
    irrelevant: int
    questions: int
    active_ratio: float = Field(description="활동 인원 비율 0~1")
    top_questions: list[str] = []


# ---------- /eval-results, /estimates ----------
class TaskCostEstimateRequest(BaseModel):
    model_id: str
    requests_per_day: int
    avg_input_tokens: int
    avg_output_tokens: int
    working_days_per_month: int = 22


class TaskCostEstimateResponse(BaseModel):
    model: Model
    daily: Money
    monthly: Money
    fx_rate_krw_per_usd: float
    formula: str = Field(description="사람이 읽는 계산식")


# ---------- /inventory ----------
class LowUsageCandidate(BaseModel):
    employee_id: str
    employee_name: str
    dept_name: str
    last_activity_at: datetime | None
    flag: LicenseFlag
    needs_confirmation: bool


class ContractInventory(BaseModel):
    contract: Contract
    tool_name: str
    purchased: int
    assigned: int
    unassigned: int
    low_usage_candidates: list[LowUsageCandidate]


# ---------- /org/members (관리자 좌석 배치도) ----------
class MemberTool(BaseModel):
    tool_id: str
    product: str
    via: str  # seat: 본인 배정 좌석 · project: 소속 팀 API 프로젝트
    detail: str  # 좌석이면 플랜 이름, 프로젝트면 "프로젝트명 · 모델명"
    flag: LicenseFlag | None = None
    last_activity_at: datetime | None = None
    active_days_30: int | None = None  # 좌석만: 최근 30일 중 쓴 날 수 (사용 기록을 주는 제품만, 없으면 None)
    requests_30: int | None = None


class OrgMember(BaseModel):
    employee: Employee
    tools: list[MemberTool]


# ---------- /setup (초기 설정 마법사) · /budgets ----------
# 회사는 한 곳. 첫 관리자가 가입하면 회사·부서·관리자 계정이 생기고, 이어서 부서·직원 → 구독 제품(좌석) → AI 모델·API 프로젝트 → 연간 예산 순으로 채운다.
# 설정 변경은 처리 요청(plan) 없이 관리자가 바로 하되 모두 audit_log에 남긴다 (초기 구성이라 되돌릴 대상이 없음).
class SetupStatus(BaseModel):
    company_name: str | None
    has_admin: bool
    setup_completed: bool
    steps: dict[str, int]  # departments·employees·contracts·models·projects·budgets 건수
    dev_login: bool = False  # APP_ENV가 prod가 아니면 true. 로그인 화면이 '시연 계정' 버튼을 보일지 정한다


class SignupRequest(BaseModel):
    company_name: str = Field(min_length=1, max_length=100)
    admin_name: str = Field(min_length=1, max_length=50)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    password: str = Field(min_length=8, max_length=128)
    dept_name: str = Field(default="경영지원", min_length=1, max_length=50, description="관리자가 속한 부서")
    job_title: str = Field(default="IT 자산 관리자", min_length=1, max_length=50)


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class EmployeeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    dept_id: str
    job_title: str = Field(min_length=1, max_length=50)
    role: Role = Role.employee
    hire_date: date | None = None
    slack_user_id: str | None = Field(None, pattern=r"^[UW][A-Z0-9]{8,}$", description="Slack 회원 ID (프로필 → ⋮ → 회원 ID 복사). 넣으면 Slack 봇이 바로 알아본다")


# ---------- /desks (좌석 배치도의 책상, F4 신입 배치) ----------
class Desk(BaseModel):
    id: str = Field(description="예: 4F-mkt-07")
    floor: str = Field(description="층. 예: 4F")
    dept_id: str = Field(description="이 책상이 속한 부서 구역")
    seq: int = Field(description="구역 안 순서. 6개씩 한 섬")
    employee_id: str | None = None


class DeskAssign(BaseModel):
    employee_id: str | None = Field(None, description="앉힐 직원. 다른 자리에 있으면 옮긴다. null이면 자리를 비운다")


class DeskCreate(BaseModel):
    floor: str = Field(pattern=r"^[0-9]{1,2}F$")
    dept_id: str
    count: int = Field(6, ge=1, le=30)


class ContractCreate(BaseModel):
    """구독 제품(사내 자산) 등록. tool_id가 없으면 제품을 새로 만든다."""
    tool_id: str | None = None
    vendor: str = Field(min_length=1, max_length=50)
    product: str = Field(min_length=1, max_length=80)
    category: str = Field(default="chat_assistant", max_length=50)
    kind: str = Field(default="ai", pattern="^(ai|software)$")
    allowed_data_class: DataClass = DataClass.internal
    plan_name: str = Field(min_length=1, max_length=80)
    billing_type: BillingType
    seats_purchased: int = Field(ge=0, le=100000)
    seat_price_krw: int = Field(ge=0)
    term_start: date
    term_end: date


class ModelApproval(BaseModel):
    model_ids: list[str] = Field(description="승인할 모델 id. 목록(GET /setup/model-catalog)에 있는 것만. 빠진 모델은 승인 해제")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    dept_id: str
    owner_id: str
    purpose: str = Field(min_length=1, max_length=300)
    model_id: str
    environment: Environment = Environment.test
    monthly_budget_krw: int = Field(ge=0)


class BudgetSet(BaseModel):
    fiscal_year: int = Field(ge=2000, le=2100)
    seat_annual_krw: int = Field(ge=0)
    api_annual_krw: int = Field(ge=0)


class BudgetView(BudgetSet):
    seat_monthly_krw: int
    api_monthly_krw: int
    seat_monthly_cost_krw: int = Field(description="구매 좌석 × 단가 (월). 미배정 좌석도 돈을 낸다")
    project_budget_sum_krw: int = Field(description="API 프로젝트 월 예산 합. api_monthly_krw를 넘으면 경고")


# ---------- /usage ----------
class UsageQuery(BaseModel):
    scope: UsageScope
    scope_id: str
    start: datetime
    end: datetime
    granularity: Granularity = Granularity.day
    group_by: BreakdownBy | None = None


class UsageResponse(BaseModel):
    summary: UsageSummary
    series: list[UsagePoint] = []
    breakdown: list[BreakdownRow] = []


# ---------- /usage/seats (F3 좌석별 토큰 소비 속도) ----------
class SeatPace(BaseModel):
    """예상 = 오늘 사용 ÷ 업무 시간(09~18시 KST) 경과 비율. 평균 = 같은 제품 좌석들의 최근 14일 하루 사용량 평균."""
    employee_id: str
    employee_name: str
    dept_name: str
    contract_id: str
    product: str
    tokens_today: int
    projected_today: int
    peer_avg_daily: int
    daily_quota: int = Field(description="좌석당 하루 토큰 한도 (contracts.daily_token_quota)")
    quota_used_pct: float
    projected_quota_pct: float
    ratio: float = Field(description="예상 ÷ 평균")
    level: PaceLevel
    last_activity_at: datetime | None = None


class SeatPaceDetail(BaseModel):
    pace: SeatPace
    by_hour: list[BreakdownRow] = Field(default=[], description="오늘 시간대별 (key=KST 시, 예: 14)")
    by_input_length: list[BreakdownRow] = []
    by_model: list[BreakdownRow] = []


class KeyReveal(BaseModel):
    """API 사용 승인 뒤 발급한 키를 본인이 한 번만 보는 링크의 결과. 열고 나면 서버에서 키 값을 지운다."""
    product: str
    project_name: str
    key_prefix: str = Field(description="키 앞부분. Slack 안내에도 같은 값이 나가 본인 키인지 맞춰 본다")
    secret: str = Field(description="키 전체. 이 응답에서 한 번만 준다")
    expires_at: datetime


# ---------- /plans ----------
class PlanCreate(BaseModel):
    type: PlanType
    items: list[dict]
    rationale: str
    evidence: list[Evidence]


class PlanListQuery(BaseModel):
    status: PlanStatus | None = None
    type: PlanType | None = None


class PlanDecision(BaseModel):
    reason: str = Field(min_length=1, description="승인·반려 모두 사유 필수")


class PlanExecuteResponse(BaseModel):
    plan: ActionPlan
    result: dict
    verified: dict = Field(description="실행 후 재조회 결과")
    savings: list[dict] = []


# ---------- /confirmations ----------
class ConfirmationCreate(BaseModel):
    """저사용 이용권 소유자에게 사용 의사를 묻는다. 응답 기한은 서버가 5영업일로 정한다 (9절 알림 정책)."""

    employee_id: str
    plan_id: str
    question: str


class ConfirmationAnswerRequest(BaseModel):
    answer: ConfirmationAnswer


# ---------- /savings ----------
class SavingsSummary(BaseModel):
    by_kind: dict[SavingsKind, int]
    total_krw: int


# ---------- /internal/jobs ----------
class JobRunResponse(BaseModel):
    job: str
    started_at: datetime
    finished_at: datetime
    result: dict


# ---------- 경로 표 (문서용, 백엔드 라우터와 일치해야 함) ----------
ROUTES: list[tuple[str, str, str, list[Role]]] = [
    ("POST", "/auth/login", "사번·비밀번호로 로그인, JWT 발급", []),
    ("POST", "/auth/dev-login", "시드 직원으로 토큰 발급 (APP_ENV=dev 전용)", []),
    ("POST", "/auth/slack-exchange", "Slack 봇이 Slack 사용자 대신 직원 토큰을 받는다", [Role.system]),
    ("GET", "/me", "본인 프로필·업무·실제로 쓸 수 있는 도구", [Role.employee, Role.admin, Role.system]),
    ("GET", "/me/tasks", "본인 등록 업무", [Role.employee]),
    ("POST", "/me/tasks", "업무 등록", [Role.employee]),
    ("PATCH", "/me/tasks/{task_id}", "업무 수정", [Role.employee]),
    ("GET", "/tools", "승인 도구·플랜", [Role.employee, Role.admin, Role.system]),
    ("GET", "/models", "승인 모델·단가", [Role.employee, Role.admin, Role.system]),
    ("GET", "/updates", "수집된 업데이트", [Role.employee, Role.admin, Role.system]),
    ("GET", "/updates/{update_id}", "업데이트 상세", [Role.employee, Role.admin, Role.system]),
    ("GET", "/updates/{update_id}/relevant-tasks", "관련 업무 후보", [Role.system, Role.admin]),
    ("GET", "/suggestions", "제안 목록", [Role.system, Role.admin]),
    ("POST", "/suggestions", "제안 저장", [Role.system, Role.admin]),
    ("POST", "/suggestions/{suggestion_id}/feedback", "피드백", [Role.employee, Role.admin]),
    ("GET", "/adoption/teams", "팀 단위 활용 현황 (5명 이상 팀)", [Role.admin]),
    ("GET", "/eval-results", "모델 평가 결과", [Role.employee, Role.admin, Role.system]),
    ("POST", "/estimates/task-cost", "예상 비용 계산", [Role.employee, Role.admin, Role.system]),
    ("GET", "/inventory", "이용권 현황", [Role.admin, Role.system]),
    ("GET", "/employees/{employee_id}/assets", "직원이 실제로 쓸 수 있는 도구(배정 좌석·팀 프로젝트 API)", [Role.admin, Role.system]),
    ("GET", "/usage", "사용량", [Role.employee, Role.admin, Role.system]),
    ("GET", "/projects", "API 프로젝트 목록·월 예산", [Role.admin, Role.system]),
    ("GET", "/org/members", "직원별 쓰는 도구(좌석·팀 프로젝트) — 좌석 배치도", [Role.admin, Role.system]),
    ("GET", "/anomalies", "경보 목록", [Role.admin, Role.system]),
    ("GET", "/setup/status", "초기 설정 상태 (가입 전이면 company_name=null)", []),
    ("POST", "/setup/signup", "첫 관리자 가입: 회사·부서·관리자 계정 생성 후 토큰 발급. 회사가 있으면 409", []),
    ("POST", "/setup/complete", "초기 설정 완료 표시", [Role.admin]),
    ("GET", "/setup/model-catalog", "고를 수 있는 AI 모델 목록(공식 단가)", [Role.admin]),
    ("GET", "/departments", "부서 목록", [Role.admin, Role.system]),
    ("POST", "/departments", "부서 추가", [Role.admin]),
    ("POST", "/employees", "직원 여러 명 추가 (비밀번호 없음 = 로그인 전)", [Role.admin]),
    ("GET", "/desks", "책상 목록과 앉은 직원 (좌석 배치도)", [Role.admin, Role.system]),
    ("PUT", "/desks/{desk_id}", "책상에 직원 앉히기·옮기기·비우기", [Role.admin]),
    ("POST", "/desks", "부서 구역에 책상 추가", [Role.admin]),
    ("POST", "/contracts", "구독 제품·계약 등록", [Role.admin]),
    ("PUT", "/models/approved", "쓸 AI 모델 승인 목록 교체", [Role.admin]),
    ("POST", "/projects", "API 프로젝트 등록", [Role.admin]),
    ("GET", "/budgets", "연간 예산과 월 배분", [Role.admin, Role.system]),
    ("PUT", "/budgets", "연간 예산 설정", [Role.admin]),
    ("GET", "/usage/seats", "좌석별 오늘 토큰 소비 속도", [Role.admin, Role.system]),
    ("GET", "/usage/seats/{employee_id}", "좌석 소비 속도 상세 (직원은 본인만)", [Role.employee, Role.admin, Role.system]),
    ("POST", "/keys/handoff/{handoff_id}/reveal", "발급된 API 키를 본인이 한 번만 확인 (24시간)", [Role.employee, Role.admin]),
    ("GET", "/plans", "조치안 목록", [Role.employee, Role.admin, Role.system]),
    ("POST", "/plans", "처리 요청 생성. 모든 요청은 승인 대기(pending_approval). 직원: seat_assign·seat_reclaim(본인), api_access, model_change(본인 소유 프로젝트, test)", [Role.employee, Role.admin, Role.system]),
    ("GET", "/plans/{plan_id}", "조치안 상태", [Role.employee, Role.admin, Role.system]),
    ("PATCH", "/plans/{plan_id}/opinion", "AI 검토 의견 붙이기 (승인 대기 건)", [Role.system, Role.admin]),
    ("GET", "/employees/{employee_id}", "직원 프로필 (검토용)", [Role.system, Role.admin]),
    ("POST", "/plans/{plan_id}/approve", "승인", [Role.admin]),
    ("POST", "/plans/{plan_id}/reject", "반려", [Role.admin]),
    ("POST", "/plans/{plan_id}/execute", "실행 (Idempotency-Key 필수)", [Role.admin, Role.system]),
    ("GET", "/confirmations", "사용 의사 확인 요청 목록 (answered=false: 미응답만). Slack 봇 워커가 DM 발송에 씀", [Role.admin, Role.system]),
    ("POST", "/confirmations", "사용 의사 확인 요청 생성 (due_at = 5영업일)", [Role.admin, Role.system]),
    ("POST", "/confirmations/{confirmation_id}/answer", "사용 의사 응답", [Role.employee]),
    ("GET", "/savings", "절감 합계", [Role.admin]),
    ("GET", "/audit-log", "감사 기록", [Role.admin]),
    ("GET", "/review-runs", "자동 제안 판단 기록 (v2)", [Role.admin]),
    ("GET", "/notify-outbox", "발송함 (발송 배치가 넣고 Slack 봇 워커가 읽음)", [Role.admin, Role.system]),
    ("PATCH", "/notify-outbox/{outbox_id}/delivered", "Slack 전송 뒤 전달 표시 (멱등)", [Role.admin, Role.system]),
    ("POST", "/internal/jobs/{job_name}/run", "배치 수동 실행", [Role.system, Role.admin]),
    # AI 작업 중계: 요청·응답은 specs/openapi.agent.json (schemas.agent) 그대로. 세부 권한은 에이전트가 다시 확인
    ("POST", "/ai/tasks", "에이전트 작업 생성 중계 (202)", [Role.employee, Role.admin, Role.system]),
    ("GET", "/ai/tasks", "작업 이력 중계", [Role.employee, Role.admin, Role.system]),
    ("GET", "/ai/tasks/{task_id}", "작업 조회 중계", [Role.employee, Role.admin, Role.system]),
    ("GET", "/ai/tasks/{task_id}/stream", "진행 이벤트 SSE 중계", [Role.employee, Role.admin, Role.system]),
    ("POST", "/ai/tasks/{task_id}/approval", "처리 확인 중계 (관리자)", [Role.employee, Role.admin, Role.system]),
]
