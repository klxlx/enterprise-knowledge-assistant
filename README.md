# Robot RAG Customer Service

基于 Vue 3、FastAPI、LangChain 和 PostgreSQL + pgvector 的扫地机器人智能客服示例。

项目将前端展示与 RAG 服务解耦：Vue 负责聊天和知识库上传，FastAPI 负责会话管理、向量检索、模型调用和 SSE 流式输出。

## 功能

- 基于 PostgreSQL + pgvector 的向量知识库检索和 HNSW 余弦索引
- 基于文件的多轮会话历史，侧边栏可查看、继续和删除历史会话
- 会话历史自动压缩：只把最近几轮原文交给模型，更早的对话用 qwen-flash 摘要，避免上下文无限增长
- Vue 3 聊天界面和 SSE 流式回答
- 知识库上传支持 TXT / Markdown / PDF / Word / 图片，图片与扫描件自动走 OCR；SHA-256 去重，文档与向量在同一事务中写入
- 按 Markdown 章节切块，每块带章节前缀参与向量化，并把更大范围的上下文交给模型
- 向量召回后用 DashScope 重排序精排，减少无关片段干扰；重排序不可用时自动退回向量排序
- 模型可自主调用工具：检索知识库、查实时天气、转人工建单，可以多轮调用后再作答
- 适用范围隔离：资料带 `scope` 元数据（型号 / 部门 / 业务线），检索时只命中该范围 + 通用资料，绝不串范围；用户提过一次范围会在会话里记住
- 权限两级：部门主管可以导入本部门资料、增删通讯录、看盲区和审计；新人也可以导入本部门或公开资料、提问和查通讯录；人事主管可跨部门管理资料；未登录只能看公开资料
- 部门隔离：资料带归属部门和可见性（全员 / 本部门 / 仅主管 / 敏感），新人只能检索公开数据和本部门资料，敏感资料不进入问答
- 资料时效：制度类资料带生效 / 失效日期，过期的直接退出检索，回答里会说明有效期
- 用户反馈与知识盲区：每轮回答可点赞点踩，答不上来、转人工、被点「没用」的问题自动汇总成盲区清单
- 审计日志：登录、上传资料、删除会话、转人工、提交反馈都留痕，可按动作或账号查
- 多源汇聚：数据源适配器（本地/共享目录、钉盘模拟、飞书云文档模拟）定时同步进知识库，支持新增 / 更新 / 下线，同一个来源只保留一个当前版本
- 前端展示来源文件、片段位置、相关度与所属型号；转人工时显示工单卡片
- 可调检索参数（召回条数 + 相关度阈值），无关问题会明确拒答而不是编造
- 内置评测集与评测脚本，改动检索或提示词后可量化对比效果
- FastAPI 健康检查接口
- Vite 开发代理和基础 CI 检查

## 项目结构

```text
RAG项目实例/
├── api.py                    # FastAPI 接口入口
├── rag.py                    # RAG 检索和问答链
├── eval_dataset.json         # 评测集（问题、期望来源、期望要点）
├── platform_setup.py         # Windows 事件循环兼容设置
├── documents.py              # 上传文件解析（TXT/Markdown/PDF/Word/图片 OCR）
├── chunking.py               # 按章节结构切块，小块检索 + 大块喂模型
├── rerank.py                 # DashScope 文本重排序调用
├── tools.py                  # 模型可调用的工具（知识库检索、实时天气、转人工）
├── sources/                  # 数据源适配器（本地目录、钉盘模拟、飞书云文档模拟）
├── scripts/                  # 维护、导入、评测命令
│   ├── evaluate.py           # 检索与问答效果评测
│   ├── list_knowledge.py     # 查看/维护知识库
│   ├── sync_knowledge.py     # 外部数据源增量同步
│   ├── import_handbook.py    # CSV 导入 FAQ/流程/员工卡片
│   ├── manage_users.py       # 账号管理
│   ├── review_feedback.py    # 反馈与知识盲区
│   ├── view_audit.py         # 审计日志
│   └── assign_sessions.py    # 历史会话归属
├── mock/dingtalk/            # 钉盘数据源的模拟清单与文件
├── mock/feishu/              # 飞书云文档数据源的模拟清单与文件
├── history_summary.py        # 会话历史截断与摘要压缩
├── knowledge_base.py         # 知识库写入服务
├── vector_stores.py          # PGVectorStore 检索器
├── permissions.py            # 部门与资料可见性规则（公开/本部门/仅主管/敏感）
├── database.py               # 数据库初始化、去重与事务写入
├── file_history_store.py     # 文件会话历史
├── config_data.py            # 配置和本地数据路径
├── data/                     # 员工、FAQ、流程种子数据
├── frontend/                 # Vue 3 + Vite 前端
├── tests/                    # API 与真实 PostgreSQL 集成测试
├── deploy/                   # 生产服务配置与维护说明
├── releases/                 # 版本发布说明
├── .env.example              # 环境变量模板
├── requirements.txt          # 运行依赖
└── .github/workflows/ci.yml  # GitHub Actions
```

## 环境准备

需要 Python 3.10+、Node.js 18+、PostgreSQL + pgvector，以及 DashScope API Key。

```powershell
Set-Location "path\to\robot-rag-customer-service"

python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
Copy-Item .env.example .env
```

将 `.env` 中的 `DASHSCOPE_API_KEY` 替换为真实密钥。`.env` 已被 Git 忽略，不会上传。

在数据库中由管理员执行 `CREATE EXTENSION vector`，然后配置 `DATABASE_URL`。
开发环境可使用 `postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/robot_rag`；不要提交真实连接凭据。
生产部署使用 1Panel PostgreSQL 实例、专用数据库角色与密码认证，仅通过 `127.0.0.1:5432` 连接，配置模板见 `.env.example`。
数据库可创建后执行 `python database.py` 初始化业务表；`EMBEDDING_MODEL` 和 `EMBEDDING_DIMENSIONS` 必须与数据库记录一致，默认使用 `text-embedding-v4` 的 1024 维向量。

## 启动后端

```powershell
Set-Location "path\to\robot-rag-customer-service"
& ".\.venv\Scripts\python.exe" -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
```

API 文档：<http://127.0.0.1:8000/docs>

## 启动前端

```powershell
Set-Location "path\to\robot-rag-customer-service\frontend"
npm install
npm run dev
```

访问 <http://localhost:5173>。Vite 会将 `/api` 请求代理到本地 FastAPI 服务。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 检查数据库连通性、存储类型及模型配置状态；数据库不可用时返回 503 |
| POST | `/api/chat` | SSE 流式问答，JSON 参数为 `input`、`session_id`；先推送 `sources` 事件（来源列表），再推送 `content` 事件，最后 `[DONE]` |
| GET | `/api/sessions` | 列出**当前账号自己**的历史会话；未登录返回空，换账号看不到别人的记录 |
| GET | `/api/scopes` | 知识库里出现过的适用范围，供前端做下拉建议 |
| GET | `/api/employees` | 员工通讯录，支持按事项关键词或部门筛选；按角色过滤可见性 |
| POST | `/api/employees` | 新增员工卡片（员工专属），自动生成 `emp-*` 编号，并写审计日志 |
| DELETE | `/api/employees/{employee_id}` | 删除员工卡片（员工专属），返回仍引用他的流程，并写审计日志 |
| GET | `/api/dingtalk/files/{file_id}/download` | 下载钉盘文件（需登录，按 `visibility` 校验；mock 模式返回本地文件） |
| POST | `/api/auth/login` | 登录，返回 token 和用户信息 |
| POST | `/api/feedback` | 给某轮回答打分：`turn_id` + `rating`（1=有用，-1=没用）+ 可选 `comment` |
| GET | `/api/reviews/blind-spots` | 知识盲区清单（员工专属） |
| GET | `/api/reviews/stats` | 满意度与盲区概览（员工专属） |
| GET | `/api/audit` | 审计日志，支持 `action` / `username` 过滤（员工专属） |
| GET | `/api/sessions/{session_id}` | 读取某个会话的全部消息；不属于当前账号返回 403 |
| DELETE | `/api/sessions/{session_id}` | 删除会话及其摘要；不属于当前账号返回 403 |
| POST | `/api/knowledge/upload` | 所有登录用户可上传：`X-Filename`、`X-Scope`、`X-Department`（归属部门）、`X-Visibility`（all/department/staff/sensitive）、`X-Effective-From` / `X-Expires-At`；支持 TXT / Markdown / PDF / Word(.docx) / 图片，默认上限 10MB |
| GET | `/api/files` | 当前账号可见的资料文件库，带 `imported` / `can_import` 标记 |
| POST | `/api/files/{file_id}/import` | 把资料文件导入知识库（敏感文件 403） |

## 更新知识库

**界面方式（日常用这个）**

1. 登录后侧栏切到「知识库」（主管和新人都可以导入）
2. 填适用范围（型号 / 部门 / 业务线，留空=通用资料），点「点击选择文件」
3. 弹窗里确认归属部门、可见性、生效日期 / 失效日期，点「确认上传」
4. 上传成功后立刻参与检索，不需要重启服务

支持 TXT / Markdown / PDF / Word(.docx) / 图片（扫描件自动走 OCR），单文件默认上限 10MB。

- 内容完全相同的文件按 SHA-256 去重，直接跳过
- **同名 + 同适用范围再次上传 = 新版本**：旧版本在同一个事务里下线，不会新旧两份一起被检索到
- **同名但适用范围不同 = 两份独立资料**（例如两份都叫「说明.txt」，一份 X20 Pro、一份 T30），互不覆盖
- 只换标签或时效不用重新上传，用下面的维护命令改即可

**角色与部门规则**

| 账号 | 能导入到哪里 | 能设置哪些可见性 | 能看什么 |
| --- | --- | --- | --- |
| 部门主管（`role=staff`） | 本部门或通用 | 全员 / 本部门 / 仅主管 | 公开、本部门、本部门的主管资料；不能看其他部门 |
| 人事主管（人力资源部） | 任意部门 | 全部（含敏感） | 所有部门资料；敏感资料只能在文件库看，不进入问答 |
| 新人（`role=newcomer`） | 本部门或通用 | 全员 / 本部门 | 公开 + 自己部门 |

演示账号：客服主管 `staff / staff123`，人事主管 `hr / hr123`，客服新人 `newbie / newbie123`。

**批量 / 目录方式**

资料在一个共享目录或钉盘、飞书里时，用同步脚本（自动处理新增 / 更新 / 下线）：

```powershell
& ".\.venv\Scripts\python.exe" scripts\sync_knowledge.py --source folder --path "D:\新人资料" --scope "人力资源部"
& ".\.venv\Scripts\python.exe" scripts\sync_knowledge.py --source feishu --manifest mock/feishu/manifest.json
```

## 查看知识库

知识库内容存在 PostgreSQL 的 `rag_documents`（文件清单）和 `rag_chunks`（片段与向量）两张表里，项目目录下没有对应文件。用附带脚本查看：

```powershell
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py                     # 列出所有文件
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py --content           # 连正文一起打印
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py --file 名字.txt      # 只看某个文件
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py --set-validity 制度.txt --expires-at 2026-12-31
```

列表里的「时效」列会显示每份资料的生效区间，已过期的会标上 `[已过期]`——它们已经退出检索，不会再有用户问到时被引用。

页面目前没有删除入口，需要删除某个文件时直接执行 SQL（会连同它的片段一起删除）：

```sql
DELETE FROM rag_documents WHERE filename = '要删除的文件名';
```

## 会话历史

每个会话一个 JSON 文件，存在 `chat_history/<会话 ID>`；摘要存在 `chat_history/summaries/<会话 ID>.json`。前端侧边栏可以查看、继续和删除历史会话，刷新页面也会自动载入上次的对话。

**会话按账号隔离**：归属记录在 `chat_history/owners/<会话 ID>.json`。登录后只会列出自己的会话，读别人的会话或删除都会返回 403；未登录不返回任何历史，只能继续当前这份匿名会话。换账号登录时前端会自动开一个新会话，不会看到上一个人的记录。

早期（登录入口不明显时）产生的会话没有归属，默认对登录用户不可见。需要把老会话认领给某个账号时：

```powershell
& ".\.venv\Scripts\python.exe" scripts\assign_sessions.py --list-unowned
& ".\.venv\Scripts\python.exe" scripts\assign_sessions.py --username staff --all-unowned
```

历史压缩规则：超过 `RAG_HISTORY_SUMMARY_AFTER`（默认 10 条）消息后，只把最近 `RAG_HISTORY_KEEP_TURNS`（默认 4 轮）的原文交给模型，更早的对话累积到 `RAG_HISTORY_SUMMARY_TRIGGER`（默认 4 条）就调用 `RAG_SUMMARY_MODEL`（默认 `qwen-flash`）压缩一次，摘要会随每次压缩滚动更新。这样长对话的上下文不会无限增长，但早期信息仍然保留在摘要里。

## 工具调用

主链路是**工具调用**：模型自己决定要不要查知识库、查流程、找同事、搜钉盘、查天气、转人工，可以多轮调用后再作答（上限 `RAG_MAX_TOOL_ROUNDS`，默认 3）。目前提供六个工具：

| 工具 | 作用 | 什么时候用 |
| --- | --- | --- |
| `search_knowledge` | 检索知识库，可带 `scope`（型号 / 部门 / 业务线） | 产品、制度、流程类问题，以及不依赖实时天气的穿衣建议 |
| `lookup_process` | 查公司办事流程，返回步骤、入口和负责人 | 「XX 怎么办 / XX 流程」 |
| `find_employee` | 按事项找负责的同事 | 「XX 找谁 / XX 谁负责」 |
| `search_dingtalk_files` | 在钉盘 / 群文件里搜文件，返回发布人和日期 | 「钉盘上找…」「某某发的资料」 |
| `get_weather` | 查实时天气（Open-Meteo，免密钥） | 用户明确说了城市、且问题需要实时天气 |
| `escalate_to_human` | 建转人工工单，返回工单号 | 知识库查不到、用户要求人工、投诉或安全问题 |

转人工做了**防重复建单**：同一会话 10 分钟内已有未关闭工单时复用原工单号，避免用户连点建出一堆单子。

工具执行过程中会通过 SSE 推送 `tool` 事件，页面在等待时显示「正在检索知识库…」「正在查询实时天气…」。检索到的来源仍以 `sources` 事件推给前端。

另外 `rag.py` 里保留了 `stream_answer()`（直连检索、不调用工具）作为评测基线，用来对比两种链路的效果。

评测：

```powershell
& ".\.venv\Scripts\python.exe" scripts\evaluate.py                 # 只评测检索链路，秒级
& ".\.venv\Scripts\python.exe" scripts\evaluate.py --with-answer   # 直连检索 + 生成
& ".\.venv\Scripts\python.exe" scripts\evaluate.py --agent         # 完整工具链路，统计工具调用正确率
```

`--agent` 输出六项指标：**工具调用正确率**（是否调用期望工具、参数是否传对）、**答案要点命中率**、**无关问题拒答率**、**范围隔离正确率**、**文件检索正确率**、**平均工具调用次数与端到端耗时**。

评测集里带 `agent_only` 标记的用例只在 `--agent` 模式生效（它们看的是回答措辞和工具行为，不是检索结果）。

## 面向制造业：型号隔离

制造业客服的典型特征是「同一个问题，不同型号答案不同」。这套系统从三个层面处理：

1. **资料带适用范围**：`rag_documents` 和 `rag_chunks` 都有 `scope`（型号/部门/业务线）和 `department`（归属部门）两列。过滤必须落在片段表的真实列上，不能只放在 JSONB 里。
2. **检索按范围过滤**：指定型号时只命中「该型号 + 通用」，`is_current` 永远强制为真；该型号没有专属资料时**只退回通用资料**，绝不带出别的型号的结论。部门资料再叠加部门级权限过滤。
3. **适用范围会话记忆**：用户提过一次型号/部门就记进会话（`chat_history/scopes/<会话 ID>.txt`），后续追问不必重复说。

实测：同一个问题「滤网可以水洗吗」，X20 Pro 答「可以水洗、每 6 个月更换」，T30 答「不支持水洗、每 3 个月直接更换」；没说型号时会区分型号并反问用户。

**一份资料覆盖多个型号**：型号用逗号分隔存储（例如「DEEBOT X5 MAX, DEEBOT X5 PRO」），匹配时用包含式条件，所以用户说 X5 PRO、X5 MAX 甚至只说系列名 X5 都能命中同一份资料。

**防止贴错标签**：

- 上传时弹窗二次确认型号；如果文件名里出现型号字样、和填写的型号对不上，会给出黄色提醒。
- 维护命令 `--check` 会扫描「正文反复提到的型号和标签不符」的资料，把可疑项列出来。
- `--set-scope` 一键改标签，**不需要重新向量化**（范围只用于过滤，不参与嵌入）。

```powershell
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py --check
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py --by-scope
& ".\.venv\Scripts\python.exe" scripts\list_knowledge.py --set-scope "旧文件.pdf" --scope "DEEBOT T20"
```

## 资料时效

公司制度有年度版本、产品手册有改版，把过期资料留在库里比没有资料更危险。所以时效做成检索层的硬过滤：

1. **入库时带日期**：`rag_documents` / `rag_chunks` 都有 `effective_from` 和 `expires_at`（`date` 类型）。上传弹窗可以填，留空就是「今天生效、长期有效」（用 `9999-12-31` 表示，避免 `NULL` 参与比较时被误过滤）。
2. **过滤在片段表上**：检索条件里带 `expires_at >= 今天`，过期的资料连候选都进不了，模型不可能拿旧标准回答。
3. **两边同步**：过滤跑在 `rag_chunks`，所以改日期时文档和片段一起改，历史数据在启动迁移里自动对齐。
4. **回答里说明时效**：资料设了失效日期时，`search_knowledge` 的返回里会附上「资料时效：X 起生效，Y 失效」，提示词要求模型回答制度类问题时说明有效期。
5. **界面可见**：来源卡片显示「有效期至 2026-12-31」，过期的显示红色标记。

## 多源汇聚：数据源适配器

资料常常散在几个人的共享盘、钉钉群文件里。摄取层抽象成数据源适配器，新增来源只需实现 `list_items()`，后面的解析、分块、向量化、入库全部复用：

```text
sources/
├── base.py            # SourceItem + DataSource 接口
├── local_folder.py    # 本地目录 / 共享盘（真实可用）
├── dingtalk_drive.py  # 钉钉云盘（模拟实现）
└── feishu_docs.py     # 飞书云文档（模拟实现）
```

```powershell
& ".\.venv\Scripts\python.exe" scripts\sync_knowledge.py --source folder --path "D:\新人资料" --scope "人力资源部"
& ".\.venv\Scripts\python.exe" scripts\sync_knowledge.py --source dingtalk --manifest mock/dingtalk/manifest.json --webhook "https://oapi.dingtalk.com/robot/send?access_token=xxx"
& ".\.venv\Scripts\python.exe" scripts\sync_knowledge.py --source feishu --manifest mock/feishu/manifest.json
& ".\.venv\Scripts\python.exe" scripts\sync_knowledge.py --source folder --path "D:\2026 制度" --expires-at 2026-12-31
```

同步规则（靠 `source_key` + `is_current` 实现）：

| 情况 | 行为 |
| --- | --- |
| 来源里有、库里没有 | 新增 |
| 同一来源内容变了 | 旧版本下线、新版本入库，检索不会再命中旧版 |
| 内容没变 | 跳过，不重复向量化 |
| 来源里删掉了 | 标记为历史版本（软删除，历史仍保留） |

**关于钉钉**：群自定义机器人的 webhook **只能发不能收**——推送同步通知可以真用；要在群里接收提问，必须做企业内部应用机器人（需要企业管理员授权）。所以 `dingtalk_drive.py` 用「结构相同的本地清单 + 本地文件」模拟钉盘的文件列表与下载接口，接生产只需替换 `_load_manifest()` 和 `_download()`，同步管道不用改。

**关于飞书**：真实接入要走开放平台的三个接口——`tenant_access_token/internal` 换令牌、`drive/v1/files` 列文件、`drive/v1/files/{token}/download` 下文件；飞书云文档（docx）不是文件，要用 `docx/v1/documents/{id}/raw_content` 拉纯文本。`feishu_docs.py` 把这三个调用拆成 `_list_files()` / `_download()` / `_raw_content()` 三个方法，模拟实现读本地清单，接生产时只换这三个方法。清单里 `type=docx` 的记录走 raw_content，其余按普通文件解析（PDF/Word/图片都支持）。

## 钉盘文件检索（模拟，可切真实 API）

除了把钉盘资料同步进知识库，还可以在会话里直接找原始文件：

> 帮我在钉盘上搜索关于各部门中秋后战略安排
>
> 张伟在群里发的资料

模型会调用 `search_dingtalk_files`，返回文件名、发布人、发布时间、所在群，前端渲染成可下载的卡片；`GET /api/dingtalk/files/{file_id}/download` 负责真正下载（中文文件名用 RFC 5987 编码）。文件信息跟着这轮问答存进 `qa_turns.files`，刷新或重开会话后卡片仍然在。

**模拟数据**：`mock/dingtalk/index.json` + `mock/dingtalk/files/`，8 份文件覆盖五个部门的中秋后安排、产品说明，以及一份敏感薪资资料。搜索支持关键词、发布人、日期范围；薪资资料标了 `sensitive`，只有人事主管能在资料文件库查看和下载，且不会进入问答检索。

**切换真实钉钉**：

```powershell
# .env
DINGTALK_MODE=real
DINGTALK_APP_KEY=xxx
DINGTALK_APP_SECRET=xxx
```

`RealDingTalkFileProvider` 已经写好获取 `accessToken` 和上层接口，拿到企业应用授权后只需要补两个方法：

- `_search_files()`：调用钉盘 / 群文件搜索接口，返回原始记录列表
- `_download_file()`：按 file_id 下载文件

原始字段到内部结构的映射在 `_to_file()` 里，工具、接口、前端和评测都不用改。

**评测**：`eval_dataset.json` 里有 3 条钉盘用例（关键词、发布人、新人越权），`--agent` 会额外输出「文件检索正确率」。

## 资料文件库

页面右上角（登录入口旁）有「资料文件」按钮，打开后能看到**当前账号有权限查看的所有模拟文件**，每个文件显示发布人、发布时间、部门、所属群和可见性：

- 主管 / 新人：公开文件 + 本部门文件
- 人事主管：所有部门文件，敏感文件带「敏感」标记
- 每份文件可以「下载」；非敏感文件可以一键「导入知识库」，导入后按文件所属部门和可见性入库
- 薪资等敏感文件只给人事查看和下载，**不能导入问答知识库**，避免进入检索

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/files` | 当前账号可见的文件清单（带 `imported`、`can_import` 标记） |
| POST | `/api/files/{file_id}/import` | 把文件导入知识库；按当前账号权限收紧部门归属，敏感文件返回 403 |

## 反馈与知识盲区

只靠"知识库覆盖了多少资料"没法判断客服好不好用，所以每轮问答都留痕到 `qa_turns`，记录这轮回答的依据来自哪里（FAQ / 知识库 / 流程 / 员工卡片 / 天气 / 转人工），前端每个回答下面有赞和踩。

盲区清单的判定口径（写在 `database.list_blind_spots()` 里，方便解释"盲区到底怎么算"）：

| 口径 | 含义 |
| --- | --- |
| `evidence` 为空且没有文件附件 | 这轮既没命中 FAQ/流程/员工卡片，也没检索到资料或钉盘文件，纯靠模型常识作答，制度类问题最危险 |
| 触发转人工 | 知识库接不住，用户被转给人工 |
| 被点「没用」 | 用户明确说回答不行，可以附一句原因 |
| 因权限被拒绝 | **不计入盲区**：钉盘资料存在，只是当前账号看不到（`permission_denied`）。这类记录仍会出现在差评明细里，方便管理员核实权限配置 |

```powershell
& ".\.venv\Scripts\python.exe" scripts\review_feedback.py              # 概览 + 盲区清单
& ".\.venv\Scripts\python.exe" scripts\review_feedback.py --days 7     # 只看最近一周
& ".\.venv\Scripts\python.exe" scripts\review_feedback.py --negative   # 差评明细（含用户留言）
```

页面侧的「盲区」标签页给员工看同一份清单，补完资料后用同样的问题再问一遍，就能看到条数下降——这是"知识库运营"的闭环。

## 审计日志

知识库是共享资源，谁改了什么必须能查。这些动作会写进 `audit_logs`：

| 动作 | 触发点 |
| --- | --- |
| `auth.login` / `auth.logout` | 登录（含失败）、退出 |
| `knowledge.upload` | 上传资料，带适用范围、可见性、时效 |
| `session.delete` | 删除会话 |
| `feedback.submit` | 提交反馈 |
| `handoff.created` | 转人工建单 |
| `employee.create` | 新增员工卡片（带姓名、部门、可见性） |
| `employee.delete` | 删除员工卡片（带姓名、部门、仍引用他的流程） |
| `dingtalk.download` | 下载资料文件 |
| `file.import` | 把资料文件导入知识库（带部门、可见性） |

```powershell
& ".\.venv\Scripts\python.exe" scripts\view_audit.py                   # 最近 30 条
& ".\.venv\Scripts\python.exe" scripts\view_audit.py --action knowledge.upload
& ".\.venv\Scripts\python.exe" scripts\view_audit.py --username staff --limit 100
```

审计写入是**旁路**：`database.log_audit()` 内部吞掉异常只打警告，日志写不进去也不会让上传或问答失败。接口上员工可以在页面的「审计」标签页直接看。

## 流式输出与推理模型

回答通过 SSE 逐块推送。两个容易踩的点：

1. `ChatTongyi` 必须显式传 `streaming=True`。否则 LangChain 会判定该模型不支持流式，把整段回答一次性返回，前端看起来就是"瞬间出现"。
2. `deepseek-v4-pro` 这类推理模型会先输出思考过程再给答案，首字延迟可能长达几十秒。默认配置用 `RAG_DISABLE_THINKING=true` 关掉思考，首字约 1 秒；需要保留推理过程就设为 `false`。

回答正文里的【编号】由代码统一摘除，来源只在前端的来源卡片里展示，不在正文里重复。

## 检索参数与效果评测

检索分三步：向量相似度召回 `RAG_RETRIEVAL_K` 条候选 → `RAG_SCORE_THRESHOLD` 过滤明显无关的片段 → 重排序模型精排，保留 `RAG_RERANK_TOP_N` 条并丢弃低于 `RAG_RERANK_THRESHOLD` 的。相关度取值为 0~1，1 表示完全一致。检索为空时机器人会明确回答“没有查到相关资料”，而不是用模型自身的知识补答。

这些参数都可以写进 `.env` 覆盖。`RAG_SCORE_THRESHOLD` 默认 `0.35`，是在示例知识库上实测出来的：相关问题的向量分数在 0.36 以上，无关问题在 0.33 以下，阈值取在两者中间。知识库内容变化后建议重新标定。

重排序的效果（同一套 17 条评测集）：关闭时平均每次返回 1.88 个片段、检索耗时 183 ms；开启后降到 1.24 个片段、耗时 1542 ms，检索通过率都是 17/17。也就是说它用约 1.4 秒换掉了约三分之一的无关上下文。对延迟敏感时可以把 `RAG_RERANK_ENABLED` 设为 `false`，此时检索退回向量排序，功能不受影响。

分块策略：文件先按 Markdown 标题（Word 的标题样式会转成 Markdown 标题）或【小标题】切成章节，再在章节内切到 `RAG_CHUNK_SIZE`。每块都带「文档标题 · 章节路径」前缀参与向量化，同时按 `RAG_PARENT_CHARS` 保留更大范围的上下文交给模型，做到小块检索、大块喂模型。

```powershell
& ".\.venv\Scripts\python.exe" scripts\evaluate.py                 # 只评测检索，秒级完成
& ".\.venv\Scripts\python.exe" scripts\evaluate.py --with-answer   # 连最终答案一起评测，会消耗模型额度
```

评测集在 `eval_dataset.json`，每条包含问题、期望命中的来源文件和期望出现的要点关键词，另有应被拒答的无关问题。改动检索参数、分块策略或提示词之后跑一遍，就能看到指标是涨还是跌。检索未全部通过时脚本返回非零退出码，可以直接接 CI。

## 测试

```powershell
Set-Location "path\to\robot-rag-customer-service"
& ".\.venv\Scripts\python.exe" -m pytest
```

## 数据说明

向量、原文片段和元数据存储在 PostgreSQL 的 `rag_chunks` 表，上传去重记录存储在 `rag_documents` 表，嵌入模型和维度记录在 `rag_configuration` 表。聊天历史仍保存到本地 `chat_history/`，由 `RAG_DATA_DIR` 决定其父目录。

更换聊天模型不需要重建知识库；更换嵌入模型或维度需要重新生成知识向量，并使用独立表或数据库迁移。旧 Chroma 数据不会自动读取或删除；若已有数据，应单独备份、导出与核验后迁移。

数据库集成测试必须使用名为 `robot_rag_test` 的独立测试库，先由管理员启用 pgvector，然后设置 `DATABASE_URL` 指向测试库及 `RAG_RUN_DB_TESTS=1` 后运行 `pytest -q`。测试会清空该测试库中的知识表，拒绝在生产库上运行。未设置该标志时跳过数据库集成测试。CI 已配置独立 PostgreSQL + pgvector 服务。

生产部署与备份恢复见 [deploy/README.md](deploy/README.md)。`requirements-deployed.txt` 记录本次 Ubuntu / Python 3.10 部署实际验证的依赖版本。
