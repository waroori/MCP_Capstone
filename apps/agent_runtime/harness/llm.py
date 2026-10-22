"""모델 생성. LLM_MODEL="provider:model" 형식이며, "fake"면 키 없이 도는 스크립트 모델이다."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from apps.agent_runtime.config import LLM_FALLBACK_MODEL, LLM_MODEL


class ScriptedModel(BaseChatModel):
    """키 없이 그래프를 돌리는 스크립트 모델. 시나리오 D-1(이용권)과 프로필 조회만 흉내 낸다."""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: D401
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        last_idx = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        tools_done = [m for m in messages[last_idx + 1:] if isinstance(m, ToolMessage)]  # 이번 요청 이후의 도구 결과만
        last_human = str(humans[-1].content) if humans else ""
        names = [m.name for m in tools_done]

        def _cid():
            return f"call_{uuid.uuid4().hex[:8]}"

        def _last_json():
            try:
                return json.loads(tools_done[-1].content)
            except Exception:  # noqa: BLE001
                return {}

        import re

        if "의도를 아래 다섯 가지" in last_human:  # classify_intent 호출
            from apps.agent_runtime.harness.intents import classify_by_keywords
            text = last_human.split("요청:")[-1].split("코드:")[0]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=classify_by_keywords(text)))])
        if "활용 제안을 만들어줘" in last_human:  # review_update (시스템 계정, F1)
            uid = re.search(r"upd_[A-Za-z0-9_]+", last_human).group(0)
            if not tools_done:
                msg = AIMessage(content="", tool_calls=[{"name": "get_vendor_update", "args": {"update_id": uid}, "id": _cid()}])
            elif names[-1] == "get_vendor_update":
                msg = AIMessage(content="", tool_calls=[{"name": "find_relevant_tasks", "args": {"update_id": uid}, "id": _cid()}])
            elif names[-1] == "find_relevant_tasks":
                found = _last_json()
                cands = found.get("candidates") or []
                upd = found.get("update") or {}
                if upd.get("title", "").startswith("새 모델 출시"):
                    jobs = list(dict.fromkeys(c.get("job_title") for c in cands if c.get("job_title")))[:4]
                    body = "핵심 기능\n1. 긴 문서를 나누지 않고 요약·비교\n2. 표 정리와 차트 생성\n3. 이미지 속 표를 편집 가능한 표로\n이렇게 써 보세요\n" + "\n".join(f"• {j}: 등록 업무 자료를 한 번에 올려 정리해 보세요." for j in jobs)
                    args = {"update_id": uid, "target_type": "all", "relevance": f"승인 공급사 새 모델 · 직무 {len(jobs)}개 활용 예", "body": body}
                    msg = AIMessage(content="", tool_calls=[{"name": "save_suggestion", "args": args, "id": _cid()}])
                    return ChatResult(generations=[ChatGeneration(message=msg)])
                if cands:
                    c = cands[0]
                    args = {"update_id": uid, "target_type": "employee", "target_id": c.get("employee_id"), "task_id": c.get("task_id"),
                            "relevance": f"등록 업무 '{c.get('summary', '')[:30]}'에 바로 쓸 수 있는 기능", "body": "새 기능을 등록 업무에 이렇게 쓸 수 있어요: 반복 작업을 한 번에 처리합니다."}
                else:
                    args = {"update_id": uid, "target_type": "all", "status": "on_hold", "hold_reason": "관련 업무 근거 없음"}
                msg = AIMessage(content="", tool_calls=[{"name": "save_suggestion", "args": args, "id": _cid()}])
            else:
                msg = AIMessage(content="업데이트 검토를 마치고 제안을 저장했습니다.")
            return ChatResult(generations=[ChatGeneration(message=msg)])
        if "검토해 관리자용 의견" in last_human:  # review_plan (시스템 계정)
            import re as _re
            pid = _re.search(r"plan_[A-Za-z0-9]+", last_human).group(0)
            if not tools_done:
                msg = AIMessage(content="", tool_calls=[{"name": "get_plan_status", "args": {"plan_id": pid}, "id": _cid()}])
            elif names[-1] == "get_plan_status":
                op = {"recommendation": "approve", "summary": "미배정 좌석이 있어 바로 배정해도 됩니다.", "requester": {"name": "박서준", "dept": "마케팅", "job_title": "콘텐츠 마케터", "current_products": ["ChatGPT Enterprise"]},
                      "asset": {"product": "ChatGPT Enterprise", "purchased": 60, "assigned": 59, "unassigned": 1, "seat_price_krw": 45000},
                      "facts": ["ChatGPT Enterprise 60석 중 59석 배정, 1석 남음", "요청자는 마케팅 콘텐츠 마케터로 등록 업무에 이 제품을 씀"], "risks": ["남은 좌석이 1석"],
                      "changes": [["직원", "박서준 (마케팅 · 콘텐츠 마케터)"], ["제품", "ChatGPT Enterprise (Enterprise 플랜)"]], "evidence_text": ["이용권 현황: ChatGPT Enterprise 60석 중 미배정 1석"]}
                msg = AIMessage(content="", tool_calls=[{"name": "attach_plan_opinion", "args": {"plan_id": pid, "opinion": op}, "id": _cid()}])
            else:
                msg = AIMessage(content="미배정 좌석이 있어 바로 배정해도 됩니다.")
            return ChatResult(generations=[ChatGeneration(message=msg)])
        if not tools_done:
            if "안써" in last_human.replace(" ", "") or "반납" in last_human:
                call = {"name": "create_action_plan", "args": {"type": "seat_reclaim", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "본인 반납 요청", "evidence": []}, "id": _cid()}
            elif "api" in last_human.lower() and ("사용" in last_human or "써야" in last_human):
                call = {"name": "create_action_plan", "args": {"type": "api_access", "items": [{"tool_id": "tool_claude", "purpose": "세일즈 데이터 자동 분석"}], "rationale": "API 제품 사용 신청", "evidence": []}, "id": _cid()}
            elif "회수" in last_human or "재배정" in last_human:
                call = {"name": "create_action_plan", "args": {"type": "seat_reclaim", "items": [{"employee_id": "emp_sales_02", "contract_id": "ctr_chatgpt_2026", "confirmation_id": "cfm_001", "reassign_to_employee_id": "emp_mkt_01"}], "rationale": "휴직 중, 본인 반납 응답 있음", "evidence": [{"source": "confirmations", "ref": "cfm_001", "value": "return_seat"}]}, "id": _cid()}
            elif "실행" in last_human and re.search(r"plan_[A-Za-z0-9]+", last_human):
                call = {"name": "execute_approved_plan", "args": {"plan_id": re.search(r"plan_[A-Za-z0-9]+", last_human).group(0), "idempotency_key": f"idem_{uuid.uuid4().hex[:8]}"}, "id": _cid()}
            elif "이용권" in last_human or "좌석" in last_human:
                call = {"name": "create_action_plan", "args": {"type": "seat_assign", "items": [{"employee_id": "emp_mkt_01", "contract_id": "ctr_chatgpt_2026"}], "rationale": "미배정 이용권이 있어 신청자에게 배정", "evidence": [{"source": "inventory", "ref": "ctr_chatgpt_2026", "value": {"unassigned": 2}}]}, "id": _cid()}
            else:
                call = {"name": "get_my_profile", "args": {}, "id": _cid()}
            msg = AIMessage(content="", tool_calls=[call])
        else:
            last = tools_done[-1].content[:300]
            status = _last_json().get("status")
            summary = {"executed": "처리했습니다.", "pending_approval": "관리자에게 전달했어요. 결정되면 알려드릴게요.", "approved": "승인된 요청입니다."}.get(status, "요청을 처리했습니다.")
            msg = AIMessage(content=f"(스크립트 모델) {summary} 마지막 단계 {names[-1]} 결과 근거: {last}")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def make_model(name: str) -> BaseChatModel:
    if name == "fake":
        return ScriptedModel()
    if ":" not in name:
        raise ValueError('LLM_MODEL은 "provider:model" 또는 "fake" 형식이어야 합니다.')
    provider, model = name.split(":", 1)
    if provider == "xai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=os.getenv("XAI_API_KEY"),
            base_url=os.getenv("XAI_BASE_URL", "https://api.x.ai/v1"),
        )
    from langchain.chat_models import init_chat_model

    return init_chat_model(name)


def get_models() -> tuple[BaseChatModel, BaseChatModel | None]:
    primary = make_model(LLM_MODEL)
    fallback = make_model(LLM_FALLBACK_MODEL) if LLM_FALLBACK_MODEL else None
    return primary, fallback
