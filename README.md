# AIOps 智能故障诊断 Agent（aiops-diagnosis-agent）

面向运维（AIOps）场景的 RAG + Agent 诊断系统：**混合检索（Qdrant 向量 + BM25 关键词 + RRF 融合 + 重排序）× LangGraph 诊断工作流（含补充检索、工具调用、工单人工审批）× 多租户 JWT 权限 × SSE 流式 × 全链路评测**。

技术栈：Python 3.11+ / FastAPI / LangGraph / MySQL（可退化 SQLite） / Qdrant / Redis / Docker Compose / pytest / GitHub Actions / Langfuse（可选）。

> **零依赖即可跑通**：默认 `mock` 模式（进程内向量库 + 哈希 Embedding + 规则式 LLM），无需任何外部服务与 API Key；配置真实 LLM/Qdrant/MySQL 后无缝切换生产模式。

---

## 1. 功能总览

| 模块 | 说明 |
| --- | --- |
| 混合检索 | Qdrant 向量检索 + BM25（jieba）关键词检索，RRF 融合，可插拔重排（启发式 / Cross-Encoder）；支持 `dense / bm25 / hybrid` 三种模式对比 |
| 诊断工作流 | LangGraph 编排：意图路由 → 检索 → 相关性判断 →（不足时）查询改写补充检索 → 工具调用（服务指标 / 故障史）→ 生成回答 → 工单人工审批（interrupt/resume） |
| 数据与权限 | MySQL 管理会话、文档元数据、故障记录、工单、审批单、检索日志；Qdrant 存文档向量；JWT 认证 + admin/engineer/viewer 三级角色；业务表与向量查询双重租户过滤 |
| 服务交付 | FastAPI 提供会话 / 知识库 / Agent / 工单 / 指标接口；SSE 流式返回节点状态、检索命中、工具事件、回答与审批请求；Docker Compose 一键部署；pytest + GitHub Actions 回归 |
| 效果评测 | 固定问题集（错误码 / 服务名 / 自然语言 / 无关 / 工单流程），Recall@K、MRR、关键词覆盖率、启发式忠实度、耗时 P95；Langfuse 记录检索 / 重排 / 工具 / Token 用量 |

诊断工作流图：

```
START → route_intent → retrieve → grade ──(不足且未超1轮)→ rewrite → retrieve
                                        └─(充足/超限)→ call_tools → generate
                                                            → approval_gate ─(创建工单)→ create_ticket → END
                                                                        └─(无需建单)→ END
```

- **approval_gate 使用 LangGraph `interrupt`**：需要建单时暂停整张图，生成审批单入库；审批人调用审批接口后以 `Command(resume)` 恢复，图继续执行创建/拒绝工单。

## 2. 快速开始（离线验收，无外部依赖）

```bash
cd aiops-diagnosis-agent
python -m venv .venv && .venv\Scripts\activate      # Windows；Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 1) 灌入种子数据（11 篇运维 SOP、8 条历史故障、4 个账号）
python -m scripts.seed

# 2) 启动服务（默认 SQLite + 进程内向量库 + MockLLM）
uvicorn app.main:app --reload

# 3) 另开终端跑端到端冒烟（登录 → SSE 诊断 → 工单审批）
#    Windows: set BASE_URL=http://127.0.0.1:8000 ；Linux/macOS: export BASE_URL=...
python scripts/smoke_test.py

# 4) 检索方案对比评测（dense / bm25 / hybrid）
python -m eval.run_eval            # 报告输出 eval/reports/eval_report.md

# 5) 全量回归测试（57 个用例）
pytest -q
```

内置账号（`scripts/seed.py`）：

| 账号 | 密码 | 角色 | 租户 |
| --- | --- | --- | --- |
| admin | admin123 | admin | default |
| dev01 | dev123456 | engineer | default |
| viewer01 | view123456 | viewer | default |
| acme_admin | acme123456 | admin | acme（用于验证租户隔离） |

## 3. Docker Compose 一键部署（MySQL + Redis + Qdrant + API）

```bash
cp .env.example .env      # 按需修改；配好 OPENAI_API_KEY / OPENAI_BASE_URL 即启用真实 LLM
docker compose up -d --build
# 启动后自动执行种子脚本；约 1 分钟后访问
curl http://localhost:8000/health
python scripts/smoke_test.py
```

- `api` 服务依赖 mysql（healthcheck）、redis（healthcheck）、qdrant；
- 数据库连接：`mysql+pymysql://aiops:aiops123@mysql:3306/aiops`；
- 接入真实 LLM：设置 `LLM_PROVIDER=openai`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`（兼容 DeepSeek / 智谱 / vLLM 等任意 OpenAI 兼容 API）；Embedding 同理 `EMBEDDING_PROVIDER=openai`（**注意**：更换 Embedding 提供方后需重新入库文档，且 `EMBEDDING_DIM` 要与模型输出维度一致）。

## 4. 常用 API 示例

```bash
# 登录
curl -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 入库文档（admin/engineer）
curl -X POST http://localhost:8000/api/kb/documents -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"MySQL 主从延迟处理","content":"# 主从延迟\n\nSeconds_Behind_Master 增大时……"}'

# 检索（可对比 mode=dense / bm25 / hybrid）
curl -X POST http://localhost:8000/api/kb/search -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"ERR_5003 连接池耗尽怎么处理","mode":"hybrid","top_k":5}'

# SSE 流式诊断（返回节点状态/检索命中/工具事件/回答/审批请求）
curl -N -X POST http://localhost:8000/api/agent/chat/stream -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"<session_id>","message":"order-service 大量超时，如何排查？"}'

# 工单人工审批（admin/engineer）
curl -X POST http://localhost:8000/api/agent/approvals/<approval_id>/decide \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"approved":true,"comment":"同意，尽快处理"}'
```

完整接口清单见 [docs/API.md](docs/API.md)；SSE 事件协议、架构图与设计取舍见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；评测方法见 [docs/EVAL.md](docs/EVAL.md)。

## 5. 项目结构

```
aiops-diagnosis-agent/
├── app/
│   ├── main.py               # FastAPI 入口（lifespan 启动时初始化租户/管理员/索引）
│   ├── config.py             # pydantic-settings 全局配置
│   ├── container.py          # 依赖容器（检索器/LLM/图/缓存 单例）
│   ├── db/                   # SQLAlchemy 模型与会话（MySQL/SQLite 双兼容）
│   ├── auth/                 # JWT 签发、角色依赖、认证路由
│   ├── api/                  # 知识库 / 会话 / Agent / 工单 / 管理路由 + Pydantic 模型
│   ├── rag/                  # chunking / embeddings / vector_store(Qdrant) / bm25 / fusion(RRF) / rerank / hybrid
│   ├── agent/                # LangGraph 状态、节点、图、LLM 抽象、工具集
│   └── services/             # 会话/知识库/工单/指标服务、Redis 缓存限流、Langfuse 追踪
├── eval/                     # 固定问题集 + 指标 + 评测脚本（生成 reports/）
├── scripts/                  # 种子数据 + 知识库文档 + 冒烟测试
├── tests/                    # 57 个 pytest 用例（认证/RAG/Agent/审批/SSE/缓存/评测）
├── docker-compose.yml        # mysql + redis + qdrant + api
├── Dockerfile
└── .github/workflows/ci.yml  # ruff + pytest
```

## 6. 关键设计说明

- **降级链路**：Qdrant 不可达 → 进程内向量库；Redis 不可达 → 进程内缓存；LLM 调用失败 → 规则式回答。所有外部依赖都不阻塞服务启动（生产部署自然使用完整链路）。
- **租户隔离**：所有业务查询强制 `tenant_id` 过滤；Qdrant 检索带 `Filter(key=tenant_id)`；BM25 按租户分桶建索引；跨租户访问文档/会话/工单一律 404。
- **进程内向量库说明**：默认 `QDRANT_MODE=memory` 下向量不跨进程存活，服务启动时通过 `kb_service.warm_indexes` 从 MySQL 全量回填（BM25 亦然）；使用 Qdrant 持久化时检测到已有数据则跳过回填。
- **Checkpointer**：LangGraph 使用 `MemorySaver`（单进程内存态）。多 worker 生产部署建议替换为 `langgraph-checkpoint-postgres`/Redis checkpointer（依赖已在 requirements-optional 提示），审批单本身已落库（`approval_requests` 表），服务重启后可查询历史审批。

## 7. 测试与 CI

```bash
pytest -q              # 57 passed
ruff check app scripts eval tests
```

GitHub Actions（`.github/workflows/ci.yml`）：Python 3.11 + `pip install` + `ruff check` + `pytest`，全部依赖 Mock，无需任何外部服务。

## 8. 参考的开源项目

本项目的检索与工作流设计调研并借鉴了以下开源实现：

- [IgnazioDS/langgraph-fastapi-starter](https://github.com/IgnazioDS/langgraph-fastapi-starter) — LangGraph + FastAPI 有状态 Agent API 的工程化组织方式
- [Nj-kol/hybrid_search_rag](https://github.com/Nj-kol/hybrid_search_rag) — Qdrant 混合检索（向量 + 关键词）链路
- [bbat996/Yuxi-agent](https://github.com/bbat996/Yuxi-agent) — LangGraph 知识库问答 + 工作流编排
- [Milvus 官方 Build RAG with Milvus](https://milvus.io/docs/build-rag-with-milvus.md) 与 [Milvus multi-tenancy](https://milvus.io/docs/multi_tenancy.md) — 向量库多租户过滤模式（同样适用于 Qdrant payload filter）
- [Zilliz: FastAPI + LangGraph + Milvus](https://zilliz.com/blog/build-intelligent-rag-with-langserve-langgraph-and-milvus) — 检索服务化与流式接口设计
- [langchain-ai/langgraph 官方示例](https://github.com/langchain-ai/langgraph) — `interrupt`/`Command(resume)` 人工审批模式

## 9. 简历描述

见 [RESUME.md](RESUME.md)。


## 真实模型离线评测（2026-10-30）

固定题库位于 `eval/datasets/v1`，每项目 20 篇资料、100 题（dev20/test80）。
本轮报告：[`report.md`](eval/reports/real_ops_20261029T221616/report.md)，包含四方案检索对照、模型裁判、工具/审批结果和失败明细。
通过进程环境提供 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 后，运行 `python -m eval.real_eval --output eval/reports/<新实验目录>`；中断可对原目录加 `--resume`。
旧 `eval/reports/eval_report.md` 是早期 Mock 结果，不作为真实模型成绩。正式报告区分真实模型调用、本地向量后端、模拟业务工具和未验证的外部服务，不代表线上业务收益。
