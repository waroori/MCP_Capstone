"""scripts/seed_team.py: 빈 회사에서 셋팅 기능만으로 6명 회사를 만든다 (셋팅 흐름 전체 점검)."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import seed_team  # noqa: E402

from apps.backend import db  # noqa: E402
from apps.backend.main import app  # noqa: E402


def test_team_company_built_through_setup_api():
    seed_team.reset_schema()
    client = TestClient(app)
    out = seed_team.build(client, "dev.sys", "test-pass-1234")
    assert out == {"employees": 6, "admins": ["최승우"], "seats": {"ChatGPT Enterprise": "4/5석", "Acrobat Pro": "2/2석"}}
    rows = {r["name"]: r for r in db.rows("SELECT e.name, e.slack_user_id, e.role, e.job_title, d.name AS dept FROM employees e JOIN departments d ON d.id = e.dept_id")}
    assert rows["최승우"]["slack_user_id"] == seed_team.ADMIN[1] and rows["최승우"]["dept"] == "HRD" and rows["system"]["role"] == "system"  # 가입이 sys도 만든다
    assert {r["slack_user_id"] for r in rows.values()} >= {t[1] for t in seed_team.TEAM}
    assert db.one("SELECT daily_token_quota FROM contracts c JOIN tools t ON t.id = c.tool_id WHERE t.product = 'ChatGPT Enterprise'")["daily_token_quota"] == 150_000
    assert db.one("SELECT count(*) AS n FROM usage_records WHERE employee_id IS NOT NULL")["n"] > 0  # F3 평균 기준
    assert db.one("SELECT flag FROM seat_assignments s JOIN employees e ON e.id = s.employee_id WHERE e.name = '정윤지'")["flag"] == "no_record"
    assert db.one("SELECT count(*) AS n FROM models WHERE approved AND tool_id IS NULL")["n"] == 0  # ChatGPT 좌석 모델도 제품에 묶인다
    login = client.post("/auth/login", json={"login_id": "seungwoo.choi@hanbit.example", "password": "test-pass-1234"})
    assert login.status_code == 200 and login.json()["data"]["employee"]["role"] == "admin"
    sim = client.post("/internal/jobs/collect_release_notes/run?simulate=true", headers={"Authorization": "Bearer dev.sys"})
    assert sim.status_code == 200, sim.text  # Mock 새 모델의 tool_id(시드 기준 tool_chatgpt)가 아니라 이 회사의 제품 id로 들어가야 한다
    assert db.one("SELECT t.product FROM models m JOIN tools t ON t.id = m.tool_id WHERE m.id = 'openai:gpt-5.6'")["product"] == "ChatGPT Enterprise"
