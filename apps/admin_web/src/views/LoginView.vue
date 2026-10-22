<script setup>
// 로그인. 화면은 backend /auth/login만 부른다 (통신 구조: 브라우저 → /api → backend).
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { login, devLogin, errorLabel, setupStatus } from '../request.js'
import NodiLogo from '../components/NodiLogo.vue'

const route = useRoute()
const router = useRouter()
const loginId = ref('')
const password = ref('')
const showPw = ref(false)
const busy = ref(false)
const error = ref('')
const bad = ref({ id: false, pw: false }) // 오류 문구를 어느 입력칸에 연결할지
const caps = ref(false)
const idEl = ref(null)
const pwEl = ref(null)
const isDev = ref(false) // 개발 화면(npm run dev)이고 서버가 dev-login을 열어 둘 때(APP_ENV≠prod)만 시연 계정 버튼을 보인다
// 아직 가입한 회사가 없으면 '회사 등록' 안내를 띄운다
const noCompany = ref(false)
setupStatus().then((s) => { noCompany.value = !s.company_name; isDev.value = import.meta.env.DEV && s.dev_login }).catch(() => {})
// 개발 서버에서만 보이는 시연 계정 (db/seed/seed.py)
// 페르소나 4명 (docs User Persona)
const DEMO = [
  { id: 'emp_md_01', name: '정서진', role: '실무진 · 상품기획·MD 과장' },
  { id: 'emp_mkt_01', name: '박지후', role: '실무진 · 브랜드 마케팅 대리' },
  { id: 'emp_mgmt_01', name: '윤서연', role: '관리자 · 경영지원팀장' },
  { id: 'emp_asset_01', name: '최광철', role: '관리자 · 자산관리 담당' },
]
const FEATURES = [
  ['업데이트 활용 제안', '새 기능 중 우리 팀 업무에 맞는 것만 골라 알려요'],
  ['업무별 모델 추천', '품질 기준을 통과한 모델 중 비용이 낮은 것을 근거와 함께'],
  ['비용 급증 알림', '사용량이 튀면 원인 후보와 대응안을 먼저 정리해요'],
  ['이용권 운영', '남은 좌석은 바로 배정, 없으면 AI 의견과 함께 승인 대기'],
]

async function done(fn) {
  busy.value = true
  error.value = ''
  try {
    await fn()
    router.replace(route.query.next || '/')
  } catch (e) {
    error.value = e.code === 'unauthorized' || e.code === 'forbidden' ? e.message : errorLabel(e)
    return e
  } finally {
    busy.value = false
  }
}
async function submit() {
  const id = loginId.value.trim()
  bad.value = { id: !id, pw: !password.value }
  if (!id || !password.value) {
    error.value = '사번(또는 이메일)과 비밀번호를 모두 입력하세요'
    ;(id ? pwEl : idEl).value.focus()
    return
  }
  const e = await done(() => login(id, password.value))
  // 계정 잠김(forbidden)·네트워크 오류는 입력값 문제가 아니므로 입력칸을 오류로 표시하지 않는다
  if (e?.code === 'unauthorized') { bad.value = { id: true, pw: true }; pwEl.value.select() }
}
// 오류 문구·Caps Lock 안내를 입력칸에 연결 (스크린리더가 입력칸 포커스 시 함께 읽음)
const describedBy = (...ids) => ids.filter(Boolean).join(' ') || undefined
</script>

<template>
  <div class="login">
    <section class="hero" aria-labelledby="hero-h">
      <div class="brand">
        <NodiLogo :size="30" /><span class="sr-only">nodi</span>
      </div>
      <div class="pitch">
        <span class="big-word" aria-hidden="true">nodi</span>
        <h1 id="hero-h">직무 맞춤 AI 활용과<br />AI·SaaS 자산·비용 관리 <mark>통합 지원</mark></h1>
        <p>개인별 AI 활용 추천부터 이용권·사용량·비용 관리까지. 일하는 사람과 관리하는 팀의 소프트웨어를 하나의 흐름으로 <strong>연결</strong>합니다.</p>
      </div>
      <ol class="features">
        <li v-for="([t, d], i) in FEATURES" :key="t">
          <span class="num" aria-hidden="true">F{{ i + 1 }}</span>
          <div><strong>{{ t }}</strong><span>{{ d }}</span></div>
        </li>
      </ol>
    </section>

    <section class="panel" aria-labelledby="form-h">
      <form class="card form" novalidate @submit.prevent="submit">
        <div class="head">
          <h2 id="form-h">로그인</h2>
          <p class="small muted">사번 또는 회사 이메일로 로그인하세요.</p>
        </div>
        <label class="field">사번 또는 이메일
          <input id="login-id" ref="idEl" v-model="loginId" type="text" autocomplete="username" placeholder="예: emp_mkt_01"
            :aria-invalid="bad.id" :aria-describedby="describedBy(bad.id && error && 'login-err')" required @input="bad.id = false" />
        </label>
        <label class="field">비밀번호
          <span class="pw">
            <input id="login-pw" ref="pwEl" v-model="password" :type="showPw ? 'text' : 'password'" autocomplete="current-password"
              :aria-invalid="bad.pw" :aria-describedby="describedBy(bad.pw && error && 'login-err', caps && 'caps-hint')" required
              @input="bad.pw = false" @keyup="caps = $event.getModifierState?.('CapsLock') ?? false" @blur="caps = false" />
            <button type="button" class="reveal" :aria-pressed="showPw" @click="showPw = !showPw">{{ showPw ? '숨기기' : '보기' }}</button>
          </span>
          <span v-if="caps" id="caps-hint" class="caps" aria-live="polite">Caps Lock이 켜져 있어요</span>
        </label>
        <p v-if="error" id="login-err" class="error" role="alert">{{ error }}</p>
        <button type="submit" class="btn btn-dark submit" :disabled="busy">{{ busy ? '확인하는 중…' : '로그인' }}</button>
        <p class="xs muted">5번 틀리면 5분 동안 잠겨요. 로그인은 1시간 동안 유지됩니다.</p>
        <RouterLink v-if="noCompany" to="/signup" class="btn first">처음 쓰시나요? 회사 등록하기</RouterLink>
      </form>

      <div v-if="isDev && !noCompany" class="demo">
        <span class="xs muted">개발 서버 전용 · 시연 계정으로 바로 들어가기</span>
        <div class="tags">
          <button v-for="d in DEMO" :key="d.id" type="button" class="chip" :disabled="busy" @click="done(() => devLogin(d.id))">
            <strong>{{ d.name }}</strong>&nbsp;<span class="muted">{{ d.role }}</span>
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.login { word-break: keep-all; min-height: 100dvh; display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); background: var(--paper); }
.hero { background: var(--ink); color: var(--white); padding: 40px 56px; display: flex; flex-direction: column; gap: 48px; justify-content: space-between; }
.brand { display: flex; align-items: center; gap: 12px; font-size: 14px; color: var(--muted-on-ink); }
.brand :deep(.word) { color: var(--white); }
.pitch { display: flex; flex-direction: column; gap: 16px; max-width: 640px; word-break: keep-all; }
.pitch h1 { font-size: clamp(28px, 3.4vw, 44px); line-height: 1.35; letter-spacing: -0.02em; word-break: keep-all; }
.pitch mark { background: var(--lime); color: var(--ink); padding: 0.04em 0.2em; border-radius: 6px; box-decoration-break: clone; -webkit-box-decoration-break: clone; }
.pitch p strong { color: var(--white); }
.pitch mark { white-space: nowrap; } /* '통합 지원'이 두 줄로 갈라지지 않게 */
.big-word { font-size: clamp(56px, 7vw, 96px); font-weight: 700; letter-spacing: -0.05em; line-height: 1; color: var(--white); }
.pitch p { color: var(--muted-on-ink); font-size: 16px; line-height: 1.7; max-width: 44ch; margin: 0; }
.features { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.features li { display: flex; gap: 12px; padding: 16px; border-radius: 14px; border: 1px solid rgba(255, 255, 255, 0.12); }
.features li div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.features strong { font-size: 14px; }
.features li span:not(.num) { font-size: 14px; color: var(--muted-on-ink); line-height: 1.5; }
.num { flex: none; width: 30px; height: 22px; border-radius: 6px; background: var(--lime); color: var(--ink); font-size: 11px; font-weight: 700; display: grid; place-items: center; }
.panel { display: flex; flex-direction: column; justify-content: center; gap: 20px; padding: 40px max(24px, 6vw); }
.form { max-width: 420px; width: 100%; gap: 18px; padding: 32px; }
.head { display: flex; flex-direction: column; gap: 4px; }
.head h2 { font-size: 24px; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; }
.field input { min-height: 48px; border-radius: 10px; border: 1px solid var(--line-strong); background: var(--white); padding: 0 14px; font-size: 15px; font-weight: 400; width: 100%; }
.field input:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; border-color: var(--ink); }
.field input[aria-invalid='true'] { border-color: var(--ink); box-shadow: inset 0 0 0 1px var(--ink); }
.pw { position: relative; display: block; }
.pw input { padding-right: 72px; }
.reveal { position: absolute; right: 6px; top: 6px; height: 36px; padding: 0 10px; border: 0; border-radius: 8px; background: var(--paper); font-size: 12px; font-weight: 600; }
.caps { font-size: 12px; font-weight: 600; align-self: flex-start; padding: 2px 8px; border-radius: 6px; background: var(--lime); }
.error { margin: 0; font-size: 13px; padding: 10px 12px; border-radius: 10px; background: var(--ink); color: var(--lime); }
.submit { width: 100%; min-height: 50px; font-size: 15px; }
.first { width: 100%; min-height: 48px; }
.demo { max-width: 420px; width: 100%; display: flex; flex-direction: column; gap: 8px; }
.demo .chip { display: inline-flex; align-items: center; }
@media (max-width: 900px) {
  .login { grid-template-columns: 1fr; }
  /* 모바일은 폼이 첫 화면에 보이도록 소개를 줄이고 기능 카드는 숨긴다 */
  .hero { padding: 24px 16px; gap: 16px; }
  .pitch h1 { font-size: 24px; }
  .big-word { font-size: 48px; }
  .pitch p { display: none; }
  .features { display: none; }
  .panel { padding: 28px 16px 40px; }
  .form { padding: 24px; }
}
</style>
