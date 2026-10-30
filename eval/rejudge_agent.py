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
    args = parser.parse_args()
    report = args.report
    cases, answers = validate_dataset(), load(report / "answers.json")
    config, rubric = load(DATASET / "config.json"), load(DATASET / "rubric.json")
    settings = Settings(_env_file=".env")
    revision = {"at": datetime.now(timezone.utc).isoformat(),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "reason": "首轮裁判只见到适配前的回放后端输出，丢失native citation_id和规则链实际编号上下文；改为模型实际取得证据",
                "changes": "只改裁判证据输入；同48份答案/原rubric/模型/金标不变；保留首轮全部判分，最终使用corrected文件",
                "answers_sha256": sha(report / "answers.json"), "rubric_sha256": sha(DATASET / "rubric.json"),
                "initial_judgments_sha256": sha(report / "judgments.json"), "initial_summary_sha256": sha(report / "summary.json")}
    save(report / "evaluation_revision.json", revision)
    path = report / "judgments_corrected.json"
    judgments = load(path) if path.exists() else []
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
    assert revision["answers_sha256"] == sha(report / "answers.json")
    assert revision["rubric_sha256"] == sha(DATASET / "rubric.json")
    save(report / "summary_corrected.json", summarize(cases, answers, judgments, config["judges"]))
    print("证据修正重判完成；原回答/首轮判分/rubric未改。", flush=True)


if __name__ == "__main__":
    main()
