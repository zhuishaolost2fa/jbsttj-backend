# Railway 上线步骤清单（jbsttj-backend 后端）

> 本清单与项目现有文件严格对齐：`Dockerfile`（FastAPI API + 4 个 Celery worker，单镜像由 supervisord 编排）、`railway.json`、`supervisord.conf`、`.dockerignore`（已排除 `.env`，**镜像内不含任何密钥**）。
>
> 部署目标：把"后端 API + 4 个队列 worker"托管到 Railway；Celery 队列（broker / backend / 去重）统一用 Railway 自带 Redis；Supabase / 阿里云 OSS·OCR / SiliconFlow 都是远程服务，直接注入密钥即可。

---

## 前置条件（你已具备）

- [x] 代码已推到 GitHub（Railway 从此构建 Docker 镜像）。
- [x] Supabase 四张表 + 匹配函数已建好（函数已改为 `volatile`，已跑通）。
- [x] 阿里云 OCR 服务已开通、RAM 用户已授权。
- [x] SiliconFlow 有效 Key（当前 `.env` 里的 `sk-phg…`）。
- [x] GitHub 仓库根目录已有 `Dockerfile` / `railway.json` / `supervisord.conf`。

---

## 第一步：准备仓库（确认无遗漏）

1. 确认 `Dockerfile`、`railway.json`、`supervisord.conf` 已在仓库根目录。
2. 确认 `.dockerignore` 包含 `.env`（**绝不能把本地密钥带进镜像**）。当前已包含，无需改。
3. 推送最新代码到 `main`（或你指定的分支）。

> 无需提交 `.env`。Railway 通过环境变量注入所有密钥。

---

## 第二步：在 Railway 创建项目并选择构建方式

1. 登录 [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**。
2. 选择本仓库。
3. Railway 会自动读 `railway.json`：
   - `build.builder = DOCKERFILE` → 用仓库根 `Dockerfile` 构建。
   - `deploy.startCommand = supervisord -c /app/supervisord.conf` → 镜像启动即拉起 API + 4 worker。
   - `healthcheckPath = /ready` → 对应代码里 `GET /api/v1/system/ready`（已存在）。
4. 第一次部署会先失败（因为还没配环境变量 / broker）——属正常，进入第三步补环境。

---

## 第三步：添加 Redis add-on（Celery broker + backend + 去重都用它）

Railway 自带的 Add-on 里**没有 CloudAMQP / RabbitMQ**，而 Celery 支持用 Redis 做消息队列，所以本方案统一用 Redis：

- **broker**：Celery 收发任务（Redis 库 0）
- **result_backend**：任务结果存储（Redis 库 2）
- **dedup**：去重集合（Redis 库 1）

### 3.1 创建 Redis
1. Railway 项目内 **New → Add-on → Redis**（在 Database 分类下，你截图里能直接看到 Redis）。
2. 选 plan 后创建。
3. 创建后 Redis service 自身会生成变量 `REDIS_URL`，**不会自动同步到你的 `jbsttj-backend` service**，需要下一步手动做 Variable Reference。

### 3.2 把 `REDIS_URL` 透传给后端 service（关键）

Redis 的变量默认只属于 Redis 服务，你的后端读不到。必须做这一步：

**推荐：Variable Reference（Redis 密码/地址变了会自动同步）**
1. 进入 Redis service 面板 → 上方 **Variables** 标签。
2. 看到紫色提示条 "Trying to connect this database to a service? Add a Variable Reference" → 点击 **Variable Reference**。
3. 目标 service 选择 **`jbsttj-backend`**。
4. 完成后，`jbsttj-backend` 的 Variables 里会出现一个 `REDIS_URL`。

**备选：手动复制**
- 在 Redis 面板点 `REDIS_URL` 旁的眼睛图标显示完整值，复制后去 `jbsttj-backend` → Variables 里新建同名变量。缺点：Redis 重置密码/迁移后需手动更新。

### 3.3 为什么不单独搞 RabbitMQ
- Railway 官方 Add-on 没有 RabbitMQ，CloudAMQP 在很多账号/区域已不可见（你截图里 Database 列表就没有）。
- Redis 作为 broker 对本项目完全够用，且 Railway Redis 是长连接友好型，比 Upstash serverless Redis 更适合 Celery worker。

---

## 第四步：配置环境变量（核心）

在 Railway 项目 **Variables** 里逐条添加。**不要**填 `PORT`（Railway 自动注入）。

### A. 直接复制本地 `.env` 的真实值（同一份，远程服务通用）
| 变量 | 取值来源 | 备注 |
|---|---|---|
| `SUPABASE_URL` | 本地 `.env` | 远程 Supabase，不变 |
| `SUPABASE_SERVICE_ROLE_KEY` | 本地 `.env` | **严禁泄露到前端** |
| `SUPABASE_ANON_KEY` | 本地 `.env` | |
| `SUPABASE_JWT_SECRET` | 本地 `.env` | 或留空走 JWKS |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT` / `OSS_REGION` / `OSS_BUCKET` / `OSS_SIGNATURE_VERSION` | 本地 `.env` | 同账号 |
| `SILICONFLOW_API_KEY` | 本地 `.env`（新有效 key） | chat + embedding 共用 |
| `SILICONFLOW_CHAT_MODEL` | `Qwen/Qwen2.5-72B-Instruct` | |
| `SILICONFLOW_EMBED_MODEL` | `BAAI/bge-large-zh-v1.5` | 改模型须同步改 SQL 的 `vector(N)` |
| `SILICONFLOW_BASE_URL` | `https://api.siliconflow.cn/v1` | |
| `EMBEDDING_MAX_CHARS` | `480` | 已修的截断，必带 |
| `OCR_ENDPOINT` / `OCR_TYPE` / `OCR_DPI` | 本地 `.env`（或留空复用 OSS key） | 复用同账号 AccessKey |
| `DM_*` 解析参数 | 本地 `.env` | 一般保持默认 |

### B. 必须手工改写 / 新增（上线关键）
| 变量 | 取值 | 说明 |
|---|---|---|
| `APP_ENV` | `production` | |
| `DEBUG` | `false` | |
| `CELERY_EAGER` | **`false`** | ★**上线最重要的一项**。本地是 `true`（同步跑免 broker）；上 Railway 必须改 `false`，否则 4 个 worker 形同虚设，任务退回 web 进程同步执行。 |
| `CELERY_BROKER_URL` | `${{REDIS_URL}}/0` | ★不能用 `127.0.0.1`；用 Railway Redis 库 0 当 broker |
| `CELERY_RESULT_BACKEND` | `${{REDIS_URL}}/2` | ★用 Railway Redis 库 2 存任务结果 |
| `CELERY_REDIS_URL` | `${{REDIS_URL}}/1` | ★用 Railway Redis 库 1 做去重集合 |
| `CORS_ORIGINS` | `https://你的vercel域名,http://localhost:5173` | ★必须加 Vercel 前端域名，否则浏览器跨域被拦；生产不要用 `*` |

> 变量名全部来自 `app/core/config.py` 与 `.env.example`，一一对应，无需改代码。

---

## 第五步：重新部署并观察健康检查

1. 保存变量后 Railway 会自动重新部署。
2. 部署日志里应能看到 supervisord 依次拉起：`web`、`worker_extract`、`worker_chunk`、`worker_qa`、`worker_embed`。
3. Railway 周期请求 `/ready`：
   - 返回 `status=ready` → 部署成功。
   - 返回 `not_ready` 且 `missing_config` 非空 → 对照第四步补齐缺失变量。

> **坑②（如 web 起不来 / 连不上端口）**：`supervisord.conf` 的 `web` 用 `sh -c "... --port ${PORT:-8000}"`，依赖 supervisord 继承 Railway 注入的 `PORT`。绝大多数情况下正常；若遇端口为空，可在 `[program:web]` 增一行 `environment=PORT="%(ENV_PORT)s"` 强制透传，再重部署。

---

## 第六步：上线后验证（确认 worker 真的在消费）

部署成功 ≠ worker 链路通。做一次真实入库验证：

**方式 A（推荐，最真）：** 通过 API 触发
```
POST /api/v1/dm/guides/{code}/ingest   # 例如 code=dm-bingjiao-nanhai
```
然后在 Railway 日志看 4 个 worker 是否依次消费 `dm.extract → dm.chunk → dm.qa → dm.embed` 队列。

**方式 B：** 本地把 `.env` 的 `CELERY_EAGER` 临时改 `false`、`CELERY_BROKER_URL` 指向 Railway Redis（`${{REDIS_URL}}/0`），再跑 `scripts/drive_dm_ingest.py --force`——任务会发到云端队列，由 Railway 上的 worker 消费。验证完记得把本地 `.env` 改回 `true` 不影响。

验证通过标志：Supabase 的 `dm_chunks` / `dm_qa_pairs` 新增行，且检索接口能召回。

---

## 第七步：Vercel 前端对接（简述）

1. Vercel 部署 `frontend/` 静态站。
2. 前端请求基地址填 Railway 给的 `https://xxx.up.railway.app`。
3. 该域名已加入后端 `CORS_ORIGINS`（第四步 B）。
4. 前端只调后端 API，不直接连 Supabase 写库（写库走 service_role，只在后端）。

---

## 常见坑速查

- **坑⚠ 部署后 worker 日志报 `Connection refused ... amqp://guest:**@127.0.0.1:5672//`**：说明 worker 仍在用 `config.py` 的默认值，即 `CELERY_BROKER_URL` **未生效**（最常见原因：漏设、名字拼错成 `BROKER_URL`/`CELERY_BROKER`、或设到了别的 service）。
  - 修法：Railway 控制台 → 你的项目 → **Variables → New Variable**，Name 必须精确是 `CELERY_BROKER_URL`，Value 填 `${{REDIS_URL}}/0`（指向 Railway Redis 库 0）。保存后 Railway 自动重部署。
  - 如果日志里变成连 Redis 失败（不再是 amqp 报错），说明 broker 变量已被读到，只是 Redis URL 不对，检查 `REDIS_URL` 是否已注入。
  - supervisord 默认会继承容器环境，变量设对就能被 worker 读到；**不要**去改 `supervisord.conf` 显式透传这三个变量——变量万一缺失时 `%(ENV_X)s` 无法展开会导致 supervisord 启动失败，整容器挂掉。
  - 同样要确认 `CELERY_RESULT_BACKEND=${{REDIS_URL}}/2` / `CELERY_REDIS_URL=${{REDIS_URL}}/1` 也已设置。
- **坑① Redis 连接数**：Railway Redis 默认对连接数有限制，本服务共 5 个常驻连接（web×1 + 4 worker）。如果部署后 worker 频繁断开，先升级到更高 plan；或临时把 `supervisord.conf` 里部分 worker 数量调低。
- **坑② `CELERY_EAGER` 忘了改 false**：worker 不干活，任务全堆在 web 进程，部署看似成功但队列不动。
- **坑③ broker 仍写 `127.0.0.1`**：Railway 上连不上，worker 启动即报错退出。
- **坑④ CORS 没加 Vercel 域名**：前端能打开但请求被浏览器拦截。
- **坑⑤ 镜像带进 `.env`**：违反 `.dockerignore` 约定，密钥泄露风险；当前已排除，勿手动 COPY。
- **坑⑥ 改了 embed 模型名**：`BAAI/bge-large-zh-v1.5` 固定 1024 维，若换模型须同步改 `sql/dm_rag.sql` 的 `vector(N)` 与已建索引。
- **坑⑦ `/ready` 返回 503 且报 `EntityNotExist.Role` / STS AssumeRole 失败**：`OSS_USE_STS=true` 会强制走 STS 临时凭证，需 `AssumeRole` 一个 RAM 角色；若该角色（`OSS_STS_ROLE_ARN`，如 `acs:ram::<uid>:role/ossuploadrole`）在你的阿里云账号里不存在就报 404。后端自身上传用长期 AccessKey 即可，无需 STS → **在 Variables 设 `OSS_USE_STS=false`**（OSS 改走 `StaticCredentialsProvider` 用 `OSS_ACCESS_KEY_ID/SECRET`）。`OSS_STS_*` 变量可保留（被忽略）。仅当未来前端要"浏览器直传 OSS"时才需真正创建该 RAM 角色并挂 OSS 权限。

---

## 根据你的截图状态：必须修改的变量清单

你的 Railway Variables 目前大量还是 `.env.example` 里的占位符（如 `your-project-ref.supabase.co`、`your-oss-access-key-secret`、`your-bucket-name`、`eyJhbGciOi..anon..`），且缺了 Celery 的三个关键变量。必须按下面四组改：

### A. 必须改成真实值（从本地 `.env` 复制）
| 变量名 | 当前问题 | 改法 |
|---|---|---|
| `SUPABASE_URL` | 示例值 `https://your-project-ref.supabase.co` | 改为本地 `.env` 的真实 Supabase URL |
| `SUPABASE_ANON_KEY` | 示例值 `eyJhbGciOi..anon..` | 改为本地 `.env` 的真实 anon key |
| `SUPABASE_SERVICE_ROLE_KEY` | 示例值 `eyJhbGciOi..service_role..` | 改为本地 `.env` 的真实 service_role key |
| `SUPABASE_JWT_SECRET` | 示例值 `your-super-secret...` | 改为本地 `.env` 的真实 JWT secret；如果用 JWKS 可留空此字段并填 `SUPABASE_JWKS_URL` |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT` / `OSS_REGION` / `OSS_BUCKET` | 部分仍是占位符 | 全部改为本地 `.env` 的真实值 |
| `SILICONFLOW_API_KEY` / `SILICONFLOW_CHAT_MODEL` / `SILICONFLOW_EMBED_MODEL` / `SILICONFLOW_BASE_URL` | 截图里还没出现 | 新增；从本地 `.env` 复制（`sk-phg…` 那个 key） |

### B. 必须改为 add-on 提供的值（不能从本地 `.env` 直接复制）
| 变量名 | 来源 | 注意 |
|---|---|---|
| `CELERY_BROKER_URL` | Railway Redis add-on 的 `REDIS_URL`，末尾加 `/0` | 本地 `.env` 是 `127.0.0.1`，**不能直接复制** |
| `CELERY_RESULT_BACKEND` | Railway Redis add-on 的 `REDIS_URL`，末尾加 `/2` | 任务结果存库 2 |
| `CELERY_REDIS_URL` | Railway Redis add-on 的 `REDIS_URL`，末尾加 `/1` | 去重集合用库 1 |

### C. 建议立即改（生产行为）
- `APP_ENV`：`development` → `production`
- `DEBUG`：`true` → `false`
- `CORS_ORIGINS`：在现有 localhost 后面追加你的 Vercel 域名，例如：
  `http://localhost:5173,http://localhost:3000,http://127.0.0.1:8000,https://你的项目.vercel.app`

### D. 缺失但建议新增的 RAG 相关变量
截图里还没出现，建议从本地 `.env` 一起新增：
- `EMBEDDING_MAX_CHARS=480`
- `EMBEDDING_BATCH_SIZE=32`
- `LLM_MAX_CONCURRENCY=4`
- `CELERY_EAGER=false` ★ 否则 worker 不干活
- `CELERY_TASK_SOFT_TIME_LIMIT=1500`
- `CELERY_TASK_TIME_LIMIT=1800`
- `CELERY_WORKER_PREFETCH=1`
- `OCR_ENDPOINT=ocr-api.cn-hangzhou.aliyuncs.com`
- `OCR_TYPE=General`
- `OCR_DPI=130`
- 各种 `DM_*` 参数（可保留默认值）

> 不要直接上传本地 `.env` 到 Railway。本地 broker/redis 指向 `127.0.0.1`，上云必须指向 Railway Redis。推荐用 Railway Variables 的 **Raw Editor** 批量粘贴，但记得把 broker/backend/dedup 三处 `127.0.0.1` 手动换成 `${{REDIS_URL}}/N`。

## 当前本地状态备忘

- 本地 `.env` 仍是 `CELERY_EAGER=true`（免 broker 同步模式，便于本地验证）。**上线 Railway 前在 Railway 变量里设 `false` 即可，不必改本地文件。**
- 检索已优化为"qa 为主"（hybrid 下 qa 满额、chunk 补充），相关配置 `DM_SEARCH_TOP_K` / `DM_SEARCH_MIN_SIMILARITY` / `DM_SEARCH_QA_SUPPLEMENT_K` / `DM_SEARCH_QA_BOOST` 已在 `config.py`。`
