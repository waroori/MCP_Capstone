"""시드 SQL을 표준 출력으로 낸다. fixtures.py의 id를 그대로 넣고(계약 테스트), 시연용 더미를 덧붙인다.

사용: uv run python db/seed/seed.py | docker compose exec -T postgres psql -U capstone -d capstone
(보통은 db/reset.sh가 스키마와 함께 한 번에 넣는다)
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

import bcrypt

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 저장소 루트 (apps.backend.fixtures)
from apps.backend import fixtures as fx  # noqa: E402

ARRAY_COLS = {"tools_used", "affected_plans", "sent_dept_ids"}
JSON_COLS = {"items", "evidence", "result", "before", "after", "held", "payload"}
random.seed(7)  # 매번 같은 더미


def lit(col: str, v) -> str:
    if v is None:
        return "NULL"
    if col in JSON_COLS:
        return "'" + json.dumps(v, ensure_ascii=False, default=str).replace("'", "''") + "'::jsonb"
    if col in ARRAY_COLS:
        return "'{" + ",".join(f'"{x}"' for x in v) + "}'"
    if isinstance(v, Enum):
        v = v.value
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def insert(table: str, rows: list[dict]) -> None:
    for r in rows:
        cols = ", ".join(r)
        vals = ", ".join(lit(c, v) for c, v in r.items())
        print(f"INSERT INTO {table} ({cols}) VALUES ({vals});")


def dump(objs, drop=(), **extra) -> list[dict]:
    """pydantic 응답 모델 → 행. drop: 파생 필드(테이블에 없는 것)."""
    out = []
    for o in objs:
        d = o.model_dump(mode="python")
        for k in drop:
            d.pop(k, None)
        d.update(extra)
        out.append(d)
    return out


NOW = fx.NOW
DAY0 = NOW - timedelta(days=60)
print("SET search_path TO app;\nBEGIN;")

# ── 조직 ──
slack = {"dept_hrd": "C_HRD", "dept_mkt": "C_MKT", "dept_sales": "C_SALES", "dept_cs": "C_CS", "dept_prod": "C_PROD", "dept_mgmt": "C_MGMT", "dept_md": "C_MD"}
insert("departments", [{**d, "slack_channel_id": slack[d["id"]], "suggestions_enabled": d["id"] != "dept_prod"}
                       for d in dump(fx.DEPARTMENTS.values())])
insert("company", [{"name": "한빛제조", "setup_completed_at": DAY0, "created_at": DAY0}])  # 초기 설정을 마친 상태

SURNAMES = "김이박최정강조윤장임한오서신권황안송류홍"
GIVEN = ["민서", "지호", "나연", "도현", "하은", "준우", "예린", "시우", "수아", "건우", "지안", "유준", "다은", "현우", "채원", "승민"]
TITLES = {"dept_mgmt": "경영지원", "dept_hrd": "교육 운영", "dept_mkt": "마케터", "dept_sales": "영업 담당", "dept_cs": "CS 상담", "dept_prod": "생산 계획", "dept_md": "상품기획"}
# 이름 로마자 (회사 이메일 = 좌석 제품 SSO 아이디. 페르소나와 같은 '이름.성@' 규칙)
ROMAN_SUR = dict(zip(SURNAMES, "kim lee park choi jung kang cho yoon jang lim han oh seo shin kwon hwang ahn song ryu hong".split()))
ROMAN_GIVEN = dict(zip(GIVEN, "minseo jiho nayeon dohyun haeun junwoo yerin siwoo sua gunwoo jian yujun daeun hyunwoo chaewon seungmin".split()))
gen_emps, used_names = [], {e.name for e in fx.EMPLOYEES.values()}
used_ids, seq = set(fx.EMPLOYEES), {}
GEN = {}  # 생성 순번 → 직원 id (시나리오에 쓰는 특정 직원을 가리킬 때)
for i in range(1, 53):  # 페르소나 포함 사람 59명 → ChatGPT Enterprise 60석 중 58석 배정 (fixtures와 같은 수치)
    dept = list(TITLES)[i % len(TITLES)]
    sur, giv = random.choice(SURNAMES), random.choice(GIVEN)
    while sur + giv in used_names:  # 동명이인 없이: 이름을 차례로 바꿔 본다 (난수를 더 쓰지 않아 다른 더미 값은 그대로)
        giv = GIVEN[(GIVEN.index(giv) + 1) % len(GIVEN)]
    used_names.add(sur + giv)
    short = dept.removeprefix("dept_")  # id = 부서 + 번호 (페르소나 emp_mkt_01 과 같은 규칙, 겹치면 다음 번호)
    while f"emp_{short}_{seq.setdefault(short, 1):02d}" in used_ids:
        seq[short] += 1
    eid = f"emp_{short}_{seq[short]:02d}"
    used_ids.add(eid)
    GEN[i] = eid
    gen_emps.append({"id": eid, "name": sur + giv,
                     "email": f"{ROMAN_GIVEN[giv]}.{ROMAN_SUR[sur]}@hanbit.example", "dept_id": dept, "role": "employee",
                     "job_title": TITLES[dept], "status": "on_leave" if i in (7, 23) else "active",
                     "hire_date": (NOW - timedelta(days=20 if i in (52, 53, 54) else 400 + i * 9)).date()})
# 시연용 로그인 비밀번호. 모든 사람 계정 공통(시스템 계정 sys는 비밀번호 없음 = 로그인 불가)
DEMO_PASSWORD = "demo1234"
pw = bcrypt.hashpw(DEMO_PASSWORD.encode(), bcrypt.gensalt(rounds=10)).decode()
emps = dump(fx.EMPLOYEES.values(), drop=("dept_name",)) + gen_emps
insert("employees", [{**e, "password_hash": None if e["id"] == "sys" else pw} for e in emps])
humans = [e for e in list(fx.EMPLOYEES) + [g["id"] for g in gen_emps] if e != "sys"]
emp_dept_of = {e["id"]: e["dept_id"] for e in emps}

# ── 책상 ── 부서별 구역에 6개씩 섬. 사람 수 + 빈 자리 2개 이상이 되게 (신입 배치 시연)
FLOOR_OF = {"dept_mgmt": "5F", "dept_hrd": "5F", "dept_mkt": "4F", "dept_sales": "4F", "dept_md": "4F", "dept_cs": "3F", "dept_prod": "3F"}
desks = []
for d, fl in FLOOR_OF.items():
    people = sorted(e["id"] for e in emps if e["dept_id"] == d and e["id"] != "sys")
    for k in range(-(-(len(people) + 2) // 6) * 6):
        desks.append({"id": f"{fl}-{d.removeprefix('dept_')}-{k + 1:02d}", "floor": fl, "dept_id": d, "seq": k + 1, "employee_id": people[k] if k < len(people) else None})
insert("desks", desks)

# ── 제품·계약·모델 ──
insert("tools", dump(fx.TOOLS.values(), kind="ai") + [
    {"id": "tool_adobe", "kind": "software", "vendor": "Adobe", "product": "Acrobat Pro", "category": "pdf_editor",
     "approval_status": "approved", "allowed_data_class": "confidential", "policy_note": "계약서 PDF 편집·비교. AI 불필요 업무 안내용"}])
contracts = dump(fx.CONTRACTS.values()) + [
    {"id": "ctr_adobe_2026", "tool_id": "tool_adobe", "plan_name": "Acrobat Pro Teams", "billing_type": "seat_flat",
     "seats_purchased": 60, "seat_price_krw": 32000, "term_start": date(2026, 1, 1), "term_end": date(2026, 12, 31)},
    {"id": "ctr_canva_2026", "tool_id": "tool_canva", "plan_name": "Canva Enterprise", "billing_type": "seat_flat",
     "seats_purchased": 15, "seat_price_krw": 28000, "term_start": date(2026, 1, 1), "term_end": date(2026, 12, 31)}]
# F3: ChatGPT 좌석만 직원별 토큰 사용량을 받는다. 좌석당 하루 15만 토큰
for c in contracts:
    c["daily_token_quota"] = 150_000 if c["id"] == "ctr_chatgpt_2026" else 0
insert("contracts", contracts)
# 2026 연간 예산. 좌석: 구매 좌석료 월 504만원(ChatGPT 60·Acrobat 60·Canva 15석)의 약 105%, API: 프로젝트 월 예산 합(154.5만원)보다 약간 큰 월 180만원
insert("budgets", [{"fiscal_year": 2026, "category": "seat", "annual_krw": 63_600_000},
                   {"fiscal_year": 2026, "category": "api", "annual_krw": 21_600_000}])
# 모델 id = "공급사:API 모델명". 에이전트 LLM_MODEL·projects·usage_records·eval_results가 같은 키로 이어진다
insert("models", [{**m, "tool_id": {"anthropic": "tool_claude", "xai": "tool_grok", "openai": "tool_chatgpt"}[m["id"].split(":")[0]]}
                  for m in dump(fx.MODELS.values())])
insert("eval_results", dump(fx.EVAL_RESULTS))
# 팀별 대표 업무 (F1 관련성 판단 근거). tools_used는 그 사람이 실제로 쓸 수 있는 도구만: ChatGPT 좌석 보유자 tool_chatgpt,
# Claude API는 그 팀 프로젝트가 있을 때(고객지원·영업·경영지원·마케팅·생산관리)
TEAM_TASKS = {
    "dept_mgmt": [("월간 비용 보고서 요약", "부서별 SW 비용을 정리해 경영진 보고용 한 장으로 요약", "monthly", ["tool_chatgpt", "tool_claude"])],
    "dept_hrd": [("신입 교육 자료 초안", "직무별 온보딩 교안 초안과 퀴즈 작성", "monthly", ["tool_chatgpt"])],
    "dept_mkt": [("경쟁사 보도자료 비교", "경쟁사 보도자료 여러 건을 비교해 메시지 차이 정리", "weekly", ["tool_chatgpt", "tool_claude"])],
    "dept_sales": [("견적서 요약 검토", "고객 견적서 PDF 요약 결과를 검토해 영업 메모 작성", "daily", ["tool_chatgpt", "tool_claude"])],
    "dept_cs": [("응대 매뉴얼 갱신", "자주 묻는 문의를 모아 응대 매뉴얼 문구 개정", "weekly", ["tool_chatgpt", "tool_claude"])],
    "dept_prod": [("생산 일정 변경 공지", "라인별 일정 변경 사항을 공지문으로 정리", "weekly", ["tool_chatgpt", "tool_claude"])],
    "dept_md": [("시장조사 자료 정리", "경쟁 제품 가격·사양을 모아 신제품 기획안 표로 정리", "weekly", ["tool_chatgpt"])],
}
gen_tasks = []
for dept, tasks in TEAM_TASKS.items():
    for k, g in enumerate([g for g in gen_emps if g["dept_id"] == dept and g["status"] == "active"][:2]):
        title, desc, freq, tools = tasks[0]
        gen_tasks.append({"id": f"task_{g['id']}", "employee_id": g["id"], "title": title, "description": desc, "frequency": freq,
                          "tools_used": tools, "source": "bot" if k else "manual", "updated_at": NOW - timedelta(days=5 + k * 7)})
# 페르소나 서진(상품기획·MD 과장): 리뷰 분석 업무를 등록해 두었고, 아래에서 관련 Slack 제안을 받고 피드백한다
gen_tasks.append({"id": "task_md_review", "employee_id": "emp_md_01", "title": "신제품 고객 리뷰 분석", "frequency": "weekly", "tools_used": ["tool_chatgpt"],
                  "description": "온라인몰·SNS 리뷰 파일 여러 개를 모아 불만·요청을 분류하고 신제품 기획 보고서에 반영", "source": "manual",
                  "updated_at": NOW - timedelta(days=4)})
insert("registered_tasks", dump(fx.TASKS.values()) + gen_tasks)

# ── F1 ──
insert("vendor_sources", [
    {"id": "src_chatgpt_rn", "vendor": "OpenAI", "url": "https://openai.com/business/chatgpt-pricing/", "kind": "release_notes"},
    {"id": "src_claude_rn", "vendor": "Anthropic", "url": "https://platform.claude.com/docs/en/about-claude/pricing", "kind": "pricing"},
    {"id": "src_x_docs", "vendor": "xAI", "url": "https://docs.x.ai/developers/models", "kind": "docs"}])
insert("vendor_updates", dump(fx.UPDATES.values()))
sugs = []
for s in dump(fx.SUGGESTIONS.values()):
    tid = s.pop("target_id")
    s["target_employee_id" if s["target_type"] == fx.TargetType.employee else "target_dept_id"] = tid
    sugs.append(s)
sugs.append({"id": "sug_002", "update_id": "upd_chatgpt_doc_compare", "target_type": "team", "target_dept_id": "dept_prod",
             "relevance": "생산관리팀 등록 업무에 문서 비교 근거 없음", "body": "-", "status": "on_hold",
             "hold_reason": "관련 업무 근거 없음 · 팀 자동 발송 꺼짐"})
for i, (dept, n_fb) in enumerate([("dept_mkt", 3), ("dept_cs", 2)], start=10):
    sugs.append({"id": f"sug_{i:03d}", "update_id": "upd_chatgpt_doc_compare", "target_type": "team", "target_dept_id": dept,
                 "relevance": "팀 등록 업무에 여러 문서 비교가 있음", "body": "문서 비교 기능으로 캠페인 시안·문의 응답 초안을 나란히 비교해 보세요.",
                 "status": "sent", "sent_at": NOW - timedelta(days=3)})
sugs.append({"id": "sug_020", "update_id": "upd_chatgpt_doc_compare", "target_type": "employee", "target_employee_id": "emp_md_01", "task_id": "task_md_review",
             "relevance": "등록 업무 '신제품 고객 리뷰 분석'이 여러 리뷰 파일 비교를 필요로 함", "status": "sent", "sent_at": NOW - timedelta(days=2),
             "body": "등록하신 '신제품 고객 리뷰 분석'에 문서 비교 기능을 써 보세요. 리뷰 파일 여러 개를 올리면 공통 불만과 차이를 표로 정리해 줍니다. Enterprise 플랜에 포함돼 추가 비용이 없습니다."})
insert("suggestions", sugs)
fb = [{"id": "fb_001", "suggestion_id": "sug_001", "employee_id": "emp_hrd_01", "reaction": "useful", "created_at": NOW},
      {"id": "fb_020", "suggestion_id": "sug_020", "employee_id": "emp_md_01", "reaction": "useful", "created_at": NOW - timedelta(days=1)}]
for sid, dept, reactions in [("sug_010", "dept_mkt", ["useful", "useful", "question"]), ("sug_011", "dept_cs", ["useful", "irrelevant"])]:
    members = [g["id"] for g in gen_emps if g["dept_id"] == dept]
    fb += [{"id": f"fb_{sid}_{k}", "suggestion_id": sid, "employee_id": members[k], "reaction": r, "created_at": NOW - timedelta(days=2)}
           for k, r in enumerate(reactions)]
insert("feedback", fb)
insert("review_runs", [{"id": "rr_001", "update_id": "upd_chatgpt_doc_compare", "agent_task_id": "task_demo_001", "ran_at": NOW,
                        "sent_dept_ids": ["dept_hrd"], "held": [{"dept_id": "dept_prod", "reason": "근거 없음"}]}])

# ── F3 ──
PROJECTS = {p.id: p for p in fx.PROJECTS.values()}
PROJECTS.update({p.id: p for p in [
    fx.PROJECTS["prj_faq"].model_copy(update=dict(id="prj_mkt_copy", name="캠페인 카피 초안", dept_id="dept_mkt", owner_id="emp_mkt_01",
                                                  purpose="제품 소개 문구 여러 안 생성", model_id="anthropic:claude-sonnet-5-5", environment=fx.Environment.prod, monthly_budget_krw=80000)),
    fx.PROJECTS["prj_faq"].model_copy(update=dict(id="prj_prod_report", name="생산 일보 요약", dept_id="dept_prod", owner_id=GEN[5],
                                                  purpose="라인별 생산 일보를 요약해 일정 변경 공지 초안 작성", model_id="anthropic:claude-haiku-5-5", environment=fx.Environment.prod, monthly_budget_krw=15000)),
]})
insert("projects", dump(PROJECTS.values()))
insert("api_keys", [{"id": f"key_{p}", "project_id": p, "label": f"{p} 운영 키", "vendor_key_ref": f"vk_{p}", "created_at": DAY0}
                    for p in PROJECTS])
usage = []
base_reqs = {"prj_cs_classify": 1000, "prj_quote_summary": 300, "prj_faq": 60, "prj_mkt_copy": 150, "prj_prod_report": 800}  # 월 비용이 예산의 60~80%가 되게
TOKENS = {"0-2k": 1200, "2k-8k": 4500, "8k+": 12000}  # 구간별 요청당 입력 토큰
MIX = {"prj_cs_classify": {"0-2k": 0.95, "2k-8k": 0.05}, "prj_faq": {"0-2k": 0.7, "2k-8k": 0.3},
       "prj_quote_summary": {"2k-8k": 0.7, "8k+": 0.3}, "prj_mkt_copy": {"0-2k": 0.4, "2k-8k": 0.6}, "prj_prod_report": {"0-2k": 0.9, "2k-8k": 0.1}}
SPIKE_MIX = {"2k-8k": 0.2, "8k+": 0.8}  # 급증 구간: 긴 견적서 PDF가 몰림
for d in range(60):
    day = DAY0 + timedelta(days=d)
    for pid, reqs in base_reqs.items():
        p = PROJECTS[pid]
        m = fx.MODELS[p.model_id]
        # 사용량 증가: 견적서 요약 마지막 3일(긴 PDF), 캠페인 카피 마지막 2일(요청 수 4배). 홈의 모델별 비용에 보인다
        spike = (pid == "prj_quote_summary" and d >= 57) or (pid == "prj_mkt_copy" and d >= 58)
        total = int(reqs * random.uniform(0.9, 1.1) * ({"prj_quote_summary": 2.5, "prj_mkt_copy": 4}[pid] if spike else 1))
        for bucket, share in (SPIKE_MIX if spike and pid == "prj_quote_summary" else MIX[pid]).items():
            r = int(total * share)
            tin, tout = r * TOKENS[bucket], r * 300
            usd = round(tin / 1e6 * m.input_usd_per_mtok + tout / 1e6 * m.output_usd_per_mtok, 4)
            usage.append({"vendor_record_id": f"vr_{pid}_{d:02d}_{bucket}", "contract_id": "ctr_claude_2026", "period_start": day,
                          "project_id": pid, "api_key_id": f"key_{pid}", "model_id": p.model_id, "input_length_bucket": bucket,
                          "requests": r, "input_tokens": tin, "output_tokens": tout, "cost_usd": usd,
                          "cost_krw": round(usd * fx.FX)})
insert("usage_records", usage)

# ── F4 ── ChatGPT Enterprise 60석 중 배정 58석(emp_mkt_01은 신청 대기), Adobe 60석 중 20석
seats = []
for i, e in enumerate(x for x in humans if x != "emp_mkt_01"):
    flag, last, since = None, NOW - timedelta(days=random.randint(0, 6)), DAY0
    if e in (GEN[52],):  # 입사일(20일 전)에 배정 = plan_000 등
        flag, since = "recent_hire", NOW - timedelta(days=20)
    elif e in ("emp_sales_02", GEN[7], GEN[23]):
        flag, last = "on_leave", NOW - timedelta(days=50)
    elif i % 9 == 0:
        flag, last = "unused", NOW - timedelta(days=45)
    elif i % 13 == 0:
        flag, last = "no_record", None  # 기록 없음 = 확인 필요
    seats.append({"id": f"seat_a_{i:03d}", "contract_id": "ctr_chatgpt_2026", "employee_id": e, "status": "assigned",
                  "assigned_at": since, "last_activity_at": last, "flag": flag})
on_leave = {e["id"] for e in emps if e["status"] == "on_leave"}
for i, e in enumerate(humans[:20]):
    seats.append({"id": f"seat_ad_{i:03d}", "contract_id": "ctr_adobe_2026", "employee_id": e, "status": "assigned", "assigned_at": DAY0,
                  "last_activity_at": NOW - timedelta(days=50 if e in on_leave else random.randint(0, 30)), "flag": "on_leave" if e in on_leave else None})
# ChatGPT 좌석은 직원별 일 사용 기록(usage_records.employee_id)을 만든다. 좌석료에 포함된 사용이라 비용은 0.
# 부서마다 쓰는 정도가 다르고(마케팅·고객지원 많음, 생산관리 적음), 다섯 명 중 한 명은 가끔만 쓴다.
# 플래그와 맞춘다: 휴직자는 50일 전까지, 장기 미사용은 45일 전까지, 기록 없음은 0건, 최근 입사는 배정일부터.
DEPT_P = {"dept_md": 0.65, "dept_mkt": 0.8, "dept_cs": 0.75, "dept_sales": 0.6, "dept_mgmt": 0.55, "dept_hrd": 0.5, "dept_prod": 0.3}
chat_usage = []
for k, st in enumerate(x for x in seats if x["contract_id"] == "ctr_chatgpt_2026"):
    e = st["employee_id"]
    stop = {"on_leave": 50, "unused": 45}.get(st["flag"], 0)
    p = DEPT_P[emp_dept_of[e]] * (0.3 if k % 5 == 0 else 1)
    last = None
    for d in range(60):
        day = DAY0 + timedelta(days=d)
        if st["flag"] == "no_record" or day < st["assigned_at"] or day > NOW - timedelta(days=stop or 0) or random.random() > p * (0.15 if day.weekday() >= 5 else 1):
            continue
        r = random.randint(3, 25)
        chat_usage.append({"vendor_record_id": f"vr_chat_{e}_{d:02d}", "contract_id": "ctr_chatgpt_2026", "period_start": day, "employee_id": e,
                           "requests": r, "input_tokens": r * 1500, "output_tokens": r * 600, "cost_usd": 0, "cost_krw": 0,
                           "model_id": "openai:gpt-5.5-mini" if r < 12 else "openai:gpt-5.5", "input_length_bucket": "0-2k" if r < 12 else "2k-8k"})
        last = day + timedelta(hours=random.randint(9, 18))
    st["last_activity_at"] = last
# Canva: 마케팅 전원 + 영업 2명 (15석 중 12석). 한 명은 장기 미사용
canva = [g for g in humans if emp_dept_of[g] == "dept_mkt"] + [g for g in humans if emp_dept_of[g] == "dept_sales"][:2]
seats += [{"id": f"seat_cv_{i:03d}", "contract_id": "ctr_canva_2026", "employee_id": e, "status": "assigned", "assigned_at": DAY0,
           "last_activity_at": NOW - timedelta(days=50 if e in on_leave else 40 if i == 3 else random.randint(0, 10)),
           "flag": "on_leave" if e in on_leave else "unused" if i == 3 else None}
          for i, e in enumerate(canva)]
insert("seat_assignments", seats)
insert("usage_records", chat_usage)
# ── 처리 요청 · 절감 · 감사 ──
# plan_000: 지난달 신규 입사자 좌석 배정(실행 완료). 절감 기록 sav_001은 실행된 조치안에만 붙는다
plans = [{"id": "plan_000", "type": "seat_assign", "risk_level": "low", "status": "executed",
          "items": [{"employee_id": GEN[52], "contract_id": "ctr_chatgpt_2026"}], "rationale": "신규 입사자 배정 (미배정 좌석 사용)",
          "evidence": [{"source": "inventory", "ref": "ctr_chatgpt_2026", "value": {"unassigned": 3}}], "requested_by": "emp_asset_01",
          "approver_id": None, "execution_key": "auto_plan_000", "executed_at": NOW - timedelta(days=20),
          "result": {"vendor": "mock", "ok": True}, "created_at": NOW - timedelta(days=20)}] + dump(fx.PLANS.values())
for p in plans:
    if p["id"] == "plan_002":
        p["ai_opinion"] = "휴직자 좌석 재배정 권장. 대상자 사용 의사 확인 응답 후 실행"
insert("action_plans", plans)
insert("confirmations", dump(fx.CONFIRMATIONS.values()))
insert("savings_ledger", [{**s, "plan_id": "plan_000", "basis": "신규 입사자에게 미배정 좌석 배정, 추가 구매 회피 (월 45,000원 × 남은 2개월)"} for s in dump(fx.SAVINGS)])
insert("audit_log", dump(fx.AUDIT))

# ── 운영 ── 재시도 시연용
insert("job_runs", [
    {"id": "job_001", "name": "collect_usage", "queue": "batch", "status": "done", "attempt": 1, "idempotency_key": "collect_usage:2026-10-20T08",
     "created_at": NOW - timedelta(hours=1), "finished_at": NOW - timedelta(minutes=59)},
    {"id": "job_002", "name": "collect_usage", "queue": "batch", "status": "done", "attempt": 3, "idempotency_key": "collect_usage:2026-10-20T08:30",
     "last_error": "503 from mock_vendor (2회 재시도 후 성공)", "created_at": NOW - timedelta(minutes=30), "finished_at": NOW - timedelta(minutes=28)},
    {"id": "job_003", "name": "send_suggestion", "queue": "notify", "status": "dead", "attempt": 3, "idempotency_key": "send_suggestion:sug_001",
     "payload": {"suggestion_id": "sug_001"}, "last_error": "Slack API timeout x3", "created_at": NOW - timedelta(minutes=10)}])

print("COMMIT;")
