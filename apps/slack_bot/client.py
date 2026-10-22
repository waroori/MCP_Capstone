"""백엔드·에이전트 HTTP 호출과 SSE 소비. 봇은 요청자 토큰으로만 부른다 (관리자 키 없음)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8002")


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def _call(method: str, base: str, path: str, token: str, headers: dict | None = None, **kw) -> dict:
    r = requests.request(method, base + path, headers={"Authorization": "Bearer " + token, **(headers or {})}, timeout=30, **kw)
    try:
        body = r.json()
    except ValueError as exc:
        raise ApiError("internal", f"HTTP {r.status_code}: {r.text[:200]}") from exc
    if "error" in body:
        raise ApiError(body["error"]["code"], body["error"]["message"])
    if "data" not in body:  # 없는 라우트 등 FastAPI 기본 응답 {"detail": ...}
        raise ApiError("not_found" if r.status_code == 404 else "internal", str(body.get("detail", body))[:200])
    return body["data"]


def backend(method: str, path: str, token: str, **kw) -> dict:
    return _call(method, BACKEND_URL, path, token, **kw)


def agent(method: str, path: str, token: str, **kw) -> dict:
    return _call(method, AGENT_URL, path, token, **kw)


def create_task(token: str, text: str, thread_id: str | None, prompt_name: str | None = None, prompt_args: dict | None = None) -> dict:
    return agent("POST", "/ai/tasks", token, json={"input": text, "thread_id": thread_id, "channel": "slack", "prompt_name": prompt_name, "prompt_args": prompt_args or {}})


def stream_events(token: str, task_id: str, after: int = 0, timeout: int = 180) -> Iterator[dict]:
    """SSE를 읽어 이벤트 dict를 차례로 낸다. 종료 이벤트(DONE·FAILED·APPROVAL_REQUIRED)에서 서버가 닫는다."""
    with requests.get(f"{AGENT_URL}/ai/tasks/{task_id}/stream", params={"after": after},
                      headers={"Authorization": "Bearer " + token}, stream=True, timeout=timeout) as r:
        if r.status_code != 200:
            raise ApiError("internal", f"stream HTTP {r.status_code}: {r.text[:200]}")
        for raw in r.iter_lines():
            line = raw.decode() if isinstance(raw, bytes) else raw
            if line.startswith("data:"):
                yield json.loads(line[5:])


def approve_task(token: str, task_id: str, approved: bool, reason: str) -> dict:
    return agent("POST", f"/ai/tasks/{task_id}/approval", token, json={"approved": approved, "reason": reason})


def feedback(token: str, suggestion_id: str, reaction: str) -> dict:
    return backend("POST", f"/suggestions/{suggestion_id}/feedback", token, json={"reaction": reaction})


def answer_confirmation(token: str, confirmation_id: str, answer: str) -> dict:
    return backend("POST", f"/confirmations/{confirmation_id}/answer", token, json={"answer": answer})
