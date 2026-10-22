# admin_web — 관리자 웹 (Vue 3 + Vite)

와이어프레임(https://claude.ai/artifact/6nPHyWDmv1SGHgmiQZ4EkW)을 옮긴 관리자 화면. **실제 백엔드·에이전트에 연결돼 있다.** 화면은 `src/api.js`만 부르고, 그 안에서 백엔드 응답(`packages/schemas`)을 화면 모양으로 바꾼다.

```bash
cd apps/admin_web
npm install
npm run dev        # http://localhost:5173  (백엔드 8000, 에이전트 8002가 떠 있어야 한다 → scripts/run_all.sh)
npm run build      # dist/
```

| 경로 | 화면 | 기능 |
| --- | --- | --- |
| `/login` | 로그인 (사번·이메일 + 비밀번호, 개발 서버에선 시연 계정 버튼) | 인증 |
| `/signup` | 첫 관리자 가입 (회사가 없을 때만, 로그인 화면에 링크) | 초기 설정 |
| `/setup` | 초기 설정 마법사 겸 회사 설정: 부서·직원 → 구독 제품 → AI 모델·API 프로젝트 → 연간 예산 → 확인. 설정을 마치기 전에는 다른 화면이 이리로 온다 | 초기 설정 |
| `/seats` | 층별 좌석 배치도 (책상은 DB `desks`): 자리마다 쓸 수 있는 도구·이용권 상태·최근 30일 사용일, 오늘 토큰 소비 속도 색·호버·AI 작업 분석 (30초 갱신). **신입 등록**(Slack 회원 ID 선택) → 빈 자리에 앉히기, 자리 옮기기·비우기, 구역에 책상 추가 | F3·F4 |
| `/` | 홈 · 지금 확인할 일 | 전체 |
| `/inbox` | 승인 대기 · AI 의견 · **AI 비서 처리 확인** | 처리 요청 |
| `/licenses` | 구독·이용권 | F4 |
| `/suggestions` | 자동 제안 기록 · 새 모델 카드 뉴스 미리보기 · **[출시 시뮬레이션]** (감지 → AI 카드 작성 진행 표시 → 발송 배치) | F1 |
| `/assistant` | **AI 비서에게 묻기** (진행 스트림, 내 요청) | 전체 |
| `/key/:id` | 직원용 API 키 확인 (Slack 일회용 링크, 사이드바 없음, [키 보기]를 눌러야 한 번만 열림) | F4 |

## 구조
- `src/request.js` — fetch 공통(`/api/*`), 토큰(`POST /auth/login` JWT를 `localStorage.token`에, 없거나 401이면 `/login`), `login`·`devLogin`(개발 서버 전용)·`logout`, SSE 스트리밍(`streamTask`), 에이전트 작업 API.
- `src/api.js` — 화면별 데이터 함수. 함수 이름·반환 모양은 목업 시절과 같다.
- `src/components/TaskStream.vue` — 에이전트 진행 이벤트(TOKEN·TOOL_START/END·APPROVAL_REQUIRED·DONE·FAILED) 표시.
- `src/components/TaskApprovals.vue` — 변경 도구 앞에서 멈춘 작업 확인·거절 → 이어서 처리.
- 프록시: 개발은 `vite.config.js`, 운영은 `nginx.conf.template`. `/api/*` 전부 백엔드로 간다(AI 비서는 백엔드가 에이전트로 중계).

## 배포 (compose)
```yaml
  frontend:
    build: ./apps/admin_web
    ports: ["80:80"]
    environment:
      BACKEND_UPSTREAM: http://backend:8000
    depends_on: [backend]
```

## 백엔드에 요청한 것 (없어서 화면이 비거나 단순화된 곳)
- `GET /projects` — 프로젝트별 비용 표. 지금은 알림 상세에서 아는 프로젝트만 보인다.
- `GET/PATCH /departments` — 팀별 자동 발송 스위치·Slack 채널. 지금은 읽기 전용 목록.
- `GET /usage`의 월 단위 집계 — 홈의 월별 추이는 계약 사용량을 주 단위로 묶어 대신한다.
- `POST /confirmations`의 `plan_id` 선택화 — 지금은 회수 요청을 먼저 만들어 붙인다.

색은 4개만 쓴다: `#111113` · `#f7f8f3` · `#dcef6d` · `#ffffff` (`src/styles.css`의 `:root`).
