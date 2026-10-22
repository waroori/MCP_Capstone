<script setup>
import { ref, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getCounts, getMe } from './api.js'
import { loggedIn, logout, setupStatus } from './request.js'
import NodiLogo from './components/NodiLogo.vue'

const route = useRoute()
const router = useRouter()
const me = ref({ name: '', role: '' })
const company = ref('')
setupStatus().then((s) => (company.value = s.company_name ?? '')).catch(() => {})
const counts = ref({ inbox: 0, seats: 0 })
const mainEl = ref(null)

const groups = [
  { label: '운영', items: [
    { to: '/', label: '홈', icon: ['M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z'] },
    { to: '/inbox', label: '승인 대기', badge: 'inbox', icon: ['M4 13l2-8h12l2 8', 'M4 13v6h16v-6h-5l-1 2h-4l-1-2z'] },
    { to: '/licenses', label: '구독·이용권', icon: ['M5 5h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z', 'M7 10h4M7 14h7'] },
    { to: '/seats', label: '좌석 배치도', badge: 'seats', icon: ['M3 4h18v16H3z', 'M3 10h18M10 10v10'] },
  ] },
  { label: '활용 지원', items: [
    { to: '/suggestions', label: '자동 제안 기록', icon: ['M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z', 'M9 11h6M9 14h4'] },
  ] },
  { label: '관리', items: [
    { to: '/setup', label: '회사 설정', icon: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'] },
  ] },
]

const signOut = () => { logout(); me.value = { name: '', role: '' }; router.push('/login') }
// 처리 요청을 승인·생성하면 배지가 바뀌므로 화면을 옮길 때마다 다시 센다. 로그인 화면에서는 부르지 않는다.
watch(() => route.fullPath, async (_, prev) => {
  if (route.meta.public || route.meta.bare || !loggedIn()) return
  if (!me.value.employee) getMe().then((v) => (me.value = v)).catch(() => {})
  counts.value = await getCounts()
  if (prev) { await nextTick(); mainEl.value?.focus() }
}, { immediate: true })
</script>

<template>
  <RouterView v-if="route.meta.public || route.meta.bare" />
  <template v-else>
  <a class="skip" href="#main">본문으로 건너뛰기</a>
  <div class="layout">
    <nav class="sidebar" aria-label="관리자 메뉴">
      <div class="brand">
        <NodiLogo :size="28" :wordmark="false" />
        <div><strong>nodi</strong><span>{{ company }}</span></div>
      </div>

      <div v-for="g in groups" :key="g.label" class="group">
        <span class="group-label">{{ g.label }}</span>
        <RouterLink v-for="it in g.items" :key="it.to" :to="it.to" class="nav-item">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path v-for="d in it.icon" :key="d" :d="d" /></svg>
          <span class="nav-label">{{ it.label }}</span>
          <span v-if="it.badge && counts[it.badge]" class="badge">
            {{ counts[it.badge] }}<span class="sr-only">건</span>
          </span>
        </RouterLink>
      </div>

      <div class="foot">
        <RouterLink to="/assistant" class="btn btn-primary ask">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" /></svg>
          AI 비서에게 묻기
        </RouterLink>
        <div class="me">
          <div class="avatar" aria-hidden="true">{{ me.name.slice(0, 1) }}</div>
          <div><strong>{{ me.name }}</strong><span>{{ me.role }}</span></div>
          <button type="button" class="signout" @click="signOut" aria-label="로그아웃" title="로그아웃">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4" /><path d="M10 17l-5-5 5-5M5 12h11" /></svg>
          </button>
        </div>
      </div>
    </nav>

    <main id="main" ref="mainEl" class="main" tabindex="-1">
      <RouterView />
    </main>
  </div>
  </template>
</template>

<style scoped>
.signout { margin-left: auto; width: 36px; height: 36px; border-radius: 8px; border: 0; background: transparent; color: var(--muted-on-ink); display: grid; place-items: center; }
.signout:hover { background: rgba(255, 255, 255, 0.08); color: var(--white); }
.skip { position: absolute; left: 16px; top: -48px; z-index: 10; padding: 10px 14px; border-radius: 10px; background: var(--lime); font-weight: 700; }
.skip:focus { top: 16px; }
.main:focus { outline: none; }
.sidebar { background: var(--ink); color: var(--white); padding: 24px 16px; display: flex; flex-direction: column; gap: 28px; position: sticky; top: 0; height: 100dvh; overflow-y: auto; }
.brand { display: flex; align-items: center; gap: 10px; padding: 0 8px; }
.brand div:last-child, .me > div:nth-child(2) { display: flex; flex-direction: column; min-width: 0; }
.brand strong { font-size: 17px; letter-spacing: -0.03em; }
.brand span, .me span { font-size: 12px; color: var(--muted-on-ink); }
.group { display: flex; flex-direction: column; gap: 4px; }
.group-label { font-size: 12px; font-weight: 500; color: rgba(255, 255, 255, 0.56); padding: 0 12px 6px; }
.nav-item { display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 0 12px; border-radius: 10px; text-decoration: none; font-size: 14px; font-weight: 500; color: rgba(255, 255, 255, 0.8); }
.nav-item:hover { background: rgba(255, 255, 255, 0.08); color: var(--white); }
.nav-item.router-link-exact-active { background: var(--lime); color: var(--ink); }
.nav-label { flex: 1; }
.badge { min-width: 22px; height: 22px; padding: 0 7px; border-radius: 999px; font-size: 12px; font-weight: 700; display: grid; place-items: center; background: var(--lime); color: var(--ink); }
.router-link-exact-active .badge { background: var(--ink); color: var(--lime); }
.foot { margin-top: auto; display: flex; flex-direction: column; gap: 12px; }
.ask { width: 100%; }
.me { display: flex; align-items: center; gap: 10px; padding: 12px 8px 0; border-top: 1px solid rgba(255, 255, 255, 0.12); }
.me strong { font-size: 13px; }
.avatar { width: 32px; height: 32px; border-radius: 999px; background: var(--white); color: var(--ink); font-size: 13px; font-weight: 700; display: grid; place-items: center; }
@media (max-width: 900px) {
  .sidebar { position: static; height: auto; gap: 16px; }
  .group { flex-direction: row; flex-wrap: wrap; }
  .group-label { width: 100%; padding-bottom: 0; }
  .foot { flex-direction: row; flex-wrap: wrap; align-items: center; }
  .ask { width: auto; }
  .me { border-top: 0; padding: 0; }
}
</style>
