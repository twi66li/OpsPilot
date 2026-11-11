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

React 工作台已接通登录、完整会话历史、调查过程、文件知识库、引用回查、审批工单与已有评测记录。认证、会话、工单与知识元数据查询使用 AsyncSession（PostgreSQL 使用 asyncpg）；同步图、检索API和入库任务由线程隔离。SSE 支持真实模型正文增量与即时工具进展，检索子Agent的中间答案不作为主回答输出。受控演练修复继续开发。普通 PDF 文本提取不包含 OCR，也不保证恢复复杂表格布局。

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
# 首次建立演示手册、账号与历史故障：
docker compose -f compose.local.yml exec api python -m scripts.seed
```

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
