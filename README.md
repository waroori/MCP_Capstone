# 기업 AI 활용·운영 최적화 에이전트

기업에서 사용하는 AI 제품·좌석·비용·사용량을 한 곳에서 관리하고, AI
에이전트와 Slack으로 업무를 처리하는 Capstone MVP입니다.

## 빠른 시작

### 처음 한 번

필요한 도구:

- Docker Desktop
- Python 3.12 이상
- [uv](https://docs.astral.sh/uv/)
- Node.js/npm
- 외부 공개 시 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

Docker만 있으면 한 번에 (DB 스키마·시드도 처음 한 번 자동):

```bash
cp .env.example .env          # OPENAI_API_KEY 채우기 (키 없으면 LLM_MODEL=fake)
docker compose up -d --build  # DB·백엔드·MCP 2개·에이전트·화면 → http://localhost:5173
# Slack 봇: --profile slack · 외부 공개(Cloudflare 고정 주소): --profile public  (docs/로컬실행.md)
```

코드를 고치며 바로 반영해 보는 개발 모드:

```bash
uv sync
cp .env.example .env
```

`.env`에 사용할 LLM API 키를 넣습니다. 키 없이 화면과 API 연결만 확인하려면:

```dotenv
LLM_MODEL=fake
```

### 외부 공개·전체 실행

공개 시연은 아래 한 줄만 실행합니다.

```bash
scripts/run_public.sh
```

스크립트가 다음을 자동으로 처리합니다.

1. Docker PostgreSQL 실행 및 health check
2. 스키마가 없으면 `db/schema.sql`과 시드 데이터 자동 적용
3. backend, agent runtime, MCP 서버, 관리자 화면 실행
4. Slack bot 실행
5. Cloudflare Quick Tunnel 실행
6. 고정 Workers 주소의 API 연결 대상 갱신 시도

화면과 터널 주소는 실행 터미널 또는 로그에서 확인합니다.

```bash
tail -f logs/cloudflare.log
```

브라우저에서 `https://<발급된-주소>.trycloudflare.com` 또는 설정된
Cloudflare Workers 주소로 접속합니다.

> Cloudflare가 DB에 직접 접속하는 구조는 아닙니다.
>
> `Cloudflare Worker → Quick Tunnel → admin_web → backend → Docker PostgreSQL`
> 순서로 연결됩니다.

### 로컬 개발 실행

외부 공개 없이 개발할 때는 다음을 사용합니다.

```bash
docker compose up -d --wait postgres
scripts/run_all.sh
```

MCP 서버 없이 backend REST를 직접 사용하는 모드:

```bash
MCP=0 scripts/run_all.sh
```

Docker PostgreSQL만 실행하려면:

```bash
docker compose up -d
```

### 중지

`run_public.sh` 또는 `run_all.sh`를 실행한 터미널에서 `Ctrl+C`를 누릅니다.
PostgreSQL까지 중지하려면:

```bash
docker compose down
```

## 현재 기능

### 관리자 웹

관리자 화면은 `apps/admin_web`의 Vue 3 애플리케이션입니다.

시드 데이터에는 관리자 시연 계정이 포함되어 있습니다.

```text
아이디: test
비밀번호: demo1234
```

| 경로 | 기능 |
| --- | --- |
| `/login` | 사번 또는 회사 이메일 로그인 |
| `/setup` | 회사·부서·직원·좌석·구독 제품·AI 모델·예산 초기 설정 |
| `/` | 홈, 지금 확인할 일 |
| `/inbox` | 승인 대기, AI 의견, 처리 요청 확인 |
| `/cost/vendors` | 벤더·모델·프로젝트별 비용과 예산 |
| `/cost/teams` | 팀별 사용량, 층별 좌석 배치도, 사용량 경보 |
| `/licenses` | 구독 제품과 이용권 |
| `/suggestions` | 자동 제안, 모델 업데이트 카드, 출시 시뮬레이션 |
| `/assistant` | AI 비서 대화, 진행 스트림, 내 요청 |
| `/needs` | 미도입 제품 신청과 구매 검토 요청 |
| `/key/:id` | 직원용 API 키 확인 |

주요 화면 기능:

- JWT 로그인과 권한별 화면·API 접근
- 구독 제품, 이용권, 좌석 배치와 직원 관리
- 벤더·팀·모델·프로젝트별 비용 및 사용량 조회
- 토큰 소비 속도와 예산 초과 경보
- 사용 의사 확인, 좌석 회수, 예산 검토 요청
- AI 에이전트의 토큰·도구 호출·승인 대기 진행 표시
- Slack에서 발생한 요청과 웹 요청 이력 통합 조회

### AI 에이전트

`apps/agent_runtime`은 LangGraph 기반 에이전트입니다.

- 자연어 요청을 분류하고 적절한 업무 도구 선택
- backend REST 또는 MCP 도구 호출
- 조회 작업과 변경 작업 분리
- 변경 전 사용자 승인 대기
- SSE로 TOKEN, TOOL_START, APPROVAL_REQUIRED, DONE, FAILED 이벤트 전송
- PostgreSQL `agent` 스키마에 작업·이벤트·checkpoint 저장

예시 요청:

- “이번 달 이용권 현황 알려줘”
- “사용량이 많은 팀을 찾아줘”
- “이 좌석을 회수해줘” → 승인 후 실행

### backend API

`apps/backend`는 FastAPI 기반 업무 API입니다.

- PostgreSQL 기반 회사·부서·직원·좌석·계약·이용량 관리
- JWT 인증, 역할 기반 권한, 로그인 실패 잠금
- 제품·이용권·비용·사용량·승인·제안 API
- AI 작업 생성·상태 조회·SSE 중계·승인 API
- Slack 시스템 토큰 교환 API
- OpenAPI 문서: `http://localhost:8000/docs`

### Slack bot

`apps/slack_bot`은 Slack Socket Mode로 메시지를 받고 backend와 agent를
호출합니다.

- Slack 사용자와 사내 직원 ID 연결
- AI 작업 생성 및 결과 전달
- 승인 필요한 작업의 승인 흐름 연결
- F1 자동 제안 발송

## 디렉터리

```text
apps/
├── backend/          FastAPI 업무 API, 인증, PostgreSQL 접근
├── agent_runtime/    LangGraph 에이전트와 작업·이벤트 API
├── admin_web/        Vue 3 관리자 웹, Vite, Cloudflare Worker
├── mcp_work/         업무 데이터 MCP 서버
├── mcp_docs/         사내 문서 MCP 서버
├── slack_bot/        Slack Socket Mode bot과 발송 worker
└── mock_vendor/      외부 벤더 연동용 mock 영역

packages/schemas/     backend·agent·MCP·frontend가 공유하는 Pydantic 스키마
db/
├── schema.sql        PostgreSQL 스키마
├── schema.dbml       ERD 원본
├── seed/seed.py      초기 데이터 생성
└── reset.sh          DB를 초기 상태로 재생성
scripts/
├── run_public.sh     DB부터 Cloudflare까지 외부 공개 전체 실행
├── run_all.sh        로컬 서비스 전체 실행
└── tunnel.sh         터널만 다시 연결
specs/                OpenAPI·MCP 도구 명세와 생성 스크립트
tests/                계약·인증·DB·에이전트·MCP·Slack 테스트
docs/                 설계·아키텍처·로컬 실행·추후 개발 문서
logs/                 서비스별 실행 로그
```

## 서비스와 포트

| 서비스 | 포트 | 설명 |
| --- | ---: | --- |
| PostgreSQL | 5432 | Docker DB |
| backend | 8000 | FastAPI 업무 API |
| mock_vendor | 8001 | 외부 벤더 mock 영역 |
| agent_runtime | 8002 | AI 에이전트 API |
| mcp_work | 8010 | 업무 MCP |
| mcp_docs | 8011 | 문서 MCP |
| admin_web | 5173 | Vue 개발 서버 |

## DB 운영

평소에는 `scripts/run_public.sh`가 PostgreSQL을 켜고, DB가 비어 있을 때만
스키마와 시드 데이터를 자동으로 넣습니다. `db/reset.sh`를 매번 실행할
필요는 없습니다.

```bash
# 데이터 초기화가 필요할 때만 사용: 기존 데이터 삭제
db/reset.sh
```

기본 접속 정보:

```text
Host: localhost
Port: 5432
Database: capstone
User: capstone
Password: .env의 POSTGRES_PASSWORD
```

테스트는 개발 DB와 분리된 `capstone_test`를 사용합니다.

## 테스트와 명세

```bash
uv run pytest -q
uv run pytest tests/test_agent.py -q
uv run python specs/export.py
```

LLM 키 없이도 대부분의 테스트를 실행할 수 있습니다.

## 로그와 문제 해결

```bash
tail -f logs/backend.log
tail -f logs/agent.log
tail -f logs/admin_web.log
tail -f logs/cloudflare.log
```

Cloudflare 화면에서 `로컬 서버(터널)에 연결하지 못했습니다`가 표시되면
로컬 서비스는 살아 있고 터널만 끊긴 경우가 많습니다.

```bash
scripts/tunnel.sh
```

처음부터 다시 시작할 때는 기존 실행을 `Ctrl+C`로 종료한 뒤 다음을
실행합니다.

```bash
scripts/run_public.sh
```

## 팀원 및 기여

| 이름 | 역할 | 주요 기여 |
| --- | --- | --- |
| 채석현 | R1 - PM·PO / R5 - Frontend·Demo | 서비스 기획, HRD 관리자·실무진 페르소나 및 사용자 시나리오 작성, 관리자 웹 UI·UX 디자인, 시연 영상 촬영 |
| 정서현 | R1 - PM·PO / R6 - QA·Eval | 서비스 기획, Scrum Master, 최종 발표 |
| 정윤지 | R1 - PM·PO | 서비스 기획, PM(일정·범위 관리) |
| 유정연 | R4 - Backend·Tool / R6 - QA·Eval | 업무·문서 MCP 서버 구현(도구 22개·3개)과 MCP Prompt·정책 정리, 하네스 입력 경계(기능과 무관한 요청 거부), `docker compose up -d` 한 번 실행 구성, QA 검증 |
| 이용주 | R2 - AI Engineer(흐름) / R5 - Frontend·Demo | Slack 봇 워커(F1 자동 제안·사용 의사 확인 DM)와 승인 결과 답장, 관리자 웹 화면(비용·사용량, 좌석 패널, 해결책 선택 카드), F1 새 모델 시뮬레이션 보정, 회수 검증 보강 |
| 최승우 (개발 리드) | R4 - Backend·Tool | DB 스키마·시드·ERD, JWT 로그인·권한, FastAPI backend와 `/ai/tasks` 중계, PostgreSQL agent 스키마, Docker·Cloudflare 실행·배포 스크립트, F1 새 모델 카드 뉴스, F3·F4 흐름 |

## 참고 문서

- [로컬 실행](docs/로컬실행.md)
- [아키텍처](docs/아키텍처.md)
- [설계 문서](docs/설계문서.md)
- [에이전트 설계](apps/agent_runtime/AGENT_DESIGN.md)
- [데이터베이스 README](db/README.md)
- [관리자 웹 README](apps/admin_web/README.md)
- [기여 규칙](CONTRIBUTING.md)
