"""Slack 사용자 → 직원 토큰.

백엔드 `POST /auth/slack-exchange`를 시스템 토큰(SYSTEM_TOKEN, 서명된 JWT)으로 부른다. APP_ENV=prod에서도 동작한다.
SLACK_USER_MAP(JSON: slack_user_id → employee_id)에 있으면 그 직원으로, 없으면 백엔드가 직원의 slack_user_id로 찾는다.
토큰은 메모리에만 캐시한다.
"""

from __future__ import annotations

import json
import os
import time

from apps.slack_bot.client import ApiError, backend

SYSTEM_TOKEN = os.getenv("SYSTEM_TOKEN", "dev.sys")
_cache: dict[str, tuple[float, str, dict]] = {}  # slack_user_id -> (expires, token, employee)
TTL = 3600


def _user_map() -> dict[str, str]:
    raw = os.getenv("SLACK_USER_MAP", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def resolve(slack_user_id: str) -> tuple[str, dict]:
    """(토큰, 직원) 을 돌려준다. 연결된 직원이 없으면 ApiError(not_found)."""
    hit = _cache.get(slack_user_id)
    if hit and hit[0] > time.time():
        return hit[1], hit[2]
    d = backend("POST", "/auth/slack-exchange", SYSTEM_TOKEN, json={"slack_user_id": slack_user_id, "employee_id": _user_map().get(slack_user_id)})
    _cache[slack_user_id] = (time.time() + TTL, d["access_token"], d["employee"])
    return d["access_token"], d["employee"]


def admin_slack_ids() -> list[str]:
    """관리자 알림을 받을 Slack 사용자. SLACK_ADMIN_USER_IDS(쉼표 구분)."""
    return [s.strip() for s in os.getenv("SLACK_ADMIN_USER_IDS", "").split(",") if s.strip()]
