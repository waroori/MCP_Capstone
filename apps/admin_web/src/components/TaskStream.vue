<script setup>
// 에이전트 작업 하나의 진행 상황 (팀원 스타일 그대로: card / inset / pill). TOOL → 단계, TOKEN → 답변, APPROVAL_REQUIRED → 안내, DONE → 답과 참고한 데이터.
import { ref, onBeforeUnmount } from 'vue'
import { streamTask, errorLabel } from '../request.js'
import { TOOL_LABEL } from '../labels.js'

const props = defineProps({ taskId: { type: String, required: true }, after: { type: Number, default: 0 } })
const emit = defineEmits(['done', 'approval', 'failed'])
const steps = ref([]), answer = ref(''), evidence = ref([]), status = ref('running'), error = ref(''), approval = ref(null)
const ctrl = new AbortController()

async function run() {
  try {
    await streamTask(props.taskId, props.after, (ev) => {
      if (ev.type === 'TOOL_START') steps.value.push({ id: ev.tool_call.id, text: `${TOOL_LABEL[ev.tool_call.name] ?? ev.tool_call.name} 중…`, state: 'run' })
      else if (ev.type === 'TOOL_END') {
        const s = steps.value.find((x) => x.id === ev.tool_call.id)
        const err = ev.tool_call.error
        if (s) { s.text = `${TOOL_LABEL[ev.tool_call.name] ?? ev.tool_call.name}${err ? ' · 오류' : ' 완료'}`; s.state = err ? 'warn' : 'ok' }
      } else if (ev.type === 'TOKEN') answer.value += ev.text
      else if (ev.type === 'APPROVAL_REQUIRED') { status.value = 'waiting'; approval.value = ev.approval; emit('approval', ev) }
      else if (ev.type === 'DONE') { status.value = 'done'; answer.value = ev.result.answer; evidence.value = ev.result.evidence ?? []; emit('done', ev) }
      else if (ev.type === 'FAILED') { status.value = 'failed'; error.value = ev.error; emit('failed', ev) }
    }, ctrl.signal)
  } catch (e) { if (e.name !== 'AbortError') { status.value = 'failed'; error.value = errorLabel(e); emit('failed', e) } }
}
run()
onBeforeUnmount(() => ctrl.abort())
</script>

<template>
  <section class="card" style="gap: 12px" aria-live="polite">
    <div class="tags" style="align-items: center; gap: 8px">
      <span v-if="status === 'running'" class="pill pill-neutral">AI 비서가 처리하고 있습니다…</span>
      <span v-else-if="status === 'waiting'" class="pill pill-ok">관리자 확인 대기</span>
      <span v-else-if="status === 'done'" class="pill pill-neutral">완료</span>
      <span v-else class="pill pill-alert">실패</span>
    </div>
    <ul v-if="steps.length" class="steps-list small">
      <li v-for="s in steps" :key="s.id" :class="{ muted: s.state === 'run' }">{{ s.state === 'run' ? '…' : s.state === 'ok' ? '✓' : '!' }} {{ s.text }}</li>
    </ul>
    <div v-if="approval" class="card-lime" style="gap: 6px">
      <strong style="font-size: 14px">관리자 확인이 필요한 단계입니다</strong>
      <span class="small">{{ approval.summary }}</span>
      <span class="xs muted">승인 대기 화면의 'AI 비서 처리 확인'에서 확인하면 이어서 자동 처리됩니다.</span>
    </div>
    <p v-if="answer" class="answer">{{ answer }}</p>
    <p v-if="error" class="small" style="color: #b00020; margin: 0">{{ error }}</p>
    <details v-if="evidence.length" class="small">
      <summary>참고한 데이터 {{ evidence.length }}개</summary>
      <div class="tags" style="padding-top: 8px"><span v-for="(e, i) in evidence" :key="i" class="tag">{{ TOOL_LABEL[e.source] ?? e.source }} · {{ e.ref }}</span></div>
    </details>
  </section>
</template>

<style scoped>
.steps-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.answer { margin: 0; white-space: pre-wrap; line-height: 1.65; font-size: 14px; }
summary { cursor: pointer; font-weight: 600; min-height: 32px; display: flex; align-items: center; }
</style>
