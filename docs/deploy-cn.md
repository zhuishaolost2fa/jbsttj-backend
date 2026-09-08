# 国内轻量服务器部署（4G 内存 / Docker Compose 一键起）

面向「境内 4G 轻量机 + API/Celery/Redis/RabbitMQ 四件套」的完整落地步骤。
全部组件均为开源版本，无授权费用。

> 已经跑在 Railway 上、要整体搬过来？看 **`migrate-from-railway.md`**。
> 结论先行：**几乎不用迁数据**——Supabase / 阿里云 OSS / SiliconFlow 都是共享的外部服务，
> 一件都不在 Railway 上。真正要处理的只有环境变量和「前端怎么连过来」。

---

## 0. 一句话结论

- **能跑**。四件套常驻约 1.1GB，峰值（PyMuPDF 解析 400 页 PDF）约 1.6GB，4G 有富余。
- **必须开 2G swap**，否则 PDF 峰值会直接触发 OOM kill，且死的是 worker（流水线静默卡死）。
- **必须给每个容器设内存上限**，否则 RabbitMQ 的 Erlang VM 会一路吃到被系统杀掉。
- **不要砍掉 RabbitMQ 换成 Redis broker**。本项目用了两处 `chord`，Redis broker 下 chord 回调靠 `chord_unlock` 轮询兜底，worker 全挂时回调可能永远不触发，job 会卡在 `extracting` 且**没有任何报错**。省这 300MB 不值得。

---

## 1. 机型选择

> 数据为 2026-09-07 腾讯云「轻量云六周年庆」（活动期 2026-09-02 ~ **2026-10-12 23:59:59**）。
> 活动价随时会变，下单前以登录后订单页为准。
>
> **活动会场入口**：<https://cloud.tencent.com/act/pro/lh6th>
> （往下滚找「六周年庆·同价续费专区」；Agent 专区在 `#agent` 锚点）
> 不要从 CVM 购买页进——那是另一条产品线，同配置年费贵 5 倍。

### 结论：不要直接买 109 的 4核4G，走 188 那条路

只看首年价会踩坑——**活动价只管第一年，第二年按刊例价收**。把两年账算全：

| 路线 | 操作 | 第 1 年 | 第 2 年 | **两年合计** | 最终到手配置 |
|---|---|---:|---:|---:|---|
| **A. Agent 专区直买** | 1 步 | 109 | **780**（刊例价） | **889** | 4核4G / 3M / 300GB月流量 / 40~50G |
| **B. 同价续费专区 + 免费升配** | 3 步 | 188 | **188**（同价续费权益） | **376** | 4核4G / **5M** / **500GB月流量** / 60G |

**B 两年省 513 元，带宽和流量还都更大。** 唯一代价是多花两分钟点三下。
Celery 的 `dm.extract` 是纯 CPU 活，4 核对 2 核的分片提取速度接近翻倍——这个收益 B 也拿得到。

### 路线 B 操作步骤（顺序不能反）

1. 进六周年庆会场 → 「**六周年庆·同价续费专区**」
   → 选 `2核4G / 60G SSD / 500GB月流量 / 5M`，标 2.4 折 **188 元/年**（个人专享，限 1 个）
2. 地域选 上海 / 广州 / 北京 / 成都（以后要备案就选内地，且**购买时长必须 ≥ 3 个月**）
3. 时长 1 年 → 下单付款
4. **立刻回到同价续费专区，对这台机器再续费 1 年**（还是 188）—— 这就是「同价续费 1 次」的权益
5. 续费成功后 → 控制台 → 该实例 → 「**免费升配**」→ 升到 4核4G
6. 完成：两年 376 元，4核4G / 60G / 5M / 500GB月

> ⚠️ **升配只加 CPU，内存不变**：`2核4G → 4核4G`，内存还是 4G。
> 别以为升完内存宽裕了 —— 第 2 节的 **2G swap 照样必做**，compose 里的 `mem_limit`
> 预算也完全不用调。收益在 CPU：`dm.extract` 跑 PyMuPDF 是纯 CPU 活，
> 400 页 PDF 的分片提取会快接近一倍。

> ⚠️ **顺序铁律：先续费，后升配。**
> 一旦先升配，实例就不再是「2核4G 入门套餐」，同价续费资格直接作废，
> 第二年按 4核4G 的刊例价收。反过来操作等于白丢 500 多块。
> 活动页原文：「*选择升配后，不再享受同价续费专区活动价。建议升配前先享受同价续费*」

#### 第 4 步详解：续费入口到底在哪（最容易找不到）

**续费必须在活动页做，不要在控制台直接续费** —— 控制台走的是刊例价，拿不到 188。

按顺序试这三个入口：

1. **先确认已登录。** 未登录时活动页所有价格都显示「您尚未登录，请登录账号后查看」，
   续费权益区域也是空的。这是「看不到优惠」最常见的原因。
2. **刷新同价续费专区页面。** 买完之后回到
   `https://cloud.tencent.com/act/pro/lh6th` 的「六周年庆·同价续费专区」，
   按 F5 刷新 —— 你名下有符合条件的实例后，这里才会出现对应的续费入口。
   刚买完可能要等几分钟到几小时同步，等不到就试下一个。
3. **点页面顶部「立即获取我的续费权益」**（锚点 `#xf`，即「老用户续费专区」）。
   这个按钮会列出当前账号能享受的全部续费优惠，新买的机器也会在里面。

**判断标准**：续费订单页显示 **≈188 元** 才是优惠价。看到 780 / 900+ 就说明走错了入口，
**立刻关掉别付款**，回来按上面的顺序重试。

> 注：「老用户续费专区」主打「5 年及以上轻友低至 1 折」，你刚买的话大概率不适用；
> 你的权益来自「同价续费专区」。另外该专区明确写了「同价续费商品不参与拼团活动」。

#### 关于「轻友年限」分档（看到这段规则说明你走错专区了）

「老用户续费专区」按**账号持有轻量实例的时长**分档，官方定义举例：

> 6个月内轻友：使用年限 < 6 个月。截止 2026年8月26日，账号下存在至少一台
> 2026年3月30日前创建的实例。

两个要点：

1. **判定基准日是 2026-08-26，且是回溯判定。** 也就是说，看的是你在 8 月 26 日那天
   账号下有没有一台 3 月 30 日之前创建的老实例。如果你是 9 月才第一次买腾讯云轻量，
   那天账号下还没有任何实例 —— **不符合任何一档轻友资格**。
2. **就算符合，这个折扣也是给「老实例」续费用，不是给你新买的这台。**

所以别在这条路上耗：**你的 188 同价续费权益是跟着这台新机器走的，与轻友年限无关**，
回「同价续费专区」刷新找续费入口就对了。

### 路线 A：什么时候才该买 109 的

- Agent 专区 → `4核4G 3M` → 109 元/年（1.4 折，**首单特惠限 1 个**，地域仅 上海/广州/北京）
- 配置：3M 带宽 / **300GB 月流量** / 40~50G SSD（各渠道口径不一，以订单页为准）
- **第二年续费刊例价 780 元/年**，买之前先把这笔账算进去
- 适合：只打算用一年、或者明年准备换账号/换厂商
- 好处：标了【可拼团】，成团加赠 3 个月（同价续费专区**不参与**拼团）

### 共同注意事项

1. **需完成实名认证**；Agent 专区是「首单特惠」，要求腾讯云新用户资格。
2. 活动机**不支持随意改配置**，只能走控制台的升级功能——所以一次买够。
3. 优惠**不能与代金券/折扣券叠加**，以结算页为准。
4. 拼团：新购或续费成功后可参与双人拼团，1 年加赠 3 个月。
   一个账号只能参与一次，**拼团成功后不支持退款**。
5. **阿里云对照**：2核4G / 200M 峰值 / **不限流量** / 199 元/年。
   如果 PDF 走服务器中转、月流量会超过 500GB，选阿里云更省心；否则腾讯云的 4 核更值。

### 300GB / 500GB 月流量够吗

**够，而且大概率用不完。** 流量包主要算公网出方向（下行），而这个后端的下行只有 API 的 JSON 响应：

- 按日活 100 用户、每人 100 次请求、平均响应 20KB 算 ≈ 6 GB/月，离 300GB 差两个数量级。
- 真正吃带宽的是**上传 PDF 到服务器**（入方向），跟流量包关系不大，但会受峰值带宽限制。

所以选型时**带宽比流量重要**：3M ≈ 375 KB/s，一份 30MB 的剧本 PDF 传上来要 80 秒左右；
5M 能压到 50 秒。这也是 188 路线比 109 路线更合适的又一个理由。

嫌慢的话根本解法是**前端直传 OSS**：项目已有 presign 端点（`PRESIGN_EXPIRE_SECONDS`），
让浏览器把 PDF 直接传到 `jbs-store`，服务器只负责签名和后续处理，
上传速度就完全不受这台轻量机的带宽约束了。

### 下单后

- **地域**：中国大陆（境内才便宜；香港/新加坡价格翻倍）。
- **镜像**：腾讯云可直接选「**Docker CE**」应用模板，省掉装 Docker 的步骤。
- **防火墙**只放行 `22`（SSH）、`80`（Nginx）。**绝不要放行 6379 / 5672 / 15672**——
  Redis 未授权访问是国内服务器被挖矿的头号入口。下文 compose 已把这三个端口绑死在 `127.0.0.1`。

1. **地域**：中国大陆（境内地域才便宜；香港/新加坡价格翻倍）。
2. **镜像**：腾讯云可直接选「Docker CE」应用模板，省掉装 Docker 的步骤。
3. **活动机通常不支持付费升降配**，但六周年庆这类活动会带「免费升配」权益（2核4G → 4核4G），
   有就尽早用掉。注意「先续费、后升配」的顺序（见 1.1 节）。
4. **防火墙**只放行：`22`（SSH）、`80`（Nginx，页面+接口走这一个口；轻量机模板默认已放行）。
   **绝不要放行 6379 / 5672 / 15672**——Redis 未授权访问是国内服务器被挖矿的头号入口。
   下文 compose 已经把这三个端口绑死在 `127.0.0.1`，API 的 8000 也只绑本机。

---

## 2. 服务器初始化

```bash
# ---- 1) 2G swap（必做，不做后面一定会踩 OOM）----
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 尽量不用 swap，只在真要 OOM 时才用（默认 60 会让 Redis 也换出去，变慢）
sysctl vm.swappiness=10
echo 'vm.swappiness=10' >> /etc/sysctl.conf

# ---- 2) Docker ----
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# ---- 3) Docker 日志全局轮转兜底（compose 里也配了，双保险）----
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
systemctl restart docker

# ---- 4) 验证 ----
free -h && docker version
```

> 日志轮转不是可选项。Celery `--loglevel=info` 跑一轮 400 页流水线能吐几百 MB 日志，
> 默认无上限的 json-file 会在几周内撑爆 50G 系统盘，症状是「容器突然全部退出」。

---

## 3. 镜像从哪来（三条路，按可靠性排序）

境内服务器直接 `docker pull` 官方镜像已经不可靠：Docker Hub 加速生态在 2024–2025 洗牌后，
网易/中科大等公共源陆续停服，阿里云也公告过镜像加速对海外源可能失败，剩下的多是第三方野鸡源。

### 路线 A：GitHub Actions 海外构建 → 推阿里云 ACR → 服务器拉取（推荐）

已配好 `.github/workflows/build-cn.yml`。

1. 阿里云开通「容器镜像服务 ACR 个人版」（免费，300 仓库，无限存储）。
2. 仓库 Settings → Secrets 加 4 个：
   `ACR_REGISTRY` = `registry.cn-hangzhou.aliyuncs.com`
   `ACR_NAMESPACE` = 你的命名空间
   `ACR_USERNAME` / `ACR_PASSWORD` = 阿里云账号 + ACR 独立密码（不是登录密码）
3. Actions 里手动跑一次 `Build & Push to Aliyun ACR (CN)`。
4. 服务器上：

```bash
docker login registry.cn-hangzhou.aliyuncs.com   # 用户名=阿里云账号，密码=ACR 独立密码
```

在 `.env` 里加三行，让 compose 从 ACR 拉：

```bash
BACKEND_IMAGE=registry.cn-hangzhou.aliyuncs.com/<ns>/jbsttj-backend:latest
REDIS_IMAGE=registry.cn-hangzhou.aliyuncs.com/<ns>/redis:latest
RABBITMQ_IMAGE=registry.cn-hangzhou.aliyuncs.com/<ns>/rabbitmq:latest
```

### 路线 B：服务器本地构建

**腾讯云「Docker CE」镜像模板实测可直接走这条**（已验证）。判断依据：`docker pull hello-world`
几秒内成功，且 `/etc/docker/daemon.json` 里有 `registry-mirrors`（腾讯云模板预置了
`https://mirror.ccs.tencentyun.com` 内网加速）。

先确认能拉到全部 5 个基础镜像（约 3 分钟）：

```bash
docker pull redis:7-alpine
docker pull rabbitmq:3.13-management-alpine
docker pull python:3.13-slim        # ⚠️ 是 3.13 不是 3.11，以 Dockerfile 的 FROM 为准
docker pull nginx:1.27-alpine
```

#### ⚠️ 坑 1：ubuntu 用户默认不在 docker 组

```
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

```bash
sudo usermod -aG docker $USER
newgrp docker          # 免重连，直接开一个带新组的子 shell
id -nG                 # 输出里必须出现 docker
```

> `usermod` 之后**必须重登录**才生效；不想重连就用 `newgrp docker`。
> 不建议长期混用 `sudo docker ...` —— 会导致部分容器/卷归 root、部分归 ubuntu，后面排权限很烦。

#### 坑 2：Debian apt 源在国内会卡住

`python:3.13-slim` 是 Debian 镜像，`apt-get update` 默认打 `deb.debian.org`，国内经常超时。
Dockerfile 已内置 `APT_MIRROR` 参数，compose 默认传 `mirrors.cloud.tencent.com`：

```dockerfile
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" ... ; fi
```

海外 CI 构建时在 `.env` 里写 `APT_MIRROR=`（留空）即可回退官方源。

#### 完整构建命令

```bash
cd /opt/jbs
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
```

首次构建要点：`pip install -r requirements.txt` 含 PyMuPDF / 深度学习相关包，
**约 5~10 分钟**，中途不要中断。看到 `Creating jbs-api ... done` 才算完。

### 路线 C：本地打包上传（完全断网环境的保底）

```bash
# 你本机（能连 Docker Hub 的机器）
docker save redis:7-alpine rabbitmq:3.13-management-alpine -o infra.tar
scp infra.tar root@<IP>:/root/
# 服务器
docker load -i /root/infra.tar
```

> ⚠️ 注意：RabbitMQ 必须用 `-management` 变体。`rabbitmq:3.13-alpine` 不带管理插件，
> 15672 端口会直接连不上。

---

## 4. 准备 `.env`

把本地 `.env` 传到服务器（**走 scp，不要提交进 git**），然后改这几项：

```bash
APP_ENV=production
DEBUG=false
CORS_ORIGINS=https://www.jbs-ttj.store
```

**`CELERY_*` 三项不用改**。compose 里用 `environment:` 硬覆盖成了容器间服务名
（`amqp://jbs:***@rabbitmq:5672//`、`redis://redis:6379/0`），
compose 中 `environment` 优先级高于 `env_file`，所以你 `.env` 里本地的 `127.0.0.1` 会被自动顶掉。

RabbitMQ 凭据（不设就用默认值 `jbs / jbs123456`，**建议改**）：

```bash
RABBITMQ_USER=jbs
RABBITMQ_PASS=换成随机串
```

其余 `SUPABASE_*` / `OSS_*` / `SILICONFLOW_*` 与本地保持一致即可。

---

## 5. 启动与验证

```bash
cd /root/jbsttj-backend

# 路线 A（已有 ACR 镜像）
docker compose -f docker-compose.prod.yml --env-file .env up -d
# 路线 B（本地构建）
docker compose -f docker-compose.prod.yml --env-file .env up -d --build

# 看状态，四个都应是 healthy / running
docker compose -f docker-compose.prod.yml ps

# API
curl http://127.0.0.1:8000/ready

# Worker 是否在消费
docker compose -f docker-compose.prod.yml exec worker \
  celery -A app.tasks inspect ping

# 实时看流水线日志
docker compose -f docker-compose.prod.yml logs -f worker
```

管理后台（不暴露公网，走 SSH 隧道）：

```bash
ssh -L 15672:127.0.0.1:15672 -L 15673:127.0.0.1:8000 root@<IP>
# 浏览器开 http://127.0.0.1:15672 → RabbitMQ；http://127.0.0.1:15673/docs → API 文档
```

---

## 6. 内存预算

| 组件 | 常驻 | 上限 (`mem_limit`) | 说明 |
|---|---:|---:|---|
| OS + Docker daemon | ~350 MB | — | |
| RabbitMQ | ~120 MB | 640 MB | `VM_MEMORY_HIGH_WATERMARK=0.4` → ~256MB 触发流控 |
| Redis | ~15 MB | 256 MB | `maxmemory 200mb` |
| API (uvicorn ×1) | ~220 MB | 512 MB | 只做转发，不碰 PDF |
| Worker (threads ×6) | ~400 MB | 1536 MB | PDF 解析峰值约 1.2GB |
| **合计** | **~1.1 GB** | **2.94 GB** | 留 ~1.1GB 余量 + 2G swap |

两个刻意的配置，改动前先看这里：

- **Redis 用 `noeviction` 而非 `allkeys-lru`**：chord 的汇聚计数器存在 db0，
  内存一满被 LRU 淘汰，回调永远等不齐，流水线静默卡死。
  `noeviction` 让写入显式报错，至少看得见。结果本身有 `result_expires=86400` 兜底，不会堆积。
- **Worker 用 `--pool=threads` 而非 prefork**：4 个 prefork worker = 13 个进程、常驻 1.3GB 起，
  4G 机器必然 OOM。threads 池单进程约 400MB，且 PyMuPDF 是 C 扩展会释放 GIL，并行度并不差。
  代价：`--max-memory-per-child` / `worker_max_tasks_per_child` 在 threads 下不生效，
  长跑内存增长靠 `mem_limit` + swap 兜底。

---

## 7. 前端联调的坑：混合内容

前端在 Vercel（`https://www.jbs-ttj.store`），后端是 `http://<IP>:8000`。
**HTTPS 页面请求 HTTP 接口会被浏览器直接拦截**（Mixed Content），表现为接口「不通」但 curl 正常。

三个解法，按推荐顺序：

1. **备案 + 域名 + Caddy 自动 HTTPS**（最正规，延迟最低，需 15–20 天备案）
   ```bash
   apt install -y caddy
   # /etc/caddy/Caddyfile
   # api.你的域名.com {
   #     reverse_proxy 127.0.0.1:8000
   # }
   systemctl reload caddy
   ```
   然后把 compose 里 API 的 `8000:8000` 改成 `127.0.0.1:8000:8000`。
2. **Vercel 反代**：给前端 `vercel.json` 加一条 `/api/* → http://<IP>:8000` 的 rewrite，
   同源即无混合内容。代价是 Vercel 境外节点回源境内，多 ~100–200ms。
3. 前端也搬上这台机器，用 Caddy 一起反代。

另外：境内服务器 **80/443 在未备案情况下会被拦截**，所以 API 先用 8000 端口是对的，
别浪费时间排查「为什么 80 不通」。

> ⚠️ 上面这句「80 未备案会被拦截」要**精确一点**（2026-09-08 实测修正）：
> 拦截是按 HTTP **Host 头识别域名**的，**纯 IP 访问不受影响**。腾讯云轻量实测
> `http://<IP>/ready` 公网可达（HTTP 200 / 51ms）。所以 Nginx 直接用了 80。
> 但只要绑了**未备案的域名**到 80，就会被拦 —— 到时要么备案，要么改回高位端口。

---

## 7.5 前端 H5 构建与部署

前端是 Taro 4.x + React，输出目录是 `dist`（**不是** `dist/h5`，本项目 `config/index.ts`
的 `outputRoot: "dist"`）。后端地址用 `TARO_APP_API_ORIGIN` 构建期注入。

### 构建（本机）

```bash
cd jbsttj-frontend
# .env 里写死自建服务器地址（同源，经 Nginx /api/ 反代）
echo 'TARO_APP_API_ORIGIN=http://101.34.58.31' > .env
NODE_OPTIONS= APPDATA="C:\\Users\\Administrator\\AppData\\Roaming" npx taro build --type h5
```

三个本机构建的坑：

1. **必须 `NODE_OPTIONS=`**：沙箱给 Node 注入的 `--require` shim 会拦截 Taro 清空 dist，
   导致构建产物错乱。置空即可。
2. **必须显式设 `APPDATA`**：Git Bash 不继承 Windows 变量，`npm-conf` 在 win32 分支
   `path.resolve(process.env.APPDATA, 'npm-cache')`，APPDATA 为 undefined 会直接抛
   `TypeError: paths[0] must be of type string`，且报错点伪装成「找不到项目配置文件 config/index」。
3. **用 `npx taro build --type h5` 而不是 `npm run build:h5`**：后者会额外跑
   gen:favicon / gen:og / gen:seo / seo:verify，既慢又可能因缺依赖失败。只 build 的话
   dist 里**没有 favicon 和 SEO 文件**（sitemap.xml / feed.xml / llms.txt），favicon 手动补：

```bash
cp src/assets/favicon/* dist/   # favicon.ico / favicon.svg / favicon-32.png / apple-touch-icon.png
```

### 上传并生效

```bash
cd jbsttj-frontend
tar -czf /tmp/h5.tar.gz -C dist .
scp /tmp/h5.tar.gz jbs:/tmp/
ssh jbs 'cd /opt/jbs/frontend && rm -rf ./* && tar -xzf /tmp/h5.tar.gz && rm -f /tmp/h5.tar.gz'
```

Nginx 挂的是 `./frontend`（`:ro`），文件替换后**无需重启容器**，刷新即生效。

### 验证

```bash
curl -s http://101.34.58.31/            # 200 + <!DOCTYPE html>
curl -s http://101.34.58.31/ready       # {"status":"ready",...}，说明 /api 反代 OK
```

---

## 8. 运维

```bash
# 更新（路线 A）
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d

# 只重启 worker（改了 prompt / 任务代码时）
docker compose -f docker-compose.prod.yml up -d --force-recreate worker

# 看内存实占
docker stats --no-stream

# 清理构建缓存（50G 盘要定期做；真正的大头是 build cache）
docker system df                    # 先看占用
docker system prune -f              # 安全：只清停止的容器/悬空镜像/构建缓存
docker system prune -af             # 磁盘紧张时才做：会连没在跑的 jbsttj-backend 镜像一起删

# ⚠️ 这条不要随手跑，也不要写进任何定时脚本
# docker system prune -af --volumes  # 会删掉 rabbitmq_data / redis_data 两个数据卷
```

**备份**：`rabbitmq_data` 和 `redis_data` 两个卷。Redis 的 chord 结果丢了顶多重跑，
RabbitMQ 里排队的生产任务建议跑完再停机维护。

> 顺带纠正：`docker system prune` 清不掉容器日志
> （`/var/lib/docker/containers/*/*-json.log`）。防日志撑爆磁盘靠的是 compose 里
> 配置的 `max-size: 10m / max-file: 3` 轮转 + 第 2 节的全局 daemon 兜底，不用手工干预。

---

## 8.1 CI/CD：和 Railway 到底差在哪

Railway 是「推代码 → 自动构建 → 自动部署」，是自带的。自建服务器后这根管子得自己接，
已经写好了：`.github/workflows/deploy-cn.yml`。

### 差异对照

| | Railway | 自建（GitHub Actions + ACR + SSH） |
|---|---|---|
| 触发 | push 即部署 | push tag 或手动 Run workflow |
| 构建机 | Railway 机房 | GitHub Actions（海外，拉 Docker Hub 顺畅）|
| 镜像落点 | Railway 内部仓库 | 阿里云 ACR 个人版（免费）|
| 环境变量 | 网页面板填，自动注入 | 服务器 `/opt/jbs/.env` + GitHub Secrets |
| 停机时间 | 滚动更新，约 0 | `up -d` 重建，**约 3~10 秒不可用** |
| 回滚 | 点一下上一个 deploy | 脚本自动读 `.last_good_image` 回滚 |
| 日志 | 网页面板 | `docker compose logs -f` / SSH |
| 费用 | 按用量付费 | GitHub Actions 公共仓库免费，私有仓库 2000 分钟/月 |

### 一次性配置

1. 服务器生成密钥对，公钥进 `~/.ssh/authorized_keys`：
   ```bash
   ssh-keygen -t ed25519 -C "gh-actions" -f ~/.ssh/gh_actions -N ""
   cat ~/.ssh/gh_actions.pub >> ~/.ssh/authorized_keys
   cat ~/.ssh/gh_actions      # ← 私钥全文放进 GitHub Secret SSH_KEY
   ```
2. 仓库 **Settings → Secrets and variables → Actions** 加这些：

   | Secret | 值 |
   |---|---|
   | `ACR_REGISTRY` | `registry.cn-hangzhou.aliyuncs.com` |
   | `ACR_NAMESPACE` | ACR 命名空间（小写）|
   | `ACR_USERNAME` | 阿里云账号 / RAM 子账号 |
   | `ACR_PASSWORD` | ACR 独立密码（**不是**阿里云登录密码）|
   | `SSH_HOST` | 服务器公网 IP |
   | `SSH_USER` | `ubuntu`（或 `root`，看镜像模板）|
   | `SSH_KEY` | 上一步的私钥全文 |
   | `SSH_PORT` | `22`（可选）|
   | `APP_DIR` | `/opt/jbs`（可选）|

3. 服务器上把项目放好：
   ```bash
   sudo mkdir -p /opt/jbs && sudo chown $USER /opt/jbs
   # 至少要有：docker-compose.prod.yml 和 .env
   ```

### 日常用法

```bash
git tag v1.2.0 && git push origin v1.2.0     # 触发构建+部署
```

或在 GitHub → Actions → **Deploy to CN Lighthouse** → Run workflow。

### 三个和 Railway 不一样、必须知道的点

**1. 有 3~10 秒不可用。** 单机单副本，`up -d` 要 recreate 容器。要真零停机得上
双端口 + 反代切换，对 4G 单机不划算。选在半夜/低峰部署即可。

**2. 改了 Celery 任务签名/参数，必须勾选 `drain_worker`。**
API 和 worker 在 Railway 上是同一个容器（supervisord），永远同版本；拆成两个容器后，
`up -d` 期间存在「新 API + 旧 worker」窗口 —— 旧 worker 不认识新任务名，抛
`NotRegistered` 且**不会重投**（不像崩溃那样有 `task_acks_late` 兜底），任务直接丢。
勾了 `drain_worker` 会先停 worker 排空队列，新任务堆在 RabbitMQ 里，等新 worker 起来再消费。
只改业务逻辑（不动 `@shared_task` 的名字和参数）就不用勾。

**3. worker 优雅退出最长 5 分钟。** compose 里 `stop_grace_period: 300s` 是刻意给的：
`dm.extract` 分片任务可能跑好几分钟，Celery warm shutdown 能等它跑完再退出。
配了 `task_acks_late=True` + `task_reject_on_worker_lost=True`，即使超时被 `SIGKILL`
任务也会重投、不丢，但已经跑了 5 分钟的提取就白跑了。
代价是部署最坏情况要等 5 分钟，workflow 的 `command_timeout: 15m` 就是按这个留的。

### 手动回滚

```bash
ssh ubuntu@<IP> 'cd /opt/jbs && cat .last_good_image'
# 用上一个 sha（或直接写 latest）重新拉起
ssh ubuntu@<IP> 'cd /opt/jbs && \
  BACKEND_IMAGE=<ACR>/<NS>/jbsttj-backend:<上一个sha> \
  docker compose -f docker-compose.prod.yml up -d --remove-orphans'
```

---

## 8.2 能不能在服务器上装 AI 助手（CodeBuddy CLI / WorkBuddy）？

**能装，但我不建议。正确做法是：装在你本机，让它通过 SSH 操作服务器。**

### 三个形态分清楚

| 形态 | 服务器上能装吗 | 说明 |
|---|---|---|
| **WorkBuddy 桌面工作台** | ❌ | 图形界面应用，需要桌面环境。Linux 服务器没有 |
| **CodeBuddy Code CLI** | ✅ | 支持 Linux x86_64 / arm64，原生二进制不依赖 Node.js |
| **本机 WorkBuddy + SSH** | ✅ 推荐 | 什么都不用往服务器装 |

### 为什么不推荐装在服务器上

**1. 生产密钥会进 AI 上下文（最严重）。**
`/opt/jbs/.env` 里有 Supabase `service_role` key（可绕过 RLS 读写全库）、阿里云 OSS
AccessKey、SiliconFlow API key。agent 是靠读文件理解项目的，这些一旦被读进上下文就上传到
云端了。这是把生产密钥主动送出去。

**2. agent 有 shell 执行权，而生产库是共享的。**
Supabase 项目本地和线上共用。agent 在你生产机器上执行命令，误操作影响的是真实用户数据，
不像本地还能靠 git 回滚。

**3. 4G 内存不够挥霍。**
四件套常驻约 1.1GB、峰值 1.6GB，agent 再常驻几百 MB，加上它的自动更新守护进程，
PDF 峰值时更容易触发 OOM。

**4. 登录这一步就是障碍。**
CLI 首次启动的认证流程是「自动打开浏览器完成 OAuth」，纯 SSH 无图形界面的服务器上
没有浏览器可用。

### 推荐方案：本机 WorkBuddy + SSH

你已经有了全套 CI/CD，改代码本来就该「本机改 → push → 自动部署」。
排查线上问题时，只要配好免密登录，WorkBuddy 就能直接帮你执行服务器命令、看日志、分析状态，
**密钥一行都不用离开服务器**。

**第 1 步**：把本机公钥放到服务器（本机已有 `~/.ssh/id_rsa.pub`）

```bash
# 本机执行，把 <IP> 换成你的公网 IP
ssh-copy-id -i ~/.ssh/id_rsa.pub ubuntu@<IP>
# 如果ssh-copy-id 不可用，用这个：
cat ~/.ssh/id_rsa.pub | ssh ubuntu@<IP> 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'
```

**第 2 步**：本机 `~/.ssh/config` 加一段（省得每次打 IP）

```
Host jbs
    HostName <IP>
    User ubuntu
    IdentityFile ~/.ssh/id_rsa
    ServerAliveInterval 60
```

**第 3 步**：验证

```bash
ssh jbs 'docker compose -f /opt/jbs/docker-compose.prod.yml ps'
```

配好之后直接说「帮我看下线上服务状态」「把 worker 最近 100 行日志拿来」就行，
剩下的由 WorkBuddy 通过 SSH 完成。

### 如果你确实要在服务器上装

至少做这三条：

```bash
# 1. 用独立的非 root 账号，别用 root 跑 agent
sudo useradd -m -s /bin/bash agent && sudo usermod -aG docker agent

# 2. 关掉自动更新守护进程，省内存
export DISABLE_AUTOUPDATER=1

# 3. .env 严格限权，且不要放在 agent 的工作目录里
sudo chmod 600 /opt/jbs/.env
```

安装本身：

```bash
curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
codebuddy --version
```

> 注意：装了也别在生产目录里跑它。真要试，先在服务器上 `git clone` 一份代码到
> `/home/agent/sandbox`，只在那里面用。

**重启开机自启**：所有 service 都是 `restart: unless-stopped`，Docker daemon 设了
`systemctl enable`，机器重启后会自动拉起。启动顺序靠 healthcheck 的
`depends_on: condition: service_healthy` 保证，不用手工排。

---

## 9. 排错速查

| 症状 | 原因 / 处理 |
|---|---|
| worker 容器反复重启，`docker stats` 看到 OOMKilled | swap 没开或 `mem_limit` 太小。先 `free -h` 确认 swap |
| job 卡在 `extracting`，worker 日志无报错 | Redis 内存满触发了 LRU 淘汰（确认 `noeviction`），或 RabbitMQ 有队列无人消费。查 `rabbitmqctl list_queues` |
| `docker pull` 超时 / `context deadline exceeded` | Docker Hub 不通，转路线 A |
| 15672 打不开 | 镜像用了 `rabbitmq:3.13-alpine`，换 `-management-alpine` |
| API 返回 401，本地正常 | 服务器时钟偏移。JWKS 校验 leeway 默认 120s，装 `chrony` 同步时间 |
| 前端请求接口浏览器报 Mixed Content | 见第 7 节 |
| 磁盘几天就满 | Docker 日志没轮转，见第 2 节 |

### 首次构建踩坑实录（2026-09-08 腾讯云轻量实测）

构建失败过三次，**每次都是国内网络环境的特性，不是代码问题**：

**① `apt-get update` exit 100 —— 镜像站元数据过期**

```
E: Release file for http://mirrors.cloud.tencent.com/debian-security/dists/trixie-security/InRelease
   is expired (invalid since 5h 25min 17s).
```

国内镜像站同步会滞后，**腾讯云的 `debian-security` 实测落后阿里云一周**（同一天查：腾讯云
InRelease 是 08-31，阿里云是 09-07）。排查方法：

```bash
curl -s http://<mirror>/debian-security/dists/trixie-security/InRelease | grep -E "^Date:"
```

修法：Dockerfile 里已加 `-o Acquire::Check-Valid-Until=false`（装的只是 libgl1/libglib2.0-0，
元数据晚几天不影响正确性），并把默认源改成 `mirrors.aliyun.com`。

**② `pip install` 报 `No matching distribution found ... (from versions: none)`**

`from versions: none` 说明**完全连不上索引**，不是包不存在。腾讯云服务器上实测：

| 源 | 结果 |
|---|---|
| `pypi.tuna.tsinghua.edu.cn` | ❌ **403**（清华源对该出口返回 403） |
| `mirrors.aliyun.com/pypi/simple/` | ✅ 200，0.08s |
| `pypi.org` | ⚠️ 200 但 **10s+** |

→ 国内服务器上 `PIP_INDEX_URL` 用**阿里云**。

**③ `failed to resolve reference "docker.io/library/jbsttj-backend:latest": 403 Forbidden`**

compose 里 worker 服务只写了 `image:` 没写 `build:`，compose 就去 registry 拉这个本地 tag，
而镜像站对不存在的本地 tag 返回 **403 而不是 404**。

→ 已用 YAML 锚点 `x-backend-build` 让 api 和 worker **共用同一份 build 定义**，两处都必须有 `build`。

**④ RabbitMQ 无限 Restarting，健康检查永远 unhealthy**

```
error: RABBITMQ_VM_MEMORY_HIGH_WATERMARK is set but deprecated
error: deprecated environment variables detected
```

3.13 起该变量废弃，**一旦设置就拒绝启动**。官方默认值本来就是 `0.4`，删掉即可。
（症状有迷惑性：api 和 worker 因 `depends_on: service_healthy` 全部卡在 Created 起不来，
看起来像是应用的问题，其实根因在 broker。）

### 部署完成后必做：放行防火墙

Nginx 走 **80 端口**（轻量机模板默认已放行，无需加规则）。
> 「未备案拦截」按 HTTP Host 头识别**域名**，纯 IP 访问不受影响——2026-09-08 在腾讯云轻量
> 实测 `http://<IP>/ready` 公网可达（HTTP 200 / 51ms）。但**绑了未备案域名到 80 会被拦**。

容器都 healthy 但公网访问不了，八成就是这一步没做。判断方法：

```bash
# 服务器上（应返回 200）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:80/ready
# 你本机（HTTP:000 + 超时 = 防火墙没放行）
curl -s -m 10 -o /dev/null -w "%{http_code}\n" http://<IP>/ready
```

只需放行 `22` 和 `80`。API 的 8000 已绑 `127.0.0.1`，无需也不应放行。
