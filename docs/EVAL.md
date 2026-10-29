# 效果评测

## 1. 评测目标

对固定问题集（`eval/questions.yaml`，12 题）回答三个问题：

1. **检索质量**：期望文档是否被检索到？（Recall@K、MRR）
2. **回答质量**：回答是否覆盖关键信息、是否基于检索上下文？（关键词覆盖率、忠实度）
3. **性能**：检索 + 生成的端到端耗时（平均 / P95）；以及不同检索方案的横向对比（dense / bm25 / hybrid）。

问题集覆盖五类：错误码（ERR_5003 等）、服务名（order-service）、自然语言描述、无关问题（应拒答）、工单审批端到端流程。

## 2. 运行

```bash
python -m scripts.seed          # 首次先灌数据
python -m eval.run_eval         # 默认对比 dense/bm25/hybrid，Top-K=5
python -m eval.run_eval --modes hybrid --rerank heuristic   # 只测 hybrid
python -m eval.run_eval --rerank none                       # 关闭重排对照
```

输出：

- `eval/reports/eval_report.md` — 对比表格 + 工单工作流端到端结果（可直接贴进报告/简历）
- `eval/reports/eval_report.json` — 全量明细（每题命中与耗时）

## 3. 指标定义

| 指标 | 定义 |
| --- | --- |
| Recall@K | 期望文档命中 Top-K 的比例；无期望文档的问题记 1.0 |
| MRR | 首个期望文档排名倒数的均值 |
| 关键词覆盖率 | 回答命中期望关键词的比例（忠实度代理指标） |
| 忠实度（启发式） | 回答句子中，与检索上下文词面重合≥50% 的句子占比；生产配置真实 LLM 后可切换 LLM-as-judge |
| 延迟 | 检索 + 回答生成总耗时，avg / P95 |
| 工作流 | 工单流程是否触发 interrupt → 审批 → 工单落库 → 审批单闭环，及端到端耗时 |

## 4. 基线结果（Mock 模式，供验收对照）

`mock` 模式下 Embedding 为词面哈希（语义≈词面重合），错误码/服务名类问题几乎必中，因此 Recall 偏高属预期；MRR 与忠实度仍有区分度：

| 方案 | Recall@5 | MRR | 关键词覆盖 | 忠实度 | 平均耗时 |
| --- | --- | --- | --- | --- | --- |
| dense | 1.0 | 1.0 | 0.60 | 0.56 | 3.4ms |
| bm25 | 1.0 | 0.95 | 0.65 | 0.57 | 1.8ms |
| hybrid | 1.0 | 1.0 | 0.65 | 0.56 | 4.0ms |

真实语料 + 真实 Embedding 下，`hybrid` 对“自然语言描述 + 专业标识（错误码/服务名）”混合查询的优势会明显放大——这正是本问题集的设计意图（如“支付服务对账失败 ERR_7010 是什么原因？”同时包含口语描述与错误码）。

## 5. 用 Langfuse 定位失败环节

配置 `.env` 中 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST` 后，每次诊断会在 Langfuse 中生成 trace，包含：

- `retrieval` span：模式、租户、命中数、耗时 → 判断“检索没召回”还是“召回没排上”；
- `rewrite_query` span：补充检索的改写结果 → 判断改写是否引入噪声；
- `tools` span：工具调用清单与耗时 → 判断工具链路是否拖慢响应；
- `generate` span：回答长度与 Token 用量 → 定位生成阶段成本。

结合 `retrieval_logs` 表（MySQL）可离线统计每种模式的命中率分布，定位失败环节。
