# OpsPilot 运维调查 Agent

面向运维调查的个人工程项目：按需查询日志、指标、运维手册与历史故障，展示证据与不确定性，通过人工审批创建工单。

技术栈：Python / FastAPI / LangGraph / PostgreSQL 16 / Qdrant / BM25 + RRF + Rerank / Redis / MCP / React + TypeScript + Ant Design / Docker Compose。

## 已实现

- 原生 Function Calling 调查循环：工具白名单、参数校验、超时、调用次数与模型预算限制。
- 真实 MCP Client/Server 协议封装只读日志和指标查询；可选的独立检索子 Agent。
- JWT 多租户权限、个人会话、PostgreSQL 检查点、审批后恢复与幂等建单。
- PDF / Markdown / TXT 上传，持久化入库任务、进度、失败重试与文件去重。
- 普通文本 PDF 逐页提取；复杂 PDF 可同时上传原文件及本机 MinerU 的 `content_list.json`。
- 结构化父子块、页码及来源回查、有效知识版本切换；失败和历史版本在召回前过滤。
- SSE 节点事件、调查 trace、模型用量与轻量评测接口。

React 工作台已接通登录、完整会话历史、调查过程、文件知识库、引用回查、审批工单与已有评测记录。认证、会话、工单与知识元数据查询使用 AsyncSession（PostgreSQL 使用 asyncpg）；同步图、检索API和入库任务由线程隔离。SSE 支持真实模型正文增量与即时工具进展，检索子Agent的中间答案不作为主回答输出。受控演练修复已接入：申请与审批后执行固定的测试故障注入关闭动作，保留执行前后实际请求验证。普通 PDF 文本提取不包含 OCR，也不保证恢复复杂表格布局。

## 本机运行

```powershell
uv sync --frozen
Copy-Item .env.example .env
# 默认 SQLite、内存向量库和 mock 模型，可先离线调试；真实模式在 .env 中配置。
.venv/Scripts/python -m scripts.seed
cd frontend
npm ci
npm run build
cd ..
.venv/Scripts/python -m uvicorn app.main:app --reload
```

访问工作台 `http://127.0.0.1:8000`，接口文档 `http://127.0.0.1:8000/docs`。
前端需要 Node 24；Docker 使用独立构建阶段，运行容器只托管静态产物。开发前端可在 `frontend` 执行 `npm run dev`，代理API至本机8000端口。
默认管理员是 `admin / admin123`，云端部署前需修改管理员密码和 JWT 密钥。

## PostgreSQL + Qdrant 本地联调

```powershell
# 在 .env 中设置 POSTGRES_PASSWORD，并按 .env.example 配置真实模型。
docker compose -f compose.local.yml --profile app --profile observability up -d --build
```

启动时先初始化手册和历史故障，再启动API，避免两个模型进程同时持有同一预算账本。管理员按环境配置创建；额外固定演示账号仅在本机显式执行 `python -m scripts.seed --demo-users` 时创建。

`compose.local.yml` 为当前主配置；`docker-compose.yml` 保留早期 MySQL 配置。
业务数据库、Qdrant 与原文件均需持久化；原文件默认放在 `data/knowledge`。入库 worker 与模型预算账本共用 API 进程，当前部署使用单个 API worker。复杂 OCR 在本机或独立环境执行，2GB 演示服务器不部署 MinerU 模型。

## 知识文件接口

先通过 `POST /auth/login` 取得 Bearer token，再调用：

| 接口 | 用途 |
| --- | --- |
| `POST /api/kb/documents/upload` | multipart：`file` 原文件、可选 `title`；更新时传 `doc_id`；导入 MinerU 时附 `parsed_file` |
| `GET /api/kb/versions/{id}/status` | 入库状态、进度、错误与警告 |
| `GET /api/kb/versions/{id}` | 解析页与检索子块预览 |
| `POST /api/kb/versions/{id}/retry` | 重试失败任务 |
| `GET /api/kb/documents/{id}/versions` | 查看历史版本 |
| `POST /api/kb/versions/{id}/activate` | 切换已完成索引的有效版本 |
| `GET /api/kb/chunks/{id}` | 命中片段、父块、页码与原文件地址 |
| `GET /api/kb/versions/{id}/file` | 经过租户鉴权后下载原文件 |

文件上限10MB，直接解析的 PDF 上限100页；纯文本按 UTF-8 读取。入库状态为 `uploaded → parsing → chunking → indexing → ready`，错误进入 `failed`；重启中断的任务标记失败后可重试。历史原文件保留用于溯源。

## 开发检查

```powershell
.venv/Scripts/python -m pytest tests/test_knowledge_files.py tests/test_kb.py tests/test_rag.py -q
.venv/Scripts/python -m ruff check app tests
```

模型额度、`.env`、原文数据和本地运行日志不要提交到 GitHub。已有评测是开发案例记录，不能据此宣称独立根因准确率。

## 受控演练修复

本地 Compose 默认可通过 `REMEDIATION_ENABLED=true` 开启（`.env.example` 默认关闭）。只为 default 租户配置了 order-service 演练目标。

调查完成后，工作台可提出修复申请；管理员或工程师在“审批与工单”批准后才执行。固定动作是关闭 metrics-demo 的故障注入，不接受URL、Shell或模型自定义脚本。执行前后各发送3个实际请求，保存状态码、耗时、请求编号与故障配置。短探针恢复不等同于生产根因修复或长期SLO恢复。

- `GET /api/remediations/actions`：当前环境可用动作。
- `POST /api/remediations`：关联本人调查记录的修复申请。
- `GET /api/remediations`：本租户申请与审计结果。
- `POST /api/remediations/{id}/decide`：批准后执行或拒绝；重复批准不重复执行。

运行状态在执行前持久化；服务重启时未完成的执行标为interrupted，需重新调查核对，不自动重放。演练控制服务只应绑定本机或容器内网。

## 完整演示与小型部署

`uv run python -m scripts.demo_acceptance` 串起真实PDF、来源回查、真实模型流式调查和审批修复。它使用已配置的模型额度，并保存一条工作台可读的验收记录；不是批量评测或独立准确率测试。

2026-11-11的一次实际记录：调查83.1秒，首个正文增量63.2秒，模型与Embedding/Rerank合计13,148 tokens；引用命中上传手册，审批后3次测试请求由500恢复为200。记录位于 `eval/night20261111/demo_acceptance/results.json`。该耗时包含检索子Agent，不能作为延迟SLA。

`compose.demo.yml` 使用本机构建的 `opspilot:20261111` 镜像，包含PostgreSQL、Redis、Qdrant、API、演练服务和Prometheus。上传镜像、配置与 `.env` 后执行：

```bash
docker compose -f compose.demo.yml up -d
# 本机建立访问隧道：
ssh -N -L 18000:127.0.0.1:18000 your-server
```

随后打开 `http://127.0.0.1:18000`。配置独立管理员密码、JWT密钥和数据库密码；原文件及预算账本放持久卷。演示按单API worker运行，预算账本是单实例限制，不能当作跨主机共享额度。云端默认关闭检索子Agent以减少耗时，用开关保留对照能力。公网域名与TLS入口可在需要分享演示时另行配置。
