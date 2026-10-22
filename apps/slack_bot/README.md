# Slack 봇 (담당: Agent+프론트)

직원 접점. Bolt + Socket Mode라 공개 URL이 필요 없다. 설계는 `docs/설계문서.md` 4·9절, 배포는 `docs/아키텍처.md` 9절(`slack_bot` 서비스, profile `slack`).

| 파일 | 역할 |
| --- | --- |
| `app.py` | 수신: DM·멘션 → 에이전트 작업 → 한 메시지에 진행 상황 갱신 → 답변(자연어만, 내부 정보 비노출). 버튼 3종 처리. 승인 필요 시 관리자 DM(웹 링크). 웹에서 승인되면 폴링으로 감지해 직원 메시지를 이어서 갱신 |
| `worker.py` | 발신 스레드(10초 주기): 미응답 사용 의사 확인 요청 → 직원 DM(버튼), 발송함(notify_outbox) → 직원·부서원 DM(제안+반응 버튼), 웹에서 승인·실행/반려/실패로 끝난 처리 요청 → 요청자(와 좌석 당사자) DM. 결과 알림은 봇이 뜬 뒤 바뀐 건만(과거 건 재알림 없음). F1 한 바퀴(`--run-f1`): 수집 배치 → 검토 안 된 업데이트마다 에이전트 `review_update` → 발송 정책 배치 → Slack 전송 |
| `send.py` | 메시지 형식 5종(제안·확인 요청·승인 요청·알림·답변)의 Block Kit 빌더와 발송 함수 |
| `client.py` | 백엔드·에이전트 HTTP, SSE 소비 |
| `identity.py` | Slack 사용자 → 직원 토큰. 백엔드 `POST /auth/slack-exchange`(시스템 토큰)로 받는다. `SLACK_USER_MAP`의 직원 id를 같이 넘긴다 |

## 준비
`.env`
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_USER_MAP={"U0XXXX":"emp_mkt_01","U0YYYY":"emp_asset_01"}   # Slack 사용자 id → 직원 id (시드의 slack_user_id는 가짜라 이 매핑이 우선)
SLACK_ADMIN_USER_IDS=U0YYYY                                        # 처리 확인 DM을 받을 관리자
```
Slack 앱 설정: Socket Mode, Bot scopes(chat:write, im:history, im:read, im:write, app_mentions:read, channels:read, channels:join, channels:history, users:read), Events(message.im, app_mention, message.channels), Interactivity On, App Home의 Messages Tab On.

## 실행
```bash
uv run python -m apps.slack_bot --check   # 토큰·채널·매핑 점검
uv run python -m apps.slack_bot           # 백엔드(8000)·에이전트(8002)가 떠 있어야 한다
uv run python -m apps.slack_bot --run-f1  # F1 한 바퀴만 돌리고 종료 (시연용). F1_INTERVAL_MIN=30 이면 봇이 30분마다 자동
```
Slack 대상은 `SLACK_USER_MAP` 역매핑으로 찾는다 (시드의 `slack_user_id`는 가짜). 매핑에 없는 직원에게는 보내지 않고 보류한다.
보낸 확인 요청은 `logs/slack_worker_state.json`에 기록해 재시작해도 두 번 보내지 않는다.

## 백엔드와의 접점
- 워커가 쓰는 경로(2026-10-21 추가, 시스템 계정): `GET /confirmations?answered=false`, `GET /notify-outbox`, `PATCH /notify-outbox/{id}/delivered`, `POST /internal/jobs/{collect_release_notes|send_suggestions}/run`.
- 아직 없는 것: `POST /auth/slack-exchange {slack_user_id}` (이메일 기반 자동 연결). 생기면 `identity.py`가 자동으로 그걸 먼저 쓴다.
- HRD 리포트 메일은 범위 밖.
