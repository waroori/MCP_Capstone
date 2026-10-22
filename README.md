# 기업 AI 활용·운영 최적화 에이전트 (Capstone MVP)

설계 문서: `docs/설계문서.md` (v2, 2026-10-21) · 아키텍처: `docs/아키텍처.md` · 추후 개발: `docs/추후개발.md` · 에이전트 설계: `apps/agent_runtime/AGENT_DESIGN.md` · 개발 규칙: `CONTRIBUTING.md`

## 시작하기

```bash
uv sync                       # 전부 설치 (테스트 포함). pip라면: pip install -e ".[dev]"
cp .env.example .env          # OPENAI_API_KEY 채우기 (키 없으면 LLM_MODEL=fake)
scripts/run_all.sh            # 백엔드·MCP 2개·에이전트·관리자 화면을 한 번에. Ctrl+C로 종료
```

통합 실행은 `apps/admin_web`의 Vue 관리자 화면을 사용한다. `scripts/run_all.sh`가
필요하면 Node 의존성을 설치하고 백엔드·MCP·에이전트·Vite를 함께 실행한다.

**관리자 화면은 백엔드(8000)가 떠 있어야 동작한다.** 따로 띄우려면:

```bash
uv run uvicorn apps.backend.main:app --reload --port 8000         # http://localhost:8000/docs
uv run uvicorn apps.agent_runtime.main:app --reload --port 8002   # http://localhost:8002/docs
cd apps/admin_web && npm install && npm run dev                   # http://localhost:5173 (Vue 관리자 콘솔)
uv run pytest                 # 계약 테스트 + 에이전트 테스트 (키 불필요)
uv run python specs/export.py # specs/*.json 갱신
```

에이전트는 `.env`의 `LLM_MODEL`로 모델을 고른다. 값은 `공급사:API 모델명`이다
(기본 `openai:gpt-5.4-mini`, 예: `openai:gpt-5.4-nano`, `xai:grok-4.7`). 공급사별 API 키가 필요하다.
에이전트 작업 이력과 LangGraph checkpoint는 같은 PostgreSQL의 `agent` 스키마(`agent.tasks` 등)에 쌓인다.
키가 없으면 `LLM_MODEL=fake`로 배선만 확인할 수 있다. `MCP_WORK_URL`이 비어 있으면 백엔드 REST를
직접 부르는 로컬 모드다. 설계는 `apps/agent_runtime/AGENT_DESIGN.md`.

## 로그인 (JWT)

- 화면: `http://localhost:5173/login` → 사번 또는 회사 이메일 + 비밀번호. 로그인하지 않으면 모든 화면이 로그인으로 보낸다.
- API: `POST /auth/login {login_id, password}` → `access_token` (HS256, 1시간). 이후 `Authorization: Bearer <token>`.
- 시드 계정 비밀번호는 `db/seed/seed.py`의 `DEMO_PASSWORD` (시스템 계정 `sys`는 로그인 불가). 5번 틀리면 5분 잠금.
- 개발 편의: `APP_ENV=dev`(기본)에서는 `/auth/dev-login`과 `dev.<직원id>` 토큰도 받는다. 로그인 화면의 "시연 계정" 버튼과 테스트가 쓴다. **운영(`APP_ENV=prod`)에서는 막히고 `JWT_SECRET`이 필수다.** 외부 공개(Cloudflare 터널) 때는 prod로 띄운다.
- Slack 봇은 `SYSTEM_TOKEN`으로 `/auth/slack-exchange`를 불러 직원 토큰을 받는다. prod에서는 `SYSTEM_TOKEN`이 JWT여야 한다: `uv run python -m apps.backend.auth`

```bash
curl -s localhost:8000/auth/login -H 'content-type: application/json' -d '{"login_id":"emp_asset_01","password":"<DEMO_PASSWORD>"}'
# 시드 직원: emp_asset_01(관리자) emp_mkt_01·emp_cs_01·emp_hrd_01(직원) sys(시스템)
```

## 로컬 점검 (Cloudflare 공개 전에)

```bash
docker compose up -d postgres   # DB
scripts/run_all.sh              # 서버 전부. 아래는 다른 터미널에서
```

**1. 자동 테스트**: LLM 키 없이 돈다(fake 모델). 승인 대기 → 승인 → 재개 같은 에이전트 흐름도 여기서 확인한다.

```bash
uv run pytest -q                       # 전부
uv run pytest tests/test_agent.py -q   # 에이전트만 (test_agent_mcp.py: MCP 경유, test_relay.py: backend 중계)
```

> ⚠️ 테스트는 로컬 DB를 쓴다. `app` 스키마를 시드로 되돌리고 `agent` 스키마(작업 이력)를 지운다.
> 서버가 떠 있었다면 테스트 뒤 `scripts/run_all.sh`를 다시 켠다 (에이전트가 기동 때 `agent` 스키마를 만든다).

**2. API 직접 호출**: 화면과 같은 경로(`:5173/api` → backend)로 부른다.

```bash
B=localhost:5173/api
TOKEN=$(curl -s $B/auth/login -H 'content-type: application/json' \
  -d '{"login_id":"emp_asset_01","password":"<DEMO_PASSWORD>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')
H="Authorization: Bearer $TOKEN"
curl -s $B/me -H "$H"          # 토큰 주인
curl -s $B/inventory -H "$H"   # 업무 API (관리자). 전체 경로·모델은 http://localhost:8000/docs (호출은 위처럼 헤더를 붙여서)
```

**3. 에이전트(AI 비서)**: `.env`의 `LLM_MODEL` 키가 필요하다(없으면 `LLM_MODEL=fake`). backend `/ai/tasks`가 에이전트로 중계한다.

```bash
TASK=$(curl -s $B/ai/tasks -H "$H" -H 'content-type: application/json' \
  -d '{"input":"이용권 현황 알려줘"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["task_id"])')
curl -sN "$B/ai/tasks/$TASK/stream" -H "$H"   # TOOL_START → TOKEN… → DONE(또는 APPROVAL_REQUIRED·FAILED)에서 끝난다
curl -s $B/ai/tasks/$TASK -H "$H"             # 상태·답변(result)·도구 호출
# APPROVAL_REQUIRED로 멈췄다면: curl -s $B/ai/tasks/$TASK/approval -H "$H" -H 'content-type: application/json' -d '{"approved":true,"reason":"확인"}'
tail -f logs/agent.log                        # LLM·키 오류는 여기
```

**4. 화면**: http://localhost:5173 에서 로그인 → AI 비서·승인함. 여기까지 되면 Cloudflare 터널로 공개한다(`docs/로컬실행.md`).

## DB (PostgreSQL)

```bash
db/reset.sh        # postgres 컨테이너 기동 → db/schema.sql → 시드 적재 (DBeaver: localhost:5432 capstone/capstone, 스키마 app)
uv run pytest      # 테스트도 이 DB를 쓴다. 테스트마다 스키마·시드를 다시 넣는다
```
ERD 원본은 `db/schema.dbml`(dbdiagram.io), 점검 결과는 `db/README.md`.

## 구조

```
packages/schemas/   공유 Pydantic 스키마 = 명세 (common, entities, rest, mcp_tools, agent)
apps/backend/       FastAPI REST API (PostgreSQL 데이터·권한·승인·실행 규칙)
apps/agent_runtime/ LangGraph 에이전트: graph.py(StateGraph) · harness/(8요소) · prompts/ · tasks.py(작업·이벤트) · main.py(/ai/tasks API)
apps/admin_web/     Vue 3 관리자 콘솔 (화면은 backend만 호출, Vite `/api` 프록시 · nginx 이미지). apps/admin_web/README.md
apps/slack_bot/     Slack 봇 (수신) + 발신 메시지 빌더 send.py. apps/slack_bot/README.md
apps/mcp_work, apps/mcp_docs   MCP 서버 (schemas.mcp_tools.TOOLS를 순회해 등록)
db/                스키마(schema.sql)·시드
apps/mock_vendor, jobs/   (추후) Mock 벤더·대기열 배치 — docs/추후개발.md
specs/              export.py가 생성하는 openapi·mcp_tools JSON
tests/              계약 테스트
```

## 포트

| 서비스          | 포트 |
| --------------- | ---- |
| backend         | 8000 |
| mock_vendor     | 8001 |
| agent_runtime   | 8002 |
| mcp_work        | 8010 |
| mcp_docs        | 8011 |
| admin_web (Vue) | 5173 |

## 현재 연결 범위

Vue 화면은 `apps/admin_web/src/api.js`를 통해 백엔드 REST API를 호출한다.

- 로그인(JWT)·로그아웃, 만료·401이면 로그인 화면으로
- 제품·이용권·승인 대기 조회
- 비용 급증 알림 조회 및 예산 검토 요청 생성
- 처리 요청 승인·반려·실행
- 업데이트·제안·팀 활용·미지원 요청 조회
- 팀별 자동 발송 토글은 아직 백엔드 API(`PATCH /departments/{id}`)가 없어 읽기 전용이다.
- AI 비서는 backend `/ai/tasks`가 에이전트로 중계한다(SSE 포함). 화면은 에이전트 주소를 모른다.

## 통신 구조

`docs/diagrams/`(PNG)와 `docs/architecture.drawio`. 요약: 브라우저 → admin_web(`/api/*`) → backend(JWT, 업무 규칙·DB) / agent(LLM·MCP). Slack 봇은 Socket Mode로 받아 backend·agent를 부르고, 제안 발송은 backend 발송함(`notify_outbox`)을 거친다.

## 팀원

| 이름   | 영역              |
| ------ | ----------------- |
| 최승우 | DB + 백엔드, 리드 |
| 이용주 | Agent + 프론트    |
| 유정연 | MCP               |
