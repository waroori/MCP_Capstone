"""스텁용 고정 데이터 (10절 시드 '한빛제조' 축약판). 실제 DB가 붙으면 이 파일은 사라진다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schemas.common import *  # noqa: F403
from schemas.entities import *  # noqa: F403

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FX = 1380.0


def krw(usd: float) -> Money:
    return Money(krw=round(usd * FX), usd=round(usd, 4))


DEPARTMENTS = {d.id: d for d in [
    Department(id="dept_mgmt", name="경영지원"),
    Department(id="dept_hrd", name="HRD"),
    Department(id="dept_mkt", name="마케팅"),
    Department(id="dept_sales", name="영업"),
    Department(id="dept_cs", name="고객지원"),
    Department(id="dept_prod", name="생산관리"),
    Department(id="dept_md", name="상품기획·MD"),
]}

EMPLOYEES = {e.id: e for e in [
    Employee(id="emp_hrd_01", name="김하나", email="hana.kim@hanbit.example", dept_id="dept_hrd", dept_name="HRD", role=Role.employee, job_title="교육기획", slack_user_id="U_HRD01"),
    Employee(id="emp_md_01", name="정서진", email="seojin.jung@hanbit.example", dept_id="dept_md", dept_name="상품기획·MD", role=Role.employee, job_title="상품기획·MD 과장", slack_user_id="U_MD01"),
    Employee(id="emp_mkt_01", name="박지후", email="jihoo.park@hanbit.example", dept_id="dept_mkt", dept_name="마케팅", role=Role.employee, job_title="브랜드 마케팅 대리", slack_user_id="U_MKT01"),
    Employee(id="emp_cs_01", name="이지우", email="jiwoo.lee@hanbit.example", dept_id="dept_cs", dept_name="고객지원", role=Role.employee, job_title="CS 운영", slack_user_id="U_CS01"),
    Employee(id="emp_mgmt_01", name="윤서연", email="seoyeon.yoon@hanbit.example", dept_id="dept_mgmt", dept_name="경영지원", role=Role.admin, job_title="경영지원팀장", slack_user_id="U_MGMT01"),
    Employee(id="emp_asset_01", name="최광철", email="gwangcheol.choi@hanbit.example", dept_id="dept_mgmt", dept_name="경영지원", role=Role.admin, job_title="자산관리 담당", slack_user_id="U_ADM01"),
    Employee(id="emp_sales_02", name="한도윤", email="doyun.han@hanbit.example", dept_id="dept_sales", dept_name="영업", role=Role.employee, job_title="영업 담당", status=EmployeeStatus.on_leave),
    Employee(id="sys", name="system", email="system@hanbit.example", dept_id="dept_mgmt", role=Role.system, job_title="batch"),
]}

TOOLS = {t.id: t for t in [
    Tool(id="tool_chatgpt", vendor="OpenAI", product="ChatGPT Enterprise", category="chat_assistant", approval_status=ToolApprovalStatus.approved, allowed_data_class=DataClass.internal, policy_note="기업용 워크스페이스·관리 기능 제공. 고객 개인정보는 승인된 업무에만 입력"),
    Tool(id="tool_claude", vendor="Anthropic", product="Claude API", category="llm_api", approval_status=ToolApprovalStatus.approved, allowed_data_class=DataClass.internal, policy_note="긴 문서 분석과 요약에 적합. 민감정보는 사내 정책 준수"),
    Tool(id="tool_grok", vendor="xAI", product="Grok API", category="llm_api", approval_status=ToolApprovalStatus.under_review, allowed_data_class=DataClass.public, policy_note="실시간 검색 도구 연동 후보. 기업 계약 전 검토 필요"),
    Tool(id="tool_canva", vendor="Canva", product="Canva Enterprise", category="design_collaboration", approval_status=ToolApprovalStatus.approved, allowed_data_class=DataClass.internal, policy_note="브랜드 키트·디자인 협업·AI 디자인 기능"),
]}

CONTRACTS = {c.id: c for c in [
    Contract(id="ctr_chatgpt_2026", tool_id="tool_chatgpt", plan_name="Enterprise (seat + usage)", billing_type=BillingType.seat_plus_usage, seats_purchased=60, seat_price_krw=45000, term_start=datetime(2026, 1, 1, tzinfo=UTC), term_end=datetime(2026, 12, 31, tzinfo=UTC)),
    Contract(id="ctr_claude_2026", tool_id="tool_claude", plan_name="API pay-as-you-go", billing_type=BillingType.api_usage, seats_purchased=0, seat_price_krw=0, term_start=datetime(2026, 1, 1, tzinfo=UTC), term_end=datetime(2026, 12, 31, tzinfo=UTC)),
]}

TOOLS_WITH_PLAN = [
    ToolWithPlan(**TOOLS["tool_chatgpt"].model_dump(), contract=CONTRACTS["ctr_chatgpt_2026"]),
    ToolWithPlan(**TOOLS["tool_claude"].model_dump(), contract=CONTRACTS["ctr_claude_2026"]),
    ToolWithPlan(**TOOLS["tool_grok"].model_dump()),
]

MODELS = {m.id: m for m in [  # id = "공급사:API 모델명" = 에이전트 LLM_MODEL 값과 같은 키. 단가는 공식 가격표(2026-10-21) 기준
    Model(id="anthropic:claude-opus-5-5", vendor="Anthropic", name="Claude Opus 5.5", capabilities="복잡한 추론·긴 문서 분석·고품질 작성", input_usd_per_mtok=4.0, output_usd_per_mtok=20.0, allowed_data_class=DataClass.internal),
    Model(id="anthropic:claude-sonnet-5-5", vendor="Anthropic", name="Claude Sonnet 5.5", capabilities="범용 추론·분류·요약·코드", input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, allowed_data_class=DataClass.internal),
    Model(id="anthropic:claude-haiku-5-5", vendor="Anthropic", name="Claude Haiku 5.5", capabilities="빠른 단순 분류·추출 (입력 10만 토큰 이하 단가)", input_usd_per_mtok=0.1, output_usd_per_mtok=0.5, allowed_data_class=DataClass.internal),
    Model(id="xai:grok-4.7", vendor="xAI", name="Grok 4.7", capabilities="빠른 범용 응답·실시간 검색 도구 연동", input_usd_per_mtok=2.0, output_usd_per_mtok=6.0, approved=False, allowed_data_class=DataClass.public),
    Model(id="xai:grok-4.3", vendor="xAI", name="Grok 4.3", capabilities="저비용 범용 응답", input_usd_per_mtok=1.25, output_usd_per_mtok=2.5, approved=False, allowed_data_class=DataClass.public),
    # ChatGPT Enterprise 좌석 안에서 고르는 모델. 좌석 정액에 포함돼 토큰 단가가 없다 (F3 모델 변경 권장용)
    Model(id="openai:gpt-5.5", vendor="OpenAI", name="GPT-5.5 (ChatGPT)", capabilities="깊은 추론·긴 문서 분석 · 토큰을 많이 쓴다", input_usd_per_mtok=0, output_usd_per_mtok=0, allowed_data_class=DataClass.internal),
    Model(id="openai:gpt-5.5-mini", vendor="OpenAI", name="GPT-5.5 mini (ChatGPT)", capabilities="짧은 질의·초안·분류·요약 · 빠르고 가볍다", input_usd_per_mtok=0, output_usd_per_mtok=0, allowed_data_class=DataClass.internal),
]}

# F1 Mock 공급사 모델 목록에 차례로 나타날 '새 모델'. collect_release_notes(simulate=true)가 한 번에 하나씩 공개한다.
# 실제 연동 때는 공급사 모델 목록 API(OpenAI GET /v1/models, Anthropic GET /v1/models)를 폴링해 DB models에 없는 것을 찾는다.
MOCK_RELEASES = [
    {"model": Model(id="openai:gpt-5.6", vendor="OpenAI", name="GPT-5.6", capabilities="긴 문서 추론·표 분석·이미지 이해", input_usd_per_mtok=0, output_usd_per_mtok=0,
                    allowed_data_class=DataClass.internal),
     "tool_id": "tool_chatgpt", "url": "https://openai.com/index/introducing-gpt-5-6/", "plans": ["ChatGPT Enterprise"],
     "summary": "GPT-5.6 출시. (1) 한 번에 읽는 문서 길이가 2배(최대 50만 토큰)로 늘어 긴 보고서·계약서를 나누지 않고 요약·비교 "
                "(2) 엑셀·CSV를 올리면 표를 정리하고 차트와 해석 문장을 함께 생성 (3) 스크린샷·사진 속 표와 글자를 읽어 바로 편집 가능한 표로 변환. "
                "ChatGPT Enterprise에서 모델 선택으로 바로 사용."},
    {"model": Model(id="anthropic:claude-sonnet-5-6", vendor="Anthropic", name="Claude Sonnet 5.6", capabilities="범용 추론·분류·요약·코드 (5.5보다 빠름)",
                    input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, allowed_data_class=DataClass.internal),
     "tool_id": "tool_claude", "url": "https://www.anthropic.com/news/claude-sonnet-5-6", "plans": ["Claude API"],
     "summary": "Claude Sonnet 5.6 출시. (1) 같은 가격에 응답 속도 약 1.5배 (2) 분류·추출 정확도 향상으로 대량 문의 분류에 유리 "
                "(3) 출력 형식(JSON) 지키기가 좋아져 자동화 파이프라인 오류 감소. API 모델 이름만 바꾸면 사용."},
    {"model": Model(id="xai:grok-5", vendor="xAI", name="Grok 5", capabilities="실시간 검색 연동", input_usd_per_mtok=3.0, output_usd_per_mtok=15.0,
                    allowed_data_class=DataClass.public),
     "tool_id": "tool_grok", "url": "https://x.ai/news/grok-5", "plans": [], "summary": "Grok 5 출시. 승인 전 공급사라 알리지 않는다."},
]

TASKS = {t.id: t for t in [
    RegisteredTask(id="task_hrd_survey", employee_id="emp_hrd_01", title="교육 과정별 설문 결과 비교", description="과정별 만족도 설문 파일을 비교해 공통 개선점과 과정별 차이를 정리", frequency="monthly", tools_used=["tool_chatgpt"], updated_at=NOW - timedelta(days=10)),
    RegisteredTask(id="task_mkt_copy", employee_id="emp_mkt_01", title="캠페인 카피 초안 작성", description="제품 소개 문구 3안 작성", frequency="weekly", tools_used=["tool_chatgpt"], updated_at=NOW - timedelta(days=3)),
    RegisteredTask(id="task_cs_classify", employee_id="emp_cs_01", title="고객 문의 유형 분류", description="하루 1,000건 문의를 10개 유형으로 분류 (API)", frequency="daily", tools_used=["tool_claude"], updated_at=NOW - timedelta(days=30)),
]}

UPDATES = {u.id: u for u in [
    VendorUpdate(id="upd_chatgpt_doc_compare", source_id="src_chatgpt_rn", vendor="OpenAI", published_at=NOW - timedelta(days=1), title="ChatGPT Enterprise 문서 비교 기능", summary="여러 파일을 함께 올리고 차이점과 공통점을 표로 정리할 수 있다. Enterprise 플랜부터 제공.", url="https://openai.com/business/chatgpt-pricing/", affected_plans=["Enterprise"], fetched_at=NOW),
    VendorUpdate(id="upd_claude_price", source_id="src_claude_rn", vendor="Anthropic", published_at=NOW - timedelta(days=5), title="Claude API 가격·모델 업데이트", summary="Claude Opus 5.5(입력 $4·출력 $20/MTok), Sonnet 5.5($2·$10), Haiku 5.5($0.10·$0.50) 가격표. 분류처럼 짧은 작업은 하위 모델로 비용을 줄일 수 있다.", url="https://platform.claude.com/docs/en/about-claude/pricing", affected_plans=["API"], fetched_at=NOW),
    VendorUpdate(id="upd_chatgpt_data_analysis", source_id="src_chatgpt_rn", vendor="OpenAI", published_at=NOW - timedelta(hours=6), title="ChatGPT Enterprise 데이터 분석 강화: 파일을 올리면 표·차트로 정리", summary="CSV·엑셀·텍스트 파일을 올리면 항목별 집계와 차트를 바로 만들고, 리뷰·설문처럼 자유 서술 데이터는 주제별로 묶어 요약한다. Enterprise 플랜에서 사용 가능.", url="https://help.openai.com/en/articles/8437071-data-analysis-with-chatgpt", affected_plans=["Enterprise"], fetched_at=NOW),
]}

SUGGESTIONS = {s.id: s for s in [
    Suggestion(id="sug_001", update_id="upd_chatgpt_doc_compare", target_type=TargetType.employee, target_id="emp_hrd_01", task_id="task_hrd_survey", relevance="등록 업무 '교육 과정별 설문 결과 비교'가 여러 파일 비교를 필요로 함", body="등록하신 '교육 과정별 설문 결과 비교' 업무에 쓸 수 있는 기능이 추가됐어요. ...", status=SuggestionStatus.draft),
]}

EVAL_RESULTS = [
    EvalResult(id="ev_001", task_type="customer_inquiry_classification", model_id="anthropic:claude-opus-5-5", quality_score=0.95, threshold=0.90, passed=True, avg_input_tokens=600, avg_output_tokens=20, latency_ms=1700, evaluated_at=NOW - timedelta(days=14)),
    EvalResult(id="ev_002", task_type="customer_inquiry_classification", model_id="anthropic:claude-sonnet-5-5", quality_score=0.93, threshold=0.90, passed=True, avg_input_tokens=600, avg_output_tokens=20, latency_ms=850, evaluated_at=NOW - timedelta(days=14)),
    EvalResult(id="ev_003", task_type="customer_inquiry_classification", model_id="anthropic:claude-haiku-5-5", quality_score=0.87, threshold=0.90, passed=False, avg_input_tokens=600, avg_output_tokens=20, latency_ms=350, evaluated_at=NOW - timedelta(days=14)),
    EvalResult(id="ev_004", task_type="customer_inquiry_classification", model_id="xai:grok-4.7", quality_score=0.89, threshold=0.90, passed=False, avg_input_tokens=600, avg_output_tokens=20, latency_ms=600, evaluated_at=NOW - timedelta(days=14)),
]

PROJECTS = {p.id: p for p in [
    Project(id="prj_cs_classify", name="고객 문의 분류", dept_id="dept_cs", owner_id="emp_cs_01", purpose="문의 자동 분류", model_id="anthropic:claude-opus-5-5", environment=Environment.prod, monthly_budget_krw=600000),
    Project(id="prj_quote_summary", name="견적서 요약", dept_id="dept_sales", owner_id="emp_sales_02", purpose="견적서 PDF 요약", model_id="anthropic:claude-opus-5-5", environment=Environment.prod, monthly_budget_krw=800000),
    Project(id="prj_faq", name="사내 FAQ", dept_id="dept_mgmt", owner_id="emp_asset_01", purpose="사내 규정 Q&A", model_id="anthropic:claude-sonnet-5-5", environment=Environment.test, monthly_budget_krw=50000),
]}

PLANS = {p.id: p for p in [
    ActionPlan(id="plan_001", type=PlanType.seat_assign, risk_level=RiskLevel.low, status=PlanStatus.approved, items=[{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], rationale="미배정 좌석 2석 중 1석 배정", evidence=[Evidence(source="inventory", ref="ctr_chatgpt_2026", value={"unassigned": 2})], requested_by="emp_mkt_01", approver_id=None, created_at=NOW - timedelta(minutes=30)),
    ActionPlan(id="plan_002", type=PlanType.seat_reclaim, risk_level=RiskLevel.medium, status=PlanStatus.pending_approval, items=[{"employee_id": "emp_sales_02", "contract_id": "ctr_chatgpt_2026"}], rationale="휴직 중, 본인 확인 응답: 반납", evidence=[Evidence(source="confirmations", ref="cfm_001", value="return_seat")], requested_by="emp_asset_01", created_at=NOW - timedelta(minutes=10)),
]}

CONFIRMATIONS = {c.id: c for c in [
    Confirmation(id="cfm_001", plan_id="plan_002", employee_id="emp_sales_02", question="ChatGPT Enterprise 이용권을 계속 사용하시나요?", answer=ConfirmationAnswer.return_seat, due_at=NOW + timedelta(days=5), answered_at=NOW - timedelta(minutes=15)),
]}

SAVINGS = [SavingsEntry(id="sav_001", plan_id="plan_001", kind=SavingsKind.purchase_avoided, amount_krw=45000 * 2, basis="재배정 1석 × 월 45,000원 × 남은 2개월")]

AUDIT = [AuditEntry(id="aud_001", actor_id="emp_asset_01", actor_role=Role.admin, action="plan.approve", target="plan_001", before={"status": "pending_approval"}, after={"status": "approved"}, created_at=NOW - timedelta(minutes=20))]

EXECUTIONS: dict[str, dict] = {}  # idempotency_key -> 첫 실행 결과
