"""LangGraph 诊断工作流节点实现。

流程：route_intent → retrieve → grade ─(不足)→ rewrite → retrieve（最多 1 轮补充检索）
                              └─(充足)→ call_tools → generate → approval_gate ─(建单)→ create_ticket → END
纯统计路径：route_intent ─(显式行范围+纯统计诉求)→ pure_range_answer（文档权威读取+程序模板，零检索/零模型）→ END
"""
import logging
import re

from app.agent.llm import detect_intent
from app.agent.tools import make_tool_executor, run_diagnostic_tools
from app.services.observability import estimate_tokens, get_tracer
from app.services.ticket_service import get_or_create_approval

logger = logging.getLogger(__name__)

# 证据路径策略（模块级：评测脚本可在不发请求的情况下预判策略与预算需求）。
# 强解释诉求 → 混合检索；仅"是什么/哪些"类字段询问且存在可程序推导条件 → 纯统计路径。
_STRONG_SEMANTIC_CUE = re.compile(
    r"根因|为什么|为何|怎么|如何|排查|处理|建议|影响|原因|涉及|硬件|故障|能否确认|"
    r"说明什么|反映了什么|意味着|解读|概述|诊断")
_WEAK_SEMANTIC_CUE = re.compile(r"是什么|哪些|什么")


def evidence_strategy(question: str, intent: str = "diagnose") -> str:
    """显式行范围+纯统计诉求 → range_stats_direct（零 embedding/rerank/LLM）；
    显式行范围+语义解释 → range_hybrid（统计已程序化，检索补充语义）；
    其余 → hybrid。"""
    from app.rag.evidence import parse_line_targets
    from app.rag.logstats import parse_conditions

    targets = parse_line_targets(question)
    if not targets or intent != "diagnose":
        return "hybrid"
    _conditions, _unsupported, wants = parse_conditions(question)
    if _STRONG_SEMANTIC_CUE.search(question):
        return "range_hybrid"
    weak = _WEAK_SEMANTIC_CUE.search(question)
    program_answerable = wants.get("count") or wants.get("time") or wants.get("mode")
    if weak and not program_answerable:
        return "range_hybrid"  # 只有解释性追问、无可程序推导条件
    return "range_stats_direct"


class DiagnosisNodes:
    def __init__(self, container):
        self.container = container
        self.tracer = get_tracer()

    def select_path(self, state):
        mode = self.container.settings.agent_mode
        if mode not in ("rule", "native"):
            raise ValueError("agent_mode 必须是rule或native")
        if mode == "rule" and state.get("strategy") == "range_stats_direct":
            return "pure"
        return mode

    def native_tools(self, state):
        from app.agent.tool_loop import run_tool_loop

        if not hasattr(self.container.llm, "_api"):
            raise ValueError("原生工具模式需要真实OpenAI兼容模型，不回退Mock")
        delegated = self.container.settings.retrieval_subagent_enabled
        tool_names = ("delegate_retrieval" if delegated else "search_runbook", "query_service_metrics", "query_logs", "query_fault_history")
        result = run_tool_loop(
            state["question"], self.container.llm._api, make_tool_executor(self.container, state["tenant_id"], state["user_id"]),
            history=state.get("history", []), timeout=self.container.settings.tool_timeout_seconds,
            max_tool_calls=self.container.settings.max_tool_calls,
            tool_names=tool_names, tool_timeouts={"delegate_retrieval": self.container.settings.retrieval_subagent_timeout_seconds},
        )
        answer, citations, hits = result["answer"], [], []
        citations_indexed = []
        events = []
        for event in result["tool_events"]:
            output = event["result"]
            if event["name"] in ("search_runbook", "delegate_retrieval"):
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

    @staticmethod
    def _stats_summary(stats: dict) -> str:
        """统计结果的人类可读摘要（进入 SSE 工具事件与工作台过程证据面板）。"""
        parts = []
        for r in stats.get("ranges", []):
            parts.append(f"范围 L{r['lo']}-L{r['hi']}：请求 {r['requested']} 行，解析 {r['resolved']} 行"
                         + (f"，缺失 {len(r['missing'])} 行" if r.get("missing") else ""))
        for c in stats.get("conditions", []):
            label = f"级别 {c['value']}" if c["kind"] == "level" else f"子串「{c['value']}」"
            parts.append(f"{label}：{c['count']} 条（行 {c['line_numbers'][:8]}{'…' if len(c['line_numbers']) > 8 else ''}）")
        if stats.get("incomplete_excluded"):
            parts.append(f"残行未计入：L{','.join(map(str, stats['incomplete_excluded']))}")
        if stats.get("unsupported"):
            parts.append("无法确定性解析：" + "；".join(stats["unsupported"]))
        tr = stats.get("time_range")
        if tr:
            parts.append(f"时间范围（{tr['field']}）：{tr['first']}（L{tr['first_line']}）~ {tr['last']}（L{tr['last_line']}）")
        return "；".join(parts)

    def route_after_native(self, state):
        return "approval" if state.get("end_reason") == "answered" else "end"

    # ---- 意图路由 ----
    def route_intent(self, state):
        intent = detect_intent(state["question"])
        # 重置上一轮残留状态，保证同会话多次提问互不污染
        return {
            "intent": intent,
            "strategy": self._evidence_strategy(state["question"], intent),
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

    # 证据路径策略（P1-QUALITY-20261102 必做A）：显式行范围 + 纯统计诉求先走
    # 租户文档权威读取 + 程序统计（零 embedding/rerank/LLM）；含语义解释诉求才混合检索。
    _SEMANTIC_CUE = re.compile(r"根因|为什么|为何|怎么|如何|排查|处理|建议|影响|原因|内容|涉及|什么|哪些")

    def _evidence_strategy(self, question: str, intent: str) -> str:
        return evidence_strategy(question, intent)

    # ---- 纯统计路径（P1-QUALITY-20261102 必做A）：零 embedding/rerank/LLM ----
    def pure_range_answer(self, state):
        question = state["question"]
        with self.container.db_session() as db:
            from app.rag.evidence import assemble_evidence
            from app.rag.logstats import compute_range_stats

            evidence_items, range_meta = assemble_evidence(
                [], question, db, state["tenant_id"],
                per_hit_chars=getattr(self.container.settings, "evidence_excerpt_chars", 500),
                range_chars=getattr(self.container.settings, "evidence_range_chars", 12000))
            range_stats = compute_range_stats(question, [], db=db, tenant_id=state["tenant_id"])
        missing = (range_stats or {}).get("missing_lines") or []
        incomplete = (range_stats or {}).get("incomplete_excluded") or []
        range_coverage = {
            "targets": (range_meta or {}).get("targets", 0),
            "resolved": (range_meta or {}).get("targets_found", 0),
            "missing": missing, "incomplete": incomplete,
            "sufficient": not missing and not incomplete,
        }
        answer = self._render_stats_template(range_stats, evidence_items)
        stats_event = {"type": "tool", "name": "compute_log_stats", "status": "ok",
                       "args": {"ranges": (range_stats or {}).get("ranges"),
                                "conditions": [{"kind": c["kind"], "value": c["value"]}
                                               for c in (range_stats or {}).get("conditions", [])]},
                       "result": range_stats, "summary": self._stats_summary(range_stats) if range_stats else ""}
        citations_indexed = [it.get("doc_title", "") for it in evidence_items]
        citations = []
        for title in citations_indexed:
            if title and title not in citations:
                citations.append(title)
        return {
            "answer": answer, "citations": citations, "citations_indexed": citations_indexed,
            "used_evidence": evidence_items, "range_assembly": range_meta,
            "range_stats": range_stats, "range_coverage": range_coverage,
            "strategy": "range_stats_direct", "end_reason": "answered",
            "events": [stats_event,
                       {"type": "answer", "answer": answer, "citations": citations,
                        "citations_indexed": citations_indexed, "used_evidence": evidence_items}],
        }

    @staticmethod
    def _render_stats_template(stats: dict, evidence_items: list[dict]) -> str:
        """确定性模板回答：只陈述程序统计结果、口径与缺口，不做语义解释、不编造缺失部分。"""
        lines = ["以下结果由程序按固定口径对知识库文档原文直接统计得出（数值非模型生成）。"]
        for r in stats.get("ranges", []):
            if r.get("missing"):
                miss = "、L".join(str(n) for n in r["missing"][:50])
                lines.append(
                    f"⚠ 目标行范围 L{r['lo']}-L{r['hi']} 共请求 {r['requested']} 行，知识库中仅能解析其中 "
                    f"{r['resolved']} 行（缺失 {len(r['missing'])} 行：L{miss}）。"
                    "以下统计只覆盖已解析行，不能当作该范围的完整总数；缺失行的内容需要补充数据源后才能确认。")
            else:
                lines.append(f"范围 L{r['lo']}-L{r['hi']} 共 {r['resolved']} 行全部解析成功（无缺失行）。")
        if stats.get("incomplete_excluded"):
            lines.append("残行未计入统计：L" + ",".join(str(n) for n in stats["incomplete_excluded"]) + "。")
        for c in stats.get("conditions", []):
            nums = "、L".join(str(n) for n in c["line_numbers"])
            if c["kind"] == "level":
                lines.append(f"级别 {c['value']}：{c['count']} 条（行号 L{nums}）。")
            elif c["kind"] == "substring":
                lines.append(f"子串「{c['value']}」：{c['count']} 条（行号 L{nums}）。")
            elif c["kind"] == "prefix":
                lines.append(f"前缀「{c['value']}」：{c['count']} 条（行号 L{nums}）。" + (c.get('note','') or ''))
                sc = c.get('suggested_contains')
                if sc:
                    lines.append('疑似口径差异提示（包含口径，非前缀口径）：' + '；'.join(f"「{t['message'][:120]}」×{t['count']}" for t in sc['items']))
            elif c["kind"] == "literal_top":
                tops = c.get("top_items") or []
                tie = "（并列）" if len(tops) > 1 else ""
                head = "；".join(f"「{t['message']}」×{t['count']}" for t in tops)
                lines.append(f"按消息正文逐字分组，出现最多的消息共 {c.get('top_count')} 条{tie}：{head}。")
        tr = stats.get("time_range")
        if tr:
            lines.append(f"时间范围（{tr['field']}）：{tr['first']}（L{tr['first_line']}）至 {tr['last']}（L{tr['last_line']}）。")
        nd = stats.get("nodes")
        if nd:
            parts = "；".join(f"{n['node']} ×{n['count']}" for n in nd[:6])
            more = "…" if len(nd) > 6 else ""
            lines.append(f"节点/位置字段分布：{parts}{more}。")
        lg = stats.get("literal_groups")
        if lg:
            lines.append(f"不同消息原文共 {lg['distinct_messages']} 种（逐字口径，未做归一化），全部分组与来源行号如下：")
            for g in lg["groups"]:
                nums = "、L".join(str(n) for n in g["line_numbers"])
                lines.append(f"  「{g['message']}」×{g['count']}（行号 L{nums}）")
            if lg.get("listing_truncated"):
                lines.append("  （分组超过 50 种，仅列出前 50）")
        if stats.get("unsupported"):
            lines.append("以下统计诉求无法由程序确定性解析，请澄清口径后再问：" + "；".join(stats["unsupported"]) + "。")
        lines.append(stats.get("basis", ""))
        titles = []
        for it in evidence_items:
            t = it.get("doc_title", "")
            if t and t not in titles:
                titles.append(t)
        if titles:
            lines.append("来源：知识库文档《" + "》《".join(titles) + "》（编号证据 [1] 为上述行范围的原文摘录）。")
        return "\n".join(lines)

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
            "strategy": state.get("strategy", "hybrid"),
        }
        if state.get("strategy") == "range_hybrid":
            # 混合诉求组合的策略说明（P1-QUALITY-20261102 必做A）：统计已由程序在
            # 权威文档上完成；检索仅为语义解释补充上下文
            event["why_retrieval"] = ("显式行范围的统计已由程序在租户文档权威原文上完成（不消耗检索）；"
                                      "本查询还包含语义解释诉求，混合检索用于补充相关上下文")
        return {
            "hits": hits,
            "events": [event],
        }

    # ---- 相关性判断（决定是否补充检索） ----
    def grade(self, state):
        sufficient = self.container.llm.is_sufficient(state["question"], state.get("hits", []))
        iteration = state.get("iteration", 0) + 1
        event = {"type": "grade", "docs_sufficient": sufficient, "iteration": iteration}
        # "相关"与"足以回答"分开（P1-ARAG-20261102）：问题引用显式行范围时，
        # 目标行覆盖必须实际参与充足性决策——有相关日志但缺目标行段不能判充足。
        # 缺失/不完整行的定向补取走租户文档读取（确定性来源，免 embedding/rerank）；
        # 文档亦无此行时无法靠再次检索补齐，带缺口进入生成并由回答如实说明。
        range_coverage = None
        from app.rag.evidence import _resolve_target_lines, parse_line_targets, tenant_docs

        targets = parse_line_targets(state["question"])
        if targets:
            with self.container.db_session() as db:
                docs = tenant_docs(db, state["tenant_id"])
            found, incomplete = _resolve_target_lines(targets, state.get("hits", []), docs)
            missing = [t for t in targets if t not in found]
            range_coverage = {"targets": len(targets), "resolved": len(found),
                              "missing": missing, "incomplete": incomplete,
                              "sufficient": not missing and not incomplete}
            if missing or incomplete:
                sufficient = False
                event["range_coverage"] = {**range_coverage, "decision": "insufficient_until_complete"}
            else:
                event["range_coverage"] = range_coverage
        event["docs_sufficient"] = sufficient
        return {"docs_sufficient": sufficient, "iteration": iteration,
                "range_coverage": range_coverage, "events": [event]}

    def route_after_grade(self, state):
        if state.get("iteration", 0) >= 2:
            return "tools"
        cov = state.get("range_coverage")
        if cov and (cov.get("missing") or cov.get("incomplete")):
            # 缺目标行段：定向补取已按文档读取完成且为确定性来源，再次整套检索
            # 不会改变文档内容——带缺口进入生成，由回答如实说明，不做无目的补查
            return "tools"
        if state.get("docs_sufficient"):
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
                range_chars=getattr(self.container.settings, "evidence_range_chars", 12000))
            # 确定性统计（P1-ARAG-20261102）：显式行范围 + 可解析条件由程序计算，
            # 模型只依据结果解释；无法解析的口径如实列入 unsupported 供澄清。
            range_stats = None
            if range_meta.get("requested"):
                from app.rag.logstats import compute_range_stats

                range_stats = compute_range_stats(
                    state["question"], state.get("hits", []), db=db, tenant_id=state["tenant_id"])
        with self.tracer.span("generate") as span:
            if range_stats is not None:
                stat_summary = self._stats_summary(range_stats)
                state.setdefault("tool_events", []).append({
                    "name": "compute_log_stats", "status": "ok",
                    "args": {"ranges": range_stats.get("ranges"), "conditions": [
                        {"kind": c["kind"], "value": c["value"]} for c in range_stats.get("conditions", [])]},
                    "result": range_stats, "summary": stat_summary,
                })
            # 只有原始日志时，模型无法补出维修或整体运行状态的证据；该类决策用确定性边界回答。
            log_only = bool(evidence_items) and all(
                it.get("text", "").strip() and all(
                    re.match(r"^L\d+:\s+\d+\s+\d{4}\.\d{2}\.\d{2}\s", line)
                    for line in it["text"].splitlines() if line.strip()) for it in evidence_items)
            decision_question = re.search(r"根因|硬件|换件|宕机|中断|崩溃|运行状态", state["question"])
            extra_evidence = state.get("fault_records") or any(
                e.get("name") != "compute_log_stats" for e in state.get("tool_events", []))
            generation_policy = "llm"
            if range_stats and log_only and decision_question and not extra_evidence:
                generation_policy = "log_only_evidence_boundary"
                answer = self._render_stats_template(range_stats, evidence_items)
                first = evidence_items[0]["text"].splitlines()[0]
                answer += (f"\n\n原始事件示例（仅一行，不代表全部事件类型）：{first} [1]"
                           "\n\n仅凭这些日志无法确认根因、系统当前或历史整体运行状态，也无法决定是否更换硬件。"
                           "即使原文标注corrected（已纠正）或INFO，也只表示该事件的记录状态或级别，"
                           "不能据此判断有无崩溃、服务中断、其他未纠正错误或业务影响。"
                           "\n请补充同节点同时间窗的完整日志、监控、厂商诊断和变更记录；"
                           "取得这些证据及适用的厂商规范后再决定是否隔离、停机或换件。")
            else:
                answer = self.container.llm.generate_answer(
                    state["question"], state.get("hits", []), state.get("tool_events", []), state.get("fault_records", []), history,
                    evidence_items=evidence_items, range_stats=range_stats,
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
        stats_event = {"type": "tool", "name": "compute_log_stats", "status": "ok",
                       "args": {"ranges": (range_stats or {}).get("ranges"),
                                "conditions": [{"kind": c["kind"], "value": c["value"]}
                                               for c in (range_stats or {}).get("conditions", [])]},
                       "result": range_stats,
                       "summary": self._stats_summary(range_stats) if range_stats else ""} \
            if range_stats is not None else None
        payload = {"answer": answer, "citations": citations, "citations_indexed": citations_indexed,
                   "generation_policy": generation_policy,
                   "used_evidence": evidence_items, "range_assembly": range_meta,
                   "range_stats": range_stats, "range_coverage": state.get("range_coverage"),
                   "events": ([stats_event] if stats_event else []) +
                             [{"type": "answer", "answer": answer, "citations": citations,
                               "citations_indexed": citations_indexed, "used_evidence": evidence_items}]}
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
