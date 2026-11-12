# OpsPilot 网站维护工作台与智能助手

面向个人开发者和小团队的网站维护工作台：集中管理网站环境、维护资料、异常问题和处理记录。智能助手按任务提供资料咨询、日志排查、操作结果核对与历史查询。用户通过上传资料和提交证据即可开始，个人网站的实际操作由用户执行。

技术栈：Python / FastAPI / LangGraph / PostgreSQL 16 / Qdrant / BM25 + RRF + Rerank / Redis / MCP / React + TypeScript + Ant Design / Docker Compose。

## 已实现

- 网站中心：网站列表与详情、环境编辑、关联资料、问题概况，以及咨询/排查/历史入口。
- 问题中心：独立登记和编辑异常、接口、影响与近期变更，筛选状态，用户确认解决、归档、重新打开；归档与解决分别记录，兼容旧版saved数据。
- 任务分流：知识咨询、异常排查、结果核对、历史查询和寒暄分别处理；咨询不自动创建故障，结果核对以程序统计为准。
- 已确认计划的逐项检查记录、方案版本校验和步骤证据关联；检查完成不自动表示恢复。
- 程序化提交样本核对：支持ISO带时区与Nginx常见访问日志时间、原接口匹配、HTTP状态/耗时/请求数、明确连接池异常行与容器State。空日志、其他接口成功、旧样本、重定向和仅running均不会直接通过恢复核对。
- 数据库连接异常、Nginx 502/504、Docker退出/重启三类场景指南，包括症状、只读检查、正常/异常分支、修改风险与回退和明确标注的教学样例。

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

## 维护工作台工程升级（2026-11-12）

- 页面具有资源URL，例如 `/issues/问题ID`、`/websites/网站ID`、`/operations/审批ID`；刷新和前进后退保留位置。移动端使用抽屉导航，助手提问区优先显示。
- 助手显示执行阶段、开始及更新时间，支持停止与同一请求重连。普通咨询显示回答草稿；诊断报告校验后展示。停止不会保证供应商已发出的请求零用量，已有证据及上一份有效报告保留。
- 运行使用请求编号去重、每会话单运行约束和追加事件表；默认 `MAX_CONCURRENT_RUNS=1`。排队入库任务可在重启后恢复；执行中被中断的解析和修复需要显式核对或重试。当前仍按单API worker部署。
- 网站提供最近检查提交时间、资料任务概况和主动单次HTTP检查；HTTP仅访问公开80/443端口，固定已验证IP并禁止跳转。结果具有时间和范围，不能代表实时监控或整体恢复。
- 助手提供Docker/systemd只读证据收集向导和日志文件脱敏预览。问题支持严重度、标签、个人补充记录、维护时间线和Markdown报告导出。
- 资料库提供有效版本检索测试、选中文档的失败任务重试及文档移出检索。历史引用与原文件保留。`scripts/parse-pdf.cmd`支持拖入PDF；扫描件需要本机MinerU兼容的Content List格式，未安装时只能提取普通文本。
- 审批详情集中展示依据、固定动作、影响、操作人和验证；评测支持共同问题对比、程序检查回退与失败分类。对比只针对共有输入，不能用作独立准确率。
- `/api/workspace/{cases,sites,sessions,documents,approvals,tickets,remediations}` 使用 `items/total/page/page_size` 协议，支持服务端搜索；旧列表接口继续兼容已有验收脚本。

### 安全与数据库升级

`APP_ENV=production`拒绝默认/弱JWT密钥与管理员密码；同源默认不开放CORS。`REGISTRATION_ENABLED`控制注册入口，演示部署默认关闭，需自助注册时显式开启。登录具有独立限流；JWT带服务端登录会话，退出和修改密码撤销旧登录。MCP使用限定用途的短期令牌。

数据库由Alembic升级，`uv run python -m scripts.migrate`可显式执行。迁移基线兼容既有完整业务表，不为缺失的必填业务字段猜测数据。升级前备份；基线和审计迁移不自动删表回退，需审阅迁移或恢复备份。新镜像以UID/GID 10001运行，Linux宿主的`data`持久目录需要授予该用户读写权限。

生产首次部署先配置独立密钥和至少12字符管理员密码，并准备持久目录，再运行：

```bash
docker compose -f compose.demo.yml run --rm api python -m scripts.init_admin
docker compose -f compose.demo.yml up -d
```

重复初始化不会覆盖账号。只有显式运行`python -m scripts.init_admin --reset-password`才使用配置重设已有管理员密码并撤销登录。迁移会使旧版无登录会话的令牌失效，升级后需要重新登录。

`/live`检查进程，`/ready`检查数据库、向量库、缓存和入库线程；镜像及云端Compose使用readiness。真实基础设施检查：`docker compose -f compose.local.yml exec -T api python -m integration.check_stack`，不调用模型供应商，只创建并清理本次随机测试资源。CI另设独立PostgreSQL/Qdrant/Redis与三条浏览器流程；浏览器流程要求`OPSPILOT_E2E_ISOLATED=1`，禁止直接针对生产运行。

本轮代码已在本机升级；云端仍须另行同步镜像、配置及目录权限。PDF报告、生产Prometheus连接器、组织成员邀请/共享和从页面启动新评测继续后置。

## 第一次使用

首次访问可在登录页注册普通账号，注册后直接进入独立私人空间。普通用户可以使用网站、手册、问题和维护对话；工程师另有演练审批权限，管理员另可管理组织账号及查看评测。公开注册不会授予管理员权限，也不能加入已有组织。本机登录页提示默认演示管理员，外部访问请注册或使用部署者分配的账号。

1. 从“我的网站”登记环境，或直接打开“智能维护助手”咨询。自己的PDF/MD/TXT手册在“维护资料库 → 我的网站手册”上传，入库后可关联。
2. 有异常时在“问题中心”独立创建记录，填写异常、接口、影响和变更；再打开助手，提交脱敏日志。基础知识咨询可以保持为普通对话。
3. 确认检查方案后自行操作，逐项记录检查结果。新日志可关联到具体检查步骤，作为“操作后结果”提交。
4. 查看程序核对的接口、时间和请求样本，再让助手解释结果。样本核对结果与用户确认解决分别记录。
5. 在问题中心确认实际结局或归档。未解决的问题也可归档，后续重新打开保留证据；同一网站的历史查询可直接打开处理记录。

网站支持删除，默认保留问题及环境快照，也可勾选同时删除关联问题和维护对话。问题中心、个人助手和演练会话支持勾选批量删除。这里的删除指移出工作区并阻断常规访问；原记录保留在数据库以关联审批、工单及执行审计，未执行的审批会取消，上传手册不受影响。

评测记录默认打开43条明细的历史基线，默认列表展示历史基线、重排恢复、质量改进三个已整理批次；开发及流程验收记录需要显式勾选展示。无逐题明细的记录会展示用途说明和原始验收结果，不显示空白评测表。这些历史记录不代表当前线上准确率。

`app/knowledge/scenarios.json` 保存三类场景素材，相关内容会一起进入已有官方来源索引。`uv run python -m scripts.workspace_acceptance` 使用已配置的真实模型，按网站登记、咨询、排查、程序核对、状态管理和历史查询验收一次；它提交的是明确标记的样例，不是生产恢复评测。个人验收脚本默认注册独立私人账号；演练验收使用专用工程师账号，均不再写入管理员的会话或网站。需要继续已有验收时，显式设置原账号的 `ACCEPTANCE_USERNAME` / `ACCEPTANCE_PASSWORD`。

2026-11-12，维护业务流程已在本机完成一次真实模型验收，四类任务合计38.11秒，咨询分离、网站与问题关联、检查标记不代表恢复、程序样本比较、解决与归档分离和同网站历史复用六项检查通过。记录为 `eval/night20261112/maintenance_workspace/results.json`；25项相关离线检查与前端生产构建通过。当前云端仍为此前演练版本。

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

`compose.demo.yml` 使用本机构建的 `opspilot:20261112` 镜像，包含PostgreSQL、Redis、Qdrant、API、演练服务和Prometheus。上传镜像、配置与 `.env` 后执行：

```bash
docker compose -f compose.demo.yml up -d --wait --wait-timeout 180
# 本机建立访问隧道：
ssh -N -L 18000:127.0.0.1:18000 your-server
```

随后打开 `http://127.0.0.1:18000`。配置独立管理员密码、JWT密钥和数据库密码；原文件及预算账本放持久卷。演示按单API worker运行，预算账本是单实例限制，不能当作跨主机共享额度。云端默认关闭检索子Agent以减少耗时，用开关保留对照能力。公网域名与TLS入口可在需要分享演示时另行配置。

云端也已通过一次真实闭环：54.25秒完成调查，指标和日志工具返回ok，审批后测试请求从500恢复为200。该次调查调用8,059 tokens。验收后六容器内存快照约431.6MiB，不含宿主系统和Docker，也不是总峰值或并发能力。成功记录和首次8秒工具超时记录均保留在 `eval/night20261111`；演示配置的工具上限现为30秒，并通过健康检查等待API就绪。

评测文件通过 `./eval:/app/eval:ro` 持久化挂载。`eval/reports/cloud_runtime_20261111.json` 保存资源快照；账户密码、API密钥和SSH隧道信息保存在本机被忽略的 `data/cloud-deploy`，不要提交到仓库。
