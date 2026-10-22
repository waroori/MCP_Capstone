<script setup>
// 층별 좌석 배치도 (관리자). 직원·부서·도구는 GET /org/members(DB) 값이고, 층·책상 위치만 화면에서 임의로 정한다.
// 도구는 '쓸 수 있는 것'(배정 좌석 + 팀 프로젝트 API). 좌석은 최근 30일 사용일 수도 보인다 — 관리자는 개인별 사용량을 볼 수 있지만(설계문서 3절)
// 이용권 관리 목적으로만 쓰고 인사평가에 쓰지 않는다. 팀 프로젝트 API는 개인 기록이 아니라 숫자가 없다.
import { ref, computed, onMounted } from 'vue'
import { getOrgMembers } from '../api.js'
import { FLAG, onTabKeys } from '../labels.js'
import ProductLogo from '../components/ProductLogo.vue'

// 조직도(경영지원본부 / 사업본부)를 DB 부서에 맞춰 층으로 나눈다. 시연용 임의 배치.
const FLOORS = [
  { id: '5F', label: '5층', title: '경영진 · 경영지원본부', depts: ['dept_mgmt', 'dept_hrd'], rooms: ['경영진실', '회의실 A'] },
  { id: '4F', label: '4층', title: '사업본부 · 마케팅 · 영업', depts: ['dept_mkt', 'dept_sales'], rooms: ['회의실 B', '고객 상담실'] },
  { id: '3F', label: '3층', title: '사업본부 · 고객지원 · 생산관리', depts: ['dept_cs', 'dept_prod'], rooms: ['회의실 C', '탕비실'] },
]
const CLUSTER = 6 // 책상 6개(3×2 마주 보기)가 한 섬

const members = ref([])
const error = ref('')
const floorId = ref(FLOORS[0].id)
const toolFilter = ref('all')
const selectedId = ref('')

onMounted(async () => {
  try { members.value = await getOrgMembers() } catch (e) { error.value = e.message }
})

const floor = computed(() => FLOORS.find((f) => f.id === floorId.value))
const tools = computed(() => {
  const m = new Map()
  for (const t of members.value.flatMap((x) => x.tools)) m.set(t.tool_id, t.product)
  return [...m].map(([id, product]) => ({ id, product }))
})
const uses = (p, toolId) => p.tools.some((t) => t.tool_id === toolId)
const needsCheck = (p) => p.status !== 'active' || p.tools.some((t) => t.flag && t.flag !== 'recent_hire')

// 부서별 섬 단위로 자른다. 모자라는 자리는 빈 책상.
const zones = computed(() => floor.value.depts.map((id) => {
  const people = members.value.filter((p) => p.dept_id === id)
  const n = Math.max(CLUSTER, Math.ceil(people.length / CLUSTER) * CLUSTER)
  const desks = Array.from({ length: n }, (_, i) => people[i] ?? null)
  const clusters = Array.from({ length: n / CLUSTER }, (_, i) => desks.slice(i * CLUSTER, (i + 1) * CLUSTER))
  const count = tools.value.map((t) => ({ ...t, n: people.filter((p) => uses(p, t.id)).length })).filter((t) => t.n)
  return { id, name: people[0]?.dept_name ?? id, people, clusters, count }
}))
const floorPeople = (f) => members.value.filter((p) => f.depts.includes(p.dept_id)).length
const selected = computed(() => members.value.find((p) => p.id === selectedId.value))
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
function pickFloor(id) { floorId.value = id; selectedId.value = '' }
</script>

<template>
  <header class="page-head">
    <div><span class="small muted">운영 · 관리자 보기</span><h1>좌석 배치도</h1></div>
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
        <div><h2>{{ floor.label }} · {{ floor.title }}</h2><span class="small muted">책상을 누르면 오른쪽에 쓰는 도구가 나옵니다</span></div>
        <div class="legend">
          <span><i class="swatch" style="background: var(--white); outline: 1px solid var(--line-strong)" />재직</span>
          <span><i class="swatch hatch" />휴직</span>
          <span><i class="swatch" style="outline: 2px dashed var(--ink); background: var(--white)" />이용권 확인 필요</span>
          <span><i class="act-legend" />최근 30일 사용일 (평일 {{ WORKDAYS }}일 = 가득)</span>
        </div>
      </div>

      <div class="floor">
        <div v-for="z in zones" :key="z.id" class="zone">
          <div class="zone-head">
            <strong>{{ z.name }}</strong><span class="xs muted">{{ z.people.length }}명<template v-if="zoneAvg(z) != null"> · 평균 {{ zoneAvg(z) }}일 사용</template></span>
            <span v-for="c in z.count" :key="c.id" class="zone-count xs"><ProductLogo :tool-id="c.id" :name="c.product" :size="14" />{{ c.n }}</span>
          </div>
          <div class="islands">
            <div v-for="(cl, ci) in z.clusters" :key="ci" class="island">
              <template v-for="(p, di) in cl" :key="p?.id ?? `empty-${ci}-${di}`">
                <button v-if="p" type="button" class="desk" :class="{ leave: p.status !== 'active', check: needsCheck(p), dim: dim(p) }"
                  :aria-pressed="p.id === selectedId" :aria-label="deskLabel(p)" @click="selectedId = p.id">
                  <span class="desk-name">{{ p.name }}</span>
                  <span class="desk-tools">
                    <ProductLogo v-for="t in p.tools" :key="t.tool_id" :tool-id="t.tool_id" :name="t.product" :size="16" />
                    <span v-if="!p.tools.length" class="xs muted">없음</span>
                  </span>
                  <span v-if="days(p) != null" class="act" aria-hidden="true">
                    <span class="act-bar"><i :style="{ width: `${Math.min(100, (days(p) / WORKDAYS) * 100)}%` }" /></span>
                    <span class="act-n">{{ days(p) }}일</span>
                  </span>
                </button>
                <div v-else class="desk empty" aria-hidden="true"><span class="xs">빈 자리</span></div>
              </template>
            </div>
          </div>
        </div>
        <div class="rooms" aria-hidden="true">
          <div v-for="r in floor.rooms" :key="r" class="room">{{ r }}</div>
          <div class="room core">엘리베이터 · 계단</div>
        </div>
      </div>
      <p class="xs muted" style="margin: 0">직원·부서·도구·사용일은 DB 값이고, 층과 책상 위치는 시연용으로 임의 배치했습니다. 사용일은 이용권 관리에만 쓰고 인사평가에 쓰지 않습니다. Acrobat·Canva는 공급사가 사용 기록을 주지 않아 마지막 사용일만 보입니다.</p>
    </section>

    <aside class="card col-1 detail" aria-live="polite" aria-labelledby="who-h">
      <template v-if="selected">
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
        <RouterLink v-if="needsCheck(selected)" to="/licenses" class="btn btn-dark">구독·이용권에서 확인하기</RouterLink>
      </template>
      <template v-else>
        <h2 id="who-h">직원을 선택하세요</h2>
        <p class="small muted" style="margin: 0">책상을 누르면 그 자리 직원이 쓸 수 있는 도구와 이용권 상태가 나옵니다. 위의 도구 버튼으로 특정 도구를 쓰는 자리만 강조할 수 있습니다.</p>
      </template>
    </aside>
  </div>
</template>

<style scoped>
.filter { gap: 8px; }
.card-head > .legend { flex-direction: row; }
.tool-chip { display: inline-flex; align-items: center; gap: 8px; padding-left: 8px; }
.floor { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 16px; padding: 16px; border-radius: 12px; background: var(--paper); border: 2px solid var(--line-strong); }
.zone { display: flex; flex-direction: column; gap: 12px; padding: 14px; border-radius: 10px; background: var(--white); border: 1px solid var(--line); }
.zone-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.zone-count { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px 2px 4px; border-radius: 999px; border: 1px solid var(--line-strong); }
.islands { display: flex; flex-wrap: wrap; gap: 16px; }
.island { display: grid; grid-template-columns: repeat(3, minmax(0, 76px)); gap: 4px; padding: 6px; border-radius: 8px; background: var(--track); }
.desk { min-height: 64px; padding: 6px 4px; border-radius: 6px; border: 1px solid var(--line-strong); background: var(--white); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; }
.desk:hover { border-color: var(--ink); }
.desk[aria-pressed='true'] { background: var(--ink); color: var(--white); border-color: var(--ink); }
.desk.check { outline: 2px dashed var(--ink); outline-offset: -4px; }
.desk.leave { background: repeating-linear-gradient(45deg, var(--white) 0 6px, var(--track) 6px 12px); }
.desk.dim { opacity: 0.3; }
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
