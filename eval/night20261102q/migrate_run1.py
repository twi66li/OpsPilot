"""heldout run#1 → run#2 迁移：快照 run#1、清留出记录、登记迭代说明。"""
import json
import shutil

p = r"eval\night20261102q\quality_run\results.json"
shutil.copy(p, r"eval\night20261102q\quality_run\run1_snapshot.json")
d = json.load(open(p, encoding="utf-8"))
d.setdefault("iterations", []).append({
    "at": "heldout-run-1 后",
    "action": ("留出 run#1 复盘：heldout-q1（纯路径）答案暴露确定性模板两处缺陷——"
               "消息原文截断至100字符（逐字分组口径被破坏）、分组清单只列 top3 未含全部并列与行号；"
               "双裁判一致判 factual=False（缺陷被评测如实捕获）。修复模板后 run#2 重跑："
               "纯路径题零成本重渲染；混合题（q3/q5/q7）保留 run#1 结果不重跑（其答案与模板无关）；"
               "q1 的旧判分随之作废并重判。run#2 的纯路径题已在 run#1 出现过失败模式，"
               "不宣称未见泛化；run#1 全量快照存 run1_snapshot.json"),
})
for k in list(d["questions"]):
    if k.startswith("heldout"):
        d["questions"].pop(k)
for k in list(d["judges"]):
    if k.startswith("heldout"):
        d["judges"].pop(k)
d["not_run"] = {k: v for k, v in d.get("not_run", {}).items() if not k.startswith("heldout")}
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("run1 snapshotted; heldout records cleared for run2")
