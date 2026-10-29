# 简历项目描述（可直接使用）

## 版本一：完整版（校招/社招通用）

**AIOps 智能故障诊断 Agent**（个人项目｜Python / FastAPI / LangGraph / MySQL / Qdrant / Redis / Docker）

- 基于 LangGraph 编排 AIOps 故障诊断工作流：意图路由 → 混合检索 → 相关性判断 → 查询改写补充检索 → 工具调用（服务指标 / 历史故障）→ 带引用生成回答；工单创建接入 **LangGraph interrupt/Command 人工审批**（human-in-the-loop），审批单落库可追溯。
- 构建**混合检索链路**：Qdrant 向量检索 + BM25（jieba，中英混合分词，保证错误码/服务名整体命中）双路召回，RRF 融合，可插拔重排序（启发式 / Cross-Encoder）；实现固定问题集离线评测，对比 dense/bm25/hybrid 三种方案的 Recall@5、MRR、忠实度与 P95 耗时，并输出自动对比报告。
- 落地**多租户数据与权限体系**：MySQL 管理会话/文档元数据/故障记录/工单，JWT 认证 + admin/engineer/viewer 角色控制；业务查询与 Qdrant 向量检索双重 `tenant_id` 过滤，跨租户访问完全隔离（pytest 覆盖）。
- 服务化交付：FastAPI 提供会话/知识库/Agent/工单接口，**SSE 流式**推送节点状态、检索命中、工具事件与回答；Redis 承担缓存与接口限流；Docker Compose 编排 MySQL+Redis+Qdrant+API 一键部署；接入 Langfuse 记录检索/重排/工具调用/Token 用量，定位失败环节；43 个 pytest 用例 + GitHub Actions（ruff+pytest）回归。

## 版本二：精简版（三行）

**AIOps 智能故障诊断 Agent**（Python/FastAPI/LangGraph/Qdrant/MySQL/Redis/Docker）
- LangGraph 诊断工作流（补充检索 + 工具调用 + 工单人工审批 interrupt/resume），Qdrant+BM25 混合检索经 RRF 融合与重排序，固定问题集评测 Recall@5/MRR/忠实度并输出方案对比报告。
- FastAPI + SSE 流式返回节点/工具/回答事件，JWT 多租户隔离（业务表 + 向量过滤），MySQL 存元数据、Qdrant 存向量、Redis 限流缓存，Docker Compose 一键部署，pytest 43 例 + GitHub Actions 回归，Langfuse 全链路追踪。

## 面试要点提示（自述素材）

1. **为什么 hybrid 优于单路**：错误码/服务名是强标识（BM25/词面必中），自然语言描述需要语义（向量）；RRF 无需调分数量纲即可融合两路排名。实测见 `eval/reports/eval_report.md`。
2. **人工审批如何实现**：LangGraph `interrupt()` 暂停整图 + `MemorySaver` 保存断点，审批单先落库保证幂等（恢复重放不会重复建审批），`Command(resume)` 携带审批决定恢复图执行；服务重启后审批单仍可查（表持久化），但内存断点丢失会返回 409，生产应换持久化 Checkpointer。
3. **小语料 BM25 的坑**：rank-bm25 在极小语料下 idf 为负，未命中文档反而得 0 分排到命中文档前面——实现里先做词面预过滤再按 BM25 排序（测试覆盖）。
4. **降级可验收**：Qdrant/Redis/LLM 任一不可用都自动退化（进程内向量库/缓存/规则式回答），保证 CI 与离线环境零依赖跑通；生产配置下自动走完整链路。
