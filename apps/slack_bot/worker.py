"""발신 워커: 백엔드에 쌓인 '보낼 것'을 Slack으로 보낸다. 봇 프로세스 안에서 스레드로 돈다.

1) 사용 의사 확인 요청(미응답) → 직원 DM (계속 필요·반납·휴직 버튼)               [F4]
2) 발송함(notify_outbox, 미전달) → 직원 DM 또는 부서원 DM (활용 제안 + 반응 버튼)   [F1]
3) 처리 요청: 새 승인 대기 → 관리자 DM([승인]/[반려]), 결과(executed·rejected·failed) → 요청자 DM과 사용 안내   [F4]
   좌석 제품은 SSO 로그인 안내(아이디=회사 이메일, 비밀번호 없음), API는 키 앞부분 + 본인만 한 번 여는 링크. 키 값은 Slack에 보내지 않는다
5) F3 좌석 토큰 경보: 사용량 수집 → 소비 속도 경보 배치 → 새 경보를 본인 DM (현재 작업 분석 버튼). F3_INTERVAL_MIN(기본 5분)마다
4) F1 체인 한 바퀴: 수집 배치 → 검토 안 된 업데이트마다 에이전트 review_update(시스템 계정)
   → 발송 정책 배치(팀당 주 2건·업무 시간·보류) → 2)
   수동: uv run python -m apps.slack_bot --run-f1     자동: F1_INTERVAL_MIN>0 이면 그 주기로

Slack 대상 찾기: SLACK_USER_MAP(slack→직원) 역매핑이 1순위. 없으면 DB의 slack_user_id가 실제 ID 형식일 때만 쓴다
(시드의 U_HRD01 같은 값은 가짜라 건너뛴다). 대상이 없는 건은 보내지 않고 로그만 남긴다.
보낸 확인 요청은 logs/slack_worker_state.json에 적어 재시작해도 두 번 보내지 않는다 (발송함은 DB delivered_at으로 구분).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from apps.slack_bot import client as api
from apps.slack_bot import identity
from apps.slack_bot.send import build_plan_request, send_confirmation, send_seat_alert, send_suggestion

log = logging.getLogger("slack_bot.worker")
SYSTEM_TOKEN = os.getenv("SYSTEM_TOKEN", "dev.sys")
WEB_URL = os.getenv("WEB_URL", "http://localhost:5173")
STATE_PATH = Path(os.getenv("SLACK_WORKER_STATE", "logs/slack_worker_state.json"))
REAL_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
_emp_cache: dict[str, dict] = {}


# ---------- 상태 (보낸 확인 요청 id) ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"confirmations": []}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    except OSError as exc:
        log.warning("상태 저장 실패: %s", exc)


# ---------- 대상 찾기 ----------
def employee(emp_id: str) -> dict:
    if emp_id not in _emp_cache:
        _emp_cache[emp_id] = api.backend("GET", f"/employees/{emp_id}", SYSTEM_TOKEN)["employee"]
    return _emp_cache[emp_id]


def slack_id_for(emp: dict) -> str | None:
    """직원 → Slack 사용자 ID. 매핑표 역조회 → DB 값(실제 형식일 때만)."""
    for slack_id, emp_id in identity._user_map().items():
        if emp_id == emp["id"]:
            return slack_id
    sid = emp.get("slack_user_id")
    return sid if sid and REAL_SLACK_ID.match(sid) else None


def mapped_employees() -> list[dict]:
    """SLACK_USER_MAP에 연결된 직원들 (부서·전사 대상 제안을 DM으로 보낼 때 씀)."""
    out = []
    for emp_id in dict.fromkeys(identity._user_map().values()):
        try:
            out.append(employee(emp_id))
        except api.ApiError as exc:
            log.warning("직원 %s 조회 실패: %s", emp_id, exc)
    return out


def _dm(client, slack_id: str) -> str:
    return client.conversations_open(users=slack_id)["channel"]["id"]


# ---------- 1) 사용 의사 확인 DM ----------
def deliver_confirmations(client, state: dict) -> list[str]:
    """미응답 확인 요청 중 아직 안 보낸 것을 직원 DM으로. 보낸 id 목록을 돌려준다."""
    pending = api.backend("GET", "/confirmations", SYSTEM_TOKEN, params={"answered": "false"})
    todo = [c for c in pending if c["id"] not in state.setdefault("confirmations", [])]
    if not todo:
        return []
    inventory = api.backend("GET", "/inventory", SYSTEM_TOKEN)
    tool_name = {i["contract"]["id"]: i["tool_name"] for i in inventory}
    last_seen = {(i["contract"]["id"], c["employee_id"]): c.get("last_activity_at") for i in inventory for c in i.get("low_usage_candidates", [])}
    sent = []
    for cfm in todo:
        try:
            plan = api.backend("GET", f"/plans/{cfm['plan_id']}", SYSTEM_TOKEN)
            contract = (plan.get("items") or [{}])[0].get("contract_id")
            emp = employee(cfm["employee_id"])
            slack_id = slack_id_for(emp)
            if not slack_id:
                log.debug("확인 요청 %s: %s(%s) Slack 미연결이라 보류", cfm["id"], emp.get("name"), emp["id"])
                continue
            send_confirmation(client, _dm(client, slack_id), cfm, tool_name.get(contract, "이용권"), last_seen.get((contract, emp["id"])))
            state["confirmations"].append(cfm["id"])
            sent.append(cfm["id"])
            log.info("확인 요청 DM 보냄: %s → %s", cfm["id"], emp.get("name"))
        except Exception as exc:  # noqa: BLE001 — 한 건 실패가 나머지를 막지 않는다
            log.warning("확인 요청 %s 발송 실패: %s", cfm["id"], exc)
    if sent:
        save_state(state)
    return sent


# ---------- 2) 발송함 → Slack ----------
def _targets(sug: dict) -> list[dict]:
    kind, target = sug["target_type"], sug.get("target_id")
    if kind == "employee":
        return [employee(target)] if target else []
    emps = mapped_employees()
    if kind == "team":
        return [e for e in emps if e.get("dept_id") == target]
    return emps  # all


def deliver_outbox(client) -> list[str]:
    """발송함의 미전달 건을 Slack으로 보내고 전달 표시. 전달한 발송함 id 목록을 돌려준다."""
    rows = [r for r in api.backend("GET", "/notify-outbox", SYSTEM_TOKEN) if not r.get("delivered_at")]
    if not rows:
        return []
    sugs = {s["id"]: s for s in api.backend("GET", "/suggestions", SYSTEM_TOKEN)["items"]}
    delivered = []
    for row in rows:
        sug = sugs.get(row["suggestion_id"])
        if not sug:
            continue
        try:
            upd = api.backend("GET", f"/updates/{sug['update_id']}", SYSTEM_TOKEN)
            posted = 0
            for emp in _targets(sug):
                slack_id = slack_id_for(emp)
                if not slack_id:
                    continue
                title = None
                if sug.get("task_id"):
                    prof = api.backend("GET", f"/employees/{emp['id']}", SYSTEM_TOKEN)
                    title = next((t["title"] for t in prof.get("tasks", []) if t["id"] == sug["task_id"]), None)
                send_suggestion(client, _dm(client, slack_id), {**sug, "body": row["text"] or sug["body"]}, title, upd)
                posted += 1
            if posted:
                api.backend("PATCH", f"/notify-outbox/{row['id']}/delivered", SYSTEM_TOKEN)
                delivered.append(row["id"])
                log.info("제안 보냄: %s → %d명", sug["id"], posted)
            else:
                log.debug("제안 %s: 받을 Slack 사용자가 없어 보류 (대상 %s %s)", sug["id"], sug["target_type"], sug.get("target_id"))
        except Exception as exc:  # noqa: BLE001
            log.warning("발송함 %s 전송 실패: %s", row["id"], exc)
    return delivered


# ---------- 3) F1 체인 ----------
def review_updates(wait: float = 180.0) -> list[dict]:
    """제안이 하나도 없는 업데이트마다 시스템 계정으로 review_update 작업을 돌리고 끝날 때까지 기다린다."""
    updates = api.backend("GET", "/updates", SYSTEM_TOKEN)["items"]
    reviewed = {s["update_id"] for s in api.backend("GET", "/suggestions", SYSTEM_TOKEN)["items"]}
    results = []
    for upd in updates:
        if upd["id"] in reviewed:
            continue
        created = api.agent("POST", "/ai/tasks", SYSTEM_TOKEN, json={"input": f"업데이트 {upd['id']}를 검토해 관련 업무에 활용 제안을 만들어줘",
                                                                   "prompt_name": "review_update", "prompt_args": {"update_id": upd["id"]}, "channel": "system"})
        task_id, t0, status = created["task_id"], time.time(), "queued"
        while time.time() - t0 < wait:
            status = api.agent("GET", f"/ai/tasks/{task_id}", SYSTEM_TOKEN)["status"]
            if status in ("done", "failed"):
                break
            time.sleep(1)
        results.append({"update_id": upd["id"], "task_id": task_id, "status": status})
        log.info("업데이트 검토 %s: %s", upd["id"], status)
    return results


def run_f1(client, wait: float = 180.0) -> dict:
    """수집 → 검토 → 발송 정책 → Slack 전송 한 바퀴."""
    collect = api.backend("POST", "/internal/jobs/collect_release_notes/run", SYSTEM_TOKEN)["result"]
    reviews = review_updates(wait)
    send = api.backend("POST", "/internal/jobs/send_suggestions/run", SYSTEM_TOKEN)["result"]
    delivered = deliver_outbox(client) if client is not None else []
    return {"collect": collect, "reviews": reviews, "send": send, "delivered": delivered}



# ---------- 3) 처리 요청 결과 → 요청자 DM ----------
FINAL = {"executed", "rejected", "failed"}
_plan_status: dict[str, str] = {}  # plan_id → 마지막으로 본 상태. 봇이 뜰 때 기준선을 잡아 과거 건은 알리지 않는다
_tool_cache: dict[str, str] = {}


def _product_names() -> dict[str, str]:
    """계약 id·제품 id → 제품 이름."""
    if not _tool_cache:
        for t in api.backend("GET", "/tools", SYSTEM_TOKEN):
            _tool_cache[t["id"]] = t["product"]
            if t.get("contract"):
                _tool_cache[t["contract"]["id"]] = t["product"]
    return _tool_cache


# 좌석 제품 로그인 안내 (제품 이름에 들어가는 말 → 접속 주소, 로그인 방법). 모두 회사 SSO라 비밀번호를 따로 주지 않는다
SEAT_GUIDE = {
    "ChatGPT": ("https://chatgpt.com", "회사 이메일을 넣고 'SSO로 계속'을 누르세요"),
    "Acrobat": ("https://acrobat.adobe.com", "'회사 또는 학교 계정'을 고르고 회사 이메일을 넣으세요"),
    "Canva": ("https://www.canva.com/login", "'SSO로 로그인'을 누르고 회사 이메일을 넣으세요"),
}


def seat_guide(product: str, email: str | None) -> str:
    url, how = next((v for k, v in SEAT_GUIDE.items() if k in product), (None, "회사 SSO로 로그인하세요"))
    return "\n".join(filter(None, [
        f"• 접속: {url}" if url else None,
        f"• 아이디: 회사 이메일{f' ({email})' if email else ''}",
        "• 비밀번호: 따로 없어요. 평소 쓰는 회사 계정(SSO)으로 로그인해요",
        f"• 방법: {how}",
    ]))


def access_guide(product: str, handoff: dict | None) -> str:
    if not handoff:
        return f"요청하신 {product} 사용이 승인됐어요. 접근 정보는 담당자가 따로 안내할 거예요."
    return (f"요청하신 {product} API 사용이 승인됐어요. 개인 테스트 프로젝트와 키를 만들었어요.\n"
            f"• 키 앞부분: `{handoff['key_prefix']}…`\n"
            f"• 키 전체는 본인만 한 번 볼 수 있어요 (24시간): {WEB_URL}/key/{handoff['handoff_id']}\n"
            "• 키는 Slack·메일·코드 저장소에 붙여 넣지 말고 환경변수나 사내 비밀 저장소에 두세요")


def outcome_messages(plan: dict, names: dict[str, str], emails: dict[str, str] | None = None) -> dict[str, str]:
    """처리 요청 결과를 받을 사람(직원 id) → 자연어 안내. 요청자와, 좌석이 바뀌는 당사자. emails: 직원 id → 회사 이메일(SSO 아이디)."""
    emails = emails or {}
    item = (plan.get("items") or [{}])[0]
    product = names.get(item.get("contract_id") or item.get("tool_id") or "", "요청하신 제품")
    status, typ, req = plan["status"], plan["type"], plan["requested_by"]
    out: dict[str, str] = {}
    if status == "rejected":
        why = plan.get("reject_reason")
        out[req] = f"{product} 요청이 반려됐어요." + (f" 사유: {why}" if why else "") + " 궁금한 점은 저에게 물어봐 주세요."
    elif status == "failed":
        out[req] = f"{product} 요청을 처리하는 중 문제가 있어 관리자가 다시 확인할 예정이에요."
    elif status == "executed":
        holder = item.get("employee_id")
        if typ == "seat_assign":
            out[holder or req] = f"요청하신 {product} 이용권이 배정됐어요. 지금부터 쓸 수 있어요.\n" + seat_guide(product, emails.get(holder or req))
            if holder and holder != req:
                out[req] = f"{product} 이용권 배정이 끝났어요."
        elif typ == "seat_reclaim":
            if holder:
                out[holder] = f"{product} 이용권이 반납 처리됐어요. 다시 필요해지면 저에게 말씀해 주세요."
            if req != holder:
                out[req] = f"{product} 이용권 반납 처리가 끝났어요."
        elif typ == "api_access":
            out[req] = access_guide(product, next(iter((plan.get("result") or {}).get("handoffs") or []), None))
        elif typ == "model_change":
            out[req] = "요청하신 모델 변경이 반영됐어요."
        else:
            out[req] = "요청하신 처리가 끝났어요."
    return out


def notify_plan_outcomes(client, baseline: bool = False) -> list[str]:
    """상태가 바뀐 처리 요청의 요청자에게 DM. 첫 호출(baseline=True)은 기준선만 잡고 보내지 않는다. 보낸 plan id를 돌려준다."""
    plans = api.backend("GET", "/plans", SYSTEM_TOKEN)["items"]
    baseline = baseline or not _plan_status  # 기준선이 아직 없으면(기동 때 백엔드가 없었던 경우) 이번 호출이 기준선
    sent = []
    for plan in plans:
        before = _plan_status.get(plan["id"])
        _plan_status[plan["id"]] = plan["status"]
        if not baseline and before is None and plan["status"] == "pending_approval":
            ask_admins(client, plan)  # 기준선 뒤 새로 들어온 승인 대기 → 관리자 DM
            continue
        if baseline or plan["status"] not in FINAL or before == plan["status"]:
            continue  # 기준선 이후 바뀐 건만. 기준선 뒤 새로 생겨 바로 끝난 건(before None)도 알린다
        try:
            item = (plan.get("items") or [{}])[0]
            emails = {e: employee(e).get("email") for e in {plan["requested_by"], item.get("employee_id")} if e}
            for emp_id, text in outcome_messages(plan, _product_names(), emails).items():
                emp = employee(emp_id)
                if emp.get("role") != "employee":
                    continue  # 관리자 본인 건은 웹에서 보고 있다
                slack_id = slack_id_for(emp)
                if not slack_id:
                    continue
                client.chat_postMessage(channel=_dm(client, slack_id), text=text)
                log.info("처리 결과 DM: %s(%s) → %s", plan["id"], plan["status"], emp.get("name"))
            sent.append(plan["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("처리 결과 %s 알림 실패: %s", plan["id"], exc)
    return sent

def ask_admins(client, plan: dict) -> None:
    """직원이 낸 승인 대기 요청을 관리자에게 DM ([승인]/[반려]). 관리자가 직접 만든 요청은 웹에서 보고 있으니 보내지 않는다."""
    try:
        req = employee(plan["requested_by"])
        if req.get("role") != "employee":
            return
        item = (plan.get("items") or [{}])[0]
        product = _product_names().get(item.get("contract_id") or item.get("tool_id") or "", "제품")
        blocks = build_plan_request(plan, f"{req['name']}({req.get('dept_name', '')})", product, f"{WEB_URL}/inbox?id={plan['id']}")
        for admin in identity.admin_slack_ids():
            client.chat_postMessage(channel=_dm(client, admin), text=f"{req['name']} 님 요청 승인 대기", blocks=blocks)
        log.info("승인 대기 → 관리자 DM: %s", plan["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("승인 대기 %s 관리자 알림 실패: %s", plan["id"], exc)


# ---------- 5) F3 좌석 토큰 경보 ----------
def run_f3(client, state: dict) -> list[str]:
    """사용량 수집(지금은 더미) → 경보 배치 → 아직 안 보낸 경보를 본인에게 DM."""
    api.backend("POST", "/internal/jobs/collect_usage/run", SYSTEM_TOKEN)
    api.backend("POST", "/internal/jobs/check_thresholds/run", SYSTEM_TOKEN)
    sent = []
    for a in api.backend("GET", "/anomalies", SYSTEM_TOKEN, params={"status": "open"}):
        if a["scope_type"] != "employee" or a["id"] in state.setdefault("anomalies", []):
            continue
        try:
            slack_id = slack_id_for(employee(a["scope_id"]))
            if slack_id:
                send_seat_alert(client, _dm(client, slack_id), a)
                log.info("좌석 토큰 경보 DM: %s → %s", a["id"], a.get("scope_name"))
            state["anomalies"].append(a["id"])  # Slack 미연결 직원도 다시 시도하지 않는다 (웹 좌석 배치도에서 보임)
            sent.append(a["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("경보 %s 발송 실패: %s", a["id"], exc)
    if sent:
        save_state(state)
    return sent


# ---------- 루프 ----------
def loop(client, interval: float = 10.0, once: bool = False) -> None:
    state = load_state()
    f1_every = float(os.getenv("F1_INTERVAL_MIN", "0") or 0) * 60
    f3_every = float(os.getenv("F3_INTERVAL_MIN", "5") or 0) * 60
    last_f1 = last_f3 = 0.0
    try:
        notify_plan_outcomes(client, baseline=True)  # 봇이 뜬 시점의 상태를 기준선으로
    except Exception as exc:  # noqa: BLE001
        log.warning("처리 요청 기준선 실패(백엔드 미기동?): %s", exc)
    while True:
        try:
            deliver_confirmations(client, state)
            deliver_outbox(client)
            notify_plan_outcomes(client)
            if f3_every > 0 and time.time() - last_f3 >= f3_every:
                last_f3 = time.time()
                run_f3(client, state)
            if f1_every > 0 and time.time() - last_f1 >= f1_every:
                last_f1 = time.time()
                log.info("F1 자동 실행: %s", run_f1(client))
        except Exception as exc:  # noqa: BLE001 — 백엔드가 내려가 있어도 봇은 계속 산다
            log.warning("워커 한 바퀴 실패: %s", exc)
        if once:
            return
        time.sleep(interval)


def start(client, interval: float = 10.0) -> threading.Thread:
    t = threading.Thread(target=loop, args=(client, interval), daemon=True, name="slack-worker")
    t.start()
    return t
