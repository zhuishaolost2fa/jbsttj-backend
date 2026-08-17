# 文件上传服务 · FastAPI + 阿里云 OSS + Supabase

支持大文件分片上传的后端服务。浏览器把文件切片后，凭后端签发的预签名地址**直传 OSS**，
文件数据不经过应用服务器；后端只负责鉴权、签名、进度记账与分片合并。

## 特性

| 能力 | 说明 |
| --- | --- |
| 分片上传 | 基于 OSS Multipart Upload，单文件最大 5 GB（可配），最多 10000 片 |
| 前端直传 | 预签名 URL 直传，应用服务器不承担上传带宽 |
| 断点续传 | 以 OSS 实际落盘的分片为准，断网/刷新后重选同一文件即可继续 |
| 秒传 | 内容指纹命中已有文件时零字节完成，多条记录共享同一对象 |
| 用户鉴权 | 校验 Supabase 签发的 JWT，兼容 HS256 密钥与 ES256/RS256 JWKS |
| 降级通道 | OSS 无法直连时可切换为服务端代理上传分片 |
| 数据隔离 | 后端每条查询强制带 user_id；数据库侧另有 RLS 策略兜底 |
| 完整性校验 | 合并前重新列举分片，缺片/ETag 不符直接拒绝并返回缺失分片号 |

## 目录结构

```
├── app/
│   ├── main.py                 # 应用入口、中间件、路由挂载
│   ├── core/
│   │   ├── config.py           # 环境变量配置
│   │   ├── security.py         # Supabase JWT 校验与身份依赖
│   │   ├── exceptions.py       # 统一异常与错误响应
│   │   └── logging.py          # 日志与 request-id
│   ├── api/v1/
│   │   ├── auth.py             # 登录/注册/身份
│   │   ├── uploads.py          # 分片上传全流程
│   │   ├── files.py            # 文件管理
│   │   ├── system.py           # 健康检查
│   │   ├── sts.py              # STS 临时凭证下发（仅 STS 接入时启用）
│   │   ├── script_options.py   # 剧本杀筛选维度字典接口
│   │   ├── scripts.py          # 剧本库接口（列表/详情/新增/修改/下架）
│   │   └── dm_guides.py        # DM 主持人手册：触发解析 / 进度 / 检索接口
│   ├── services/
│   │   ├── oss.py              # 阿里云 OSS 封装（V2 SDK: alibabacloud_oss_v2）
│   │   ├── sts.py              # STS AssumeRole 临时凭证（服务端访问 OSS 用）
│   │   ├── supabase.py         # PostgREST / GoTrue 客户端
│   │   ├── repository.py       # 数据仓储（含 ScriptOptionRepository）
│   │   ├── upload_service.py   # 分片上传编排（秒传/续传/合并）
│   │   ├── file_service.py     # 文件管理业务
│   │   ├── script_option_service.py  # 剧本杀字典业务层（TTL 缓存 + 离线种子降级）
│   │   ├── script_service.py   # 剧本库业务层（字典校验 / slug / 展示文案）
│   │   ├── pdf_extract.py      # DM 手册 PDF 结构化提取（标题/正文/页眉页脚识别）
│   │   ├── chunking.py         # 粗分块（递归切分）+ 语义细分（自研，不依赖 langchain-experimental）
│   │   ├── dedup.py            # 全局去重（精确 SHA256 + SimHash 近似）
│   │   ├── dm_store.py         # 向量库数据访问（Supabase PostgREST，同步实现供 worker 复用）
│   │   ├── llm.py              # 硅基流动 LLM 与 bge-large-zh 向量化客户端
│   │   └── dm_service.py       # DM 手册入库/检索编排层（守门 + run_in_threadpool 包装）
│   ├── schemas/                # 请求响应模型（含 script_option.py / script.py / dm_guide.py）
│   ├── data/                   # 业务种子数据（script_options_seed.py / scripts_seed.py 唯一数据源）
│   └── utils/files.py          # 文件名清洗、分片计算
├── frontend/index.html         # 分片上传演示前端（原生 JS，无依赖）
├── sql/schema.sql              # Supabase 建表 + RLS + 索引（上传/文件相关）
├── sql/script_options.sql      # 剧本杀维度字典建表 + RLS + script_option_tree 视图
├── sql/scripts.sql             # 剧本库建表 + RLS + GIN/trgm 索引 + 校验触发器 + scripts_labeled 视图
├── sql/dm_rag.sql              # DM 手册 RAG：文档/分块/问答表 + pgvector HNSW 索引 + 任务表
├── scripts/setup_oss.py        # 一键配置 OSS 跨域与碎片清理规则
├── scripts/setup_script_options.py  # 字典建表（直连 DDL）+ 灌种子数据
├── scripts/smoke_test.py       # 离线冒烟测试（上传/文件，内存假实现，无需云资源）
├── scripts/setup_scripts.py    # 剧本库建表（直连 DDL）+ 灌 35 部种子剧本
├── scripts/smoke_test_scripts.py  # 离线冒烟测试（剧本库，内存假实现，无需云资源）
├── scripts/e2e_scripts_live.py    # 剧本库真实库连通性验证（RLS/触发器/PostgREST 语法）
├── scripts/e2e_test.py         # 端到端真实联通测试（直连 OSS + Supabase）
├── scripts/verify_oss_live.py  # 仅验 OSS + STS 真实链路（无需 Supabase）
├── scripts/smoke_dm_pipeline.py   # DM 手册提取/分块离线冒烟（合成 PDF，无需云资源/队列）
├── scripts/smoke_dm_api.py        # DM 手册 4 个接口离线冒烟（打桩，无需云资源/队列）
└── run.py                      # 本地启动
```

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

### 2. 初始化数据库

在 Supabase 控制台的 **SQL Editor** 中整段执行 `sql/schema.sql`，
会创建 `upload_tasks` / `upload_parts` / `files` 三张表及索引、触发器、RLS 策略。

若需要 **DM 主持人手册 RAG** 能力，在 SQL Editor 中再执行 `sql/dm_rag.sql`，
会创建 `script_dm_documents` / `script_dm_chunks` / `script_dm_qa` 三张向量表
（`vector(1024)` + HNSW 索引）以及 `script_dm_jobs` 任务表。脚本保存后若挂了
`extra.dmGuide`（PDF），会自动触发解析流水线；也可用显式接口手动触发。

> RAG 流水线需要 **RabbitMQ**（broker）与 **Redis**（result backend + 去重指纹集合）。
> 本地快速起一套：`docker run -d --name rabbitmq -p 5672:5672 rabbitmq:management` 与
> `docker run -d --name redis -p 6379:6379 redis:7`。对应环境变量见 `.env.example` 的
> 「DM 主持人手册 RAG 流水线」一节。
> **本地联调也可以只用 Redis**（broker 同样走 Redis），见下方「本地联调」一节。

若需**剧本杀筛选维度字典**（玩法/题材/发行方式/难度/人数/时长 6 维列表接口），
在 SQL Editor 中再执行 `sql/script_options.sql` 建表，随后灌数据：

```bash
python scripts/setup_script_options.py --seed-only   # 表已存在时，仅用 service_role 灌 6 维 66 项
# 或：提供数据库直连串，一步完成建表 + 灌数
SUPABASE_DB_URL="postgresql://postgres:<密码>@db.<project>.supabase.co:5432/postgres" \
  python scripts/setup_script_options.py
```

若需**剧本库**（剧本列表 / 多维筛选 / 新增 / 修改 / 下架），剧本表依赖上面的字典表
（`scripts` 的玩法 / 题材 / 发行方式 / 难度字段引用字典 code，并有触发器校验），因此务必
**先建字典表、再建剧本表**：在 SQL Editor 中执行 `sql/scripts.sql` 建表与索引，随后灌数据：

```bash
python scripts/setup_scripts.py --seed-only   # 表已存在时，仅用 service_role 灌 35 部种子剧本
# 或：提供数据库直连串，一步完成 建剧本表 + 灌数（仍须先有字典表）
SUPABASE_DB_URL="postgresql://postgres:<密码>@db.<project>.supabase.co:5432/postgres" \
  python scripts/setup_scripts.py
# 仅校验种子数据合法性、SQL 语句切分与触发器函数体完整性（不连库、不写库）：
python scripts/setup_scripts.py --check
```
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

必填项：

- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_ANON_KEY`
- `SUPABASE_JWT_SECRET`（旧项目）或留空自动走 JWKS（新项目使用非对称密钥）
- `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT` / `OSS_REGION` / `OSS_BUCKET`
- 若用 STS 接入：额外设置 `OSS_USE_STS=true` 与 `OSS_STS_ROLE_ARN`（长期 AccessKey 即上方
  `OSS_ACCESS_KEY_ID/SECRET`，作为 STS 调用方；RAM 授权见下方「STS 临时凭证接入」一节）

### 4. 配置 OSS 跨域（前端直传的前提）

```bash
python scripts/setup_oss.py          # 应用 CORS + 分片碎片清理规则
python scripts/setup_oss.py --show   # 查看当前配置
```

脚本会把 `CORS_ORIGINS` 里的来源写入 Bucket 跨域规则，并把 `ETag` 加入 expose-headers
（**不暴露 ETag 前端就拿不到分片校验值，上传必然失败**），
同时配置「7 天自动清理未合并分片」的生命周期规则，避免碎片持续计费。

### 5. 启动

```bash
python run.py
```

- 演示前端：http://127.0.0.1:8000/
- 接口文档：http://127.0.0.1:8000/docs
- 就绪探针：http://127.0.0.1:8000/ready

### 6. 启动 DM 手册解析 Worker（RAG 流水线）

FastAPI 主进程只负责**触发**与**检索**，真正的 PDF 解析跑在 Celery worker 上。
流水线按资源画像拆成 4 个阶段、4 条独立队列，可分别扩缩容：

| 队列 | 阶段 | 瓶颈与建议并发 |
| --- | --- | --- |
| `dm.extract` | 下载 + PyMuPDF 结构化提取 + 分片 | CPU 密集，worker 数 ≈ 物理核数 |
| `dm.chunk` | 全书文本汇总 + 语义细分 + 去重 | 单任务、内存密集，并发 1~2 |
| `dm.qa` | LLM 生成问答对 | 外部 API 延迟，受配额限制 |
| `dm.embed` | bge 向量化 + 写 pgvector | 请求更轻，可比 qa 略高 |

最小可用（4 个队列各 1 个 worker，分开进程方便独立扩缩容）：

```bash
celery -A app.tasks worker -Q dm.extract -c 2 -n extract@%h
celery -A app.tasks worker -Q dm.chunk   -c 1 -n chunk@%h
celery -A app.tasks worker -Q dm.qa      -c 4 -n qa@%h
celery -A app.tasks worker -Q dm.embed   -c 6 -n embed@%h
```

> `celery -A app.tasks` 解析的是 `app/tasks/__init__.py` 暴露的 `celery_app`。
> 默认 `task_acks_late=True` + `task_reject_on_worker_lost=True`，worker 被 kill 时任务会
> 重回队列，依赖 `script_dm_chunks.content_hash` 唯一约束保证幂等，不会重复写库。
> 本地自测不想起 broker 时，设 `CELERY_EAGER=true` 让任务同步执行（仅测试用）。

### 7. 本地联调（前端 H5 → 本地后端）

配置分层：**真实环境变量（Railway 注入）> `.env.local`（本地私有，已 gitignore）> `.env`（共享基线）**。
仓库已带一份 `.env.local`，开箱即「单 Redis」联调模式，无需 RabbitMQ：

| 模式 | 依赖 | 用法 |
| --- | --- | --- |
| 零依赖自测 | 无 | `.env.local` 里 `CELERY_EAGER=true`，任务在 API 进程内同步执行（接口会被阻塞，仅调 API 用） |
| **本地联调（推荐）** | 本地 Redis（6379） | `.env.local` 默认即此模式：broker/result/去重分别走本地 Redis 的 db2/db0/db1 |
| 完整镜像线上 | docker-compose | `docker compose up -d` 起 RabbitMQ + Redis，把 `.env.local` 的 broker 改回 `amqp://dm:dm123456@127.0.0.1:5672//` |

本地联调启动步骤（两个终端）：

```bash
# 终端 1：API（热重载）
python run.py

# 终端 2：Celery worker，单进程消费全部队列，Windows 用 threads 池
python run_worker.py
```

要点：

- `.env.local` 的 `CORS_ORIGINS` 已放行前端 H5 本地源 `http://localhost:8010` / `http://127.0.0.1:8010`；
  前端侧把 API 基地址指向 `http://127.0.0.1:8000` 即可联调，改完不用部署 Railway。
- `python run_worker.py` 默认 `--pool=threads -c 4`（prefork 在 Windows 不可用）；
  可用 `LOCAL_WORKER_QUEUES` / `LOCAL_WORKER_POOL` / `LOCAL_WORKER_CONCURRENCY` 覆盖。
- Supabase / OSS / SiliconFlow 本地与线上共用同一套云端资源，数据互通 ——
  本地解析入库的手册，线上检索也能命中（注意别用脏数据污染线上库）。
- Railway 侧若挂了 Redis 插件，注入的 `REDIS_URL` 会被自动派生为
  `CELERY_RESULT_BACKEND` / `CELERY_REDIS_URL`（仅在未显式配置时生效）。

## 接口一览

所有 `/api/v1` 下的业务接口都需要 `Authorization: Bearer <access_token>`。

### 分片上传

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/uploads/init` | 初始化任务，返回分片规格；可能命中秒传或断点续传 |
| POST | `/api/v1/uploads/{task_id}/presign` | 批量签发分片直传地址 |
| POST | `/api/v1/uploads/{task_id}/parts/callback` | 批量上报分片 ETag |
| POST | `/api/v1/uploads/{task_id}/parts/{n}/callback` | 上报单个分片 ETag |
| PUT | `/api/v1/uploads/{task_id}/parts/{n}` | 服务端代理上传分片（降级） |
| GET | `/api/v1/uploads/{task_id}` | 查询进度与已上传分片 |
| POST | `/api/v1/uploads/{task_id}/complete` | 合并分片（幂等） |
| DELETE | `/api/v1/uploads/{task_id}` | 取消任务并清理 OSS 碎片 |
| GET | `/api/v1/uploads` | 我的上传任务列表 |

### 文件管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/files/simple-upload` | 小文件（≤20MB）一次性上传 |
| GET | `/api/v1/files` | 文件列表，支持关键词与分页 |
| GET | `/api/v1/files/{file_id}` | 文件详情 |
| GET | `/api/v1/files/{file_id}/download-url` | 临时下载/预览签名地址 |
| DELETE | `/api/v1/files/{file_id}` | 删除（无引用时同步删 OSS 对象） |

### 鉴权与系统

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` `/login` `/refresh` | Supabase 认证代理，便于调试 |
| GET | `/api/v1/auth/me` | 当前登录身份（含昵称、头像） |
| PATCH | `/api/v1/auth/me` | 编辑个人资料：昵称 / 头像 URL / 简介（部分更新） |
| GET | `/health` `/ready` | 存活 / 就绪探针 |

### 剧本杀筛选维度字典（公开只读）

为「剧本杀」前端筛选器提供 6 个维度的标准化选项列表：**玩法 / 题材 / 发行方式 / 难度 / 人数 / 时长**。
除 `POST /refresh` 外，下列 GET 接口均**公开可访问、无需登录**（只读字典）；
`POST /refresh` 用于刷新服务端进程内缓存，需 `Authorization: Bearer <access_token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/script-options` | 全量维度树（维度 + 选项）；可带 `?only_hot=true` 仅返回热门项 |
| GET | `/api/v1/script-options/categories` | 仅维度列表（6 个维度的元信息） |
| GET | `/api/v1/script-options/{category_code}` | 某维度的选项列表；支持 `?keyword=情感` 模糊匹配、`?only_hot=true` 仅热门 |
| POST | `/api/v1/script-options/refresh` | 刷新进程内缓存（鉴权） |

`category_code` 取值：`playstyle`（玩法）/ `theme`（题材）/ `release`（发行方式）/
`difficulty`（难度）/ `player_count`（人数）/ `duration`（时长）。

选项数据结构要点：

- `code`：稳定机器码（如 `emotion`、`boxed`、`lte_4`），前端筛选应存 `code` 而非 `label`
- `label` / `aliases`：展示名与别名（别名用于后端模糊检索兜底）
- `is_hot`：是否热门推荐项
- 区间型维度（`player_count` / `duration`）额外带 `min_value` / `max_value` / `unit`，
  可直接翻译为范围查询（如 `lte_4` → 1–4 `person`，`gt_8h` → 480–1440 `minute`）

> 数据源为 `app/data/script_options_seed.py`（唯一真相来源，共 66 项）。
> 业务层带进程内 TTL 缓存（默认 600s）与「数据库不可用时降级到内存种子」的兜底，
> 保证字典接口在 Supabase 抖动时也不会白屏。

### 剧本库（公开读 / 登录写）

剧本列表与详情**公开可访问、无需登录**；新增 / 修改 / 下架需要 `Authorization: Bearer <access_token>`。
所有筛选值都是字典维度（`/api/v1/script-options`）下发的 `code`，前端直接把 `code` 回传即可。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/scripts` | 剧本列表：关键词搜索 + 多维度筛选 + 排序 + 分页 |
| GET | `/api/v1/scripts/byname` | **按名称查找**：传 `name` 模糊匹配标题/别名，返回小驼峰剧本列表（找不到返回 `found=false`，不报 404） |
| POST | `/api/v1/scripts` | 新增剧本（鉴权）；`title` 必填，`code` 不传则按标题自动生成 |
| GET | `/api/v1/scripts/{id_or_code}` | 详情，路径参数可传 UUID 或业务编码 `code` |
| PATCH | `/api/v1/scripts/{script_id}` | 局部修改（只传要改的字段，传 `null` 即清空） |
| PUT | `/api/v1/scripts/{script_id}` | 等价于 PATCH，供习惯 PUT 的调用方 |
| DELETE | `/api/v1/scripts/{script_id}` | 下架（软删除：置 `deleted_at` + 状态 `offline`） |

列表接口 `GET /api/v1/scripts` 支持的关键查询参数：

- `keyword`：按剧本名 / 简介 / 作者 / 发行方 / 别名模糊搜索
- `playstyle` / `theme`：玩法 / 题材编码，可重复传参多选（命中任意一个即匹配）
- `release` / `difficulty`：发行方式 / 难度编码，可多选
- `players`：按人数匹配，如 `players=6` 命中「6-7人」的剧本
- `duration`：按时长匹配（分钟），如 `duration=300` 命中「4-6小时」的剧本
- `min_rating`：最低评分；`recommended_only=true`：只看推荐位
- `sort`：`hot`（热度）/ `rating`（评分）/ `newest`（最新录入）/ `year`（发行年份）/ `title`（名称）
- `limit` / `offset`：分页

> 数据源为 `app/data/scripts_seed.py`（唯一真相来源，共 35 部有据可查的剧本：谜圈标记量 TOP10、
> 2023 口碑盒装、情感本 TOP10、欢乐短本等）。写接口会校验玩法/题材/发行方式/难度编码是否真实存在，
> 传错会返回 422 并在 `details.allowed` 列出全部可选值；人数/时长区间必须成对提供。

### DM 主持人手册（RAG 检索，公开读 / 登录触发）

把 360~400 页的 DM（主持人）手册 PDF 转成可向量检索的「原文块 + 问答对」。
剧本上挂了 `extra.dmGuide`（OSS 的 PDF objectKey）后，保存剧本即**自动触发**解析；
也可用下方接口手动触发或查询进度。

**前置条件（缺任一则接口明确报错，不静默失败）**：`SILICONFLOW_API_KEY` +
`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` + `CELERY_BROKER_URL` + `CELERY_RESULT_BACKEND`。
即除已配的 Supabase 外，还需硅基流动密钥与 RabbitMQ/Redis。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/scripts/{id_or_code}/dm-guide/ingest` | 触发/复用手册解析（鉴权）。`force=true` 取消旧任务重跑 |
| GET | `/api/v1/scripts/{id_or_code}/dm-guide` | 手册索引状态（公开）：`hasGuide` / `indexed` / 进度 |
| GET | `/api/v1/scripts/{id_or_code}/dm-guide/jobs/{job_id}` | 某次解析任务进度（公开） |
| GET | `/api/v1/scripts/{id_or_code}/dm-guide/search` | 向量检索（公开）：`q` / `mode=chunk|qa|hybrid` / `topK` / `minSimilarity` / `category` |
| GET | `/api/v1/scripts/{id_or_code}/dm-guide/qa-titles` | 手册标题链（公开）：全部 QA 按标题树分组、行文顺序，含同名分片聚合 |
| GET | `/api/v1/dm-guide/qa-titles?title={剧本名}` | 按剧本杀名称直接提取标题链（公开），也可传 `code` |

标题链说明（需先执行 `sql/dm_qa_title.sql` 迁移）：

- QA 落库时把来源块的**末级章节标题**写入 `script_dm_qa.title`，完整层级仍在 `sectionPath`；
- 响应 `titles` 为根级标题节点，节点含 `title` / `path` / `qa`（本标题下的问答，行文顺序）/ `children`（子标题）；
- 无章节信息的问答归入「未分节」节点；同名分片剧本（相同 `script_code`）的 QA 聚合到同一棵树。

检索返回要点：

- `mode=hybrid` 同时查原文块与问答对，每类最多 `topK` 条；
- 原文块带 `sectionPath`（层级面包屑，如 `["第二章 搜证阶段","2.1 流程要点"]`）与 `pageStart`/`pageEnd`；
- 相似度统一保留 4 位小数；空查询或非法 `mode` 返回 422。

> 同一 PDF 重复提交会**复用**已完成的索引（按 `content_hash` 幂等 + `total_chunks` 完成标记），
> 不会重复烧 embedding / LLM 配额。

### STS 临时凭证（仅 `OSS_USE_STS=true` 时启用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/sts/token` | 为已登录用户签发浏览器直传 OSS 用的 STS 临时凭证 |

> 该接口会把**短期、可过期**的 OSS 临时凭证（AccessKeyId / Secret / SecurityToken）
> 下发给前端；长期 AccessKey 永不离开服务端。若不想把凭证下发到浏览器，
> 可不调用本接口，改用「后端 STS + 预签名 URL」模式（前端只拿预签名 PUT 地址，
> 见分片上传流程），两种模式共用同一套 STS 接入。

## STS 临时凭证接入（生产推荐）

开启 `OSS_USE_STS=true` 后，服务端用**长期 AccessKey**（`OSS_ACCESS_KEY_ID/SECRET`，
或专用的 `OSS_STS_ACCESS_KEY_ID/SECRET`）调用 STS 的 `AssumeRole` 扮演
`OSS_STS_ROLE_ARN` 指定的 RAM 角色，拿到临时凭证再去访问 OSS。长期密钥永不下发，
安全性优于直配长期密钥。

两种用法：

1. **后端 STS + 预签名 URL（默认，推荐）**：OSS 客户端使用自动刷新的临时凭证，
   前端仍只拿预签名 PUT 地址直传，无任何密钥落到浏览器。
2. **下发 STS 临时凭证给浏览器**：调用 `POST /api/v1/sts/token`，前端用 OSS 浏览器
   SDK 直传（适合需要更灵活前端逻辑的场景）。

### 前置：RAM 授权（必做，否则 AssumeRole 返回 403）

仅完成代码配置还不够，必须在本账号 RAM 控制台补齐两项授权（否则调用 STS 会报
`NoPermission: sts:AssumeRole`）：

**① 给该 RAM 用户/用户组挂权限策略，允许其扮演目标角色：**

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "acs:ram::1167188680072123:role/ossuploadrole"
    }
  ]
}
```

**② 角色 `ossuploadrole` 的信任策略（信任策略）中，允许该 RAM 用户扮演它：**

```json
{
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "RAM": ["acs:ram::1167188680072123:user/<你的RAM用户名>"] },
      "Action": "sts:AssumeRole"
    }
  ],
  "Version": "1"
}
```

**③ 角色本身还要有访问 OSS 的权限**（决定临时凭证能干什么），例如挂载
`AliyunOSSFullAccess`，或更推荐按 bucket/前缀最小化授权：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:PutObject", "oss:GetObject", "oss:DeleteObject",
                 "oss:InitiateMultipartUpload", "oss:UploadPart",
                 "oss:CompleteMultipartUpload", "oss:AbortMultipartUpload",
                 "oss:ListParts", "oss:HeadObject"],
      "Resource": ["acs:oss:*:*:your-bucket-name", "acs:oss:*:*:your-bucket-name/*"]
    }
  ]
}
```

> 验证授权是否生效：打开 `/ready` 探针（需 `OSS_BUCKET` 等也配好），或运行
> `python scripts/e2e_test.py`。授权未通过时，AssumeRole 会直接返回 403。

## 分片上传调用流程

```text
① POST /uploads/init          { filename, file_size, file_hash }
       ├─ instant=true  → 秒传完成，直接拿 file，结束
       ├─ resumed=true  → 跳过 uploaded_parts 中已完成的分片
       └─ 否则           → 全新任务，需上传 total_parts 个分片

② POST /uploads/{id}/presign  { part_numbers: [1,2,...] }
       → 返回每片的预签名 PUT 地址

③ 前端并发 PUT 分片到 OSS
       Content-Type 必须是 application/octet-stream
       从响应头读取 ETag

④ POST /uploads/{id}/parts/callback  { parts: [{part_number, etag, size}] }

⑤ POST /uploads/{id}/complete
       → 服务端重新列举 OSS 分片校验完整性后合并，返回文件信息
```

分片大小由服务端在 `init` 时返回（`chunk_size`），**前端必须严格按该值切片**。
当文件过大导致分片数超过 10000 时，服务端会自动上调分片大小。

## 关键设计说明

**为什么合并时不信任前端上报的 ETag？**
`complete` 前会重新调用 OSS `ListParts` 拿真实分片列表，前端上报的 ETag 仅作交叉校验。
这样即使前端漏报、错报或被篡改，也不会合并出损坏的文件。

**为什么 `files.object_key` 没有唯一约束？**
秒传会让多个文件记录指向同一个 OSS 对象。删除时按引用计数判断：
只有当没有任何未删除记录引用该对象时，才真正删除 OSS 上的数据。

**为什么分片的 Content-Type 固定为 `application/octet-stream`？**
底层使用阿里云 OSS V2 SDK（`alibabacloud_oss_v2`）。该 SDK 的 `presign(UploadPartRequest)`
不会把 `Content-Type` 列入签名头（返回的 `signed_headers` 为空），因此浏览器实际 PUT 时携带的
`Content-Type: application/octet-stream` 属于**未签名头**，OSS 直接忽略、不参与签名校验，
**不会**因此触发 `SignatureDoesNotMatch`。我们仍统一约定该 Content-Type 以与前端保持一致。
最终对象的真实 MIME 类型在 `InitiateMultipartUpload` 时就已确定，不受分片请求头影响。

> 注：V2 SDK 的公开 API 为同步阻塞，所有调用仍统一丢进线程池（`run_in_threadpool`），
> 行为上与旧版 `oss2` 一致，事件循环不被阻塞。

## DM 主持人手册 RAG：关键设计

**页眉页脚识别为什么不能用「全局出现比例」一刀切？**
运行页眉（running head）往往整章才出现一次，全局占比可能只有 5%，用
`dm_header_footer_ratio=0.6` 这种全局阈值会**漏掉**它们；而正文首段恰好落在页边缘带、
且在一章内密集重复（密度 ≥ 0.45）时，又被误判为页眉。所以识别是**双通道**的：
全局 `count ≥ 0.6×总页数` 或「局部分页段内密度 ≥ 0.45」任一命中即判定为版式噪声，
并叠加字号与「是否真正贴边」两个物理闸门，避免把 14pt 的二级标题当噪声删掉。

**为什么去重要「精确 + SimHash」两层？**
剧本手册大量页面是重复的固定话术（规则条款、结算口径），精确 SHA256 去重能秒杀
逐字重复；但同一规则换个措辞、或换字体导致字符宽度微变，精确哈希就失灵了。
SimHash 用汉明距离 ≤ `dm_simhash_threshold`(3) 兜底近似重复，且指纹的全局集合放在
Redis，跨 shard、跨任务共享——单 PDF 内部与多 PDF 之间都能去重。

**为什么 `DMStore` 是同步实现，FastAPI 侧还要包一层 `run_in_threadpool`？**
Celery worker 是同步进程，直接复用同一套 `DMStore` 能少维护一套 SQL；但它做的是
网络 IO（PostgREST / Supabase），若在 async 事件循环里直接调会卡住整个 worker。
所以 FastAPI 侧所有 `DMStore` 调用都走 `run_in_threadpool`，worker 侧则直接同步调用。

**BGE 指令前缀只在查询侧加**
`script_dm_chunks`/`script_dm_qa` 存的是**纯文档向量**；检索时 `LLM.aembed_query`
内部给查询文本加 `为这个句子生成用于表示问答检索的向量。` 指令前缀，让查询向量与
文档向量处于同一分布。这个不对称极易写错，统一收口在 `DMGuideService.search`，
上层无需关心。

**自动触发为什么「吞掉所有异常」？**
`scripts` 的保存接口在 `BackgroundTasks` 里挂了 `dm.maybe_trigger`。手册没配、Key
没填、队列挂了，都不该让「改个剧本简介」失败。所以 `maybe_trigger` 捕获全部异常只留
日志；需要确定性结果（成功/失败）的调用方走显式 `POST .../dm-guide/ingest`。

## 常见问题

**`SignatureDoesNotMatch`**
前端 PUT 时的 `Content-Type` 与签名时不一致。必须是 `application/octet-stream`。
另外检查服务器时间是否准确，以及 `OSS_REGION` 与 Bucket 实际地域是否匹配（V4 签名会校验地域）。

**前端读不到 ETag**
Bucket 跨域规则没有把 `ETag` 放进 expose-headers。执行 `python scripts/setup_oss.py` 修复。

**`/ready` 返回 503**
响应体的 `checks` 与 `missing_config` 会指出是配置缺失、OSS 不通还是 Supabase 不通。

**token 校验报 `jwks_unavailable`**
新版 Supabase 使用非对称密钥。确认 `SUPABASE_URL` 正确且服务器能访问 Supabase 域名；
使用旧版对称密钥的项目请直接填 `SUPABASE_JWT_SECRET`。

**大量僵尸分片占用存储**
`scripts/setup_oss.py` 已配置生命周期规则自动清理；数据库侧可用
`select public.expire_stale_upload_tasks(24);` 把超时任务标记为失败，建议配合 pg_cron 定时执行。

## 测试

项目提供两套互补的测试，覆盖不同层级：

### 离线冒烟测试（无需云资源）

用内存假实现替换 OSS 与数据库两个 IO 边界，**真实业务编排逻辑参与测试**，
不消耗任何云服务配额。验证鉴权、缺片检测、秒传、断点续传、越权隔离、引用计数删除、
危险扩展名拦截等核心逻辑（当前 27 项用例）。

```bash
python scripts/smoke_test.py
```

剧本库另有独立冒烟测试 `scripts/smoke_test_scripts.py`（30 项用例），覆盖列表 / 筛选 / 排序 / 分页、
按 code 取详情、鉴权拦截、非法编码拦截并提示可选值、自动生成 code、人数 / 时长展示文案、性别配置校验、
局部更新、传 `null` 清空、半截区间拦截、软删除下架等：

```bash
python scripts/smoke_test_scripts.py
```

剧本库还有一个**打真实 Supabase** 的连通性验证 `scripts/e2e_scripts_live.py`（45 项用例）。
它与上面的离线冒烟测试分工不同：冒烟测试验证「业务逻辑对不对」，这个脚本验证
「线上链路通不通」——建表 SQL、RLS 策略、字典校验触发器、PostgREST 过滤语法
（`ov.` 数组重叠、区间包含匹配、`ilike` 模糊搜索）与代码里的假设是否一致。

写操作走服务身份通道（`X-API-Key` + `X-User-Id`）真实鉴权，不做 mock；
脚本自建的测试剧本会在结束时硬删清理，不污染业务数据。前提是已完成建表与灌数据：

```bash
python scripts/e2e_scripts_live.py
```

DM 主持人手册另有两套离线冒烟测试，均**无需 RabbitMQ / Redis / Supabase / 硅基流动**：

- `scripts/smoke_dm_pipeline.py`：生成一本合成 DM 手册 PDF，离线跑完
  「PyMuPDF 结构化提取 → 分块 → 去重」全流程，验证页眉页脚识别、标题层级、
  运行页眉剥离等关键逻辑（合成样本刻意复现「running head == 章名」的陷阱）：

  ```bash
  python scripts/smoke_dm_pipeline.py                 # 生成 60 页样本
  python scripts/smoke_dm_pipeline.py --pages 200    # 自定义页数
  python scripts/smoke_dm_pipeline.py --pdf real.pdf # 用真实手册
  ```

- `scripts/smoke_dm_api.py`：用打桩替换 `DMStore` / LLM / MQ 派发，对 4 个
  HTTP 接口做端到端验证（首次触发 / 复用 / force 重跑 / 状态 / 进度 / 混合检索 /
  缺 `dmGuide` 报 422 等 7 个场景），验证接口契约与错误码：

  ```bash
  python scripts/smoke_dm_api.py
  ```

### 端到端真实联通测试

`scripts/e2e_test.py` 会真正连通 **阿里云 OSS** 与 **Supabase**，通过实际 HTTP 接口跑完整链路：
初始化分片 → 预签名直传地址 → 浏览器直传 OSS → 回报 ETag → 合并 → 列表 → 下载校验 → 删除，
并额外覆盖**鉴权拒绝**、**秒传**、**断点续传**三条分支。

鉴权使用「服务间调用通道」`X-API-Key + X-User-Id`（见 `app/core/security.py`），
免去申请一张真实 Supabase JWT 的麻烦。

```bash
# 1) 先确保 .env 已填好 Supabase / OSS 配置，并设置一个 SERVICE_API_KEY
# 2) 建表：Supabase SQL Editor 执行 sql/schema.sql
# 3) 配置 OSS 跨域：python scripts/setup_oss.py
python scripts/e2e_test.py            # 完整联通测试
python scripts/e2e_test.py --config-only   # 仅自检配置是否齐全
```

配置缺失时脚本会列出具体缺项并以退出码 2 退出；连通性探测失败时以退出码 3 退出；
任意用例失败退出码 1。所有用例通过退出码 0。

### 仅验证 OSS + STS 层（无需 Supabase）

如果你只配好了阿里云、尚未搭建 Supabase，可用 `verify_oss_live.py` 单独验证
OSS 分片上传的真实云链路（init → 预签名直传 → list_parts → complete → 下载校验 → 删除），
它用真实 STS 临时凭证操作 `OSS_BUCKET`：

```bash
python scripts/verify_oss_live.py
```

> 注意：该脚本**强制走公网 endpoint**（`.env` 中的 `OSS_INTERNAL_ENDPOINT` 会被忽略）。
> 内网 endpoint 仅同地域 ECS 可达；应用部署到 ECS 时由 `OSS_INTERNAL_ENDPOINT` 控制服务端直连走内网。

## 生产部署建议

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

- 收紧 `CORS_ORIGINS`，不要使用 `*`
- 服务部署在与 Bucket 同地域的 ECS 时，填写 `OSS_INTERNAL_ENDPOINT` 走内网，省流量费
- `SUPABASE_SERVICE_ROLE_KEY` 只能存在于服务端，绝不可下发到前端
- 建议为下载链路绑定 CDN 域名（`OSS_CDN_DOMAIN`）
- 反向代理需放行 `PUT` 方法并调大 `client_max_body_size`（仅代理上传模式需要）
