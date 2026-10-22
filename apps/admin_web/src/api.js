// 화면이 부르는 데이터 함수 전부. 백엔드 응답(Envelope{data}, packages/schemas)을 화면 모양으로 바꾼다. 화면은 이 파일만 본다.
// 함수 이름과 반환 모양은 목업 시절과 같다 (화면 수정 없이 연결). 변환 규칙은 각 함수 주석.
import { get, post, put, request, createTask } from './request.js'
import { PLAN_TYPE, PLAN_STATUS, BILLING, TOOL_LABEL, cap } from './labels.js'

// 서버 시각은 UTC('…Z'). 화면은 한국 시간 'MM-DD HH:mm' (sv-SE 형식이 'YYYY-MM-DD HH:mm:ss')
export const dt = (s) => (s ? new Date(s).toLocaleString('sv-SE', { timeZone: 'Asia/Seoul' }).slice(5, 16) : '')
const daysAgo = (s) => (s ? `${Math.max(0, Math.round((Date.now() - new Date(s)) / 86400000))}일 전` : '기록 없음')

// 화면에 ID 대신 이름을 보이기 위한 사전: 직원·계약(제품)·제품·모델·프로젝트. 세션 동안 한 번만 받는다.
// ponytail: 새로 생긴 직원·프로젝트는 새로고침 전까지 ID로 보인다. 필요하면 화면 이동 때 다시 받는다.
let namesP
export function getNames() {
  namesP ??= Promise.all(['/org/members', '/tools', '/models', '/projects'].map((u) => get(u).catch(() => []))).then(([ms, tools, models, projects]) => new Map([
    ['sys', 'AI 비서(시스템)'],
    ...ms.map((m) => [m.employee.id, m.employee.name]),
    ...ms.map((m) => [m.employee.dept_id, m.employee.dept_name]),
    ...tools.map((t) => [t.id, t.product]),
    ...tools.filter((t) => t.contract).map((t) => [t.contract.id, t.product]),
    ...models.map((m) => [m.id, m.name]),
    ...projects.map((p) => [p.id, p.name]),
  ]))
  return namesP
}
const named = (names, v) => (typeof v === 'string' ? names.get(v) ?? v : typeof v === 'number' ? v.toLocaleString('ko-KR') : String(v))

// GET /me
export async function getMe() {
  const m = await get('/me')
  const e = m.employee
  return { name: e.name, role: `${e.role === 'admin' ? '관리자' : '직원'} · ${e.job_title}`, employee: e }
}

// GET /inventory → 제품 목록 (좌석형) + GET /tools (API형 제품)
export async function getProducts() {
  const [inv, tools, projects, models] = await Promise.all([get('/inventory'), get('/tools'), get('/projects').catch(() => []), get('/models').catch(() => [])])
  const vendorOf = Object.fromEntries(models.map((m) => [m.id, m.vendor])) // 모델 → 공급사 → 그 공급사의 API 제품
  const seatProducts = inv.map((i) => ({
    id: i.contract.tool_id, name: i.tool_name, plan: i.contract.plan_name, kind: (tools.find((t) => t.id === i.contract.tool_id)?.kind === 'software') ? '일반 소프트웨어' : 'AI',
    billing: BILLING[i.contract.billing_type] ?? i.contract.billing_type,
    seatsPurchased: i.purchased, seatsAssigned: i.assigned, seatPrice: i.contract.seat_price_krw, monthCost: i.purchased * i.contract.seat_price_krw, // 미배정 좌석도 돈을 낸다
    renewal: i.contract.term_end?.slice(0, 10),
    seats: i.low_usage_candidates.map((c) => ({
      id: c.employee_id, name: c.employee_name, dept: c.dept_name, flag: c.flag, lastUsed: daysAgo(c.last_activity_at),
      confirmation: c.needs_confirmation ? '아직 묻지 않음' : '확인 필요 없음', action: c.needs_confirmation ? 'ask' : null,
      contractId: i.contract.id,
    })),
  }))
  const apiProducts = tools.filter((t) => t.contract?.billing_type === 'api_usage').map((t) => ({
    id: t.id, name: t.product, plan: t.contract.plan_name, kind: 'AI', billing: BILLING.api_usage, seatsPurchased: 0, seatsAssigned: 0, seatPrice: 0,
    monthCost: null, renewal: t.contract.term_end?.slice(0, 10), projects: projects.filter((p) => vendorOf[p.model_id] === t.vendor).length, seats: [],
  }))
  return [...seatProducts, ...apiProducts]
}

// GET /plans?type=seat_assign → 이용권 신청 목록
export async function getSeatRequests() {
  const [page, names] = await Promise.all([get('/plans', { type: 'seat_assign' }), getNames()])
  return page.items.map((p) => ({
    id: p.id, who: named(names, p.requested_by), product: named(names, p.items[0]?.contract_id ?? ''), purpose: p.rationale,
    status: p.status === 'pending_approval' ? 'pending' : 'done',
    note: p.status === 'executed' ? `미배정 좌석 배정 완료 · ${dt(p.executed_at)}` : p.status === 'pending_approval' ? '미배정 좌석 없음 → AI 의견과 함께 승인 대기' : (PLAN_TYPE[p.type] + ' · ' + (PLAN_STATUS[p.status] ?? p.status)),
  }))
}

// GET /anomalies + GET /anomalies/{id} → 알림 + 추이 + 조사 틀. AI 조사 결과는 알림 상세의 project·breakdown으로 근거만 채우고, 원인 후보는 '원인 조사 실행'(에이전트)으로 받는다.
export async function getAnomalies() {
  const [list, names, tools, models] = await Promise.all([get('/anomalies'), getNames(), get('/tools').catch(() => []), get('/models').catch(() => [])])
  const productOf = (modelId) => tools.find((t) => t.vendor === models.find((m) => m.id === modelId)?.vendor && t.contract?.billing_type === 'api_usage')?.product ?? ''
  return Promise.all(list.map(async (a) => {
    const d = await get(`/anomalies/${a.id}`)
    const daily = d.series.map((p) => p.cost.krw)
    const now = new Date()
    const monthToDate = d.series.filter((p) => new Date(p.period_start).getMonth() === now.getMonth()).reduce((s, p) => s + p.cost.krw, 0)
    return {
      id: a.id, status: a.status, scopeName: a.scope_name ?? a.scope_id, product: productOf(d.project?.model_id), model: named(names, d.project?.model_id ?? ''), env: d.project?.environment === 'prod' ? '운영' : '테스트', dept: named(names, d.project?.dept_id ?? ''),
      detectedAt: dt(a.created_at), baseline: Math.round(a.baseline), observed: Math.round(a.observed), ratio: a.ratio, projected: a.projected_monthly_cost?.krw ?? 0,
      budget: d.project?.monthly_budget_krw ?? 0, monthToDate, daily, dailyStart: d.series[0]?.period_start?.slice(5, 10).replace('-', '/') ?? '',
      projectId: d.project?.id, purpose: d.project?.purpose,
      investigation: {
        doneAt: null, causes: [],
        evidence: ['프로젝트 등록 목적·예산', ...d.breakdown_by_model.map((b) => `모델 ${b.key} ${Math.round(b.share * 100)}%`), ...d.breakdown_by_input_length.map((b) => `입력 ${b.key} ${Math.round(b.share * 100)}%`)],
        actions: [
          { id: 'repeat_check', label: '반복 요청 점검 · 부서 확인 요청' },
          { id: 'budget_review', label: '예산 검토' },
          { id: 'model_review', label: '품질 평가 후 모델 검토' },
        ],
      },
    }
  }))
}

// GET /projects + 프로젝트별 이번 달 GET /usage. 급증 표시는 열린 알림에서.
export async function getProjects() {
  const [projects, anomalies, names] = await Promise.all([get('/projects'), get('/anomalies', { status: 'open' }).catch(() => []), getNames()])
  const now = new Date(), start = new Date(now.getFullYear(), now.getMonth(), 1)
  return Promise.all(projects.map(async (p) => {
    const u = await get('/usage', { scope: 'project', scope_id: p.id, start: start.toISOString(), end: now.toISOString() }).catch(() => null)
    return { id: p.id, name: p.name, dept: named(names, p.dept_id), model: named(names, p.model_id), env: p.environment === 'prod' ? '운영' : '테스트', cost: u?.summary.cost.krw ?? 0, budget: p.monthly_budget_krw,
      anomaly: anomalies.some((a) => a.scope_id === p.id) }
  }))
}

// GET /projects + GET /usage(scope=contract, group_by=model) → 최근 n개월 API 모델별 비용과 예산.
// 좌석 정액료는 특정 모델에 귀속되지 않으므로 넣지 않는다.
// 예산 = API 연간 예산 ÷ 12 (설정 안 했으면 프로젝트 월 예산 합). 과거 예산 이력은 없어 지난달도 지금 예산과 비교한다.
export async function getModelCostMonths(n = 3) {
  const [projects, tools, b] = await Promise.all([get('/projects'), get('/tools'), get('/budgets').catch(() => null)])
  const contracts = tools.filter((t) => t.contract?.billing_type === 'api_usage').map((t) => t.contract.id)
  const allocated = projects.reduce((s, p) => s + p.monthly_budget_krw, 0)
  const budget = b?.api_monthly_krw || allocated, budgetSource = b?.api_monthly_krw ? 'annual' : 'projects'
  const now = new Date()
  return Promise.all(Array.from({ length: n }, async (_, k) => {
    const start = new Date(now.getFullYear(), now.getMonth() - (n - 1 - k), 1)
    const next = new Date(start.getFullYear(), start.getMonth() + 1, 1)
    const ongoing = next > now
    const us = await Promise.all(contracts.map((id) => get('/usage', { scope: 'contract', scope_id: id, start: start.toISOString(), end: (ongoing ? now : new Date(next - 1)).toISOString(), group_by: 'model' })))
    const models = new Map()
    for (const b of us.flatMap((u) => u.breakdown)) {
      const m = models.get(b.key) ?? { name: b.key, cost: 0, requests: 0 }
      m.cost += b.cost.krw; m.requests += b.requests; models.set(b.key, m)
    }
    const days = us.flatMap((u) => u.series.map((p) => p.period_start.slice(0, 10))).sort()
    const monthDays = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate()
    return { label: `${start.getMonth() + 1}월`, budget, budgetSource, allocated, monthDays, elapsed: ongoing ? now.getDate() : monthDays, ongoing,
      dataDays: new Set(days).size, firstDay: days[0]?.slice(5).replace('-', '/'), models: [...models.values()].sort((a, b) => b.cost - a.cost) }
  }))
}

// GET /org/members → 좌석 배치도. 도구 판정(배정 좌석 + 팀 프로젝트 API)은 백엔드가 한다.
export async function getOrgMembers() {
  return (await get('/org/members')).map((m) => ({ ...m.employee, tools: m.tools }))
}

// GET /inventory + /savings + /budgets → 홈 요약. 좌석 정액료만 센다 (API 사용량 비용은 getModelCostMonths).
export async function getSummary() {
  const [inv, savings, products, budget] = await Promise.all([get('/inventory'), get('/savings').catch(() => ({ by_kind: {}, total_krw: 0 })), getProducts(), get('/budgets').catch(() => null)])
  const seatsTotal = inv.reduce((s, i) => s + i.purchased, 0), seatsAssigned = inv.reduce((s, i) => s + i.assigned, 0)
  const seatsToCheck = inv.reduce((s, i) => s + i.low_usage_candidates.filter((c) => c.needs_confirmation).length, 0)
  const seatCost = products.reduce((s, p) => s + (p.monthCost || 0), 0)
  return { seatCost, seatBudget: budget?.seat_monthly_krw ?? 0, apiBudget: budget?.api_monthly_krw ?? 0, seatsAssigned, seatsTotal, seatsToCheck, savingsMonth: savings.total_krw,
    savingsNote: Object.entries(savings.by_kind).map(([k, v]) => `${k === 'purchase_avoided' ? '아낀 구매 비용' : k} ${Math.round(v).toLocaleString('ko-KR')}원`).join(' · ') || '기록 없음' }
}

// GET /plans → 승인 대기·이력. steps는 상태에서 만들고, AI 의견은 ai_opinion(없으면 요청 사유)에서 만든다.
const PLAN_STEPS = (p) => {
  const base = [{ label: '요청 접수', at: dt(p.created_at), done: true }]
  if (p.ai_opinion) base.push({ label: 'AI 의견', at: '첨부됨', done: true })
  if (p.status === 'pending_approval') base.push({ label: p.risk_level === 'high' ? '책임자 결정' : '관리자 승인', at: '지금 단계', current: true }, { label: '실행·안내', at: '승인 후' })
  else if (p.status === 'approved') base.push({ label: '관리자 승인', at: dt(p.approved_at), done: true }, { label: '실행·안내', at: '실행 전', current: true })
  else if (p.status === 'executed') base.push({ label: '관리자 승인', at: dt(p.approved_at) || '자동', done: true }, { label: '실행·안내', at: dt(p.executed_at), done: true })
  else if (p.status === 'rejected') base.push({ label: '반려', at: p.reject_reason ?? '', done: true })
  return base
}
// id → 이름 해석 (AI 의견이 없는 요청의 변경 내용·참고 데이터용). 카탈로그는 한 번만 읽고, 직원은 관리자 전용 GET /employees/{id}로 본다.
const names = { tools: null, contracts: null, models: null, employees: {} }
async function loadNames() {
  if (names.tools) return
  const [tools, inv, models] = await Promise.all([get('/tools').catch(() => []), get('/inventory').catch(() => []), get('/models').catch(() => [])])
  names.tools = Object.fromEntries(tools.map((t) => [t.id, t.product]))
  names.contracts = Object.fromEntries(inv.map((i) => [i.contract.id, `${i.tool_name} (${i.contract.plan_name})`]))
  names.models = Object.fromEntries(models.map((m) => [m.id, m.name]))
}
async function employeeName(id) {
  if (!(id in names.employees)) {
    try { const p = await get(`/employees/${id}`); names.employees[id] = `${p.employee.name} (${p.employee.dept_name ?? ''} · ${p.employee.job_title})` } catch { names.employees[id] = id }
  }
  return names.employees[id]
}
async function humanize(k, v) {
  if (k === 'employee_id' || k === 'reassign_to_employee_id') return employeeName(v)
  if (k === 'contract_id') return names.contracts?.[v] ?? v
  if (k === 'tool_id') return names.tools?.[v] ?? v
  if (k === 'from_model_id' || k === 'to_model_id' || k === 'model_id') return names.models?.[v] ?? v
  if (k === 'confirmation_id') return '사용 의사 응답 있음'
  return humanValue(k, v)
}
const EVIDENCE_KO = { confirmations: '사용 의사 응답', inventory: '이용권 현황', get_license_inventory: '이용권 현황', eval_results: '모델 품질 평가', get_eval_results: '모델 품질 평가', usage_records: '사용량', get_usage_summary: '사용량', get_anomaly_detail: '비용 급증 알림 상세', get_my_profile: '직원 프로필', vendor_update: '제품 업데이트', get_vendor_update: '제품 업데이트', me: '직원 프로필' }
async function evidenceText(e) {
  const label = EVIDENCE_KO[e.source] ?? TOOL_LABEL[e.source] ?? e.source
  const ref = names.contracts?.[e.ref] ?? names.tools?.[e.ref] ?? names.models?.[e.ref] ?? (String(e.ref).startsWith('emp_') ? await employeeName(e.ref) : String(e.ref).startsWith('cfm_') ? '반납 응답' : e.ref)
  return `${label}: ${ref}`
}
const RECO = { approve: '승인 권장', reject: '반려 권장', alternative: '대안 권장' }
const ENV_KO = { test: '테스트', prod: '운영' }
const humanValue = (k, v) => (k === 'environment' ? (ENV_KO[v] ?? v) : k.endsWith('_krw') ? `${Number(v).toLocaleString('ko-KR')}원` : String(v))
function opinionView(p) {
  const o = p.ai_opinion
  if (!o) return p.status === 'pending_approval' ? { title: 'AI 의견 작성 중…', body: '요청자·자산 현황을 확인하고 있습니다. 잠시 뒤 자동으로 표시됩니다.', pending: true } : { title: '요청 사유', body: p.rationale }
  const lines = []
  if (o.requester) lines.push(`요청자: ${o.requester.name} · ${o.requester.dept} · ${o.requester.job_title}${o.requester.current_products?.length ? ` · 현재 ${o.requester.current_products.join(', ')}` : ''}`)
  if (o.asset) lines.push(`자산: ${o.asset.product}${o.asset.purchased != null ? ` ${o.asset.purchased}석 중 배정 ${o.asset.assigned}석, 미배정 ${o.asset.unassigned}석` : ''}${o.asset.seat_price_krw ? ` · 좌석 월 ${Number(o.asset.seat_price_krw).toLocaleString('ko-KR')}원` : ''}${o.asset.note ? ` · ${o.asset.note}` : ''}`)
  return { title: `${RECO[o.recommendation] ?? 'AI 의견'} · ${o.summary}`, body: lines.join('\n'), facts: o.facts ?? [], risks: o.risks ?? [], alternative: o.alternative ?? null, recommendation: o.recommendation }
}
const KEY_KO = { employee_id: '직원', contract_id: '계약', project_id: '프로젝트', tool_id: '제품', purpose: '용도', model_id: '모델', expected_requests_per_day: '예상 일 요청 수', from_model_id: '현재 모델', to_model_id: '변경 모델', environment: '환경', confirmation_id: '사용 의사 응답', reassign_to_employee_id: '재배정 대상', tool_id: '제품', quantity: '수량', current_budget_krw: '현재 예산', proposed_budget_krw: '제안 예산', note: '메모', capability_tag: '필요 기능' }
export async function getPlans() {
  const page = await get('/plans', { limit: 100 })
  await loadNames()
  return Promise.all(page.items.map(async (p) => ({
    id: p.id, type: p.type, risk: p.risk_level, status: p.status, createdAt: dt(p.created_at),
    title: PLAN_TYPE[p.type] ?? p.type,
    subtitle: '',  // 아래 changes에서 채운다 (이름 기준)
    requester: p.ai_opinion?.requester ? `${p.ai_opinion.requester.name} (${p.ai_opinion.requester.dept})` : await employeeName(p.requested_by), summary: p.rationale, steps: PLAN_STEPS(p),
    aiOpinion: opinionView(p),
    opinion: p.ai_opinion ?? null,
    changes: p.ai_opinion?.changes?.length ? p.ai_opinion.changes : await Promise.all(Object.entries(p.items[0] ?? {}).filter(([, v]) => v != null && v !== '').map(async ([k, v]) => [KEY_KO[k] ?? k, await humanize(k, v)])),
    evidence: p.ai_opinion?.evidence_text?.length ? p.ai_opinion.evidence_text : await Promise.all((p.evidence ?? []).map(evidenceText)),
    raw: p,
  }))).then((rows) => rows.map((r) => ({ ...r, subtitle: r.changes.slice(0, 2).map((c) => c[1]).join(' · ') })))
}

// 웹에서 만든 처리 요청에도 AI 검토를 붙인다 (에이전트가 만든 요청은 에이전트가 스스로 검토를 띄운다)
function requestReview(planId) {
  createTask(`처리 요청 ${planId}를 검토해 관리자용 의견을 붙여줘`, { promptName: 'review_plan', promptArgs: { plan_id: planId } }).catch(() => {})
}

// GET /updates + GET /suggestions → 업데이트별 판단 (팀별 결과는 제안 기록에서)
export async function getUpdates() {
  const [upd, sugg] = await Promise.all([get('/updates'), get('/suggestions', { limit: 200 })])
  const byUpdate = (id) => (sugg.items ?? sugg).filter((s) => s.update_id === id)
  return upd.items.map((u) => {
    const ss = byUpdate(u.id)
    const anySent = ss.some((s) => s.status === 'sent' || s.status === 'draft' || s.status === 'pending_approval')
    const hold = ss.find((s) => s.status === 'on_hold')
    return {
      id: u.id, product: u.vendor, date: u.published_at.slice(5, 10).replace('-', '/'), title: u.title, sourceUrl: u.url,
      result: anySent ? 'sent' : 'held', note: !ss.length ? '아직 검토하지 않음' : hold && !anySent ? `전체 보류 · ${hold.hold_reason}` : undefined,
      teams: ss.filter((s) => s.status !== 'on_hold' || s.target_id).map((s) => ({
        team: s.target_id ?? '전사', result: s.status === 'sent' ? 'sent' : s.status === 'on_hold' ? 'held' : 'sent', at: s.sent_at ? dt(s.sent_at) : undefined,
        reason: s.status === 'on_hold' ? s.hold_reason : s.relevance, reaction: undefined,
      })),
    }
  })
}
export async function getSuggestionStats() {
  const [upd, sugg, teams] = await Promise.all([get('/updates'), get('/suggestions', { limit: 200 }), get('/adoption/teams').catch(() => [])])
  const items = sugg.items ?? sugg
  const useful = teams.reduce((s, t) => s + t.useful, 0), irrelevant = teams.reduce((s, t) => s + t.irrelevant, 0)
  return { collected: upd.total ?? upd.items.length, sent: items.filter((s) => s.status === 'sent').length, held: items.filter((s) => s.status === 'on_hold').length,
    helpfulRate: useful + irrelevant ? Math.round((useful / (useful + irrelevant)) * 100) : 0, nextRun: '주 1회' }
}
// 팀별 발송 스위치: 백엔드에 GET/PATCH /departments 가 아직 없다 (백엔드 요청 항목). 팀 목록만 활용 현황에서 가져온다.
export async function getTeamSwitches() {
  const teams = await get('/adoption/teams').catch(() => [])
  return teams.map((t) => ({ id: t.team_id, team: t.team_name, channel: '', enabled: true, readonly: true }))
}
export function setTeamSwitch() { return Promise.resolve() }

// GET /unmet-needs
export async function getUnmetNeeds() {
  const rows = await get('/unmet-needs')
  return rows.map((n) => ({ capability: cap(n.capability_tag), count: n.count, depts: Array.from({ length: n.dept_count }, (_, i) => `부서 ${i + 1}`), deptCount: n.dept_count,
    example: n.sample_summaries[0] ?? '', thresholdMet: n.threshold_met }))
}
// GET /adoption/teams
export async function getTeamAdoption() {
  const rows = await get('/adoption/teams')
  return rows.map((t) => ({ team: t.team_name, rate: Math.round(t.active_ratio * 100), seats: t.headcount }))
}

// 사이드바 배지
export async function getCounts() {
  const [plans, anomalies] = await Promise.all([get('/plans', { status: 'pending_approval' }).catch(() => ({ items: [] })), get('/anomalies', { status: 'open' }).catch(() => [])])
  return { inbox: plans.items.length, cost: anomalies.length }
}

// POST /confirmations — 사용 의사 확인 요청. 백엔드가 plan_id를 요구하므로 그 직원의 회수 요청이 있으면 그것을, 없으면 먼저 회수 요청(승인 대기)을 만든다.
export async function requestConfirmation(employeeId) {
  const plans = await get('/plans', { type: 'seat_reclaim' })
  let plan = plans.items.find((p) => p.items[0]?.employee_id === employeeId && p.status === 'pending_approval')
  if (!plan) {
    const inv = await get('/inventory')
    const contract = inv.find((i) => i.low_usage_candidates.some((c) => c.employee_id === employeeId))?.contract.id
    plan = await post('/plans', { type: 'seat_reclaim', items: [{ employee_id: employeeId, contract_id: contract }], rationale: '장기 미사용 이용권 사용 의사 확인', evidence: [{ source: 'get_license_inventory', ref: contract }] })
  }
  const c = await post('/confirmations', { employee_id: employeeId, plan_id: plan.id, question: '이용권을 계속 사용하시나요?' })
  requestReview(plan.id)
  return c
}

// POST /plans — 비용 급증 알림 대응 → 처리 요청
export async function createPlanFromAnomaly(anomalyId, actionId) {
  const a = (await getAnomalies()).find((x) => x.id === anomalyId)
  const label = a.investigation.actions.find((x) => x.id === actionId)?.label ?? actionId
  const plan = await post('/plans', { type: 'budget_review', items: [{ project_id: a.projectId, current_budget_krw: a.budget, note: label }], rationale: `${a.scopeName} 비용 급증(${a.ratio}배) 대응: ${label}`, evidence: [{ source: 'get_anomaly_detail', ref: anomalyId }] })
  requestReview(plan.id)
  return plan.id
}

// POST /plans/{id}/approve → (책임자 결정이 아니면) POST /plans/{id}/execute
export async function approvePlan(id, reason = '관리자 승인') {
  const p = await post(`/plans/${id}/approve`, { reason })
  if (p.risk_level === 'high') return p.status
  const r = await request(`/plans/${id}/execute`, { method: 'POST', headers: { 'Idempotency-Key': `ui_${id}_${Date.now()}` } })
  return r.plan.status
}
export async function rejectPlan(id, reason = '관리자 반려') {
  await post(`/plans/${id}/reject`, { reason })
  return 'rejected'
}

// ── 초기 설정 마법사·설정 (GET/POST /setup·/departments·/employees·/contracts·/models/approved·/projects·/budgets) ──
export const getSetupStatus = () => get('/setup/status')
export const completeSetup = () => post('/setup/complete')
export const getDepartments = () => get('/departments')
export const createDepartment = (name) => post('/departments', { name })
export const createEmployees = (rows) => post('/employees', rows)
export const createContract = (body) => post('/contracts', body)
export const getModelCatalog = () => get('/setup/model-catalog')
export const approveModels = (ids) => put('/models/approved', { model_ids: ids })
export const createProject = (body) => post('/projects', body)
export const getBudgets = (year) => get('/budgets', year ? { fiscal_year: year } : undefined)
export const setBudgets = (body) => put('/budgets', body)
export async function getContracts() {
  const tools = await get('/tools')
  return tools.filter((t) => t.contract).map((t) => ({ toolId: t.id, product: t.product, vendor: t.vendor, ...t.contract }))
}
export const getEmployees = async () => (await get('/org/members')).map((m) => m.employee)
