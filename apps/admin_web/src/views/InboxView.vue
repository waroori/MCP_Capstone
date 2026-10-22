<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getPlans, approvePlan, rejectPlan } from '../api.js'
import { RISK, PLAN_STATUS, onTabKeys } from '../labels.js'
import TaskApprovals from '../components/TaskApprovals.vue'

const route = useRoute()
const router = useRouter()
const plans = ref([])
const tab = ref('pending')
const notice = ref('')
const busy = ref(false)
const rejecting = ref(false) // 반려는 사유를 받은 뒤에만 보낸다
const reason = ref('')
const reasonEl = ref(null)

const load = async () => { plans.value = await getPlans() }
onMounted(() => {
  load()
  // AI 의견이 아직 없는 승인 대기 건이 있으면 5초마다 다시 읽는다 (검토 에이전트가 붙이는 중)
  const timer = setInterval(() => { if (plans.value.some((p) => p.status === 'pending_approval' && !p.opinion)) load() }, 5000)
  onBeforeUnmount(() => clearInterval(timer))
})

const list = computed(() => plans.value.filter((p) => (tab.value === 'pending') === (p.status === 'pending_approval')))
const selected = computed(() => list.value.find((p) => p.id === route.query.id) ?? list.value[0])
watch(tab, () => router.replace({ query: {} }))
watch(() => selected.value?.id, () => { rejecting.value = false; reason.value = '' })
async function startReject() { rejecting.value = true; await nextTick(); reasonEl.value?.focus() }

const select = (id) => router.replace({ query: { id } })

async function decide(fn, verb) {
  busy.value = true
  const p = selected.value
  let status
  try { status = await fn(p.id) } catch (e) { notice.value = `${p.title} ${verb} 실패: ${e.message}`; busy.value = false; return }
  notice.value = `${p.title} · ${p.subtitle} ${verb}: ${PLAN_STATUS[status ?? 'rejected']}`
  await load()
  router.replace({ query: {} })
  busy.value = false
}
</script>

<template>
  <header class="page-head">
    <div><span class="small muted">운영 · 처리 요청</span><h1>승인 대기</h1></div>
    <div role="tablist" aria-label="보기" class="segment" @keydown="onTabKeys">
      <button type="button" role="tab" :aria-selected="tab === 'pending'" :tabindex="tab === 'pending' ? 0 : -1" @click="tab = 'pending'">승인 대기 {{ plans.filter((p) => p.status === 'pending_approval').length }}</button>
      <button type="button" role="tab" :aria-selected="tab === 'history'" :tabindex="tab === 'history' ? 0 : -1" @click="tab = 'history'">처리 이력</button>
    </div>
  </header>

  <p v-if="notice" class="status" role="status">{{ notice }}</p>

  <div v-if="list.length" class="row">
    <section class="col-1 stack" style="gap: 8px" aria-label="처리 요청 목록">
      <button v-for="p in list" :key="p.id" type="button" class="list-item" :aria-current="p.id === selected?.id" @click="select(p.id)">
        <span class="li-top">
          <span class="pill" :class="`pill-${RISK[p.risk].tone}`">{{ RISK[p.risk].label }}</span>
          <span class="xs muted">{{ tab === 'pending' ? p.createdAt : PLAN_STATUS[p.status] }}</span>
        </span>
        <strong style="font-size: 15px">{{ p.title }}</strong>
        <span class="small muted">{{ p.subtitle }}</span>
      </button>
    </section>

    <section v-if="selected" class="card col-3 detail" aria-labelledby="plan-h">
      <div class="card-head" style="align-items: flex-start">
        <div style="gap: 6px">
          <span class="xs muted">처리 요청 {{ selected.id }} · 신청 {{ selected.requester }}</span>
          <h2 id="plan-h" style="font-size: 22px">{{ selected.title }}</h2>
          <span style="font-size: 14px">{{ selected.summary }}</span>
        </div>
        <span class="pill" :class="`pill-${RISK[selected.risk].tone}`">{{ RISK[selected.risk].label }}</span>
      </div>

      <ol class="steps" aria-label="진행 단계">
        <li v-for="(st, i) in selected.steps" :key="st.label" :aria-current="st.current ? 'step' : undefined">
          <div class="track" :class="{ done: st.done, current: st.current }" />
          <span class="xs" :class="st.done || st.current ? 'strong' : 'muted'">{{ i + 1 }} {{ st.label }}</span>
          <span class="xs" :class="st.current ? '' : 'muted'">{{ st.at }}</span>
        </li>
      </ol>

      <div class="card-lime">
        <div class="ai-title">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" /></svg>
          <strong style="font-size: 14px">{{ selected.aiOpinion.title }}</strong>
        </div>
        <span style="font-size: 14px; line-height: 1.65; white-space: pre-line">{{ selected.aiOpinion.body }}</span>
        <ul v-if="selected.aiOpinion.facts?.length" class="facts small">
          <li v-for="f in selected.aiOpinion.facts" :key="f">{{ f }}</li>
        </ul>
        <span v-if="selected.aiOpinion.risks?.length" class="small"><strong>주의</strong> · {{ selected.aiOpinion.risks.join(' / ') }}</span>
        <span v-if="selected.aiOpinion.alternative" class="small"><strong>대안</strong> · {{ selected.aiOpinion.alternative }}</span>
      </div>

      <div class="two">
        <div class="stack" style="gap: 10px">
          <strong class="small">변경 내용</strong>
          <dl class="kv">
            <div v-for="[k, v] in selected.changes" :key="k"><dt class="muted">{{ k }}</dt><dd>{{ v }}</dd></div>
          </dl>
        </div>
        <div class="stack" style="gap: 10px">
          <strong class="small">참고한 데이터</strong>
          <div class="tags"><span v-for="e in selected.evidence" :key="e" class="tag">{{ e }}</span></div>
        </div>
      </div>

      <form v-if="selected.status === 'pending_approval' && rejecting" class="foot reject" @submit.prevent="decide((id) => rejectPlan(id, reason.trim()), '반려')">
        <label class="reason">반려 사유 <span class="xs muted">처리 이력에 남습니다</span>
          <textarea ref="reasonEl" v-model="reason" rows="2" required placeholder="예: 다음 분기 예산 확정 후 다시 요청해 주세요" @keydown.esc="rejecting = false" />
        </label>
        <button type="button" class="btn" style="min-height: 48px" @click="rejecting = false">취소</button>
        <button type="submit" class="btn btn-dark" style="min-height: 48px" :disabled="busy || !reason.trim()">반려 확정</button>
      </form>
      <div v-else-if="selected.status === 'pending_approval'" class="foot">
        <span class="xs muted" style="flex: 1 1 240px">
          {{ selected.risk === 'high' ? '책임자 결정은 기록만 남기고, 구매·운영 변경은 시스템 밖에서 진행합니다.' : '승인하면 실행 직전 서버가 상태를 다시 확인합니다.' }}
        </span>
        <button type="button" class="btn" style="min-height: 48px" :disabled="busy" @click="startReject">반려</button>
        <button type="button" class="btn btn-dark" style="min-height: 48px; padding: 0 24px" :disabled="busy" @click="decide(approvePlan, '승인')">
          {{ selected.risk === 'high' ? '결정 기록' : '승인하고 실행' }}
        </button>
      </div>
      <p v-else class="small muted" style="margin: 0">상태: {{ PLAN_STATUS[selected.status] }}</p>
    </section>
  </div>
  <p v-else class="card small muted">{{ tab === 'pending' ? '승인 대기 중인 요청이 없습니다.' : '처리 이력이 없습니다.' }}</p>

  <TaskApprovals />
</template>

<style scoped>
.list-item { text-align: left; border: 1px solid var(--line); border-radius: 14px; background: var(--white); padding: 17px; display: flex; flex-direction: column; gap: 8px; }
.list-item:hover { border-color: var(--line-strong); }
.list-item[aria-current='true'] { border: 2px solid var(--ink); padding: 16px; }
.li-top { display: flex; justify-content: space-between; gap: 8px; width: 100%; }
.detail { padding: 28px; gap: 24px; }
.steps { margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
.steps li { display: flex; flex-direction: column; gap: 8px; }
.track { height: 4px; border-radius: 999px; background: rgba(17, 17, 19, 0.1); }
.track.done { background: var(--ink); }
.track.current { background: var(--lime); outline: 1px solid rgba(17, 17, 19, 0.24); }
.strong { font-weight: 700; }
.ai-title { display: flex; align-items: center; gap: 8px; }
.facts { margin: 4px 0 0; padding-left: 18px; line-height: 1.7; }
.two { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
.kv { margin: 0; display: flex; flex-direction: column; font-size: 14px; }
.kv div { display: flex; justify-content: space-between; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.kv div:last-child { border-bottom: 0; }
.kv dd { margin: 0; }
.reason { flex: 1 1 100%; display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; }
.reason textarea { font: inherit; font-weight: 400; font-size: 14px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line-strong); resize: vertical; }
.foot { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; padding-top: 20px; border-top: 1px solid var(--line); }
</style>
