"""에이전트 MCP 모드: 팀원의 업무·문서 MCP 서버(인메모리)를 FastMCP Client로 붙여 그래프가 끝까지 도는지 확인한다."""

import json
import os
import time

os.environ["LLM_MODEL"] = "fake"
os.environ["MCP_WORK_URL"] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402

from apps.agent_runtime.harness import tools as tools_mod  # noqa: E402
from apps.agent_runtime.main import app as agent_app  # noqa: E402
from apps.backend.main import app as backend_app  # noqa: E402
from apps.mcp_docs.server import mcp as docs_mcp  # noqa: E402
from apps.mcp_work import auth as mcp_auth  # noqa: E402
from apps.mcp_work import backend as mcp_backend  # noqa: E402
from apps.mcp_work.server import mcp as work_mcp  # noqa: E402

SERVERS = {"work": work_mcp, "docs": docs_mcp}


def factory(server_key: str, token: str) -> Client:
    os.environ["MCP_DEV_TOKEN"] = token  # 인메모리 연결은 헤더가 없어 MCP 서버가 이 토큰을 쓴다
    mcp_auth.clear_cache()
    return Client(SERVERS[server_key])


@pytest.fixture(scope="module")
def client():
    mcp_backend.use_transport(httpx.ASGITransport(app=backend_app))
    tools_mod.TRANSPORT_OVERRIDE = httpx.ASGITransport(app=backend_app)
    tools_mod.MCP_CLIENT_FACTORY = factory
    tools_mod._MCP_CACHE.clear()
    with TestClient(agent_app) as c:
        yield c
    tools_mod.MCP_CLIENT_FACTORY = None
    tools_mod._MCP_CACHE.clear()


def events(c, task_id, headers, after=0):
    out = []
    with c.stream("GET", f"/ai/tasks/{task_id}/stream", params={"after": after}, headers=headers, timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:]))
    return out


def wait_done(c, task_id, headers, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"/ai/tasks/{task_id}", headers=headers).json()["data"]
        if t["status"] in ("done", "failed", "waiting_approval"):
            return t
        time.sleep(0.1)
    raise AssertionError("timeout")


ASSET = {"Authorization": "Bearer dev.emp_asset_01"}
EMP = {"Authorization": "Bearer dev.emp_mkt_01"}


def test_tools_come_from_mcp_and_are_role_filtered(client):
    import asyncio

    from schemas.common import Role

    asset_tools = asyncio.run(tools_mod.load_tools("dev.emp_asset_01", Role.admin))
    emp_tools = asyncio.run(tools_mod.load_tools("dev.emp_mkt_01", Role.employee))
    names_a, names_e = {t.name for t in asset_tools}, {t.name for t in emp_tools}
    assert "execute_approved_plan" in names_a and "execute_approved_plan" not in names_e
    assert "search_vendor_docs" in names_a  # 문서 MCP도 붙는다


def test_execute_via_mcp_with_approval(client):
    """승인된 조치안(plan_001) 실행 요청 → MCP의 execute_approved_plan 앞에서 interrupt → 승인 → 실행."""
    task_id = client.post("/ai/tasks", json={"input": "plan_001 실행해줘"}, headers=ASSET).json()["data"]["task_id"]
    evs = events(client, task_id, ASSET)
    assert [e["type"] for e in evs] == ["APPROVAL_REQUIRED"]
    assert wait_done(client, task_id, ASSET)["status"] == "waiting_approval"
    assert client.post(f"/ai/tasks/{task_id}/approval", json={"approved": True, "reason": "ok"}, headers=ASSET).status_code == 202
    evs2 = events(client, task_id, ASSET, after=1)
    assert evs2[0]["tool_call"]["name"] == "execute_approved_plan" and evs2[-1]["type"] == "DONE"
    exec_out = json.loads(evs2[1]["tool_call"]["output"])
    assert exec_out["status"] == "executed" and evs2[1]["tool_call"]["error"] is None


def test_seat_assign_via_mcp_waits_for_admin(client):
    from apps.backend import db
    with db.tx() as c:  # 좌석을 꽉 채워 승인 대기로 들어가게 한다 (남으면 바로 배정)
        c.execute(db.text("UPDATE contracts SET seats_purchased = (SELECT count(*) FROM seat_assignments WHERE contract_id = 'ctr_chatgpt_2026' AND status = 'assigned') WHERE id = 'ctr_chatgpt_2026'"))
    task_id = client.post("/ai/tasks", json={"input": "박지후 님 ChatGPT Enterprise 이용권 배정해줘"}, headers=ASSET).json()["data"]["task_id"]
    evs = events(client, task_id, ASSET)
    ends = [e for e in evs if e["type"] == "TOOL_END"]
    assert [e["tool_call"]["name"] for e in ends] == ["create_action_plan"] and evs[-1]["type"] == "DONE"
    assert json.loads(ends[0]["tool_call"]["output"])["status"] == "pending_approval"


def test_employee_seat_request_through_mcp(client):
    """좌석이 남아 있으면 신청 즉시 시스템이 배정한다."""
    task_id = client.post("/ai/tasks", json={"input": "이용권 신청", "channel": "slack"}, headers=EMP).json()["data"]["task_id"]
    evs = events(client, task_id, EMP)
    ends = [e for e in evs if e["type"] == "TOOL_END"]
    assert [e["tool_call"]["name"] for e in ends] == ["create_action_plan"] and evs[-1]["type"] == "DONE"
    assert json.loads(ends[0]["tool_call"]["output"])["status"] == "executed"
