"""인증. 사번(또는 이메일)+비밀번호로 로그인하면 JWT(HS256, 1시간)를 준다.

개발·테스트 편의로 APP_ENV가 prod가 아니면 'dev.<employee_id>' 토큰도 받는다 (MCP·에이전트 테스트가 쓴다).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, Header

from apps.backend import db
from apps.backend.errors import ApiError
from schemas.common import ErrorCode, Role
from schemas.entities import Employee

PREFIX = "dev."
APP_ENV = os.getenv("APP_ENV", "dev")
JWT_SECRET = os.getenv("JWT_SECRET") or "dev-only-secret-change-me-in-env-0123456789"  # .env에 빈 값(JWT_SECRET=)이어도 기본값
TOKEN_TTL = timedelta(minutes=int(os.getenv("JWT_TTL_MINUTES", "60")))
if APP_ENV == "prod" and JWT_SECRET.startswith("dev-only"):
    raise RuntimeError("APP_ENV=prod에서는 JWT_SECRET을 반드시 설정하세요")

# 같은 아이디로 5번 틀리면 5분 잠금.
# ponytail: 프로세스 메모리라 서버가 여러 대면 공유 안 됨. 그때 Redis/DB 카운터로 옮긴다
MAX_FAILS, LOCK_SECONDS = 5, 300
_fails: dict[str, tuple[int, float]] = {}


def issue_token(employee: Employee, ttl: timedelta = TOKEN_TTL) -> str:
    now = datetime.now(UTC)
    claims = {"sub": employee.id, "role": employee.role.value, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def get_employee(employee_id: str) -> Employee | None:
    r = db.one("SELECT e.*, d.name AS dept_name FROM employees e JOIN departments d ON d.id = e.dept_id WHERE e.id = :id", id=employee_id)
    return Employee(**r) if r else None


def authenticate(login_id: str, password: str) -> Employee:
    """사번 또는 이메일 + 비밀번호. 실패 이유를 구분해 알려주지 않는다(계정 존재 여부 노출 방지)."""
    key = login_id.strip().lower()
    n, until = _fails.get(key, (0, 0.0))
    if until > time.monotonic():
        raise ApiError(ErrorCode.unauthorized, f"로그인 시도가 많아 잠겼습니다. {int(until - time.monotonic()) // 60 + 1}분 뒤 다시 시도하세요")
    r = db.one("SELECT id, password_hash, status FROM employees WHERE lower(id) = :k OR lower(email) = :k", k=key)
    ok = bool(r and r["password_hash"] and bcrypt.checkpw(password.encode(), r["password_hash"].encode()))
    if not ok:
        n += 1
        _fails[key] = (n, time.monotonic() + LOCK_SECONDS if n >= MAX_FAILS else 0.0)
        raise ApiError(ErrorCode.unauthorized, "사번(또는 이메일)이나 비밀번호가 맞지 않습니다")
    _fails.pop(key, None)
    if r["status"] == "resigned":
        raise ApiError(ErrorCode.forbidden, "퇴사 처리된 계정입니다")
    return get_employee(r["id"])


def current_user(authorization: str | None = Header(default=None)) -> Employee:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(ErrorCode.unauthorized, "Authorization: Bearer <token> 헤더가 필요합니다")
    token = authorization.split(" ", 1)[1].strip()
    if token.startswith(PREFIX):
        if APP_ENV == "prod":
            raise ApiError(ErrorCode.unauthorized, "개발용 토큰은 운영 환경에서 쓸 수 없습니다")
        employee_id = token[len(PREFIX):]
    else:
        try:
            employee_id = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["sub", "exp"]})["sub"]
        except jwt.ExpiredSignatureError:
            raise ApiError(ErrorCode.unauthorized, "로그인이 만료됐습니다. 다시 로그인하세요")
        except jwt.InvalidTokenError:
            raise ApiError(ErrorCode.unauthorized, "토큰이 올바르지 않습니다")
    emp = get_employee(employee_id)
    if not emp:
        raise ApiError(ErrorCode.unauthorized, "알 수 없는 직원 토큰입니다")
    return emp


def require(*roles: Role):
    """역할 검사 의존성. 3절 권한 매트릭스를 엔드포인트 단위로 적용한다."""

    def dep(user: Employee = Depends(current_user)) -> Employee:
        if user.role not in roles:
            raise ApiError(
                ErrorCode.forbidden,
                f"{user.role.value} 역할은 이 작업을 할 수 없습니다",
                {"allowed_roles": [r.value for r in roles]},
            )
        return user

    return dep


if __name__ == "__main__":
    # SYSTEM_TOKEN 발급: uv run python -m apps.backend.auth  → 출력을 .env의 SYSTEM_TOKEN= 에 넣는다 (JWT_SECRET이 같아야 한다)
    # ponytail: 1년짜리 토큰이다. 유출되면 JWT_SECRET을 바꿔 모든 토큰을 무효로 하고 다시 발급한다
    print(issue_token(get_employee("sys"), timedelta(days=365)))
