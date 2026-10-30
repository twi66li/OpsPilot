"""冻结回答的双裁判敏感性分析，不生成回答、不重新检索。"""
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from openai import APIConnectionError

from app.services.budget import BudgetExceededError, BudgetLedgerError
from app.services.model_api import ChatAPI
from eval.real_eval import error_info, save

FIELDS = ("answer_correct", "grounded", "abstained")


def clean(value):
    text = json.dumps(value, ensure_ascii=False)
    for name in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        if os.getenv(name):
            text = text.replace(os.environ[name], "[REDACTED]")
    return json.loads(re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text))


def hashes(root, folders):
    return {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in folders for path in sorted(folder.rglob("*")) if path.is_file()}


def legacy_rubric(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "generate_and_judge")
    return next(ast.literal_eval(n.args[0]) for n in ast.walk(function)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "chat"
                and n.args and isinstance(n.args[0], ast.Constant))


def judge_payload(question, answer, retrieval):
    # 字段、顺序、内容均与原 generate_and_judge 相同，不传旧判定或模型名称。
    return {"question": question["question"], "reference_answer": question["reference_answer"],
            "required_points": question["answer_points"], "expected_abstention": question["should_abstain"],
            "retrieved_context": retrieval["hits"], "answer": answer["answer"]}


def retryable(exc):
    return isinstance(exc, (APIConnectionError, httpx.TransportError)) or getattr(exc, "status_code", None) == 429


class Audit:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def append(self, event):
        event = clean({"time": datetime.now(timezone.utc).isoformat(), **event})
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def recovered(self):
        """终态复用；请求已发而终态未保存时记未知，不自动再判以免挑结果。"""
        events = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()] if self.path.exists() else []
        started = {e["id"] for e in events if e["event"] == "started"}
        finished = {e["record"]["id"]: e["record"] for e in events if e["event"] == "completed"}
        return finished, started - finished.keys()


def score_one(question, answer, retrieval, rubric, api, audit, max_retries=1):
    """初次失败最多额外重试 1 次（api_gate：全层重试合计初次+1）。"""
    record = {"id": answer["id"], "task_type": answer["task_type"], "mode": answer["mode"],
              "old": answer["judgment"], "new": None, "status": "error", "calls": [], "attempt_errors": []}
    payload = json.dumps(judge_payload(question, answer, retrieval), ensure_ascii=False)
    record["input_sha256"] = hashlib.sha256((rubric + "\n" + payload).encode()).hexdigest()
    try:
        for attempt in range(max_retries + 1):
            audit.append({"event": "started", "id": answer["id"], "attempt": attempt + 1,
                          "input_sha256": record["input_sha256"]})
            try:
                raw = api.chat(rubric, payload, json_mode=True)
                record["raw_response"] = raw
                judgment = json.loads(raw)
                if not all(type(judgment.get(key)) is bool for key in FIELDS) or not isinstance(judgment.get("reason"), str):
                    raise ValueError("裁判未返回三个布尔值及理由")
                record.update(status="ok", new=judgment)
                break
            except (BudgetExceededError, BudgetLedgerError):
                raise  # 预算门控/账本失败不重试、不吞掉
            except Exception as exc:
                record["attempt_errors"].append({"attempt": attempt + 1, **error_info(exc)})
                audit.append({"event": "attempt_failed", "id": answer["id"], "attempt": attempt + 1,
                              "error": error_info(exc), "call": api.calls[-1] if api.calls else {}})
                if not retryable(exc) or attempt == max_retries:
                    record["error"] = error_info(exc)
                    break
                time.sleep(.5 * 2 ** attempt)
    finally:
        record["calls"] = api.calls
        record["retries"] = max(0, len(api.calls) - 1)
        api.client.close()
    record = clean(record)
    audit.append({"event": "completed", "record": record})
    return record


def compare(rows):
    valid = [r for r in rows if r["status"] == "ok"]
    dimensions = {}
    for field in FIELDS:
        table = {f"old_{str(a).lower()}_new_{str(b).lower()}": 0 for a in (True, False) for b in (True, False)}
        disagreements = []
        for row in valid:
            old, new = row["old"][field], row["new"][field]
            table[f"old_{str(old).lower()}_new_{str(new).lower()}"] += 1
            if old != new:
                disagreements.append(row["id"])
        count = len(valid) - len(disagreements)
        dimensions[field] = {"agree": count, "comparable": len(valid),
                             "rate": count / len(valid) if valid else None,
                             "disagreements": disagreements, "decision_table": table}
    all_agree = sum(all(row["old"][key] == row["new"][key] for key in FIELDS) for row in valid)
    agreement = {"expected": len(rows), "comparable": len(valid), "errors": len(rows) - len(valid),
                 "coverage": len(valid) / len(rows) if rows else None,
                 "all_three": {"agree": all_agree, "rate": all_agree / len(valid) if valid else None,
                               "over_all_expected": all_agree / len(rows) if rows else None},
                 "dimensions": dimensions, "unresolved": [r["id"] for r in rows if r["status"] != "ok"]}
    summary = {}
    for name, kind, field in (("answer_correctness", "retrieval", "answer_correct"),
                              ("groundedness", "retrieval", "grounded"),
                              ("correct_abstention", "abstain", "abstained")):
        sample = [r for r in rows if r["task_type"] == kind]
        def passed(row, side):
            judgment = row.get(side)
            return bool(judgment and judgment[field] and (kind != "abstain" or judgment["answer_correct"]))
        old, new = (sum(passed(r, side) for r in sample) for side in ("old", "new"))
        n = len(sample)
        summary[name] = {"n": n, "old_pass": old, "new_pass": new,
                         "old_rate": old / n if n else None, "new_rate": new / n if n else None,
                         "difference_pp": (new - old) * 100 / n if n else None,
                         "unresolved": sum(r["status"] != "ok" for r in sample)}
    for row in rows:
        row["agreement"] = ({key: row["old"][key] == row["new"][key] for key in FIELDS}
                            if row["status"] == "ok" else None)
        row["all_agree"] = all(row["agreement"].values()) if row["agreement"] else None
    return agreement, summary


def pct(value):
    return "不可计算" if value is None else f"{value:.2%}"


def render_report(manifest, rows, agreement, summary, usage):
    lines = ["# 双裁判敏感性分析", "",
             f"- 原裁判：{manifest['old_judge']}；第二裁判：{manifest['new_judge']}。",
             f"- 同一冻结测试答案：{len(rows)} 条；有效评分：{agreement['comparable']}；错误/未知：{agreement['errors']}。",
             "- 原题目、参考答案、必要要点、检索上下文及回答只读；原 rubric 原文复用，温度 0，单次有效判分。",
             "- 不重跑生成/检索，不按新分数重选方案；仅连接/传输错误及 HTTP 429 最多重试两次，全部留痕。",
             f"- 原始输入目录与数据 SHA256 前后校验：{manifest['frozen_unchanged']}。", "",
             "## 分数对照", "", "| 指标 | 旧裁判 | 新裁判 | 差异（百分点） | 未完成 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for name, item in summary.items():
        lines.append(f"| {name} | {item['old_pass']}/{item['n']}（{pct(item['old_rate'])}） | "
                     f"{item['new_pass']}/{item['n']}（{pct(item['new_rate'])}） | {item['difference_pp']:+.2f} | {item['unresolved']} |")
    lines += ["", "## 逐题一致率", "",
              f"- 三项同时一致：{agreement['all_three']['agree']}/{agreement['comparable']}，{pct(agreement['all_three']['rate'])}。",
              f"- 有效配对覆盖率：{pct(agreement['coverage'])}；有失败时同时报告覆盖率，不把未完成样本静默剔除。",
              "", "| 维度 | 一致数/有效配对 | 一致率 | 不一致题号 |", "| --- | ---: | ---: | --- |"]
    for field, item in agreement["dimensions"].items():
        lines.append(f"| {field} | {item['agree']}/{item['comparable']} | {pct(item['rate'])} | {', '.join(item['disagreements']) or '无'} |")
    lines += ["", "## 结论与范围", "",
              "该对照衡量分数对裁判选择的敏感性；分数变化来自裁判变化，不能表述为被测系统能力提升。",
              "Qwen生成模型与Qwen裁判可能存在同系列偏好；当前实验无法排除，也不能据此认定哪个裁判更准确。",
              "题库 human_reviewed=false，本任务不含人工标注或人工金标；一致率高也不等于人工准确率高。",
              "裁判偏差研究参考：[Judging LLM-as-a-Judge](https://arxiv.org/abs/2306.05685)。",
              "错误用例保留在分母中，新分数包含未完成题时仅为保守下界，须结合有效评分覆盖率阅读。",
              "", "## 用量与失败", "", "~~~json", json.dumps(usage, ensure_ascii=False, indent=2), "~~~",
              "", "所有尝试见 audit.jsonl；per_question.json 保留原始新回复、三项判定、双方理由及错误；未完成请求不自动重判。",
              "", "## 逐题对照", "",
              "判定顺序：正确 / 证据支持 / 明确拒答；理由为摘要，完整理由保留在 JSON。",
              "", "| 题号 | 类型 | 旧判定 | 新判定 | 三项一致 | 旧理由摘要 | 新理由摘要 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    def label(judgment):
        return "/".join("是" if judgment[key] else "否" for key in FIELDS) if judgment else "未完成"
    def brief(value):
        return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")[:220]
    for row in rows:
        reason = (row.get("new") or {}).get("reason", row.get("error", {}))
        lines.append(f"| {row['id']} | {row['task_type']} | {label(row['old'])} | {label(row.get('new'))} | "
                     f"{'是' if row['all_agree'] else '否' if row['all_agree'] is False else '未知'} | "
                     f"{brief(row['old'].get('reason', ''))} | {brief(reason)} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--judge-model", default="qwen3.8-27b")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source, output = args.source.resolve(), args.output.resolve()
    dataset = root / "eval/datasets/v1"
    if output.is_relative_to(source) or output.is_relative_to(dataset):
        raise ValueError("输出不可写入冻结输入目录")
    before = hashes(root, [dataset, source])
    source_manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    mode = source_manifest["selected_mode"]
    answers = [r for r in json.loads((source / "answers.json").read_text(encoding="utf-8"))
               if r["split"] == "test" and r["mode"] == mode]
    if len(answers) != 72 or len({r["id"] for r in answers}) != 72 or any(r["status"] != "ok" for r in answers):
        raise ValueError("冻结输入必须是 72 个唯一、已完成的测试答案")
    retrieval = {r["id"]: r for r in json.loads((source / "retrieval.json").read_text(encoding="utf-8"))
                 if r["split"] == "test" and r["mode"] == mode}
    questions = {q["id"]: q for q in yaml.safe_load((dataset / "questions.yaml").read_text(encoding="utf-8"))["questions"]}
    rubric = legacy_rubric(root / "eval/real_eval.py")
    key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
    base = os.getenv("DASHSCOPE_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if not key or not base:
        raise ValueError("必须通过进程环境提供真实模型密钥和地址，不允许 Mock")
    output.mkdir(parents=True, exist_ok=args.resume)
    manifest_path = output / "run_manifest.json"
    if args.resume:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["frozen_before"] != before or manifest["new_judge"] != args.judge_model or manifest["rubric"] != rubric:
            raise ValueError("续跑输入、裁判或规则与首次运行不同")
    else:
        manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "source": str(source), "selected_mode": mode,
                    "old_judge": source_manifest["judge_model"], "new_judge": args.judge_model,
                    "expected": len(answers), "rubric": rubric, "frozen_before": before,
                    "temperature": 0, "max_tokens": 1536, "enable_thinking": False, "max_transport_retries": 2,
                    "credential_saved": False, "old_judgments_in_prompt": False, "human_reviewed": False,
                    "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "baseline_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()}
    manifest["completion_status"] = "running"
    save(manifest_path, manifest)
    audit = Audit(output / "audit.jsonl")
    records, unknown = audit.recovered()
    for answer in answers:
        if answer["id"] in unknown:
            records[answer["id"]] = {"id": answer["id"], "task_type": answer["task_type"], "mode": mode,
                                    "old": answer["judgment"], "new": None, "status": "unknown",
                                    "calls": [], "retries": 0,
                                    "error": {"type": "UnresolvedRequest", "message": "上轮已发请求但未保存终态，禁止自动重判"}}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(score_one, questions[a["id"]], a, retrieval[a["id"]], rubric,
                            ChatAPI(key, base, args.judge_model), audit) for a in answers if a["id"] not in records]
        for future in as_completed(jobs):
            row = future.result()
            records[row["id"]] = row
            save(output / "per_question.json", sorted(records.values(), key=lambda r: r["id"]))
            if len(records) % 12 == 0:
                print(f"第二裁判已记录 {len(records)}/{len(answers)}", flush=True)
    rows = sorted(records.values(), key=lambda r: r["id"])
    agreement, summary = compare(rows)
    calls = [call for row in rows for call in row["calls"]]
    usage = {"model": args.judge_model, "requests": len(calls), "retries": sum(r["retries"] for r in rows),
             "errors": sum(c["status"] != "ok" for c in calls),
             "prompt_tokens": sum(c.get("usage", {}).get("prompt_tokens", 0) for c in calls),
             "completion_tokens": sum(c.get("usage", {}).get("completion_tokens", 0) for c in calls),
             "total_tokens": sum(c.get("usage", {}).get("total_tokens", 0) for c in calls),
             "unresolved_requests": sorted(unknown)}
    after = hashes(root, [dataset, source])
    manifest.update(frozen_after=after, frozen_unchanged=before == after,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    completion_status="completed" if not agreement["errors"] else "completed_with_errors")
    if before != after:
        raise ValueError("冻结数据发生变化，停止交付")
    for name, value in (("per_question", rows), ("agreement", agreement), ("summary", summary), ("usage", usage), ("run_manifest", manifest)):
        save(output / f"{name}.json", clean(value))
    (output / "report.md").write_text(render_report(manifest, rows, agreement, summary, usage), encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": summary, "agreement": agreement["all_three"],
                      "usage": usage}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
