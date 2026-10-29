"""离线评测脚本：python -m eval.run_eval

对比不同检索方案（dense / bm25 / hybrid）在固定问题集上的效果：
Recall@K、MRR、关键词覆盖率（忠实度代理）、启发式忠实度、延迟。

用法：
  python -m scripts.seed                      # 先灌入种子数据
  python -m eval.run_eval                     # 默认 dense/bm25/hybrid 三方案
  python -m eval.run_eval --modes hybrid --rerank heuristic
  python -m eval.run_eval --rerank none       # 关闭重排对照

报告输出到 eval/reports/eval_report.md 与 .json。
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.container import get_container
from app.db.base import SessionLocal, init_db
from app.db.models import Document
from app.rag.hybrid import HybridRetriever
from app.rag.rerank import HeuristicReranker
from app.services import chat_service, ticket_service
from app.services.agent_service import decide_approval
from eval.metrics import aggregate, faithfulness_heuristic, keyword_coverage, mrr_at_k, recall_at_k

QUESTIONS_PATH = Path(__file__).parent / "questions.yaml"
REPORT_DIR = Path(__file__).parent / "reports"


def load_questions() -> list[dict]:
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["questions"]


def build_retriever(container, rerank_provider: str) -> HybridRetriever:
    reranker = HeuristicReranker() if rerank_provider == "heuristic" else None
    return HybridRetriever(
        container.vector_store, container.bm25, container.embedder, reranker, container.settings
    )


def eval_retrieval(retriever, llm, questions, tenant_id: str, top_k: int) -> list[dict]:
    rows = []
    for q in questions:
        if q.get("type") == "workflow":
            continue
        start = time.perf_counter()
        with SessionLocal() as db:
            result = retriever.retrieve(db, tenant_id, q["question"], mode=retriever.mode_name, top_k=top_k, log=False)
        retrieval_ms = (time.perf_counter() - start) * 1000

        hit_titles = [h.doc_title for h in result.hits]
        expected = q.get("expected_docs", [])

        start = time.perf_counter()
        hits_dicts = [h.to_dict() for h in result.hits]
        answer = llm.generate_answer(q["question"], hits_dicts, [], [], [])
        answer_ms = (time.perf_counter() - start) * 1000

        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "category": q.get("category", ""),
                "recall": recall_at_k(expected, hit_titles, top_k),
                "mrr": mrr_at_k(expected, hit_titles, top_k),
                "keyword_coverage": keyword_coverage(answer, q.get("expected_keywords", [])),
                "faithfulness": round(faithfulness_heuristic(answer, [h.text for h in result.hits]), 4),
                "latency_ms": round(retrieval_ms + answer_ms, 1),
                "hit_titles": hit_titles,
            }
        )
    return rows


def eval_workflow(container, top_k: int) -> dict:
    """工单审批端到端：提问 → interrupt → 审批 → 工单落库。"""
    from app.db.models import User

    start = time.perf_counter()
    with SessionLocal() as db:
        admin = db.query(User).filter(User.username == "admin").first()
        session = chat_service.create_session(db, "default", admin.id, "eval-workflow")
        result = {}
        from app.services.agent_service import chat_once

        chat_result = chat_once(container, db, "default", admin.id, admin.username, session.id, "order-service 又出现超时了，帮我创建工单跟进处理。")
        approval_ok = chat_result["status"] == "approval_required" and chat_result.get("approval")
        if approval_ok:
            auth = type("Auth", (), {"username": "admin", "tenant_id": "default", "role": "admin"})()
            decide = decide_approval(container, db, auth, chat_result["approval"]["approval_id"], True, "eval 审批通过")
            result["ticket_created"] = bool(decide.get("ticket_id"))
            pending = ticket_service.get_pending_approval(db, "default", session.id)
            result["approval_closed"] = pending is None
    elapsed = (time.perf_counter() - start) * 1000
    return {
        "id": "q_workflow_ticket",
        "interrupt_triggered": approval_ok,
        "ticket_created": result.get("ticket_created", False),
        "approval_closed": result.get("approval_closed", False),
        "latency_ms": round(elapsed, 1),
    }


def render_markdown(summary: dict, workflow: dict, top_k: int) -> str:
    lines = [
        "# 检索方案对比评测报告",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 评测方案：{', '.join(summary.keys())}",
        f"- Top-K：{top_k}",
        "",
        "| 方案 | Recall@K | MRR | 关键词覆盖 | 忠实度(启发式) | 平均耗时(ms) | P95(ms) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for mode, metrics in summary.items():
        lines.append(
            f"| {mode} | {metrics[f'recall_at_{top_k}']} | {metrics['mrr']} | {metrics['keyword_coverage']} "
            f"| {metrics['faithfulness']} | {metrics['avg_ms']} | {metrics['p95_ms']} |"
        )
    lines += [
        "",
        "## 工单审批工作流（端到端）",
        "",
        f"- 触发人工审批（LangGraph interrupt）：{'✅' if workflow['interrupt_triggered'] else '❌'}",
        f"- 审批通过后工单创建：{'✅' if workflow['ticket_created'] else '❌'}",
        f"- 审批单闭环：{'✅' if workflow['approval_closed'] else '❌'}",
        f"- 端到端耗时：{workflow['latency_ms']}ms",
        "",
        "> 忠实度为启发式代理指标（答案句子被检索上下文支撑的比例）；配置真实 LLM 后可切换 LLM-as-judge。",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="检索方案对比评测")
    parser.add_argument("--modes", nargs="+", default=["dense", "bm25", "hybrid"], choices=["dense", "bm25", "hybrid"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rerank", default="settings", choices=["settings", "none", "heuristic"], help="评测时的重排策略")
    args = parser.parse_args()

    init_db()
    container = get_container()
    with SessionLocal() as db:
        if db.query(Document).filter(Document.tenant_id == "default").count() == 0:
            from scripts.seed import seed_all

            seed_all(db, container)
            print("知识库为空，已自动灌入种子数据。")
        from app.services import kb_service

        chunk_total = kb_service.warm_indexes(db, container)
        print(f"索引就绪：{chunk_total} 个分块。")

    questions = load_questions()
    retriever = build_retriever(container, args.rerank)

    summary: dict[str, dict] = {}
    detail: dict[str, list] = {}
    for mode in args.modes:
        retriever.mode_name = mode
        rows = eval_retrieval(retriever, container.llm, questions, "default", args.top_k)
        detail[mode] = rows
        summary[mode] = aggregate(rows, args.top_k)
        print(f"[{mode}] recall@{args.top_k}={summary[mode][f'recall_at_{args.top_k}']} mrr={summary[mode]['mrr']} avg={summary[mode]['avg_ms']}ms")

    workflow = eval_workflow(container, args.top_k)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_json = {"generated_at": datetime.now(timezone.utc).isoformat(), "top_k": args.top_k, "summary": summary, "detail": detail, "workflow": workflow}
    (REPORT_DIR / "eval_report.json").write_text(json.dumps(report_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / "eval_report.md").write_text(render_markdown(summary, workflow, args.top_k), encoding="utf-8")
    print(f"报告已写入 {REPORT_DIR / 'eval_report.md'}")


if __name__ == "__main__":
    main()
