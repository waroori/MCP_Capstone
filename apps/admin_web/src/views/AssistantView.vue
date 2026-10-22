<script setup>
// AI 비서: 추천 질문 + 자유 입력 + 진행 스트림. 같은 대화(thread)를 이어간다. ?q= 로 들어오면 바로 묻는다 (비용 화면의 '더 묻기').
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { createTask, getTasks, errorLabel } from '../request.js'
import { getNames, dt } from '../api.js'
import { QUICK, TASK_STATUS, onTabKeys } from '../labels.js'
import TaskStream from '../components/TaskStream.vue'

const route = useRoute()
const text = ref(''), newThread = ref(false), threadId = ref(null), tasks = ref([]), history = ref([]), notice = ref('')

async function ask(q, promptName, promptArgs) {
  const input = (q ?? text.value).trim()
  if (!input) return
  try {
    const c = await createTask(input, { promptName, promptArgs, threadId: newThread.value || promptName ? null : threadId.value })
    threadId.value = c.thread_id
    tasks.value.unshift({ id: c.task_id, key: c.task_id })
    text.value = ''; notice.value = ''
  } catch (e) { notice.value = errorLabel(e) }
}
// 관리자는 '전체 요청'으로 모든 직원의 요청(Slack·웹·시스템)을 본다. 권한은 서버(/ai/tasks?mine=false)가 다시 확인한다.
const isAdmin = (() => { try { return JSON.parse(localStorage.me || '{}').role === 'admin' } catch { return false } })()
const CHANNEL = { slack: 'Slack', admin_ui: '웹', system: '시스템' }
const scope = ref('mine'), channel = ref(''), names = ref(new Map())
async function loadHistory() {
  try { history.value = await getTasks(scope.value === 'mine', scope.value === 'mine' ? 10 : 100) } catch { history.value = [] }
}
function setScope(s) { scope.value = s; loadHistory(); if (s === 'all') getNames().then((n) => (names.value = n)) }
onMounted(() => {
  loadHistory()
  if (route.query.q) ask(String(route.query.q), route.query.prompt, route.query.anomaly ? { anomaly_id: route.query.anomaly } : undefined)
})
</script>

<template>
  <header class="page-head">
    <div><span class="small muted">AI 비서 · 제품 선택·이용권·비용에 관한 요청</span><h1>AI 비서에게 묻기</h1></div>
  </header>
  <section class="card" style="gap: 12px">
    <div class="tags" style="gap: 8px">
      <button v-for="q in QUICK" :key="q" type="button" class="chip" @click="ask(q)">{{ q }}</button>
    </div>
    <form class="tags" style="gap: 8px; align-items: flex-end" @submit.prevent="ask()">
      <label class="ask-field" for="ask-input">요청 내용
        <input id="ask-input" v-model="text" class="ask-input" placeholder="예: 견적서 요약 프로젝트 비용이 왜 늘었는지 조사해줘" />
      </label>
      <label class="small new-thread"><input v-model="newThread" type="checkbox" />새 대화로 시작</label>
      <button type="submit" class="btn btn-dark" :disabled="!text.trim()">보내기</button>
    </form>
    <span class="xs muted">대화: {{ threadId ?? '새 대화' }} · 대화 내용은 저장하지 않고 주제와 요약만 남깁니다.</span>
    <p v-if="notice" class="status" role="status">{{ notice }}</p>
  </section>
  <TaskStream v-for="t in tasks" :key="t.key" :task-id="t.id" @done="loadHistory" @approval="loadHistory" />
  <section class="card" aria-labelledby="hist-h">
    <h2 id="hist-h">{{ scope === 'mine' ? '내 요청' : '전체 요청' }}</h2>
    <div v-if="isAdmin" class="tags" style="gap: 8px; align-items: center">
      <div role="tablist" aria-label="요청 범위" class="segment" @keydown="onTabKeys">
        <button type="button" role="tab" :aria-selected="scope === 'mine'" :tabindex="scope === 'mine' ? 0 : -1" @click="setScope('mine')">내 요청</button>
        <button type="button" role="tab" :aria-selected="scope === 'all'" :tabindex="scope === 'all' ? 0 : -1" @click="setScope('all')">전체 요청</button>
      </div>
      <label v-if="scope === 'all'" class="small">경로
        <select v-model="channel"><option value="">전체</option><option v-for="(l, c) in CHANNEL" :key="c" :value="c">{{ l }}</option></select>
      </label>
    </div>
    <details v-for="t in history.filter((x) => !channel || x.channel === channel)" :key="t.task_id" class="small">
      <summary>{{ dt(t.created_at) }} · <template v-if="scope === 'all'">{{ CHANNEL[t.channel] ?? t.channel }} · {{ names.get(t.requester_id) ?? t.requester_id }} · </template>{{ TASK_STATUS[t.status] ?? t.status }} · {{ t.input.slice(0, 60) }}</summary>
      <p v-if="t.result" style="white-space: pre-wrap; line-height: 1.6">{{ t.result.answer }}</p>
      <p v-if="t.error" style="color: #b00020">{{ t.error }}</p>
    </details>
    <p v-if="!history.length" class="small muted" style="margin: 0">아직 요청이 없습니다.</p>
  </section>
</template>

<style scoped>
.ask-field { flex: 1 1 320px; display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; }
.new-thread { display: flex; align-items: center; gap: 6px; min-height: 44px; }
.ask-input { width: 100%; font-weight: 400; min-height: 44px; padding: 0 12px; border: 1px solid rgba(17, 17, 19, 0.16); border-radius: 10px; background: var(--white); font-size: 14px; }
summary { cursor: pointer; min-height: 36px; display: flex; align-items: center; }
</style>
