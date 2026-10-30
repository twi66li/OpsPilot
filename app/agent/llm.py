"""LLM 抽象层。

- MockLLM：规则式实现，确定性输出，离线可用于开发/测试/验收。
- OpenAIChatLLM：任意 OpenAI 兼容 API（生产）；失败时自动回退 Mock 行为。
"""
import json
import logging
import re

from app.rag.embeddings import tokenize

logger = logging.getLogger(__name__)

TICKET_KEYWORDS = re.compile(r"创建工单|提交工单|开个工单|建单|提单|工单|ticket", re.IGNORECASE)


def detect_intent(question: str) -> str:
    if re.search(r"(?:不要|无需|不用|禁止|不必).{0,8}(?:工单|建单|提单|ticket)", question, re.IGNORECASE):
        return "diagnose"
    return "create_ticket" if TICKET_KEYWORDS.search(question) else "diagnose"


class BaseLLM:
    def is_sufficient(self, question: str, hits: list[dict]) -> bool:
        raise NotImplementedError

    def rewrite_query(self, question: str) -> str:
        raise NotImplementedError

    def generate_answer(self, question: str, hits: list[dict], tool_events: list[dict], fault_records: list[dict], history: list[dict]) -> str:
        raise NotImplementedError

    def draft_ticket(self, question: str, answer: str) -> dict:
        raise NotImplementedError


class MockLLM(BaseLLM):
    """规则式 Mock：判断、改写、生成全部基于词面规则，保证离线可复现。"""

    def is_sufficient(self, question: str, hits: list[dict]) -> bool:
        if not hits:
            return False
        q_tokens = set(tokenize(question))
        for hit in hits[:5]:
            h_tokens = set(tokenize(hit.get("text", "")))
            overlap = q_tokens & h_tokens
            # 命中 2 个以上词元，或包含错误码/服务名这类强标识即认为相关
            if len(overlap) >= 2:
                return True
        return False

    def rewrite_query(self, question: str) -> str:
        return f"{question} 排查步骤 根因分析 处理方案 SOP"

    def generate_answer(self, question, hits, tool_events, fault_records, history) -> str:
        q_tokens = set(tokenize(question))
        relevant = [
            h for h in hits[:5] if len(q_tokens & set(tokenize(h.get("text", "")))) >= 2
        ]
        if not relevant:
            return "知识库中未找到与该问题直接相关的资料，无法给出可靠结论。建议提供错误码、服务名或更具体的症状描述后再试。"
        lines = [f"针对问题「{question}」，基于知识库检索到 {len(relevant)} 篇相关文档："]
        for i, hit in enumerate(relevant[:3], 1):
            snippet = hit.get("text", "").replace("\n", " ")[:150]
            lines.append(f"[{i}] 《{hit.get('doc_title', '')}》 {snippet}…")
        if fault_records:
            fr = fault_records[0]
            lines.append(f"\n历史相似故障：《{fr['title']}》（{fr['service']} / {fr['error_code'] or '无错误码'}）：{fr['resolution'][:120]}")
        for ev in tool_events:
            if ev.get("name") == "query_service_metrics" and ev.get("anomalies"):
                lines.append(f"\n实时指标异常（{ev.get('args', {}).get('service')}）：{'；'.join(ev['anomalies'])}")
        lines.append("\n建议：按上述文档中的排查步骤逐项执行，优先检查变更与错误日志，并持续观察核心指标。如需我创建工单，请回复“创建工单”。")
        return "\n".join(lines)

    def draft_ticket(self, question: str, answer: str) -> dict:
        try:
            from app.agent.tools import extract_entities

            entities = extract_entities(question)
            service = (entities["services"] or ["unknown-service"])[0]
            error_code = entities["error_codes"][0] if entities["error_codes"] else ""
        except Exception:
            service, error_code = "unknown-service", ""
        severity = "P1" if re.search(r"宕机|不可用|大面积|全部失败|P1", question, re.IGNORECASE) else "P2"
        return {
            "title": f"[{severity}] {service} {'错误码 ' + error_code if error_code else '故障'}：{question[:60]}",
            "description": f"问题描述：{question}\n\n初步诊断：\n{answer[:500]}",
            "severity": severity,
            "service": service,
        }


def extract_service_hint(question: str) -> str | None:
    m = re.search(r"[a-z][a-z0-9]*-service", question, re.IGNORECASE)
    return m.group(0).lower() if m else None


def extract_error_code_hint(question: str) -> str | None:
    m = re.search(r"(?:ERR|ERROR)[_-]?[A-Za-z0-9]+", question, re.IGNORECASE)
    return m.group(0).upper().replace("-", "_") if m else None


class OpenAIChatLLM(BaseLLM):
    """OpenAI 兼容 API 实现；调用失败时回退到规则式，保证服务不中断。"""

    def __init__(self, api_key: str, base_url: str, model: str, strict: bool = False):
        from app.services.model_api import ChatAPI

        self._api = ChatAPI(api_key, base_url, model)
        self.model = model
        self.strict = strict
        self._fallback = MockLLM()

    def _chat(self, system: str, user: str) -> str:
        return self._api.chat(system, user)

    @staticmethod
    def _parse_json(text: str):
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    def is_sufficient(self, question: str, hits: list[dict]) -> bool:
        if not hits:
            return False
        docs = "\n".join(f"[{i}] {h.get('doc_title', '')}: {h.get('text', '')[:300]}" for i, h in enumerate(hits[:5], 1))
        prompt = f"问题：{question}\n\n候选文档：\n{docs}\n\n判断每个文档是否与问题相关，输出 JSON: {{\"relevant\": [true/false, ...]}}"
        try:
            data = self._parse_json(self._chat("你是运维知识库检索相关性判断助手，只输出 JSON。", prompt))
            return bool(data and any(data.get("relevant", [])))
        except Exception as exc:
            if self.strict:
                raise
            logger.warning("LLM 相关性判断失败，回退规则: %s", exc)
            return self._fallback.is_sufficient(question, hits)

    def rewrite_query(self, question: str) -> str:
        try:
            rewritten = self._chat(
                "你是运维检索查询改写助手。把用户问题改写为更适合知识库检索的短查询（补充同义词与关键术语），直接输出改写后的查询。",
                question,
            ).strip()
            if not rewritten and self.strict:
                raise ValueError("模型返回空查询改写")
            return rewritten or self._fallback.rewrite_query(question)
        except Exception as exc:
            if self.strict:
                raise
            logger.warning("LLM 查询改写失败，回退规则: %s", exc)
            return self._fallback.rewrite_query(question)

    def generate_answer(self, question, hits, tool_events, fault_records, history) -> str:
        context = "\n".join(f"[{i}] 《{h.get('doc_title', '')}》 {h.get('text', '')[:500]}" for i, h in enumerate(hits, 1))
        tools_info = "\n".join(f"- {e.get('name')}: {e.get('summary', '')}" for e in tool_events) or "（无）"
        faults = "\n".join(f"- {f['title']}: {f['resolution']}" for f in fault_records) or "（无）"
        system = (
            "你是 AIOps 故障诊断助手。仅依据给定的知识库文档、工具输出和历史故障回答，"
            "答案中使用 [n] 标注引用的文档编号；不要编造。用中文回答。"
            "指标必须注明来源和时间窗；模拟数据须明确说明。指标不可用或采样不足不等于服务正常，"
            "此时只能给出排查建议，不能确认当前状态或根因。历史故障仅为参考。"
            "工具的本次阈值判定优先于手册中其他场景的阈值；不得把已触发阈值的指标写成正常。"
        )
        user = f"用户问题：{question}\n\n知识库文档：\n{context}\n\n工具输出：\n{tools_info}\n\n历史相似故障：\n{faults}"
        try:
            return self._chat(system, user)
        except Exception as exc:
            if self.strict:
                raise
            logger.warning("LLM 生成失败，回退规则: %s", exc)
            return self._fallback.generate_answer(question, hits, tool_events, fault_records, history)

    def draft_ticket(self, question: str, answer: str) -> dict:
        system = "你是运维工单起草助手，根据对话生成工单草稿，只输出 JSON。"
        user = f"问题：{question}\n诊断结论：{answer[:800]}\n\n输出 JSON: {{\"title\": str, \"description\": str, \"severity\": \"P1|P2|P3\", \"service\": str}}"
        try:
            data = self._parse_json(self._chat(system, user))
            if data and data.get("title"):
                return data
        except Exception as exc:
            if self.strict:
                raise
            logger.warning("LLM 工单起草失败，回退规则: %s", exc)
        if self.strict:
            raise ValueError("模型没有返回有效工单草稿")
        return self._fallback.draft_ticket(question, answer)


def get_llm(settings):
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAIChatLLM(settings.openai_api_key, settings.openai_base_url, settings.chat_model, getattr(settings, "model_strict", False))
    if getattr(settings, "model_strict", False):
        raise ValueError("真实评测必须配置对话模型和密钥")
    return MockLLM()
