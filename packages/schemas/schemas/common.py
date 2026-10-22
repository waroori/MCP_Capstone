from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover
        return str(self.value)


# ---------- 역할·권한 (3절) ----------
class Role(StrEnum):
    """v2: 직원·관리자·시스템. 관리자는 자산·보안·승인 권한을 함께 가진다 (docs/설계문서.md 3절)."""

    employee = "employee"
    admin = "admin"
    system = "system"


# ---------- 오류 (7절 공통 규칙) ----------
class ErrorCode(StrEnum):
    unauthorized = "unauthorized"  # 토큰 없음/잘못됨 (401)
    forbidden = "forbidden"  # 역할 불가 (403)
    not_found = "not_found"  # 404
    invalid = "invalid"  # 입력 검증 실패 (422)
    conflict = "conflict"  # 상태 충돌 (409)
    stale = "stale"  # 승인 뒤 상태가 바뀌어 실행 중단 (409)
    internal = "internal"  # 500
    agent_unavailable = "agent_unavailable"  # backend 중계가 에이전트에 연결 못 함 (503)


HTTP_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.unauthorized: 401,
    ErrorCode.forbidden: 403,
    ErrorCode.not_found: 404,
    ErrorCode.invalid: 422,
    ErrorCode.conflict: 409,
    ErrorCode.stale: 409,
    ErrorCode.internal: 500,
    ErrorCode.agent_unavailable: 503,
}


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: dict | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class Meta(BaseModel):
    request_id: str


T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """모든 성공 응답은 이 모양이다: {"data": ..., "meta": {"request_id": "..."}}"""

    data: T
    meta: Meta


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int = 50
    offset: int = 0


# ---------- 공통 enum (6절 데이터 모델) ----------
class EmployeeStatus(StrEnum):
    active = "active"
    on_leave = "on_leave"
    resigned = "resigned"


class ToolApprovalStatus(StrEnum):
    approved = "approved"
    under_review = "under_review"
    denied = "denied"


class DataClass(StrEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"


class BillingType(StrEnum):
    seat_flat = "seat_flat"  # 정액 좌석제: 모델 선택이 청구액을 바꾸지 않음
    seat_plus_usage = "seat_plus_usage"  # 좌석 + 사용량
    api_usage = "api_usage"


class TaskSource(StrEnum):
    manual = "manual"
    bot = "bot"


class SourceKind(StrEnum):
    release_notes = "release_notes"
    pricing = "pricing"
    docs = "docs"


class TargetType(StrEnum):
    employee = "employee"
    team = "team"
    all = "all"


class SuggestionStatus(StrEnum):
    draft = "draft"
    pending_approval = "pending_approval"
    sent = "sent"
    on_hold = "on_hold"


class Reaction(StrEnum):
    useful = "useful"
    irrelevant = "irrelevant"
    question = "question"


class Environment(StrEnum):
    test = "test"
    prod = "prod"


class SeatStatus(StrEnum):
    assigned = "assigned"
    unassigned = "unassigned"


class Granularity(StrEnum):
    hour = "hour"
    day = "day"


class UsageScope(StrEnum):
    employee = "employee"
    project = "project"
    dept = "dept"
    contract = "contract"


class BreakdownBy(StrEnum):
    model = "model"
    api_key = "api_key"
    input_length = "input_length"


class PaceLevel(StrEnum):
    """좌석 토큰 소비 속도: 오늘 예상량 ÷ 같은 제품 좌석 평균 하루 사용량"""
    normal = "normal"  # 1.5배 미만
    caution = "caution"  # 1.5배 이상
    over = "over"  # 2배 이상 → 경보


class AnomalyStatus(StrEnum):
    open = "open"
    resolved = "resolved"


class PlanType(StrEnum):
    seat_assign = "seat_assign"
    seat_reclaim = "seat_reclaim"
    api_access = "api_access"  # v2.1: 사용량 과금 제품(API)의 사용 신청 → 승인 뒤 프로젝트 생성
    model_change = "model_change"
    budget_review = "budget_review"
    purchase_review = "purchase_review"


class PlanStatus(StrEnum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    executed = "executed"
    failed = "failed"
    rejected = "rejected"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


# 11절 승인 기준. 백엔드가 create_action_plan 시 risk_level을 자동 산정할 때 쓴다.
# v2.1: 에이전트가 만드는 처리 요청은 전부 관리자 승인을 거친다 (low는 제안 발송 같은 자동 알림에만 쓴다).
RISK_BY_PLAN_TYPE: dict[PlanType, RiskLevel] = {
    PlanType.seat_assign: RiskLevel.medium,
    PlanType.seat_reclaim: RiskLevel.medium,
    PlanType.api_access: RiskLevel.medium,
    PlanType.model_change: RiskLevel.medium,  # prod 환경이면 high (items.environment 기준)
    PlanType.budget_review: RiskLevel.medium,
    PlanType.purchase_review: RiskLevel.high,  # 요청만 생성, 실행 안 함
}


class ConfirmationAnswer(StrEnum):
    still_needed = "still_needed"
    return_seat = "return_seat"
    on_leave = "on_leave"


class SavingsKind(StrEnum):
    purchase_avoided = "purchase_avoided"  # 구매 회피
    renewal_reduced = "renewal_reduced"  # 갱신 절감
    billing_reduced = "billing_reduced"  # 청구 감소
    projected = "projected"  # 예상 절감


class LicenseFlag(StrEnum):
    """저사용 후보 분류. 기록 없음은 '미사용'이 아니라 '확인 필요'다."""

    unused = "unused"
    on_leave = "on_leave"
    no_record = "no_record"  # 확인 필요
    recent_hire = "recent_hire"


# ---------- 공통 값 객체 ----------
class Period(BaseModel):
    start: date
    end: date


class Money(BaseModel):
    krw: int = Field(description="원화 정수")
    usd: float = Field(description="달러 소수")


class Evidence(BaseModel):
    """에이전트가 조치안·답변에 붙이는 근거 한 건."""

    source: str = Field(description="예: usage_records, vendor_update, eval_results")
    ref: str = Field(description="예: 레코드 id, URL")
    value: object | None = None


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=False)


__all__ = [n for n in dir() if not n.startswith("_")]
