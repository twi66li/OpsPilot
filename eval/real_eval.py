"""固定数据集的真实模型离线实验：python -m eval.real_eval --output <新目录>。

检索比较与回答/行为评测分层记录。凭据仅从进程环境读取，不写入报告。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

MODES = ("bm25", "dense", "hybrid", "hybrid_rerank")


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if attempt == 7 or (exc.errno not in (13, 22) and getattr(exc, "winerror", None) not in (5, 32, 33)):
                raise
            # 只重试文件替换，不重复任何模型调用；完整旧快照仍可读取。
            time.sleep(.1 * (attempt + 1))


def error_info(exc):
    text = str(exc)
    for name in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        if os.getenv(name):
            text = text.replace(os.environ[name], "[REDACTED]")
    return {"type": type(exc).__name__, "message": re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)[:300]}


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None}
    return {"n": len(values), "mean_ms": round(sum(values) / len(values), 2),
            "p50_ms": round(values[(len(values) - 1) // 2], 2),
            "p95_ms": round(values[min(len(values) - 1, int(len(values) * .95))], 2)}


class EvaluationEmbeddings:
    """只缓存相同模型/维度/文本的向量；记录新请求耗时，不将命中耗时冒充冷请求。"""
    def __init__(self, provider, path):
        self.provider, self.path = provider, path
        self.cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.call_path = path.with_name("embedding_calls.json")
        if self.call_path.exists():
            self.provider.calls = json.loads(self.call_path.read_text(encoding="utf-8"))

    def embed(self, texts):
        keys = [hashlib.sha256(f"{self.provider.model}:{self.provider.dim}:{t}".encode()).hexdigest() for t in texts]
        missing = list(dict.fromkeys(k for k in keys if k not in self.cache))
        for start in range(0, len(missing), 16):
            batch = missing[start:start + 16]
            vectors = self.provider.embed([texts[keys.index(k)] for k in batch])
            for key, vector in zip(batch, vectors, strict=True):
                self.cache[key] = vector
            save(self.path, self.cache)
            save(self.call_path, self.provider.calls)
        return [self.cache[k] for k in keys]


def document_hits(hits, top_k=5):
    seen, result = set(), []
    for hit in hits:
        if hit.doc_id not in seen:
            result.append(hit)
            seen.add(hit.doc_id)
        if len(result) == top_k:
            break
    return result


def retrieval_rows(container, questions, reranker, output):
    from eval.metrics import mrr_at_k, recall_at_k

    row_path = output / "retrieval.json"
    rows = json.loads(row_path.read_text(encoding="utf-8")) if row_path.exists() else []
    completed = {(r["id"], r["mode"]) for r in rows}
    for index, q in enumerate(questions):
        if q["task_type"] not in ("retrieval", "abstain"):
            continue
        if all((q["id"], mode) in completed for mode in MODES):
            continue
        started = time.perf_counter()
        container.embedder.embed([q["question"]])
        embedding_ms = (time.perf_counter() - started) * 1000
        for mode in MODES:
            if (q["id"], mode) in completed:
                continue
            row = {"id": q["id"], "split": q["split"], "task_type": q["task_type"], "mode": mode,
                   "status": "error", "query_embedding_ms": embedding_ms, "recall": None, "mrr": None, "hits": []}
            started = time.perf_counter()
            try:
                result = container.retriever.retrieve(None, "default", q["question"],
                    mode="hybrid" if mode == "hybrid_rerank" else mode, top_k=8, log=False)
                hits = result.hits
                if mode == "hybrid_rerank":
                    hits = reranker.rerank(q["question"], deepcopy(hits), top_k=8)
                hits = document_hits(hits)
                row.update(status="ok", hits=[h.to_dict() for h in hits])
                if q["task_type"] == "retrieval":
                    ids = [h.doc_id for h in hits]
                    row.update(recall=recall_at_k(q["expected_doc_ids"], ids, 5),
                               mrr=mrr_at_k(q["expected_doc_ids"], ids, 5))
            except Exception as exc:
                row["error"] = error_info(exc)
            row["search_with_warm_query_vector_ms"] = (time.perf_counter() - started) * 1000
            rows.append(row)
        save(output / "retrieval.json", rows)
        save(output / "rerank_calls.json", reranker.calls)
        if (index + 1) % 10 == 0:
            print(f"检索 {index + 1}/{len(questions)}", flush=True)
    return rows


def select_on_dev(rows, answers=None):
    ranking = []
    for mode in MODES:
        sample = [r for r in rows if r["mode"] == mode and r["split"] == "dev" and r["task_type"] == "retrieval"]
        if not sample:
            raise ValueError("必须包含开发分区，不能在测试分区上选方案")
        judged = [r for r in (answers or []) if r["mode"] == mode and r["split"] == "dev"]
        correct = sum(bool((r.get("judgment") or {}).get("answer_correct")) for r in judged) / len(judged) if judged else 0
        ranking.append((correct, sum((r["recall"] or 0) for r in sample) / len(sample),
                        sum((r["mrr"] or 0) for r in sample) / len(sample), -MODES.index(mode), mode))
    return max(ranking)[-1]


def generate_and_judge(q, row, settings, is_ops, judge_model):
    from app.agent.llm import OpenAIChatLLM
    from app.services.model_api import ChatAPI

    llm = OpenAIChatLLM(settings.openai_api_key, settings.openai_base_url, settings.chat_model, strict=True)
    judge = ChatAPI(settings.openai_api_key, settings.openai_base_url, judge_model)
    record = {"id": q["id"], "split": q["split"], "task_type": q["task_type"], "mode": row["mode"],
              "status": "error", "answer": "", "judgment": None}
    try:
        if row["status"] != "ok":
            raise ValueError("所选检索方案本题失败，不能用空上下文冒充成功")
        started = time.perf_counter()
        args = [q["question"], row["hits"], [], []]
        answer = llm.generate_answer(*args, []) if is_ops else llm.generate_answer(*args)[0]
        record.update(answer=answer, generation_ms=(time.perf_counter() - started) * 1000,
                      model_first_token_ms=llm._api.calls[-1]["first_token_ms"])
        prompt = {"question": q["question"], "reference_answer": q["reference_answer"],
                  "required_points": q["answer_points"], "expected_abstention": q["should_abstain"],
                  "retrieved_context": row["hits"], "answer": answer}
        text = judge.chat(
            '你是中文问答评测裁判。只依据输入资料评分，不调用外部知识。返回JSON：'
            '{"answer_correct":true/false,"grounded":true/false,"abstained":true/false,"reason":"简要理由"}。'
            'answer_correct要求覆盖必要要点且无实质错误；grounded要求关键事实受检索内容支持，'
            '明确说明缺信息不算幻觉；abstained表示没有捏造题目索要的事实并明确表示不足或需查询。'
            '无答案题仅说明未知、边界与必要下一步即可正确，不能因措辞不完全一致判错。',
            json.dumps(prompt, ensure_ascii=False), json_mode=True)
        judgment = json.loads(text)
        if not all(type(judgment.get(k)) is bool for k in ("answer_correct", "grounded", "abstained")):
            raise ValueError("裁判没有返回有效布尔评分")
        record.update(status="ok", judgment=judgment)
    except Exception as exc:
        record["error"] = error_info(exc)
    finally:
        record["model_calls"] = llm._api.calls
        record["judge_calls"] = judge.calls
        llm._api.client.close()
        judge.client.close()
    return record


def behavior_rows(client, container, questions, is_ops, output):
    from app.auth.security import hash_password
    from app.db.base import SessionLocal
    from app.db.models import Ticket, User

    headers = {}
    for role in ("admin", "engineer" if is_ops else "agent", "viewer"):
        username, password = "eval_" + role, "eval-only-local-credentials"
        with SessionLocal() as db:
            db.add(User(tenant_id="default", username=username, password_hash=hash_password(password), role=role))
            db.commit()
        resp = client.post("/auth/login", json={"username": username, "password": password})
        resp.raise_for_status()
        headers[role] = {"Authorization": "Bearer " + resp.json()["access_token"]}
    row_path = output / "behavior.json"
    rows = json.loads(row_path.read_text(encoding="utf-8")) if row_path.exists() else []
    completed = {r["id"] for r in rows}
    for q in questions:
        if q["task_type"] in ("retrieval", "abstain"):
            continue
        if q["id"] in completed:
            continue
        row = {"id": q["id"], "split": q["split"], "task_type": q["task_type"], "status": "error"}
        before_calls = len(container.llm._api.calls)
        started = time.perf_counter()
        try:
            resp = client.post("/api/sessions", json={"title": q["id"]}, headers=headers["admin"])
            resp.raise_for_status()
            session_id = resp.json()["id"]
            # 直接逐个消费真实应用生成器，TestClient 会缓存响应，不能用它测到达顺序。
            from app.services.agent_service import chat_stream

            with SessionLocal() as db:
                admin = db.query(User).filter(User.username == "eval_admin").one()
                user_id = admin.id
            events = []
            first_answer_ms = None
            stream_started = time.perf_counter()
            for raw in chat_stream(container, "default", user_id, "eval_admin", session_id, q["question"]):
                event = json.loads(raw.removeprefix("data: "))
                events.append(event)
                if event.get("type") == "answer" and first_answer_ms is None:
                    first_answer_ms = (time.perf_counter() - stream_started) * 1000
            final = next(e for e in events if e["type"] == "final")
            checks = {"stream_start": events[0]["type"] == "start", "stream_done": events[-1]["type"] == "done"}
            if q["task_type"] == "tool":
                tools = [e for e in events if e["type"] == "tool"]
                checks["required_tools"] = set(q["require_tools"]) <= {e["name"] for e in tools}
                if is_ops:
                    checks["tool_arguments"] = any(e.get("args", {}).get("service") == q["expected_service"] for e in tools)
                else:
                    checks["tool_arguments"] = all(e.get("args", {}).get("order_id") == q["expected_order_id"] for e in tools)
            elif q["task_type"] == "chitchat":
                checks.update(intent=final["intent"] == "chitchat", retrieval_skipped=not any(e["type"] == "retrieval" for e in events))
            else:
                case = q["workflow"]
                checks["interrupt"] = final["status"] == "approval_required" and bool(final.get("approval"))
                with SessionLocal() as db:
                    checks["no_ticket_before_approval"] = db.query(Ticket).filter(Ticket.session_id == session_id).count() == 0
                approval_id = final["approval"]["approval_id"]
                response = client.post(f"/api/agent/approvals/{approval_id}/decide",
                    json={"approved": case["decision"] != "reject", "comment": "离线评测决定"},
                    headers=headers[case["actor_role"]])
                if case["decision"] == "approve_then_repeat":
                    checks["first_approval"] = response.status_code == 200
                    response = client.post(f"/api/agent/approvals/{approval_id}/decide",
                        json={"approved": True, "comment": "重复决定"}, headers=headers[case["actor_role"]])
                checks["decision_status"] = response.status_code == case["expected_decision_status"]
                with SessionLocal() as db:
                    checks["ticket_creation"] = bool(db.query(Ticket).filter(Ticket.session_id == session_id).count()) == case["expected_ticket_created"]
                row["decision_status"] = response.status_code
            row.update(status="ok", passed=all(checks.values()), checks=checks, events=events,
                       first_answer_event_ms=first_answer_ms)
        except Exception as exc:
            row["error"] = error_info(exc)
        row["elapsed_ms"] = (time.perf_counter() - started) * 1000
        row["model_calls"] = container.llm._api.calls[before_calls:]
        rows.append(row)
        save(output / "behavior.json", rows)
        print(f"行为 {q['id']}：{row.get('passed', False)}", flush=True)
    return rows


def score_tool_answers(rows, questions, api_key, base_url, judge_model):
    """工具事件存在不代表回答使用了证据；单独检查真实保存的输出与回答。"""
    from app.services.model_api import ChatAPI
    lookup = {q["id"]: q for q in questions}
    judge = ChatAPI(api_key, base_url, judge_model)
    for row in rows:
        if row["task_type"] != "tool" or row["status"] != "ok" or row.get("tool_answer_judgment"):
            continue
        before = len(judge.calls)
        try:
            final = next(e for e in row["events"] if e["type"] == "final")
            payload = {"question": lookup[row["id"]]["question"], "answer": final["answer"],
                       "tools": [e for e in row["events"] if e["type"] == "tool"]}
            verdict = json.loads(judge.chat(
                '核对回答和本次实际工具输出，只返回JSON：'
                '{"uses_tool_results":true/false,"values_consistent":true/false,"simulation_disclosed":true/false,"reason":"简述"}。'
                'uses_tool_results要求回答真正复述至少一个核心查询结果，不能只说建议查询。'
                'values_consistent要求回答涉及的订单、物流、指标或故障事实与工具一致，不杜撰。'
                'simulation_disclosed仅当回答明确说明本次数据为演示/模拟时为true；不把缺少说明自动当作数值错误。',
                json.dumps(payload, ensure_ascii=False), json_mode=True))
            if not all(type(verdict.get(k)) is bool for k in ("uses_tool_results", "values_consistent", "simulation_disclosed")):
                raise ValueError("工具回答裁判格式错误")
            row["tool_answer_judgment"] = verdict
            row["checks"]["answer_matches_tool_output"] = verdict["uses_tool_results"] and verdict["values_consistent"]
            row["checks"]["simulation_scope_disclosed"] = verdict["simulation_disclosed"]
            row["passed"] = all(row["checks"].values())
        except Exception as exc:
            row["tool_scoring_error"] = error_info(exc)
            row["checks"]["answer_matches_tool_output"] = False
            row["passed"] = False
        row["judge_calls"] = judge.calls[before:]
    judge.client.close()


def summarize(retrieval, answers, behaviors, selected):
    result = {"selected_on_dev": selected, "splits": {}}
    for split in ("dev", "test"):
        summary = {"retrieval": {}}
        for mode in MODES:
            rows = [r for r in retrieval if r["split"] == split and r["mode"] == mode and r["task_type"] == "retrieval"]
            n = len(rows)
            summary["retrieval"][mode] = {"n": n, "errors": sum(r["status"] != "ok" for r in rows),
                "recall_at_5": sum((r["recall"] or 0) for r in rows) / n if n else None,
                "mrr_at_5": sum((r["mrr"] or 0) for r in rows) / n if n else None,
                "search_with_warm_query_vector": distribution([r["search_with_warm_query_vector_ms"] for r in rows if r["status"] == "ok"])}
        rows = [r for r in answers if r["split"] == split and r["mode"] == selected]
        positives = [r for r in rows if r["task_type"] == "retrieval"]
        negatives = [r for r in rows if r["task_type"] == "abstain"]
        workflow = [r for r in behaviors if r["split"] == split]
        def rate(sample, predicate):
            return sum(bool(predicate(r)) for r in sample) / len(sample) if sample else None
        summary["answers"] = {"n": len(rows), "errors": sum(r["status"] != "ok" for r in rows),
            "judge_answer_correct_rate": rate(positives, lambda r: (r.get("judgment") or {}).get("answer_correct")),
            "judge_grounded_rate": rate(positives, lambda r: (r.get("judgment") or {}).get("grounded")),
            "correct_abstention_rate": rate(negatives, lambda r: (r.get("judgment") or {}).get("abstained") and (r.get("judgment") or {}).get("answer_correct")),
            "generation": distribution([r.get("generation_ms") for r in rows if r["status"] == "ok"]),
            "model_first_token": distribution([r.get("model_first_token_ms") for r in rows if r["status"] == "ok"])}
        summary["behavior"] = {"n": len(workflow), "passed": sum(r.get("passed", False) for r in workflow),
            "errors": sum(r["status"] != "ok" for r in workflow),
            "functional_passed": sum(r["status"] == "ok" and all(v for k, v in r.get("checks", {}).items() if k != "simulation_scope_disclosed") for r in workflow),
            "tool_cases": sum(r["task_type"] == "tool" for r in workflow),
            "tool_answer_consistent": sum(r.get("checks", {}).get("answer_matches_tool_output", False) for r in workflow),
            "simulation_disclosed": sum(r.get("checks", {}).get("simulation_scope_disclosed", False) for r in workflow)}
        result["splits"][split] = summary
    return result


def write_report(output, run, summary, answers, behaviors, calls):
    def percentage(value):
        return "不适用" if value is None else f"{value * 100:.2f}%"

    usage = {}
    for call in calls:
        item = usage.setdefault(call["model"], {"requests": 0, "total_tokens": 0, "errors": 0})
        item["requests"] += 1
        item["total_tokens"] += call.get("usage", {}).get("total_tokens", 0)
        item["errors"] += call["status"] != "ok"
    save(output / "usage.json", usage)
    test = summary["splits"]["test"]
    dev_rows = []
    for mode in MODES:
        rows = [r for r in answers if r["split"] == "dev" and r["mode"] == mode]
        correct = sum(bool((r.get("judgment") or {}).get("answer_correct")) for r in rows)
        dev_rows.append({"mode": mode, "n": len(rows), "correct": correct,
                         "rate": correct / len(rows) if rows else None})
    save(output / "development_comparison.json", dev_rows)
    lines = ["# 真实模型离线评测报告", "", f"- 完成时间（北京时间）：{datetime.fromisoformat(run['completed_at']).astimezone(timezone(timedelta(hours=8))).isoformat()}",
        f"- 数据集：{run['dataset_id']}；分区题数：{run['question_counts']}",
        f"- 对话模型：{run['chat_model']}；Embedding：{run['embedding_model']}（1024 维）",
        f"- 重排模型：{run['rerank_model']}；模型裁判：{run['judge_model']}",
        f"- 向量后端：{run['vector_backend']}；数据库：隔离的内存 SQLite",
        f"- 代码提交：{run['git_head']}；运行源码及数据摘要见 run_manifest.json", "",
        "## 实验口径", "",
        "以同一资料、同一切分、同一查询向量比较四方案，各取 8 个候选块，再按来源文档去重取 5 个上下文。",
        "只在开发集上比较答案质量并选择方案；冻结后在测试集评估，不按测试结果改参数或题目。",
        "无参考资料题单列拒答，不计入 Recall/MRR。回答评分来自模型裁判，不等同人工标注准确率。",
        "查询向量首次请求耗时单独记录；检索表为查询向量缓存后的搜索/重排耗时，不能称为生产端到端响应时间。",
        "RAG 题测检索与回答组件；工具/审批/闲聊题消费真实应用图生成器并调用审批 HTTP 接口，业务工具仍为项目模拟实现。", "",
        "## 开发集方案选择", "", "| 方案 | 问答/拒答题数 | 裁判判对 | 比例 |",
        "| --- | ---: | ---: | ---: |"]
    for row in dev_rows:
        lines.append(f"| {row['mode']} | {row['n']} | {row['correct']} | {percentage(row['rate'])} |")
    lines += ["", f"冻结方案：**{summary['selected_on_dev']}**。并列时依次比较开发集 Recall@5、MRR，再选择较简单方案。",
        "", "## 固定测试集检索结果", "", "| 方案 | 有参考题数 | Recall@5 | MRR@5 | 请求失败 | 缓存查询向量后 P95(ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for mode, metrics in test["retrieval"].items():
        lines.append(f"| {mode} | {metrics['n']} | {percentage(metrics['recall_at_5'])} | "
            f"{percentage(metrics['mrr_at_5'])} | {metrics['errors']} | {metrics['search_with_warm_query_vector']['p95_ms']} |")
    a = test["answers"]
    b = test["behavior"]
    lines += ["", "## 固定测试集回答与行为", "",
        f"- 所选方案 RAG 问答及拒答题：{a['n']}；请求/评分错误：{a['errors']}。",
        f"- 有参考问题的模型裁判答对率：{percentage(a['judge_answer_correct_rate'])}。",
        f"- 有参考问题的模型裁判证据支持率：{percentage(a['judge_grounded_rate'])}。",
        f"- 无答案问题正确拒答率：{percentage(a['correct_abstention_rate'])}。",
        f"- 生成耗时：{a['generation']}。",
        f"- 模型首个非空内容 token 耗时：{a['model_first_token']}。",
        f"- 含模拟数据声明要求的完整行为契约：{b['passed']}/{b['n']} 通过；运行错误：{b['errors']}。",
        f"- 功能及工具证据契约（不含来源声明）：{b['functional_passed']}/{b['n']}。",
        f"- 工具回答与实际输出一致：{b['tool_answer_consistent']}/{b['tool_cases']}；明确声明模拟来源：{b['simulation_disclosed']}/{b['tool_cases']}。", "",
        "模型首 token 与应用 SSE 的首个回答事件分别计时：当前应用按节点发送回答，不把节点流声称为逐 token UI 流。",
        "行为事件及审批状态见 behavior.json；逐题回答、证据、裁判理由与调用用量见 answers.json。", "",
        "## 失败明细", ""]
    failures = [r for r in answers if r["split"] == "test" and (r["status"] != "ok" or not (r.get("judgment") or {}).get("answer_correct"))]
    failures += [r for r in behaviors if r["split"] == "test" and not r.get("passed", False)]
    if not failures:
        lines.append("本次所列测试用例未记录失败；这不代表开放场景均正确。")
    for row in failures:
        reason = row.get("error") or (row.get("judgment") or {}).get("reason") or (row.get("tool_answer_judgment") or {}).get("reason") or row.get("checks")
        lines.append(f"- {row['id']}：{reason}")
    lines += ["", "## 用量与限制", "", "```json", json.dumps(usage, ensure_ascii=False, indent=2), "```", "",
        "题库为资料驱动的合成题，human_reviewed=false；商家政策、错误码与阈值包含演示设定。",
        f"调用用量覆盖说明：{run.get('usage_coverage', '本次成功运行已记录的 API usage')}。",
        "本报告没有验证真实订单/生产监控接入，也不替代 MySQL/PostgreSQL、Redis、Milvus 独立服务部署与负载测试。",
        "不得将这些结果表述为线上用户准确率、业务耗时降低或生产规模收益。", "",
        "复现：在进程环境提供 DASHSCOPE_API_KEY、DASHSCOPE_BASE_URL 后，运行：", "",
        "```powershell", "python -m eval.real_eval --dataset eval/datasets/v1 --output eval/reports/<新的实验目录>", "```", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def complete_saved_tool_scoring(output, dataset, api_key, base_url):
    """仅补齐已保存工具回答的评分，绝不重新调用主模型或重跑检索。"""
    run = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    rows = json.loads((output / "behavior.json").read_text(encoding="utf-8"))
    if all(r.get("tool_answer_judgment") for r in rows if r["task_type"] == "tool"):
        print("工具回答评分已齐全，无需重复调用。", flush=True)
        return
    previous_hash = hashlib.sha256((output / "behavior.json").read_bytes()).hexdigest()
    save(output / "behavior.before_tool_scoring.json", rows)
    save(output / "summary.before_tool_scoring.json", json.loads((output / "summary.json").read_text(encoding="utf-8")))
    questions = yaml.safe_load((dataset / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    score_tool_answers(rows, questions, api_key, base_url, run["judge_model"])
    save(output / "behavior.json", rows)
    answers = json.loads((output / "answers.json").read_text(encoding="utf-8"))
    retrieval = json.loads((output / "retrieval.json").read_text(encoding="utf-8"))
    calls = json.loads((output / "api_calls.json").read_text(encoding="utf-8"))
    calls.extend(c for row in rows for c in row.get("judge_calls", []))
    save(output / "api_calls.json", calls)
    summary = summarize(retrieval, answers, rows, run["selected_mode"])
    save(output / "summary.json", summary)
    run["tool_scoring_postprocess"] = {"completed_at": datetime.now(timezone.utc).isoformat(),
        "previous_behavior_sha256": previous_hash,
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "primary_model_requests": 0, "judge_requests": sum(len(r.get("judge_calls", [])) for r in rows)}
    run["api_errors"] = sum(c["status"] != "ok" for c in calls)
    run["tool_scoring_errors"] = sum(bool(r.get("tool_scoring_error")) for r in rows)
    save(output / "run_manifest.json", run)
    write_report(output, run, summary, answers, rows, calls)
    print(json.dumps({"output": str(output), "tool_scoring": "completed", "test_behavior": summary["splits"]["test"]["behavior"]}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("eval/datasets/v1"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chat-model", default="qwen3.8-flash")
    parser.add_argument("--judge-model", default="deepseek-v4-flash-0731")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--resume", action="store_true", help="只续跑该目录尚未保存的用例，保留已有结果")
    parser.add_argument("--complete-tool-scoring", action="store_true", help="对已完成实验补齐工具回答评分，不重发主模型请求")
    args = parser.parse_args()
    if args.output.exists() and not args.resume and not args.complete_tool_scoring:
        raise ValueError("输出目录已存在，请使用新目录保存实验，避免覆盖证据")
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if not api_key or not base_url:
        raise ValueError("先在进程环境配置模型服务密钥和地址")
    if args.complete_tool_scoring:
        return complete_saved_tool_scoring(args.output, args.dataset, api_key, base_url)
    os.environ.update(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY=api_key, OPENAI_BASE_URL=base_url,
        EMBEDDING_PROVIDER="openai", EMBEDDING_MODEL="qwen3.7-text-embedding-flash", EMBEDDING_DIM="1024",
        LLM_PROVIDER="openai", CHAT_MODEL=args.chat_model, MODEL_STRICT="true", QDRANT_MODE="memory",
        MILVUS_MODE="memory", RERANK_ENABLED="false", REDIS_URL="redis://127.0.0.1:1/0",
        LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    manifest = json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))
    for file, expected_hash in manifest["artifact_sha256"].items():
        if hashlib.sha256((args.dataset / file).read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"数据集冻结摘要不匹配：{file}")
    questions = yaml.safe_load((args.dataset / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    if args.smoke:
        types = ("retrieval", "abstain", "tool", "workflow")
        questions = [next(q for q in questions if q["split"] == "dev" and q["task_type"] == kind) for kind in types]
    from fastapi.testclient import TestClient

    from app.container import get_container
    from app.db.base import SessionLocal
    from app.db.models import Document
    from app.main import app
    from app.rag.embeddings import OpenAIEmbedding
    from app.services.kb_service import ingest_document
    from app.services.model_api import RerankAPI

    is_ops = manifest["project"] == "ops"
    code_files = list(Path("app").rglob("*.py")) + list(Path("eval").glob("*.py"))
    run = {"started_at": datetime.now(timezone.utc).isoformat(), "dataset_id": manifest["dataset_id"],
        "dataset_manifest_sha256": hashlib.sha256((args.dataset / "manifest.json").read_bytes()).hexdigest(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files},
        "chat_model": args.chat_model, "judge_model": args.judge_model,
        "embedding_model": "qwen3.7-text-embedding-flash", "embedding_dim": 1024,
        "rerank_model": "qwen3.7-text-rerank", "candidate_chunks": 8, "context_documents": 5,
        "vector_backend": "Qdrant SDK embedded memory" if is_ops else "in-process exact cosine; Milvus service not exercised",
        "database": "isolated in-memory SQLite", "query_vectors": "one fresh request per question, then identical cached vectors across modes",
        "selection": "dev judge answer correctness, then dev Recall@5/MRR, then simpler mode; test does not tune configuration",
        "generation_scope": "RAG component on retrieval/abstain questions; actual application graph and approval HTTP API on behavior questions",
        "tools": "project simulation tools, not live business/monitoring systems", "smoke": args.smoke,
        "workers": args.workers, "question_counts": dict(Counter(q["split"] for q in questions)),
        "completion_status": "running", "credential_saved": False}
    if args.resume:
        previous = json.loads((args.output / "run_manifest.json").read_text(encoding="utf-8"))
        for field in ("dataset_manifest_sha256", "chat_model", "judge_model", "embedding_model", "embedding_dim", "smoke"):
            if previous[field] != run[field]:
                raise ValueError(f"续跑配置发生变化：{field}")
        save(args.output / f"run_manifest.before_resume_{uuid.uuid4().hex[:8]}.json", previous)
        run["resume_history"] = previous.get("resume_history", []) + [{"previous_started_at": previous["started_at"],
            "previous_git_head": previous["git_head"], "previous_code_sha256": previous["code_sha256"],
            "reason": "本地快照完整；修复 Windows 临时写入/替换失败，续跑未保存用例；计分、模型、题库与参数不变"}]
        run["usage_coverage"] = "包含续跑后及已保存用例的 usage；首段中断前未保存的 Embedding/重排用量无法完整恢复，不是账户总耗用"
    save(args.output / "run_manifest.json", run)
    with TestClient(app) as client:
        container = get_container()
        provider = OpenAIEmbedding(api_key, base_url, "qwen3.7-text-embedding-flash", 1024)
        container.embedder = EvaluationEmbeddings(provider, args.output / "embeddings.json")
        container.retriever.embedder = container.embedder
        if is_ops:
            from app.rag.vector_store import QdrantVectorStore
            container.vector_store = QdrantVectorStore(mode="memory", collection="eval_v1", dim=1024)
            container.retriever.vector_store = container.vector_store
        with SessionLocal() as db:
            for doc in manifest["documents"]:
                content = (args.dataset / doc["file"]).read_text(encoding="utf-8")
                kwargs = {"source": doc["source"].get("url", doc["source"].get("path", "project"))} if is_ops else {"category": doc["scope"]}
                ingest_document(db, container, Document(id=doc["doc_id"], tenant_id="default", title=doc["title"], content=content, **kwargs))
        reranker = RerankAPI(api_key, base_url, "qwen3.7-text-rerank")
        call_path = args.output / "rerank_calls.json"
        if call_path.exists():
            reranker.calls = json.loads(call_path.read_text(encoding="utf-8"))
        retrieved = retrieval_rows(container, questions, reranker, args.output)
        lookup = {(r["id"], r["mode"]): r for r in retrieved}
        answer_path = args.output / "answers.json"
        answers = json.loads(answer_path.read_text(encoding="utf-8")) if answer_path.exists() else []
        completed_answers = {(r["id"], r["mode"]) for r in answers}
        selected = None
        for split in ("dev", "test"):
            modes = MODES if split == "dev" else (selected,)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                jobs = [pool.submit(generate_and_judge, q, lookup[q["id"], mode], container.settings, is_ops, args.judge_model)
                        for q in questions if q["split"] == split and q["task_type"] in ("retrieval", "abstain")
                        for mode in modes if (q["id"], mode) not in completed_answers]
                for job in as_completed(jobs):
                    answers.append(job.result())
                    save(args.output / "answers.json", sorted(answers, key=lambda r: (r["id"], r["mode"])))
                    if len(answers) % 10 == 0:
                        print(f"{split} 回答与裁判：累计 {len(answers)}", flush=True)
            if split == "dev":
                selected = select_on_dev(retrieved, answers)
                save(args.output / "selected_on_dev.json", {"mode": selected, "frozen_before_test_answers": True,
                    "criterion": "development judge answer correctness, retrieval metrics, simpler mode"})
                print(f"开发集方案冻结：{selected}", flush=True)
        behaviors = behavior_rows(client, container, questions, is_ops, args.output)
        score_tool_answers(behaviors, questions, api_key, base_url, args.judge_model)
        save(args.output / "behavior.json", behaviors)
        summary = summarize(retrieved, answers, behaviors, selected)
        save(args.output / "summary.json", summary)
        calls = provider.calls + reranker.calls
        for row in answers:
            calls.extend(row["model_calls"] + row["judge_calls"])
        for row in behaviors:
            calls.extend(row["model_calls"] + row.get("judge_calls", []))
        save(args.output / "api_calls.json", calls)
        run.update(completion_status="completed", completed_at=datetime.now(timezone.utc).isoformat(),
                   selected_mode=selected, api_errors=sum(c["status"] != "ok" for c in calls),
                   retrieval_errors=sum(r["status"] != "ok" for r in retrieved),
                   answer_errors=sum(r["status"] != "ok" for r in answers), behavior_errors=sum(r["status"] != "ok" for r in behaviors))
        save(args.output / "run_manifest.json", run)
        write_report(args.output, run, summary, answers, behaviors, calls)
        print(json.dumps({"output": str(args.output), "selected": selected, "test": summary["splits"]["test"], "errors": run["api_errors"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
