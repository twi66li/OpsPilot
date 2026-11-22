# OpsPilot 网站维护工作台与智能助手

面向个人开发者和小团队的网站维护工作台：集中管理网站环境、维护资料、异常问题和处理记录。智能助手按任务提供资料咨询、日志排查、操作结果核对与历史查询。用户通过上传资料和提交证据即可开始，个人网站的实际操作由用户执行。

技术栈：Python / FastAPI / LangGraph / PostgreSQL 16 / Milvus / BM25 + RRF + Rerank / Redis / MCP / React + TypeScript + Ant Design / Docker Compose。

文档：[架构设计](docs/ARCHITECTURE.md) · [API](docs/API.md) · [评测](docs/EVAL.md) · [变更与验收记录](CHANGELOG.md)

## 核心闭环

```mermaid
flowchart LR
    A[登记网站与手册] --> B[提交脱敏日志]
    B --> C[证据化诊断报告]
    C --> D[确认方案并自行操作]
    D --> E[提交新结果]
    E --> F[程序样本核对]
    F --> G[确认结局 / 归档复用]
```

- **证据优先**：报告区分日志事实、待验证原因、缺失信息与1–3条具体检查步骤；引用可回查到资料页码和日志行。
- **恢复以数据为准**：只点击完成、提供旧日志、空日志、其他接口成功、重定向或仅 running 都不会通过恢复核对；程序核对支持 ISO 带时区与 Nginx 访问日志时间、原接口匹配、HTTP 状态/耗时/请求数、连接池异常行与容器 State。
- **样本核对与用户确认分开记录**：检查完成不自动表示恢复，解决与归档分别记录。

## 功能

**维护工作台**

- 网站中心：环境编辑、关联资料、问题概况、主动单次 HTTP 检查与定时巡检（仅公开 80/443，固定已验证 IP、禁止跳转）。
- 问题中心：登记异常、接口、影响与近期变更；严重度、标签、补充记录、维护时间线、Markdown 报告导出、同租户交接。
- 任务分流：知识咨询、异常排查、结果核对、历史查询和寒暄分别处理；咨询不自动创建故障。
- 场景指南：数据库连接异常、Nginx 502/504、Docker 退出/重启；内置六类中文排障指南（Python / SQLAlchemy、Docker、Nginx、PostgreSQL），保留官方链接、适用版本、核对日期和内容指纹。
- Docker/systemd 只读证据收集向导，日志提交前脱敏预览。

**智能助手与检索**

- LangGraph 工作流 + 原生 Function Calling 调查循环：工具白名单、参数校验、超时、调用次数与模型预算限制。
- 混合检索：Milvus 向量 + BM25（jieba）+ RRF 融合 + 可选 Rerank；结构化父子块、页码回查、有效知识版本切换。
- PDF / Markdown / TXT 上传，持久化入库任务、进度、失败重试与文件去重；复杂 PDF 可附带本机 MinerU 的 `content_list.json`。
- SSE 推送节点事件、工具进展和正文增量；支持停止、断线按事件游标重连。

**组织与演练（附加能力）**

- JWT 多租户权限（admin / engineer / viewer）、服务端登录会话、登录限流。
- 独立故障演练：真实 MCP Client/Server 封装只读日志与指标、可选检索子 Agent、PostgreSQL 检查点、审批后恢复与幂等建单、受控修复。
- 评测：历史基线、共同问题对比与失败分类（不代表线上准确率）。

普通 PDF 文本提取不包含 OCR，也不保证恢复复杂表格布局。

## 第一次使用

首次访问可在登录页注册普通账号，注册后直接进入独立私人空间。普通用户可以使用网站、手册、问题和维护对话；工程师另有演练审批权限，管理员另可管理组织账号及查看评测。公开注册不会授予管理员权限，也不能加入已有组织。

1. 从“我的网站”登记环境，或直接打开“智能维护助手”咨询。自己的PDF/MD/TXT手册在“维护资料库 → 我的网站手册”上传，入库后可关联。
2. 有异常时在“问题中心”独立创建记录，填写异常、接口、影响和变更；再打开助手，提交脱敏日志。基础知识咨询可以保持为普通对话。
3. 确认检查方案后自行操作，逐项记录检查结果。新日志可关联到具体检查步骤，作为“操作后结果”提交。
4. 查看程序核对的接口、时间和请求样本，再让助手解释结果。样本核对结果与用户确认解决分别记录。
5. 在问题中心确认实际结局或归档。未解决的问题也可归档，后续重新打开保留证据；同一网站的历史查询可直接打开处理记录。

网站支持删除，默认保留问题及环境快照，也可勾选同时删除关联问题和维护对话。问题中心、个人助手和演练会话支持勾选批量删除。这里的删除指移出工作区并阻断常规访问；原记录保留在数据库以关联审批、工单及执行审计，未执行的审批会取消，上传手册不受影响。

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
离线开发首次启动会创建管理员，示例账号为 `admin / admin123`。生产环境使用下方的显式初始化流程。

## PostgreSQL + Milvus 本地联调

```powershell
# 在 .env 中设置 POSTGRES_PASSWORD，并按 .env.example 配置真实模型。
docker compose -f compose.local.yml --profile app --profile observability up -d --build
```

本地 Compose 启动前先初始化手册和历史故障。管理员按环境配置创建；额外固定演示账号仅在本机显式执行 `python -m scripts.seed --demo-users` 时创建。

`docker-compose.yml` 引用 `compose.local.yml`，本地联调统一使用 PostgreSQL 与 Milvus 2.4.15。启动完整工作台时启用 `app` 和 `observability` profiles。业务数据库、Milvus 与原文件均需持久化，原文件默认放在 `data/knowledge`。

> [!IMPORTANT]
> 默认使用单 API 进程。复杂 OCR 在本机或独立环境执行。

双副本联调需在两个 API 进程上都设置 `MULTI_REPLICA_ENABLED=true` 和 `DB_REPLICA_BUDGET=2`。两个进程共享 PostgreSQL、Milvus collection、Redis、知识文件目录 `KB_STORAGE_PATH`、JWT 密钥及预算账本作用域。默认配额为每副本业务池 8 条、检查点池 4 条，两个副本合计 24 条，另外预留 6 条管理连接。跨副本恢复使用后台 SSE 调查和持久化事件游标。

从 JSONL 预算账本升级 PostgreSQL 部署时，先用 `python -m scripts.migrate_budget_ledger --help` 核对迁移参数，确认原有用量与未结算预占导入后再启用数据库账本。单副本仍可显式使用 `BUDGET_LEDGER_BACKEND=jsonl`。

使用宿主机运行 API 时，设置 `VECTOR_STORE_BACKEND=milvus` 和 `MILVUS_URI=http://127.0.0.1:19531` 连接本机 Compose 服务。离线开发可显式选择 `VECTOR_STORE_BACKEND=memory`。

如果旧资料仍在 Qdrant，保持原 Embedding 模型和维度，在启动新 API 前暂停旧库入库并迁移现有向量和分块信息。用 `uv run --frozen --group migration python -m scripts.migrate_milvus --help` 查看参数。迁移默认只盘点源数据，显式传 `--apply` 才写入目标集合。确认迁移校验通过后，将 `MILVUS_COLLECTION` 指向该集合。运行镜像只安装核心依赖，迁移命令在单独的本机环境执行。

`/live` 检查进程，`/ready` 检查数据库、Milvus、缓存和入库线程。用 `docker compose -f compose.local.yml exec -T api python -m integration.check_stack` 检查真实依赖，只创建并清理本次随机测试资源，无需调用模型供应商。

## 生产部署

**安全**：`APP_ENV=production` 拒绝默认/弱 JWT 密钥与管理员密码；同源默认不开放 CORS。`REGISTRATION_ENABLED` 控制注册入口，演示部署默认关闭。JWT 带服务端登录会话，退出和修改密码撤销旧登录。MCP 使用限定用途的短期令牌。

**数据库**：由 Alembic 升级，`uv run python -m scripts.migrate` 可显式执行。升级前备份；基线和审计迁移不自动删表回退。新镜像以 UID/GID 10001 运行，Linux 宿主的 `data` 持久目录需要授予该用户读写权限。迁移会使旧版无登录会话的令牌失效，升级后需要重新登录。

首次生产启动前配置至少 32 字符的独立 JWT 密钥、至少 12 字符的管理员密码和持久目录。空库须先执行 `scripts.init_admin`，否则 API 会拒绝启动并提示初始化。首次启动导入内置资料，需要可用的 Embedding 配置和预算。

```bash
docker build -t opspilot:milvus .          # .env 中设置 OPSPILOT_IMAGE=opspilot:milvus
docker compose -f compose.demo.yml run --rm api python -m scripts.init_admin
docker compose -f compose.demo.yml up -d --wait --wait-timeout 180
# 本机建立访问隧道后打开 http://127.0.0.1:18000
ssh -N -L 18000:127.0.0.1:18000 your-server
```

重复初始化不会覆盖账号；`python -m scripts.init_admin --reset-password` 才会重设已有管理员密码并撤销登录。`compose.demo.yml` 包含 PostgreSQL、Redis、Milvus、API、演练服务和 Prometheus。

**反向代理**：[Nginx 代理配置](deploy/nginx/opspilot.conf) 宿主机监听 8080，转发到回环地址 18000，前端、认证、上传与 SSE 保持同源；上传上限 25 MiB，流式响应关闭代理缓冲，读写超时 300 秒。将实际 Docker 网关与 `127.0.0.1,::1` 加入 `FORWARDED_ALLOW_IPS`，按部署设置 `CORS_ORIGINS`。公网域名和证书准备好后启用 [HTTPS 配置](deploy/nginx/opspilot-https.conf)；真实账号与私人资料应通过 HTTPS 访问（HTTP 下浏览器缺少 `crypto.randomUUID` 时应用回退到 `crypto.getRandomValues`）。

评测文件通过 `./eval:/app/eval:ro` 挂载。账户密码、API 密钥和 SSH 隧道信息保存在本机被忽略的 `data/cloud-deploy`，不要提交到仓库。

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

个人排障入口为 `/api/support`。内置指南在 `app/knowledge/official.json`，场景素材在 `app/knowledge/scenarios.json`；运行 `uv run python -m scripts.collect_official_sources` 可采集固定官方页面的核对快照到被 Git 忽略的 `data/official_sources`，下载成功不会自动发布新知识。

## 受控演练修复

本地 Compose 默认关闭故障演练。体验时设置 `DRILL_ENABLED=true`，并启用 `observability` profile。模拟修复和 MCP 分别由 `REMEDIATION_ENABLED`、`MCP_ENABLED` 开启，均依赖演练总开关。演示 Compose 的总开关默认开启，可通过 `.env` 覆盖。API 与模拟器共用 `DRILL_SERVICE`，默认 `order-service`，修复仅对 default 租户开放。更换目标后，旧目标的待批申请无法批准执行。

调查完成后，工作台可提出修复申请；管理员或工程师在“审批与工单”批准后才执行。固定动作是关闭 metrics-demo 的故障注入，不接受URL、Shell或模型自定义脚本。执行前后各发送3个实际请求，保存状态码、耗时、请求编号与故障配置。短探针恢复不等同于生产根因修复或长期SLO恢复。

- `GET /api/remediations/actions`：当前环境可用动作。
- `POST /api/remediations`：关联本人调查记录的修复申请。
- `GET /api/remediations`：本租户申请与审计结果。
- `POST /api/remediations/{id}/decide`：批准后执行或拒绝；重复批准不重复执行。

运行状态在执行前持久化；服务重启时未完成的执行标为interrupted，需重新调查核对，不自动重放。演练控制服务只应绑定本机或容器内网。

## 开发检查

```powershell
uv run --frozen --group migration pytest tests/rag/test_knowledge_files.py tests/rag/test_kb.py tests/rag/test_rag.py -q
.venv/Scripts/python -m pytest tests/agent/test_logstats.py tests/agent/test_checkpoint_persistence.py tests/agent/test_tool_loop.py tests/agent/test_approval.py -q
.venv/Scripts/python -m ruff check app tests
```

验收脚本（使用已配置的真实模型额度，不是批量评测或独立准确率测试）：

- `uv run python -m scripts.workspace_acceptance`：按网站登记、咨询、排查、程序核对、状态管理和历史查询验收一次；默认注册独立私人账号，继续已有验收时设置 `ACCEPTANCE_USERNAME` / `ACCEPTANCE_PASSWORD`。
- `uv run python -m scripts.demo_acceptance`：串起真实PDF、来源回查、真实模型流式调查和审批修复。

CI 使用同一 Compose 启动隔离的 PostgreSQL、Milvus 和 Redis，验证向量迁移和浏览器流程。浏览器流程要求 `OPSPILOT_E2E_ISOLATED=1`，仅在隔离环境运行。

应用与开发依赖以 `pyproject.toml`、`uv.lock` 为准，`requirements.txt` 和 `requirements-dev.txt` 为锁文件导出。迁移检查显式加 `--group migration`。Cross-Encoder 和 Langfuse 可以用 `requirements-optional.txt` 按需安装。

从项目根目录运行 `python -m eval.offline_retrieval` 可复查冻结检索基线。默认读取本地 `eval/reports/real_ops_20261029T221616/embeddings.json` 及同目录的 `run_manifest.json`。保存在其他位置时用 `--embeddings` 指定向量文件。入口会校验数据和向量覆盖，报告 Recall@5、MRR@5 与关键词命中，回归时以非零状态退出。

模型额度、`.env`、原文数据和本地运行日志不要提交到 GitHub。已有评测是开发案例记录，不能据此宣称独立根因准确率。
