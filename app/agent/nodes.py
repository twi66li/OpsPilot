"""LangGraph 诊断工作流节点实现。

流程：route_intent → retrieve → grade ─(不足)→ rewrite → retrieve（最多 1 轮补充检索）
                              └─(充足)→ call_tools → generate → approval_gate ─(建单)→ create_ticket → END
"""
import logging

from app.agent.llm import detect_intent
from app.agent.tools import make_tool_executor, run_diagnostic_tools
from app.services.observability import estimate_tokens, get_tracer
from app.services.ticket_service import get_or_create_approval

logger = logging.getLogger(__name__)


class DiagnosisNodes:
    def __init__(self, container):
        self.container = container
        self.tracer = get_tracer()

    def select_path(self, state):
        mode = self.container.settings.agent_mode
        if mode not in ("rule", "native"):
            raise ValueError("agent_mode 必须是rule或native")
        return mode

    def native_tools(self, state):
        from app.agent.tool_loop import run_tool_loop

        if not hasattr(self.container.llm, "_api"):
            raise ValueError("原生工具模式需要真实OpenAI兼容模型，不回退Mock")
        result = run_tool_loop(
            state["question"], self.container.llm._api, make_tool_executor(self.container, state["tenant_id"]),
            history=state.get("history", []), timeout=self.container.settings.tool_timeout_seconds,
        )
        answer, citations, hits = result["answer"], [], []
        citations_indexed = []
        events = []
        for event in result["tool_events"]:
            output = event["result"]
            if event["name"] == "search_runbook":
                hits.extend(output.get("hits", []))
                for hit in output.get("hits", []):
                    citations_indexed.append(hit["doc_title"])
                    if hit["doc_title"] not in citations:
                        citations.append(hit["doc_title"])
            if event["name"] == "query_service_metrics" and "source" in output:
                answer += "\n\n指标证据：\n" + output["summary"]
            events.append({"type": "tool", **event, "summary": output.get("summary", output.get("message", "")), "anomalies": output.get("anomalies", [])})
        payload = {"answer": answer, "citations": citations, "citations_indexed": citations_indexed, "hits": hits,
                   "docs_sufficient": None, "end_reason": result["end_reason"],
                   "tool_events": result["tool_events"], "events": events + [{"type": "answer", "answer": answer, "citations": citations, "citations_indexed": citations_indexed}]}
        if state.get("intent") == "create_ticket" and result["end_reason"] == "answered":
            payload["ticket_draft"] = self.container.llm.draft_ticket(state["question"], answer)
        return payload

    def route_after_native(self, state):
        return "approval" if state.get("end_reason") == "answered" else "end"

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
            "end_reason": "answered",
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
        events = [{"type": "tool", **event} for event in tool_events]
        with self.tracer.span("tools", count=len(tool_events)) as span:
            span.metadata["tools"] = [e["name"] for e in tool_events]
        return {"tool_events": tool_events, "fault_records": fault_records, "metrics": metrics, "events": events}

    # ---- 生成回答 ----
    def generate(self, state):
        with self.container.db_session() as db:
            from app.services.chat_service import get_history

            history = get_history(db, state["session_id"], limit=6)
            # 证据组装：范围查询的完整行摘录置首 + 分块摘录行边界安全；生成器、评测记录、裁判同用一份
            from app.rag.evidence import assemble_evidence

            evidence_items, range_meta = assemble_evidence(
                state.get("hits", []), state["question"], db, state["tenant_id"],
                per_hit_chars=getattr(self.container.settings, "evidence_excerpt_chars", 500),
                range_chars=getattr(self.container.settings, "evidence_range_chars", 8000))
        with self.tracer.span("generate") as span:
            answer = self.container.llm.generate_answer(
                state["question"], state.get("hits", []), state.get("tool_events", []), state.get("fault_records", []), history,
                evidence_items=evidence_items,
            )
            metric_evidence = [event["summary"] for event in state.get("tool_events", []) if event["name"] == "query_service_metrics"]
            if metric_evidence:
                answer += "\n\n指标证据：\n" + "\n".join(metric_evidence)
            span.metadata["answer_chars"] = len(answer)
            span.metadata["tokens_in"] = estimate_tokens(state["question"]) + sum(
                estimate_tokens(it.get("text", "")) for it in evidence_items)
            span.metadata["tokens_out"] = estimate_tokens(answer)
        # 引用映射与生成器编号同源：[n] 一律对应第 n 条证据（重复文档保留各自 chunk 编号）
        citations_indexed = [it.get("doc_title", "") for it in evidence_items]
        citations = []
        for title in citations_indexed:
            if title and title not in citations:
                citations.append(title)
        payload = {"answer": answer, "citations": citations, "citations_indexed": citations_indexed,
                   "used_evidence": evidence_items, "range_assembly": range_meta,
                   "events": [{"type": "answer", "answer": answer, "citations": citations,
                               "citations_indexed": citations_indexed}]}
        if state.get("intent") == "create_ticket":
            payload["ticket_draft"] = self.container.llm.draft_ticket(state["question"], answer)
        return payload

    # 审批编号先经过一个完整节点保存；interrupt 恢复会从等待节点开头重跑。
    def prepare_approval(self, state):
        if state.get("intent") != "create_ticket":
            return {}
        draft = state.get("ticket_draft") or {"title": state["question"][:80], "description": state.get("answer", ""), "severity": "P2"}
        with self.container.db_session() as db:
            req = get_or_create_approval(db, state["tenant_id"], state["session_id"], draft, state.get("username", ""))
            approval_id = req.id
            ticket_draft = req.ticket_draft
        return {"approval_id": approval_id, "ticket_draft": ticket_draft}

    # ---- 工单人工审批门（LangGraph interrupt） ----
    def approval_gate(self, state):
        if state.get("intent") != "create_ticket":
            return {}
        approval_id = state["approval_id"]
        ticket_draft = state["ticket_draft"]
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
        from app.db.models import ApprovalRequest
        from app.services.ticket_service import create_ticket

        decision = state["approval"]
        with self.container.db_session() as db:
            req = db.get(ApprovalRequest, state.get("approval_id") or "")
            if req is None or req.tenant_id != state["tenant_id"] or req.session_id != state["session_id"]:
                raise ValueError("审批单与流程不匹配")
            if req.status not in ("approved", "rejected") or decision.get("approved") != (req.status == "approved"):
                raise ValueError("审批决定尚未由服务端确认")
            if decision.get("approved"):
                ticket = create_ticket(
                    db,
                    state["tenant_id"],
                    state["session_id"],
                    req.ticket_draft,
                    created_by=req.requested_by,
                    approver=decision.get("decided_by", ""),
                    approval_id=req.id,
                    comment=req.comment,
                )
                answer = (state.get("answer", "")) + f"\n\n✅ 工单已创建（ID: {ticket.id[:8]}），由 {decision.get('decided_by', '')} 审批通过，等待值班工程师处理。"
                return {
                    "ticket_id": ticket.id,
                    "answer": answer,
                    "events": [{"type": "ticket_created", "ticket_id": ticket.id, "title": ticket.title}],
                }
            answer = (state.get("answer", "")) + "\n\n❌ 工单创建申请被审批拒绝。"
            return {"answer": answer, "events": [{"type": "ticket_rejected", "comment": decision.get("comment", "")}]}
