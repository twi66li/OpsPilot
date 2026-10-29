"""LangGraph 诊断工作流节点实现。

流程：route_intent → retrieve → grade ─(不足)→ rewrite → retrieve（最多 1 轮补充检索）
                              └─(充足)→ call_tools → generate → approval_gate ─(建单)→ create_ticket → END
"""
import logging

from app.agent.llm import detect_intent
from app.agent.tools import run_diagnostic_tools
from app.services.observability import estimate_tokens, get_tracer
from app.services.ticket_service import get_or_create_approval

logger = logging.getLogger(__name__)


class DiagnosisNodes:
    def __init__(self, container):
        self.container = container
        self.tracer = get_tracer()

    # ---- 意图路由 ----
    def route_intent(self, state):
        intent = detect_intent(state["question"])
        # 重置上一轮残留状态，保证同会话多次提问互不污染
        return {
            "intent": intent,
            "rewritten_query": "",
            "docs_sufficient": False,
            "iteration": 0,
            "approval": None,
            "approval_id": None,
            "ticket_id": None,
            "tool_events": [],
            "fault_records": [],
            "metrics": {},
        }

    # ---- 检索 ----
    def retrieve(self, state):
        query = state.get("rewritten_query") or state["question"]
        settings = self.container.settings
        with self.container.db_session() as db, self.tracer.span("retrieval", mode=settings.retrieval_mode, tenant_id=state["tenant_id"]) as span:
            result = self.container.retriever.retrieve(db, state["tenant_id"], query, mode=settings.retrieval_mode)
            span.metadata["hits"] = len(result.hits)
            span.metadata["reranked"] = result.reranked
            span.metadata["latency_ms"] = round(result.latency_ms, 1)
        hits = [h.to_dict() for h in result.hits]
        event = {
            "type": "retrieval",
            "mode": result.mode,
            "query": query,
            "latency_ms": round(result.latency_ms, 1),
            "reranked": result.reranked,
            "hits": hits[:5],
            "round": state.get("iteration", 0) + 1,
        }
        return {
            "hits": hits,
            "events": [event],
        }

    # ---- 相关性判断（决定是否补充检索） ----
    def grade(self, state):
        sufficient = self.container.llm.is_sufficient(state["question"], state.get("hits", []))
        iteration = state.get("iteration", 0) + 1
        event = {"type": "grade", "docs_sufficient": sufficient, "iteration": iteration}
        return {"docs_sufficient": sufficient, "iteration": iteration, "events": [event]}

    def route_after_grade(self, state):
        if state.get("docs_sufficient") or state.get("iteration", 0) >= 2:
            return "tools"
        return "rewrite"

    # ---- 查询改写 → 补充检索 ----
    def rewrite(self, state):
        with self.tracer.span("rewrite_query"):
            rewritten = self.container.llm.rewrite_query(state["question"])
        return {"rewritten_query": rewritten, "events": [{"type": "rewrite", "rewritten": rewritten}]}

    # ---- 工具调用 ----
    def call_tools(self, state):
        with self.container.db_session() as db:
            tool_events, fault_records, metrics = run_diagnostic_tools(db, state["tenant_id"], state["question"])
        events = [
            {"type": "tool", "name": e["name"], "args": e.get("args", {}), "summary": e.get("summary", ""), "anomalies": e.get("anomalies", [])}
            for e in tool_events
        ]
        with self.tracer.span("tools", count=len(tool_events)) as span:
            span.metadata["tools"] = [e["name"] for e in tool_events]
        return {"tool_events": tool_events, "fault_records": fault_records, "metrics": metrics, "events": events}

    # ---- 生成回答 ----
    def generate(self, state):
        with self.container.db_session() as db:
            from app.services.chat_service import get_history

            history = get_history(db, state["session_id"], limit=6)
        with self.tracer.span("generate") as span:
            answer = self.container.llm.generate_answer(
                state["question"], state.get("hits", []), state.get("tool_events", []), state.get("fault_records", []), history
            )
            span.metadata["answer_chars"] = len(answer)
            span.metadata["tokens_in"] = estimate_tokens(state["question"]) + sum(estimate_tokens(h.get("text", "")) for h in state.get("hits", [])[:5])
            span.metadata["tokens_out"] = estimate_tokens(answer)
        citations = []
        for hit in state.get("hits", []):
            title = hit.get("doc_title", "")
            if title and title not in citations:
                citations.append(title)
        payload = {"answer": answer, "citations": citations, "events": [{"type": "answer", "answer": answer, "citations": citations}]}
        if state.get("intent") == "create_ticket":
            payload["ticket_draft"] = self.container.llm.draft_ticket(state["question"], answer)
        return payload

    # ---- 工单人工审批门（LangGraph interrupt） ----
    def approval_gate(self, state):
        if state.get("intent") != "create_ticket":
            return {}
        draft = state.get("ticket_draft") or {"title": state["question"][:80], "description": state.get("answer", ""), "severity": "P2"}
        with self.container.db_session() as db:
            req = get_or_create_approval(db, state["tenant_id"], state["session_id"], draft, state.get("username", ""))
            approval_id = req.id
            ticket_draft = req.ticket_draft
        # interrupt：暂停图执行，等待 /approvals/{id}/decide 恢复
        from langgraph.types import interrupt

        decision = interrupt({"approval_id": approval_id, "ticket_draft": ticket_draft, "session_id": state["session_id"]})
        return {
            "approval": decision,
            "approval_id": approval_id,
            "events": [{"type": "approval_decision", "approved": bool(decision.get("approved")), "approval_id": approval_id}],
        }

    def route_after_approval(self, state):
        if state.get("approval") is not None:
            return "ticket"
        return "end"

    # ---- 创建工单 ----
    def create_ticket(self, state):
        from datetime import datetime, timezone

        from app.db.models import ApprovalRequest
        from app.services.ticket_service import create_ticket

        decision = state["approval"]
        with self.container.db_session() as db:
            req = db.get(ApprovalRequest, state.get("approval_id") or "")
            if req is None:
                return {"events": [{"type": "error", "message": "审批单不存在"}]}
            if decision.get("approved"):
                ticket = create_ticket(
                    db,
                    state["tenant_id"],
                    state["session_id"],
                    req.ticket_draft,
                    created_by=req.requested_by,
                    approver=decision.get("decided_by", ""),
                )
                req.status = "approved"
                req.comment = decision.get("comment", "")
                req.decided_at = datetime.now(timezone.utc)
                db.commit()
                answer = (state.get("answer", "")) + f"\n\n✅ 工单已创建（ID: {ticket.id[:8]}），由 {decision.get('decided_by', '')} 审批通过，等待值班工程师处理。"
                return {
                    "ticket_id": ticket.id,
                    "answer": answer,
                    "events": [{"type": "ticket_created", "ticket_id": ticket.id, "title": ticket.title}],
                }
            req.status = "rejected"
            req.comment = decision.get("comment", "")
            req.decided_at = datetime.now(timezone.utc)
            db.commit()
            answer = (state.get("answer", "")) + "\n\n❌ 工单创建申请被审批拒绝。"
            return {"answer": answer, "events": [{"type": "ticket_rejected", "comment": decision.get("comment", "")}]}
