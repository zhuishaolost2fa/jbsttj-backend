# 从 Railway 迁移到国内轻量服务器

## 0. 先搞清楚：其实没什么要迁的

Railway 只是「跑你代码的地方」，**业务数据一件都不在它上面**：

| 数据 | 在哪 | 要迁吗 |
|---|---|---|
| 用户 / profiles / 微信绑定 | Supabase | ❌ 共享同一个项目 |
| DM 手册 chunks、QA、向量 | Supabase `script_dm_*` | ❌ 共享 |
| 上传的 PDF / 头像 | 阿里云 OSS `jbs-store` | ❌ 共享同一个 bucket |
| 第三方密钥 | Railway 环境变量面板 | ✅ **要搬到服务器 `.env`** |
| Celery 队列消息、result 缓存 | Railway 的 RabbitMQ / Redis | ❌ 临时状态，丢了顶多重跑 |
| PDF 解析缓存 | 容器临时目录（`DM_CACHE_DIR` 留空） | ❌ 无持久文件 |

**所以这次迁移的本质是「换一个地方跑计算」，不是搬数据。**
真正要花心思的只有两件事：**环境变量** 和 **前端怎么连过来**。

---

## 1. 两个必须先解决的拦路虎

### 拦路虎 A：混合内容（最容易让迁移卡住）

你的前端在 Vercel（`https://www.jbs-ttj.store`），后端是 `http://<IP>:8000`。
**HTTPS 页面里 fetch HTTP 接口会被浏览器直接拦截**，切过去就白屏。

三个解法：

| 方案 | 做法 | 代价 |
|---|---|---|
| **① Nginx 同源反代**（推荐，配置已就绪） | 前端 H5 也放这台机器，Nginx 监听 80：`/` 走静态、`/api/` 反代到 api:8000 | URL 带 IP（http://<IP>），不好看；不能备案前用域名 |
| ② Vercel rewrites 反代 | `vercel.json` 加一条把 `/api/*` 代理到 `http://<IP>:8000` | Vercel 出口在海外，**跨国绕一圈，国内速度优势全没了** |
| ③ 备案 + 域名 + Caddy | 最正统，全自动 HTTPS | 备案要 10~20 天，且需要已实名域名 |

> 方案 ② 看似最省事，但它会让用户请求绕：浏览器 → Vercel（海外）→ 国内服务器 → 原路返回。
> 延迟可能到 200~500ms，等于白买国内机器。只建议作为临时过渡，别当终态。

方案 ① 的 Nginx 配置已经写好了（`deploy/nginx.conf`，compose 里 `nginx` 服务默认启用）：

```bash
# 服务器上
cd /opt/jbs
cp nginx.conf ./nginx.conf                  # 从仓库 deploy/ 目录拷过来
# 把 Taro H5 的 build 产物（dist/h5/* 的内容）放到 ./frontend/
docker compose -f docker-compose.prod.yml up -d nginx
```

访问 `http://<IP>`。前端 `API_ORIGIN` 填 `http://<IP>`（同源，无 CORS），
如果能配相对路径 `/api/v1` 更好——换 IP 时不用重新 build。

注意：加了 Nginx 之后 API 的 8000 端口只绑 `127.0.0.1`，**防火墙只需放行 22 和 80**（80 轻量机模板默认已放行）。

### 拦路虎 B：切换期双活

好消息：**Railway 和自建用的是各自独立的 broker，不会重复消费同一条消息**。

坏消息：两边共享同一个 Supabase。如果同一本手册在两边同时被处理，
`script_dm_chunks` / `script_dm_qa` 会**重复写入**。

所以切换顺序必须是：**先停 Railway 的 worker，再起自建的 worker**，不要并行跑。

---

## 2. 迁移步骤（每步都可回滚）

### 阶段 0：术前检查（5 分钟）

1. **确认 Railway 上没有跑一半的任务**。
   去 Supabase 面板 → Table Editor → `script_dm_jobs`，
   看有没有 `status` 既不是 `completed` 也不是 `failed` 的记录。
   有就等它跑完，或者记下 id 准备迁移后用 `force=true` 重跑。

2. **导出 Railway 的环境变量**。
   Railway 项目 → 服务 → Variables → 全选复制下来。这是你 `.env` 的原料。

### 阶段 1：起自建环境（先不切流量）

按 `deploy-cn.md` 第 2~5 节做完初始化、swap、`.env`、起四件套。

`.env` 里这几项**必须改**（其余照搬 Railway）：

```bash
APP_ENV=production
DEBUG=false
HOST=0.0.0.0
# 生产别用 *，按你的前端实际域名/IP 填
CORS_ORIGINS=http://<IP>,https://www.jbs-ttj.store

# ⚠️ 把 Railway 注入的这行清空，否则 config 会拿它去派生 Redis 地址
REDIS_URL=
```

`CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` / `CELERY_REDIS_URL` **不用改** ——
compose 里用 `environment:` 硬覆盖成容器间服务名了，优先级高于 `env_file`。

起来后先**只在本机验证**，别急着切流量：

```bash
# 服务器上
curl http://127.0.0.1:8000/ready
docker compose -f docker-compose.prod.yml ps   # 四个都应 healthy/running
```

### 阶段 2：停 Railway 的 worker（关键顺序）

Railway 上 API 和 worker 在同一个容器里跑（supervisord），所以直接**把整个服务停掉**最简单：
Railway 控制台 → 服务 → Settings → 把 replicas 调到 0，或直接 Remove Deployments 暂停。

> 别只想着「停 worker 保留 API」：supervisord 里两个 program 都是 `autorestart=true`，
> 单独杀进程会被拉起来。停整个服务更干净。

**确认 Railway 已不再消费**：Supabase 里看 `script_dm_jobs` 有没有新进度。

### 阶段 3：切前端流量

按你在拦路虎 A 里选的方案改前端的 `API_ORIGIN`（Taro 里通常是 `TARO_APP_API_ORIGIN` 之类），重新 build 部署。

改完立刻验证：

```bash
# 服务器上能看到请求进来才算通
docker compose -f docker-compose.prod.yml logs -f api
```

### 阶段 4：观察 & 下线

- 跑一次完整的 DM 手册导入，确认流水线四个阶段都过（`extracting → chunking → qa → embedding → completed`）
- 连续观察 1~2 天没问题，再回 Railway 删除服务（**别急着删，留着当回滚后路**）

---

## 3. 验证清单

```bash
# ① API 存活
curl http://127.0.0.1:8000/ready

# ② worker 在消费（应看到 5 条队列的 ready 状态）
docker compose -f docker-compose.prod.yml exec rabbitmq rabbitmqctl list_queues

# ③ worker 进程健康
docker compose -f docker-compose.prod.yml exec worker celery -A app.tasks inspect ping

# ④ 内存是否扛得住
free -h && docker stats --no-stream

# ⑤ 日志无异常
docker compose -f docker-compose.prod.yml logs --tail=100 api worker
```

---

## 4. 回滚

迁移过程中任何一步出问题，**把前端 `API_ORIGIN` 改回 Railway 地址、重新部署即可**，
数据一点都没动过（都在 Supabase / OSS）。这也是为什么阶段 4 才让你删 Railway。

如果是自建环境本身有问题，改代码 → push → 走 `deploy-cn.yml` 重新部署，
或直接用 `.last_good_image` 回滚（见 `deploy-cn.md` 8.1）。

---

## 4.5 让接口尽可能快（别只盯着服务器位置）

换到国内机器只解决了「计算在哪跑」，**真正的大头常常在数据库往返**。

### 实测：Supabase 的连接开销

从国内访问 `*.supabase.co`（2026-09-08 实测）：

```
DNS解析:0.105s  TCP连接:0.313s  TLS握手:0.739s  首字节:1.106s
```

**TLS 握手 0.74s 意味着 RTT 约 250ms 量级** —— 源站大概率在境外（Supabase 没有中国大陆节点）。
一个业务请求只要查 3 次库，光网络往返就是 0.7s 以上，这部分换国内服务器**一分钱都省不掉**。

### 已经修好的：鉴权链路每次都重新握手

`app/services/supabase.py` 里 `SupabaseAuth` 的 10 个方法（`sign_in` / `refresh` /
`verify_password` / `admin_*` / `generate_link` / `verify_link` / OTP 系列）原本都写成：

```python
async with httpx.AsyncClient(timeout=15.0) as client:   # ❌ 每次新建 TCP + TLS
    resp = await client.post(url, ...)
```

也就是说**每次调用都要付一遍 0.74s 的 TLS 握手**，登录和刷新 token 尤其明显。
已改成复用带 keep-alive 的连接池（`SupabaseAuth.auth_client`），只在首次握手：

```python
resp = await self.auth_client.post(url, ...)            # ✅ 复用连接
```

语义完全没变（传的都是绝对 URL、请求级 headers 覆盖 client 级、`_post` 的 5s 超时单独保留）。

### 还有哪些能做的

| 手段 | 收益 | 说明 |
|---|---|---|
| 用 Supavisor 连接池 | 中 | Supabase 自带，减少建连开销 |
| 查库改批量 / 并行 | 高 | 串行的 N 次往返合并成 1 次，收益最直接 |
| 迁移 Supabase 区域到东京 | 小~中 | 比新加坡近约 30~50ms，但仍是跨境 |
| 上备案域名 + HTTPS | 无（纯体验） | 解决浏览器警告，不改善延迟 |

> 最务实的判断：**先测，再优化**。别凭感觉改。
> 在服务器上跑一次 `curl -w "%{time_starttransfer}"` 打你的实际接口，
> 对比「只查一次库」和「查三次库」的耗时，就知道瓶颈在 SQL 还是在网络。

## 5. 迁移后别忘了

- 防火墙只放行 `22` 和 `8000`（前端搬到本机也不需要额外加端口）
- 控制台开**流量预警**（80% / 90% 短信），超额是 0.8 元/GB 实时扣
- Supabase 的 `SILICONFLOW_API_KEY` 等密钥从 Railway 下线后，记得在 Railway 侧删掉
