# OpsPilot 网站维护与排障助手

面向个人开发者和小团队的网站维护助手：无需接入监控，提交问题或脱敏日志，结合内置官方来源资料和可选的网站手册，逐步检查、反馈结果并保存处理案例。平台不会自动操作用户服务器。

技术栈：Python / FastAPI / LangGraph / PostgreSQL 16 / Qdrant / BM25 + RRF + Rerank / Redis / MCP / React + TypeScript + Ant Design / Docker Compose。

## 已实现

- 登录后从首页选择个人排障、资料咨询或独立故障演练；三者有明确的用途说明。
- 内置六类中文排障指南，覆盖 Python / SQLAlchemy、Docker、Nginx、PostgreSQL，保留官方链接、适用版本、核对日期和内容指纹；资料为平台整理，不冒充官方原文。
- 网站背景可选并保留会话快照；个人手册使用独立存储命名空间，只检索当前网站选中的手册。
- 当前日志作为会话证据，提交前支持脱敏预览；检查报告区分日志事实、待验证原因、缺失信息与1–3条具体检查步骤。
- 确认方案 → 用户自行操作 → 提交新结果 → 样本范围内核对；只点击完成或提供旧日志不会标记恢复。
- 用户确认后保存案例，在本人同一网站的问题中检索复用；历次报告和当次资料版本可回查。

- 原生 Function Calling 调查循环：工具白名单、参数校验、超时、调用次数与模型预算限制。
- 真实 MCP Client/Server 协议封装只读日志和指标查询；可选的独立检索子 Agent。
- JWT 多租户权限、个人会话、PostgreSQL 检查点、审批后恢复与幂等建单。
- PDF / Markdown / TXT 上传，持久化入库任务、进度、失败重试与文件去重。
- 普通文本 PDF 逐页提取；复杂 PDF 可同时上传原文件及本机 MinerU 的 `content_list.json`。
- 结构化父子块、页码及来源回查、有效知识版本切换；失败和历史版本在召回前过滤。
- SSE 节点事件、调查 trace、模型用量与轻量评测接口。

React 工作台已接通登录、完整会话历史、调查过程、文件知识库、引用回查、审批工单与已有评测记录。认证、会话、工单与知识元数据查询使用 AsyncSession（PostgreSQL 使用 asyncpg）；同步图、检索API和入库任务由线程隔离。SSE 支持真实模型正文增量与即时工具进展，检索子Agent的中间答案不作为主回答输出。受控演练修复已接入：申请与审批后执行固定的测试故障注入关闭动作，保留执行前后实际请求验证。普通 PDF 文本提取不包含 OCR，也不保证恢复复杂表格布局。

个人排障复用 LangGraph 与原生工具循环，只开放资料检索和本人案例检索；结构化报告通过校验后整份展示，等待期间推送工具进展。演练保留日志/指标 MCP、可选检索子 Agent 与审批修复，不会将测试数据带入个人排障。

## 第一次使用

1. 打开首页，进入“我的网站排障”。没有手册也可以直接提问。
2. 可选添加网站背景；自己的 PDF/MD/TXT 手册先上传到“排障资料库 → 我的网站手册”，完成入库后在网站背景中关联。
3. 具体报错通过“添加日志 / 补充证据”粘贴或读取 `.log/.txt`，先预览脱敏，再确认提交并发送问题。
4. 按报告中的步骤自行检查；确认方案后，把新的日志或响应结果作为“操作后结果”提交，在同一会话继续问助手。
5. 核对结果并保存实际处理记录。新问题使用新会话，已保存案例保持原记录。

个人排障入口为 `/api/support`。内置指南在 `app/knowledge/official.json`；运行 `uv run python -m scripts.collect_official_sources` 可采集固定官方页面的核对快照到被 Git 忽略的 `data/official_sources`。下载成功不会自动发布新知识，需核对内容和适用版本后更新整理语料。复杂扫描 PDF 继续沿用本机 MinerU 解析产物导入流程。

2026-11-12，本机真实模型样例闭环已通过，两轮分析合计37.25秒，核对结果明确限定为提交样本。成功记录和遇到的问题见 `eval/night20261112/personal_support/results.json`。个人资料检索需串行调用Embedding和Rerank，等待上限至少30秒。供应商 `insufficient_quota` 可触发已授权的候选轮换，普通鉴权错误不会轮换；模型总预算仍然生效。该新版目前在本机运行，下面的云端部署记录对应此前的演练版本。

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
docker compose -f compose.demo.yml up -d --wait --wait-timeout 180
# 本机建立访问隧道：
ssh -N -L 18000:127.0.0.1:18000 your-server
```

随后打开 `http://127.0.0.1:18000`。配置独立管理员密码、JWT密钥和数据库密码；原文件及预算账本放持久卷。演示按单API worker运行，预算账本是单实例限制，不能当作跨主机共享额度。云端默认关闭检索子Agent以减少耗时，用开关保留对照能力。公网域名与TLS入口可在需要分享演示时另行配置。

云端也已通过一次真实闭环：54.25秒完成调查，指标和日志工具返回ok，审批后测试请求从500恢复为200。该次调查调用8,059 tokens。验收后六容器内存快照约431.6MiB，不含宿主系统和Docker，也不是总峰值或并发能力。成功记录和首次8秒工具超时记录均保留在 `eval/night20261111`；演示配置的工具上限现为30秒，并通过健康检查等待API就绪。

评测文件通过 `./eval:/app/eval:ro` 持久化挂载。`eval/reports/cloud_runtime_20261111.json` 保存资源快照；账户密码、API密钥和SSH隧道信息保存在本机被忽略的 `data/cloud-deploy`，不要提交到仓库。
