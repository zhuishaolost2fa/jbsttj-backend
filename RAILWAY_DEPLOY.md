# Railway 上线步骤清单（jbsttj-backend 后端）

> 本清单与项目现有文件严格对齐：`Dockerfile`（FastAPI API + 4 个 Celery worker，单镜像由 supervisord 编排）、`railway.json`、`supervisord.conf`、`.dockerignore`（已排除 `.env`，**镜像内不含任何密钥**）。
>
> 部署目标：把"后端 API + 4 个队列 worker"托管到 Railway；RabbitMQ 与 Redis 用外部 add-on；Supabase / 阿里云 OSS·OCR / SiliconFlow 都是远程服务，直接注入密钥即可。

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

## 第三步：添加两个 add-on（RabbitMQ + Redis）

本镜像内 **不打包** RabbitMQ/Redis，需外部托管。

### 3.1 RabbitMQ → 用 CloudAMQP（第三方 add-on）
1. Railway 项目内 **New → Add-on → CloudAMQP**。
2. 选 plan（见下方"坑①"关于免费版连接数限制）。
3. 创建后，在 CloudAMQP 的 Variables / Connection 里拿到 `CLOUDAMQP_URL`（形如 `amqp://user:pass@lionfish.rmq.cloudamqp.com/instancename`）。

### 3.2 Redis → 用 Railway 自带 Redis（推荐，长连接友好）
1. Railway 项目内 **New → Add-on → Redis**（Railway 官方 Redis，比 Upstash serverless 更适合 Celery 长连接）。
2. 创建后在 Redis 服务的 Variables 里拿到 `REDIS_URL`（形如 `redis://default:password@containers-us-east-1.railway.app:6379`）。

> 若改用 Upstash，注意它是 serverless Redis，Celery worker 保持长连接可能触发连接数/命令限制，生产不推荐。

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
| `CELERY_BROKER_URL` | CloudAMQP 的 `CLOUDAMQP_URL` | ★不能用 `127.0.0.1` |
| `CELERY_RESULT_BACKEND` | Railway Redis 的 `REDIS_URL`（库 0） | ★ |
| `CELERY_REDIS_URL` | 同 `REDIS_URL`（库 1，去重集合） | ★可改写为 `redis://.../1` |
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

**方式 B：** 本地把 `.env` 的 `CELERY_EAGER` 临时改 `false`、`CELERY_BROKER_URL` 指向 CloudAMQP，再跑 `scripts/drive_dm_ingest.py --force`——任务会发到云端队列，由 Railway 上的 worker 消费。验证完记得把本地 `.env` 改回 `true` 不影响。

验证通过标志：Supabase 的 `dm_chunks` / `dm_qa_pairs` 新增行，且检索接口能召回。

---

## 第七步：Vercel 前端对接（简述）

1. Vercel 部署 `frontend/` 静态站。
2. 前端请求基地址填 Railway 给的 `https://xxx.up.railway.app`。
3. 该域名已加入后端 `CORS_ORIGINS`（第四步 B）。
4. 前端只调后端 API，不直接连 Supabase 写库（写库走 service_role，只在后端）。

---

## 常见坑速查

- **坑① CloudAMQP 免费版连接数**：Little Lemur 免费档并发连接受限；本服务共 5 个连接（web×1 + 4 worker）。免费档可能不够 → 升级 CloudAMQP plan，或把 `supervisord.conf` 里部分 worker 并发/数量调低。
- **坑② `CELERY_EAGER` 忘了改 false**：worker 不干活，任务全堆在 web 进程，部署看似成功但队列不动。
- **坑③ broker 仍写 `127.0.0.1`**：Railway 上连不上，worker 启动即报错退出。
- **坑④ CORS 没加 Vercel 域名**：前端能打开但请求被浏览器拦截。
- **坑⑤ 镜像带进 `.env`**：违反 `.dockerignore` 约定，密钥泄露风险；当前已排除，勿手动 COPY。
- **坑⑥ 改了 embed 模型名**：`BAAI/bge-large-zh-v1.5` 固定 1024 维，若换模型须同步改 `sql/dm_rag.sql` 的 `vector(N)` 与已建索引。

---

## 当前本地状态备忘

- 本地 `.env` 仍是 `CELERY_EAGER=true`（免 broker 同步模式，便于本地验证）。**上线 Railway 前在 Railway 变量里设 `false` 即可，不必改本地文件。**
- 检索已优化为"qa 为主"（hybrid 下 qa 满额、chunk 补充），相关配置 `DM_SEARCH_TOP_K` / `DM_SEARCH_MIN_SIMILARITY` / `DM_SEARCH_QA_SUPPLEMENT_K` / `DM_SEARCH_QA_BOOST` 已在 `config.py`。
