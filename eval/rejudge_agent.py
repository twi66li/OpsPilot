"""仅修正裁判证据输入后重判同一批答案；首轮判分/答案/冻结金标保持原样。"""
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from eval.agent_eval import DATASET, judge_row, load, save, sha, summarize, validate_dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, help="更换裁判时使用新的输出目录，保留原批次")
    parser.add_argument("--judges", nargs=2, default=["qwen3.8-max", "qwen3.8-27b"])
    args = parser.parse_args()
    source, report = args.report, args.output or args.report
    cases, answers = validate_dataset(), load(source / "answers.json")
    config, rubric = load(DATASET / "config.json"), load(DATASET / "rubric.json")
    config["judges"] = args.judges
    path = report / "judgments_corrected.json"
    judgments = load(path) if path.exists() else []
    if any(j["judge"] not in config["judges"] for j in judgments):
        raise ValueError("裁判模型已变更，请用--output指定新目录，不能混合或覆盖旧成绩")
    if report == source and set(config["judges"]) != set(load(source / "run_manifest.json")["config"]["judges"]):
        raise ValueError("裁判模型已变更，请用--output指定新目录")
    settings = Settings(_env_file=".env")
    revision = {"at": datetime.now(timezone.utc).isoformat(),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "reason": "使用指定裁判和模型实际取得的编号证据重判；原答案与rubric保持不变",
                "changes": "只重判原批次答案；答案/rubric/金标不变；当前裁判写入独立运行记录，不覆盖原批次",
                "source_report": str(source.resolve()), "judges": config["judges"],
                "answers_sha256": sha(source / "answers.json"), "rubric_sha256": sha(DATASET / "rubric.json"),
                "initial_judgments_sha256": sha(source / "judgments.json"), "initial_summary_sha256": sha(source / "summary.json")}
    save(report / "evaluation_revision.json", revision)
    done = {(j["id"], j["mode"], j["judge"]) for j in judgments}
    lookup = {c["id"]: c for c in cases}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(judge_row, lookup[r["id"]], r, rubric, settings, model) for r in answers for model in config["judges"]
                if (r["id"], r["mode"], model) not in done]
        for job in as_completed(jobs):
            row = job.result()
            judgments.append(row)
            save(path, sorted(judgments, key=lambda j: (j["id"], j["mode"], j["judge"])))
            print(f"证据修正重判 {row['id']} {row['mode']} {row['judge']} {row['status']}", flush=True)
    assert revision["answers_sha256"] == sha(source / "answers.json")
    assert revision["rubric_sha256"] == sha(DATASET / "rubric.json")
    save(report / "summary_corrected.json", summarize(cases, answers, judgments, config["judges"]))
    print("证据修正重判完成；原回答/首轮判分/rubric未改。", flush=True)


if __name__ == "__main__":
    main()
