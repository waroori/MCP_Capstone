"""로그인: 사번·이메일 둘 다, 틀리면 같은 메시지, 5번 틀리면 잠금, JWT로 보호된 API 호출, 만료·위조 토큰 거절."""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient

from apps.backend import auth
from apps.backend.main import app

client = TestClient(app)


def login(login_id: str, password: str):
    return client.post("/auth/login", json={"login_id": login_id, "password": password})


def test_login_by_id_or_email_and_use_token():
    r = login("emp_asset_01", "demo1234")
    assert r.status_code == 200
    token = r.json()["data"]["access_token"]
    assert login("gwangcheol.choi@hanbit.example", "demo1234").status_code == 200
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()["data"]["employee"]
    assert me["id"] == "emp_asset_01" and me["role"] == "admin"


def test_wrong_password_same_message_then_lock():
    auth._fails.clear()
    bad = login("emp_cs_01", "nope").json()["error"]
    unknown = login("nobody", "nope").json()["error"]
    assert bad["code"] == unknown["code"] == "unauthorized" and bad["message"] == unknown["message"]
    for _ in range(4):
        login("emp_cs_01", "nope")
    r = login("emp_cs_01", "demo1234")                       # 5번 틀린 뒤엔 맞아도 잠김
    assert r.status_code == 401 and "잠겼" in r.json()["error"]["message"]
    auth._fails.clear()


def test_system_account_cannot_password_login():
    assert login("sys", "").status_code == 422
    assert login("sys", "anything").status_code == 401


def test_expired_and_forged_tokens_rejected():
    past = datetime.now(UTC) - timedelta(hours=2)
    expired = jwt.encode({"sub": "emp_mkt_01", "iat": past, "exp": past + timedelta(hours=1)}, auth.JWT_SECRET, algorithm="HS256")
    forged = jwt.encode({"sub": "emp_asset_01", "exp": datetime.now(UTC) + timedelta(hours=1)}, "wrong-secret-0123456789abcdefghij", algorithm="HS256")
    r1 = client.get("/me", headers={"Authorization": f"Bearer {expired}"})
    r2 = client.get("/me", headers={"Authorization": f"Bearer {forged}"})
    assert r1.status_code == r2.status_code == 401 and "만료" in r1.json()["error"]["message"]


def test_prod_blocks_dev_paths_but_slack_exchange_works(monkeypatch):
    """APP_ENV=prod: dev 토큰·dev-login은 막히고, Slack 봇은 시스템 JWT로 slack-exchange를 써서 직원 토큰을 받는다."""
    from apps.backend import main
    monkeypatch.setattr(auth, "APP_ENV", "prod")
    monkeypatch.setattr(main, "APP_ENV", "prod")
    assert client.get("/me", headers={"Authorization": "Bearer dev.emp_asset_01"}).status_code == 401
    assert client.post("/auth/dev-login", json={"employee_id": "emp_asset_01"}).status_code == 404

    sys_jwt = {"Authorization": f"Bearer {auth.issue_token(auth.get_employee('sys'), timedelta(days=365))}"}
    r = client.post("/auth/slack-exchange", json={"slack_user_id": "U_REAL", "employee_id": "emp_mkt_01"}, headers=sys_jwt)
    assert r.status_code == 200 and r.json()["data"]["employee"]["id"] == "emp_mkt_01"
    emp_jwt = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    assert client.get("/me", headers=emp_jwt).json()["data"]["employee"]["id"] == "emp_mkt_01"
    by_slack = client.post("/auth/slack-exchange", json={"slack_user_id": "U_MKT01"}, headers=sys_jwt).json()["data"]["employee"]["id"]
    assert by_slack == "emp_mkt_01"  # 매핑이 없으면 직원의 slack_user_id로 찾는다
    assert client.post("/auth/slack-exchange", json={"slack_user_id": "U_NONE"}, headers=sys_jwt).status_code == 404
    assert client.post("/auth/slack-exchange", json={"slack_user_id": "U_MKT01"}, headers=emp_jwt).status_code == 403  # 시스템 계정만


def test_setup_status_tells_ui_whether_dev_login_is_open(monkeypatch):
    from apps.backend import main
    assert client.get("/setup/status").json()["data"]["dev_login"] is True
    monkeypatch.setattr(main, "APP_ENV", "prod")
    assert client.get("/setup/status").json()["data"]["dev_login"] is False
