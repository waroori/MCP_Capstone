<script setup>
// 층별 좌석 배치도 (관리자). 직원·부서·도구는 GET /org/members, 층·구역·책상은 GET /desks (DB). 신입 등록과 자리 배치·이동도 여기서 한다.
// 도구는 '쓸 수 있는 것'(배정 좌석 + 팀 프로젝트 API). 좌석은 최근 30일 사용일 수도 보인다 — 관리자는 개인별 사용량을 볼 수 있지만(설계문서 3절)
// 이용권 관리 목적으로만 쓰고 인사평가에 쓰지 않는다. 팀 프로젝트 API는 개인 기록이 아니라 숫자가 없다.
// 토큰 좌석(ChatGPT 등)은 오늘 소비 속도(GET /usage/seats)로 색을 입힌다: 오늘 예상량이 같은 제품 좌석 평균 하루 사용량의 2배 이상이면 경보. 30초마다 갱신.
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getOrgMembers, getSeatPaces, getSeatPaceDetail, getDesks, putDesk, addDesks, getDepartments, createEmployees } from '../api.js'
import { FLAG, onTabKeys } from '../labels.js'
import ProductLogo from '../components/ProductLogo.vue'

// 층 이름·회의실은 화면 장식. 층에 어떤 부서 구역이 있는지는 책상(DB)에서 정한다
const FLOOR_META = {
  '5F': { title: '경영진 · 경영지원본부', rooms: ['경영진실', '회의실 A'] },
  '4F': { title: '사업본부 · 마케팅 · 영업 · 상품기획', rooms: ['회의실 B', '고객 상담실'] },
  '3F': { title: '사업본부 · 고객지원 · 생산관리', rooms: ['회의실 C', '탕비실'] },
}
const CLUSTER = 6 // 책상 6개(3×2 마주 보기)가 한 섬

const members = ref([])
const desks = ref([])
const depts = ref([])
const error = ref('')
const floorId = ref('')
const deskId = ref('') // 고른 빈 자리
const mode = ref('') // 'new' = 신입 등록 폼
const notice = ref('')
const today = () => new Date().toISOString().slice(0, 10)
const form = ref({})
const toolFilter = ref('all')
const selectedId = ref(String(useRoute().query.id ?? ''))
const paces = ref({})
const detail = ref(null)

const loadPaces = async () => { paces.value = await getSeatPaces().catch(() => paces.value) }
let timer
async function load() {
  try { [members.value, desks.value] = await Promise.all([getOrgMembers(), getDesks()]) } catch (e) { error.value = e.message }
}
onMounted(async () => {
  await load()
  depts.value = await getDepartments().catch(() => [])
  floorId.value = deskOf.value.get(selectedId.value)?.floor ?? FLOORS.value[0]?.id ?? '' // 홈에서 ?id=로 들어오면 그 사람 층으로
  await loadPaces()
  timer = setInterval(loadPaces, 30000)
})
onUnmounted(() => clearInterval(timer))

const FLOORS = computed(() => [...new Set(desks.value.map((d) => d.floor))].sort().reverse()
  .map((id) => ({ id, label: id.replace('F', '층'), title: FLOOR_META[id]?.title ?? '', rooms: FLOOR_META[id]?.rooms ?? [] })))
const floor = computed(() => FLOORS.value.find((f) => f.id === floorId.value) ?? FLOORS.value[0] ?? { id: '', label: '', title: '', rooms: [] })
const byId = computed(() => new Map(members.value.map((m) => [m.id, m])))
const deskOf = computed(() => new Map(desks.value.filter((d) => d.employee_id).map((d) => [d.employee_id, d])))
const emptyDesks = computed(() => desks.value.filter((d) => !d.employee_id))
const unplaced = computed(() => members.value.filter((m) => m.status !== 'resigned' && !deskOf.value.has(m.id)))
const deptName = (id) => depts.value.find((d) => d.id === id)?.name ?? members.value.find((m) => m.dept_id === id)?.dept_name ?? id
const tools = computed(() => {
  const m = new Map()
  for (const t of members.value.flatMap((x) => x.tools)) m.set(t.tool_id, t.product)
  return [...m].map(([id, product]) => ({ id, product }))
})
const uses = (p, toolId) => p.tools.some((t) => t.tool_id === toolId)
const needsCheck = (p) => p.status !== 'active' || p.tools.some((t) => t.flag && t.flag !== 'recent_hire')

// 층의 책상을 부서 구역별로 모아 6개씩 섬으로 자른다
const zones = computed(() => {
  const here = desks.value.filter((d) => d.floor === floor.value.id)
  return [...new Set(here.map((d) => d.dept_id))].map((id) => {
    const ds = here.filter((d) => d.dept_id === id).map((d) => ({ id: d.id, p: byId.value.get(d.employee_id) ?? null }))
    const people = ds.map((d) => d.p).filter(Boolean)
    const clusters = Array.from({ length: Math.ceil(ds.length / CLUSTER) }, (_, i) => ds.slice(i * CLUSTER, (i + 1) * CLUSTER))
    const count = tools.value.map((t) => ({ ...t, n: people.filter((p) => uses(p, t.id)).length })).filter((t) => t.n)
    return { id, name: deptName(id), people, clusters, count, empty: ds.length - people.length }
  })
})
const floorPeople = (f) => desks.value.filter((d) => d.floor === f.id && d.employee_id).length
const selected = computed(() => members.value.find((p) => p.id === selectedId.value))
// 고른 직원의 오늘 시간대·입력 길이·모델 분포. 토큰 좌석이 없으면 not_found → 비움
watch(selectedId, async (id) => { detail.value = null; if (id && paces.value[id]) detail.value = await getSeatPaceDetail(id).catch(() => null) }, { immediate: true })
const pace = (p) => paces.value[p.id]
const PACE = { normal: '보통', caution: '빠름', over: '평균의 2배 이상' }
const n = (v) => Math.round(v).toLocaleString('ko-KR')
const paceLine = (x) => `${x.product} 오늘 ${n(x.tokens_today)} 토큰 사용 · 이 속도면 ${n(x.projected_today)} 토큰(평균 ${n(x.peer_avg_daily)}의 ${x.ratio}배, ${PACE[x.level]})`
const top = (rows) => rows.slice().sort((a, b) => b.share - a.share).slice(0, 3).map((b) => `${b.key} ${Math.round(b.share * 100)}%`).join(' · ')
const dim = (p) => toolFilter.value !== 'all' && !uses(p, toolFilter.value)
// 최근 30일 사용일: 고른 도구(전체 보기면 기록이 있는 좌석 중 가장 많이 쓴 것). 기록을 주지 않는 제품뿐이면 null
const WORKDAYS = 22 // 30일 중 평일 수. 막대 100% 기준
const days = (p) => {
  const ds = p.tools.filter((t) => t.active_days_30 != null && (toolFilter.value === 'all' || t.tool_id === toolFilter.value)).map((t) => t.active_days_30)
  return ds.length ? Math.max(...ds) : null
}
const LEVEL = (d) => (d === 0 ? '30일 미사용' : d < 5 ? '가끔' : d < 12 ? '보통' : '자주')
const zoneAvg = (z) => { const ds = z.people.map(days).filter((d) => d != null); return ds.length ? Math.round(ds.reduce((a, b) => a + b, 0) / ds.length) : null }

const STATUS = { active: '재직', on_leave: '휴직 중', resigned: '퇴사' }
const VIA = { seat: '배정 좌석', project: '팀 프로젝트 API' }
const ago = (s) => (s ? `${Math.max(0, Math.round((Date.now() - new Date(s)) / 86400000))}일 전` : '기록 없음')
const deskLabel = (p) => `${p.name} · ${p.job_title}${p.status !== 'active' ? ` · ${STATUS[p.status]}` : ''} · `
  + (p.tools.length ? p.tools.map((t) => t.product + (t.flag ? `(${FLAG[t.flag]?.label})` : '')).join(', ') : '쓰는 도구 없음')
  + (days(p) != null ? ` · 최근 30일 ${days(p)}일 사용(${LEVEL(days(p))})` : '')
  + (pace(p) ? ` · ${paceLine(pace(p))}` : '')
function pickFloor(id) { floorId.value = id; selectedId.value = ''; deskId.value = ''; mode.value = '' }
function pickPerson(id) { selectedId.value = id; deskId.value = ''; mode.value = ''; notice.value = '' }
function pickDesk(id) { deskId.value = id; selectedId.value = ''; mode.value = ''; notice.value = '' }

// 신입 등록: 직원 추가(POST /employees) → 고른 빈 자리에 앉히기(PUT /desks). Slack 회원 ID를 넣으면 봇이 바로 알아본다
function openNew(desk = '') {
  const d = desks.value.find((x) => x.id === desk)
  form.value = { name: '', email: '', dept_id: d?.dept_id ?? depts.value[0]?.id ?? '', job_title: '', hire_date: today(), slack_user_id: '', desk: desk }
  mode.value = 'new'; selectedId.value = ''; notice.value = ''
}
const formDesks = computed(() => [...emptyDesks.value].sort((a, b) => (a.dept_id === form.value.dept_id ? 0 : 1) - (b.dept_id === form.value.dept_id ? 0 : 1)))
watch(() => form.value.dept_id, () => { if (mode.value === 'new' && !emptyDesks.value.some((d) => d.id === form.value.desk && d.dept_id === form.value.dept_id)) form.value.desk = formDesks.value[0]?.id ?? '' })
const busy = ref(false)
async function run(fn, ok) {
  busy.value = true; notice.value = ''
  try { await fn(); await load(); notice.value = ok } catch (e) { notice.value = `실패: ${e.message}` }
  busy.value = false
}
const registerNew = () => run(async () => {
  const f = form.value
  const [emp] = await createEmployees([{ name: f.name, email: f.email, dept_id: f.dept_id, job_title: f.job_title, hire_date: f.hire_date || null, slack_user_id: f.slack_user_id.trim() || null }])
  if (f.desk) await putDesk(f.desk, emp.id)
  mode.value = ''; selectedId.value = emp.id
  floorId.value = desks.value.find((d) => d.id === f.desk)?.floor ?? floorId.value
}, `${form.value.name} 님을 등록했어요${form.value.desk ? ` (${form.value.desk})` : ''}. Slack에서 이용권을 신청하면 남은 좌석이 있을 때 바로 배정돼요.`)
const moveTo = ref('')
const seat = (empId, desk) => run(() => putDesk(desk, empId), desk ? `${byId.value.get(empId)?.name} 님 자리를 ${desk}(으)로 정했어요.` : '')
const clearDesk = (desk) => run(() => putDesk(desk, null), `${desk} 자리를 비웠어요.`)
const addCluster = (z) => run(() => addDesks(floor.value.id, z.id, CLUSTER), `${z.name} 구역에 책상 ${CLUSTER}개를 붙였어요.`)
watch(selectedId, () => { moveTo.value = '' })
</script>

<template>
  <header class="page-head">
    <div><span class="small muted">운영 · 관리자 보기</span><h1>좌석 배치도</h1></div>
    <button type="button" class="btn btn-dark" @click="openNew()">신입 등록</button>
    <div role="tablist" aria-label="층" class="segment" @keydown="onTabKeys">
      <button v-for="f in FLOORS" :key="f.id" type="button" role="tab" :aria-selected="f.id === floorId" :tabindex="f.id === floorId ? 0 : -1" @click="pickFloor(f.id)">
        {{ f.label }} <span class="xs">{{ floorPeople(f) }}명</span>
      </button>
    </div>
  </header>

  <p v-if="error" class="status" role="alert">좌석 정보를 불러오지 못했습니다: {{ error }}</p>

  <div class="tags filter" role="group" aria-label="도구로 강조">
    <button type="button" class="chip chip-lg" :aria-pressed="toolFilter === 'all'" @click="toolFilter = 'all'">전체 보기</button>
    <button v-for="t in tools" :key="t.id" type="button" class="chip chip-lg tool-chip" :aria-pressed="toolFilter === t.id" @click="toolFilter = t.id">
      <ProductLogo :tool-id="t.id" :name="t.product" :size="22" />{{ t.product }}
    </button>
  </div>

  <div class="row">
    <section class="card col-3 plan" :aria-label="`${floor.label} ${floor.title} 배치도`">
      <div class="card-head">
        <div><h2>{{ floor.label }} · {{ floor.title }}</h2><span class="small muted">책상을 누르면 오른쪽에 쓰는 도구가 나오고, 마우스를 올리면 오늘 토큰 소비가 보입니다. 빈 자리를 누르면 사람을 앉힐 수 있어요</span></div>
        <div class="legend">
          <span><i class="swatch" style="background: var(--white); outline: 1px solid var(--line-strong)" />재직</span>
          <span><i class="swatch hatch" />휴직</span>
          <span><i class="swatch" style="outline: 2px dashed var(--ink); background: var(--white)" />이용권 확인 필요</span>
          <span><i class="act-legend" />최근 30일 사용일 (평일 {{ WORKDAYS }}일 = 가득)</span>
          <span><i class="swatch pace-caution" />토큰 소비 빠름 (1.5배)</span>
          <span><i class="swatch pace-over" />토큰 소비 2배 이상 · 본인 Slack 안내</span>
        </div>
      </div>

      <div class="floor">
        <div v-for="z in zones" :key="z.id" class="zone">
          <div class="zone-head">
            <strong>{{ z.name }}</strong><span class="xs muted">{{ z.people.length }}명<template v-if="zoneAvg(z) != null"> · 평균 {{ zoneAvg(z) }}일 사용</template></span>
            <span v-for="c in z.count" :key="c.id" class="zone-count xs"><ProductLogo :tool-id="c.id" :name="c.product" :size="14" />{{ c.n }}</span>
            <span class="xs muted">빈 자리 {{ z.empty }}</span>
            <button type="button" class="link-btn xs" :disabled="busy" @click="addCluster(z)">책상 {{ CLUSTER }}개 추가</button>
          </div>
          <div class="islands">
            <div v-for="(cl, ci) in z.clusters" :key="ci" class="island">
              <template v-for="{ id: did, p } in cl" :key="did">
                <button v-if="p" type="button" class="desk" :class="[{ leave: p.status !== 'active', check: needsCheck(p), dim: dim(p) }, pace(p) && `pace-${pace(p).level}`]"
                  :aria-pressed="p.id === selectedId" :aria-label="deskLabel(p)" @click="pickPerson(p.id)">
                  <span class="desk-name">{{ p.name }}<b v-if="pace(p) && pace(p).level !== 'normal'" class="pace-x">×{{ pace(p).ratio }}</b></span>
                  <span class="desk-tools">
                    <ProductLogo v-for="t in p.tools" :key="t.tool_id" :tool-id="t.tool_id" :name="t.product" :size="16" />
                    <span v-if="!p.tools.length" class="xs muted">없음</span>
                  </span>
                  <span v-if="days(p) != null" class="act" aria-hidden="true">
                    <span class="act-bar"><i :style="{ width: `${Math.min(100, (days(p) / WORKDAYS) * 100)}%` }" /></span>
                    <span class="act-n">{{ days(p) }}일</span>
                  </span>
                  <span v-if="pace(p)" class="pace-tip" aria-hidden="true">
                    <strong>{{ pace(p).product }} · 오늘 {{ PACE[pace(p).level] }}</strong>
                    <span>지금까지 {{ n(pace(p).tokens_today) }} 토큰</span>
                    <span>이 속도면 {{ n(pace(p).projected_today) }} 토큰</span>
                    <span>평균 {{ n(pace(p).peer_avg_daily) }}의 {{ pace(p).ratio }}배 · 하루 한도 {{ Math.round(pace(p).projected_quota_pct) }}% 예상</span>
                  </span>
                </button>
                <button v-else type="button" class="desk empty" :aria-pressed="did === deskId" :aria-label="`빈 자리 ${did}`" @click="pickDesk(did)"><span class="xs">빈 자리</span></button>
              </template>
            </div>
          </div>
        </div>
        <div class="rooms" aria-hidden="true">
          <div v-for="r in floor.rooms" :key="r" class="room">{{ r }}</div>
          <div class="room core">엘리베이터 · 계단</div>
        </div>
      </div>
      <p class="xs muted" style="margin: 0">직원·부서·도구·사용일·책상은 DB 값입니다. 사용일은 이용권 관리에만 쓰고 인사평가에 쓰지 않습니다. Acrobat·Canva는 공급사가 사용 기록을 주지 않아 마지막 사용일만 보입니다. 토큰 소비 속도 = 오늘 예상량 ÷ 같은 제품 좌석의 최근 14일 평균 하루 사용량(근무 9–18시 기준).</p>
    </section>

    <aside class="card col-1 detail" aria-live="polite" aria-labelledby="who-h">
      <p v-if="notice" class="status" role="status">{{ notice }}</p>
      <form v-if="mode === 'new'" class="new-form" @submit.prevent="registerNew">
        <h2 id="who-h">신입 등록</h2>
        <label class="field">이름<input v-model="form.name" required maxlength="50" autocomplete="off" /></label>
        <label class="field">회사 이메일 <span class="xs muted">좌석 제품 SSO 아이디</span><input v-model="form.email" type="email" required autocomplete="off" /></label>
        <label class="field">부서<select v-model="form.dept_id" required><option v-for="d in depts" :key="d.id" :value="d.id">{{ d.name }}</option></select></label>
        <label class="field">직무<input v-model="form.job_title" required maxlength="50" placeholder="예: 마케터" /></label>
        <label class="field">입사일<input v-model="form.hire_date" type="date" /></label>
        <label class="field">Slack 회원 ID <span class="xs muted">선택 · 프로필 → ⋮ → 회원 ID 복사 (U로 시작)</span><input v-model="form.slack_user_id" pattern="[UW][A-Z0-9]{8,}" placeholder="예: U0ABCDE1234" autocomplete="off" /></label>
        <label class="field">자리
          <select v-model="form.desk"><option value="">나중에 정하기</option><option v-for="d in formDesks" :key="d.id" :value="d.id">{{ d.id }} · {{ deptName(d.dept_id) }}</option></select>
        </label>
        <p v-if="!emptyDesks.length" class="xs muted" style="margin: 0">빈 자리가 없어요. 구역 이름 옆 '책상 {{ CLUSTER }}개 추가'로 늘릴 수 있어요.</p>
        <div class="tags" style="gap: 8px"><button type="submit" class="btn btn-dark" :disabled="busy">등록하고 앉히기</button><button type="button" class="btn" @click="mode = ''">취소</button></div>
      </form>
      <template v-else-if="deskId">
        <h2 id="who-h">빈 자리 {{ deskId }}</h2>
        <span class="small muted">{{ deptName(desks.find((d) => d.id === deskId)?.dept_id) }} 구역</span>
        <button type="button" class="btn btn-dark" @click="openNew(deskId)">이 자리에 신입 등록</button>
        <template v-if="unplaced.length">
          <strong class="small">자리가 없는 직원 {{ unplaced.length }}명</strong>
          <ul class="tool-list">
            <li v-for="u in unplaced" :key="u.id"><div class="cell-2"><span>{{ u.name }}</span><span>{{ u.dept_name }} · {{ u.job_title }}</span></div>
              <button type="button" class="btn" :disabled="busy" @click="seat(u.id, deskId)">여기 앉히기</button></li>
          </ul>
        </template>
        <p v-else class="small muted" style="margin: 0">자리가 없는 직원이 없어요. 다른 직원을 옮기려면 그 사람 책상을 눌러 '자리 옮기기'를 쓰세요.</p>
      </template>
      <template v-else-if="selected">
        <div class="who">
          <div class="avatar" aria-hidden="true">{{ selected.name.slice(0, 1) }}</div>
          <div><h2 id="who-h">{{ selected.name }}</h2><span class="small muted">{{ selected.dept_name }} · {{ selected.job_title }}</span></div>
        </div>
        <span class="pill" :class="selected.status === 'active' ? 'pill-neutral' : 'pill-alert'" style="align-self: flex-start">{{ STATUS[selected.status] }}</span>
        <strong class="small">쓸 수 있는 AI·소프트웨어 {{ selected.tools.length }}개</strong>
        <ul class="tool-list">
          <li v-for="t in selected.tools" :key="t.tool_id + t.detail">
            <ProductLogo :tool-id="t.tool_id" :name="t.product" :size="32" />
            <div class="cell-2">
              <span>{{ t.product }}</span>
              <span>{{ VIA[t.via] }} · {{ t.detail }}</span>
              <span v-if="t.via === 'seat'">마지막 사용 {{ ago(t.last_activity_at) }}<template v-if="t.active_days_30 != null"> · 최근 30일 {{ t.active_days_30 }}일 · 요청 {{ (t.requests_30 ?? 0).toLocaleString('ko-KR') }}건</template></span>
              <span v-if="t.via === 'project'">팀 프로젝트라 개인 사용량은 따로 세지 않습니다</span>
            </div>
            <span v-if="t.flag" class="pill" :class="`pill-${FLAG[t.flag].tone}`">{{ FLAG[t.flag].label }}</span>
          </li>
        </ul>
        <p v-if="!selected.tools.length" class="small muted" style="margin: 0">배정된 좌석도, 팀 프로젝트 API도 없습니다.</p>
        <section v-if="pace(selected)" class="pace-box" :class="`pace-${pace(selected).level}`" aria-labelledby="pace-h">
          <h3 id="pace-h" class="small">오늘 토큰 소비 · {{ PACE[pace(selected).level] }}</h3>
          <span class="small">{{ paceLine(pace(selected)) }}</span>
          <span class="act-bar" role="img" :aria-label="`하루 한도의 ${Math.round(pace(selected).projected_quota_pct)}% 예상`"><i :style="{ width: `${Math.min(100, pace(selected).projected_quota_pct)}%` }" /></span>
          <span class="xs muted">하루 한도 {{ n(pace(selected).daily_quota) }} 토큰 중 {{ Math.round(pace(selected).quota_used_pct) }}% 사용, {{ Math.round(pace(selected).projected_quota_pct) }}% 예상</span>
          <template v-if="detail">
            <span class="xs">모델: {{ top(detail.by_model) || '없음' }}</span>
            <span class="xs">입력 길이: {{ top(detail.by_input_length) || '없음' }}</span>
            <span class="xs">많이 쓴 시간: {{ top(detail.by_hour.map((b) => ({ ...b, key: `${b.key}시` }))) || '없음' }}</span>
          </template>
          <RouterLink :to="{ path: '/assistant', query: { q: `${selected.name} 님이 오늘 토큰을 많이 쓰는 이유를 분석하고 다른 모델을 추천해줘`, prompt: 'analyze_seat_usage', employee: selected.id } }" class="btn">AI로 현재 작업 분석</RouterLink>
        </section>
        <RouterLink v-if="needsCheck(selected)" to="/licenses" class="btn btn-dark">구독·이용권에서 확인하기</RouterLink>
        <section class="pace-box" aria-labelledby="desk-h">
          <h3 id="desk-h" class="small">자리 · {{ deskOf.get(selected.id)?.id ?? '없음' }}</h3>
          <label class="field">옮길 빈 자리<select v-model="moveTo"><option value="" disabled>고르세요</option><option v-for="d in emptyDesks" :key="d.id" :value="d.id">{{ d.id }} · {{ deptName(d.dept_id) }}</option></select></label>
          <div class="tags" style="gap: 8px">
            <button type="button" class="btn" :disabled="!moveTo || busy" @click="seat(selected.id, moveTo)">자리 옮기기</button>
            <button v-if="deskOf.get(selected.id) && selected.status !== 'active'" type="button" class="btn" :disabled="busy" @click="clearDesk(deskOf.get(selected.id).id)">자리 비우기</button>
          </div>
        </section>
      </template>
      <template v-else>
        <h2 id="who-h">직원을 선택하세요</h2>
        <p v-if="unplaced.length" class="small" style="margin: 0">자리가 없는 직원 {{ unplaced.length }}명 · 빈 자리를 눌러 앉히세요.</p>
        <p class="small muted" style="margin: 0">책상을 누르면 그 자리 직원이 쓸 수 있는 도구와 이용권 상태가 나옵니다. 위의 도구 버튼으로 특정 도구를 쓰는 자리만 강조할 수 있습니다.</p>
      </template>
    </aside>
  </div>
</template>

<style scoped>
.segment { display: flex; flex-direction: row; flex-wrap: wrap; gap: 4px; padding: 4px; border-radius: 12px; background: var(--white); border: 1px solid var(--line); }
.segment button { min-height: 40px; padding: 0 16px; border: 0; border-radius: 8px; background: transparent; font-size: 14px; }
.segment button[aria-selected='true'] { background: var(--ink); color: var(--white); font-weight: 600; }
.filter { gap: 8px; }
.card-head > .legend { flex-direction: row; }
.tool-chip { display: inline-flex; align-items: center; gap: 8px; padding-left: 8px; }
.floor { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 16px; padding: 16px; border-radius: 12px; background: var(--paper); border: 2px solid var(--line-strong); }
.zone { display: flex; flex-direction: column; gap: 12px; padding: 14px; border-radius: 10px; background: var(--white); border: 1px solid var(--line); }
.zone-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.zone-count { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px 2px 4px; border-radius: 999px; border: 1px solid var(--line-strong); }
.islands { display: flex; flex-wrap: wrap; gap: 16px; }
.island { display: grid; grid-template-columns: repeat(3, minmax(0, 76px)); gap: 4px; padding: 6px; border-radius: 8px; background: var(--track); }
.desk { position: relative; min-height: 64px; padding: 6px 4px; border-radius: 6px; border: 1px solid var(--line-strong); background: var(--white); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; }
.desk:hover { border-color: var(--ink); }
.desk[aria-pressed='true'] { background: var(--ink); color: var(--white); border-color: var(--ink); }
.desk.check { outline: 2px dashed var(--ink); outline-offset: -4px; }
.desk.leave { background: repeating-linear-gradient(45deg, var(--white) 0 6px, var(--track) 6px 12px); }
.desk.dim { opacity: 0.3; }
.desk.pace-caution, .swatch.pace-caution { background: rgba(220, 239, 109, 0.45); }
.desk.pace-over, .swatch.pace-over { background: var(--lime); border: 2px solid var(--ink); }
.desk.pace-over[aria-pressed='true'], .desk.pace-caution[aria-pressed='true'] { background: var(--ink); }
.pace-x { margin-left: 4px; font-size: 11px; }
.pace-tip { display: none; position: absolute; z-index: 5; left: 50%; bottom: calc(100% + 6px); transform: translateX(-50%); width: max-content; max-width: 240px;
  flex-direction: column; gap: 2px; padding: 8px 10px; border-radius: 8px; background: var(--ink); color: var(--white); font-size: 12px; text-align: left; pointer-events: none; }
.desk:hover .pace-tip, .desk:focus-visible .pace-tip { display: flex; }
.pace-box { display: flex; flex-direction: column; gap: 6px; padding: 12px; border-radius: 10px; border: 1px solid var(--line-strong); }
.pace-box h3 { margin: 0; }
.pace-box.pace-over { border: 2px solid var(--ink); background: rgba(220, 239, 109, 0.35); }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; }
.field input, .field select { min-height: 44px; border-radius: 10px; border: 1px solid var(--line-strong); background: var(--white); padding: 0 12px; font-size: 15px; font-weight: 400; width: 100%; box-sizing: border-box; }
.new-form { display: flex; flex-direction: column; gap: 12px; }
.new-form h2 { margin: 0; }
.link-btn { margin-left: auto; min-height: 32px; padding: 0 10px; border: 1px solid var(--line-strong); border-radius: 999px; background: var(--white); }
.desk.empty[aria-pressed='true'] { border: 2px solid var(--ink); color: var(--ink); }
.desk.empty { border-style: dashed; background: transparent; color: var(--muted); }
.desk-name { font-size: 13px; font-weight: 600; }
.desk-tools { display: flex; gap: 3px; min-height: 16px; }
.act { display: flex; align-items: center; gap: 4px; width: 100%; padding: 0 2px; }
.act-bar { flex: 1; height: 4px; border-radius: 999px; background: var(--track); overflow: hidden; }
.act-bar i { display: block; height: 100%; background: currentColor; }
.act-n { font-size: 11px; font-variant-numeric: tabular-nums; }
.act-legend { width: 18px; height: 4px; border-radius: 999px; display: inline-block; background: linear-gradient(90deg, var(--ink) 60%, var(--track) 60%); }
.swatch.hatch { background: repeating-linear-gradient(45deg, var(--white) 0 3px, rgba(17, 17, 19, 0.3) 3px 5px); outline: 1px solid var(--line-strong); }
.rooms { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 12px; }
.room { flex: 1 1 120px; min-height: 52px; display: grid; place-items: center; border-radius: 8px; border: 1px dashed var(--line-strong); font-size: 12px; color: var(--muted); }
.room.core { background: var(--track); border-style: solid; }
.detail { position: sticky; top: 24px; gap: 12px; }
.who { display: flex; align-items: center; gap: 12px; }
.who > div:last-child { display: flex; flex-direction: column; gap: 2px; }
.avatar { width: 44px; height: 44px; border-radius: 999px; background: var(--lime); font-weight: 700; display: grid; place-items: center; }
.tool-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; }
.tool-list li { display: flex; align-items: center; gap: 12px; padding: 12px 0; border-top: 1px solid var(--line); }
.tool-list .cell-2 { flex: 1; min-width: 0; }
@media (max-width: 900px) { .detail { position: static; } .floor { padding: 8px; } .zone { padding: 10px; } }
</style>
