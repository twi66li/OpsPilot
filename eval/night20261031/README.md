# night20261031 真实评测准备材料（T21）

新命名空间冻结草案：不改旧 v1/v2 数据、旧报告、简历，不触碰封存的门控代码路径（基线 `1b0b3b2`）。全部题标 `human_reviewed=false`（自动构造，未经人工复核），人工复核另立任务。

| 文件 | 内容 |
|---|---|
| `scenarios.json` | 40 场景（dev10/test30），8 个互斥家族：manual_only/history_only/metrics_window/metrics_missing/missing_knowledge/mixed_triage/abstain_irrelevant/tool_constraint；含金标要点、工具期望、服务/窗口参数 |
| `rubric.json` | 裁判 rubric 草案（三判定+服务名/时间窗/工具约束规则+数值容差+双裁判协议） |
| `config.json` | 冻结配置草案：模型、生成参数、检索/集合、agent_modes、seed=20261031 |
| `corpus_manifest.json` | 知识库语料清单：只读复用 `scripts/knowledge` 12 篇原文（路径+sha256），不复制不改写 |
| `failure_template.json` | 逐题失败/缺判/账本定位记录模板与填写规则 |
| `cost_estimate.json` | 成本测算：budget.json p1 列逐模型，典型口径与字节级保守上界两列（保守列为授权规划依据），含可行边界判定 |
| `execution_plan.md` | S0—S7 执行步骤、逐项前置条件与阻塞清单（明确本夜业务真实 API 不可行） |
| `manifest.json` | 本目录全部材料的 sha256 冻结清单 |

## 关键判定

- 本夜不可执行真实评测（T18 门控封存 + 真实 API 未放行）；材料供后续夜与终审包使用。
- 字节级保守上界口径下，全量 40题×2模式 正式批次超授权；可行路径为 S0 探针实测比值后重判，或先跑 dev10×2 模式。
- 22 题依赖 S2 回放快照（history/metrics 家族）；该步不可行时如实缩减并标注分母。
