import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'
import { loggedIn, setupStatus } from './request.js'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/LoginView.vue'), meta: { title: '로그인', public: true } },
  { path: '/signup', name: 'signup', component: () => import('./views/SignupView.vue'), meta: { title: '회사 등록', public: true } },
  { path: '/setup', name: 'setup', component: () => import('./views/SetupView.vue'), meta: { title: '설정' } },
  { path: '/', name: 'home', component: () => import('./views/HomeView.vue'), meta: { title: '홈' } },
  { path: '/inbox', name: 'inbox', component: () => import('./views/InboxView.vue'), meta: { title: '승인 대기' } },
  { path: '/licenses', name: 'licenses', component: () => import('./views/LicensesView.vue'), meta: { title: '구독·이용권' } },
  { path: '/seats', name: 'seats', component: () => import('./views/SeatsView.vue'), meta: { title: '좌석 배치도' } },
  { path: '/suggestions', name: 'suggestions', component: () => import('./views/SuggestionsView.vue'), meta: { title: '자동 제안 기록' } },
  { path: '/assistant', name: 'assistant', component: () => import('./views/AssistantView.vue'), meta: { title: 'AI 비서' } },
  { path: '/key/:id', name: 'key', component: () => import('./views/KeyView.vue'), meta: { title: 'API 키 확인', bare: true } }, // 직원용: 사이드바 없이
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes, scrollBehavior: () => ({ top: 0 }) })
// 로그인하지 않았으면 /login으로. 로그인했는데 /login이면 원래 가려던 곳으로
// 초기 설정을 마치지 않았으면 설정 마법사로. 끝났다고 확인되면 다시 묻지 않는다.
let setupDone = false
router.beforeEach(async (to) => {
  if (!to.meta.public && !loggedIn()) return { name: 'login', query: { next: to.fullPath } }
  if (to.name === 'login' && loggedIn()) return to.query.next || '/'
  if (!to.meta.public && to.name !== 'setup' && !setupDone) {
    setupDone = await setupStatus().then((s) => s.setup_completed).catch(() => true) // 확인 실패로 화면을 막지는 않는다
    if (!setupDone) return { name: 'setup' }
  }
})
router.afterEach((to) => { document.title = `${to.meta.title} · nodi` })

createApp(App).use(router).mount('#app')
