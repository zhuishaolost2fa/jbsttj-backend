# 向量检索优化方案（Supabase 在境外）

> 实测日期：2026-09-08　数据源：腾讯云轻量（101.34.58.31）容器内网 + 开发者本机

## 一、结论先行

1. **先修 bug 再谈优化**：`match_dm_chunks` / `match_dm_qa` 在库里有重复签名，
   PostgREST 返回 **HTTP 300 PGRST203**，检索结果**一直为空**。
   → 执行 `sql/fix_dm_match_overloads.sql`。
2. **瓶颈是网络不是向量**：一次检索 199ms，其中 DB 侧计算约 20ms，网络 179ms。
   优化向量索引/参数对当前规模基本无效，优化往返次数才有效。
3. **推荐中期方案**：把向量表下沉到服务器本地 pgvector，检索 199ms → 3~5ms。

## 二、实测数据

| 指标 | 实测值 |
| --- | --- |
| 服务器 → Supabase TCP 建连 | 中位 **74 ms**（min 68 / max 177） |
| 全新连接首次 HTTPS（含 DNS+TCP+TLS） | 226 ~ 432 ms |
| 本地（开发者机）热往返 | 460 ms ← 比服务器还慢，本地调试体感差 |
| 容器内稳态轻量 GET（长连接复用） | 179 ms |
| `match_dm_chunks` RPC | 199 ms |
| `match_dm_qa` RPC | 215 ms |
| **DB 侧向量计算（差值推算）** | **≈ 20 ms** |
| 网络占比 | **≈ 90%** |

数据规模（决定了优化方向）：

| 表 | 行数 |
| --- | --- |
| `script_dm_chunks` | 1,114 |
| `script_dm_qa` | 3,975 |
| `script_dm_stories` | 357 |
| 向量行合计 | 5,446（裸向量 21.3 MB，1024 维 float32） |

**5 千行 / 21 MB 是极小规模** —— 全量放得进内存，甚至放得进 CPU 缓存量级。
这意味着「用更强的索引」几乎没有收益，「减少往返 / 去掉网络」才是正解。

## 三、立刻要做（今天，0 成本）

### 1. 修复函数重载（必须）

```bash
# Supabase Dashboard → SQL Editor → 整段执行
sql/fix_dm_match_overloads.sql
```

根因：`create or replace function` 加参数（新增 `p_script_code` / `p_category`）时，
PG 认为参数列表不同的函数是**不同对象**，`or replace` 不覆盖，旧签名滞留库里。
PostgREST 遇到同名多签名无法选择 → 300。
`DMStore.rpc` 拿到的不是数组 → `match_chunks` 静默返回 `[]` → RAG 全走 LLM 兜底。

执行后验证：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY python scripts/_probe_vector_latency.py
# match_dm_chunks 应从 [HTTP 300] 变成 [OK]
```

### 2. 两路检索并发（已完成）

`dm_service.search()` 中 chunk / qa 两路原本串行，等于付两次跨洋往返。
已改为 `asyncio.gather` 并发，单路失败降级为「少一路召回」，全部失败才向上抛。

| 模式 | 改前 | 改后 |
| --- | --- | --- |
| hybrid（默认） | 199 + 215 = **414 ms** | max(199, 215) ≈ **220 ms** |

### 3. 可选：合并为单次 RPC

新建 `match_dm_hybrid` 一次返回 chunks + qa，两路往返变一路（≈ 200ms）。
收益不如并发明显（并发已经省掉一半），但能再压掉一次 RTT，且便于以后做融合排序。

### 4. 防免费层冷启动

Supabase 免费项目 **连续 7 天无活动会自动暂停**，唤醒首次请求要 2~3 秒
（本机测到的 1877ms 冷连接就是它）。生产环境要么升 Pro，要么加一个定时预热：

```bash
# 每天 09:00 打一次轻量请求，防止项目被暂停（服务器 crontab）
0 9 * * * curl -s -o /dev/null -H "apikey: $SUPABASE_ANON_KEY" \
  https://ratcjkjvynubglofrvkt.supabase.co/rest/v1/scripts?select=id\&limit=1
```

## 四、中期方案对比

| 方案 | 检索延迟 | 改动量 | 成本 | 适用规模 | 风险 |
| --- | --- | --- | --- | --- | --- |
| **A. 留在 Supabase，榨干网络** | ~200 ms | 小 | ¥0 | 任意 | 无（但天花板明显） |
| **B. 向量表下沉本地 pgvector** ⭐ | **3~5 ms** | 中 | ¥0（复用现有 4G 服务器） | 千万级 | 需迁移 + 去跨库外键 |
| C. 内存 numpy 暴力检索 | **~2 ms** | 中 | ¥0（+40MB/万行内存） | < 5 万行 | 多 worker 各存一份，重启需预热 |

### 方案 B（推荐）：本地 pgvector —— 已实施（2026-09-08）

#### 落地内容

| 文件 | 作用 |
| --- | --- |
| `sql/local_vector.sql` | 本地表结构 + HNSW 索引 + `match_dm_chunks` / `match_dm_qa` / `list_dm_qa_titles` 函数。挂到 `/docker-entrypoint-initdb.d/` 首次启动自动执行 |
| `app/services/vector_store.py` | `LocalVectorStore`：psycopg3 连接池，签名与 `DMStore` 完全一致 |
| `app/services/dm_store.py` | 按 `VECTOR_BACKEND` 路由：向量表读写走本地，其余表走 Supabase |
| `docker-compose.prod.yml` | 新增 pgvector 服务（384MB 上限，只绑 127.0.0.1:5432） |
| `scripts/migrate_vectors_to_local.py` | 存量数据搬迁，幂等、保留原 id |
| `sql/migrate_vector_local_supabase.sql` | Supabase 侧配套：解除 `stories.chunk_id` → `chunks` 外键 |

#### 部署步骤

```bash
# 1) 生成密码并写进 .env（只需一次）
PW=$(openssl rand -hex 16)
cat >> .env <<EOF
PGVECTOR_PASSWORD=$PW
PGVECTOR_DB=jbsvector
VECTOR_BACKEND=local
VECTOR_DB_DSN=postgresql://postgres:$PW@pgvector:5432/jbsvector
EOF

# 2) 先起向量库，init 脚本会自动建表（数据目录为空时才执行）
docker compose -f docker-compose.prod.yml --env-file .env up -d pgvector
docker exec jbs-pgvector psql -U postgres -d jbsvector -c "\dt"

# 3) 重建 api / worker（requirements 新增 psycopg，必须重新 build）
docker compose -f docker-compose.prod.yml --env-file .env build api worker
docker compose -f docker-compose.prod.yml --env-file .env up -d api worker

# 4) 搬存量数据（幂等，可重复跑）
docker compose -f docker-compose.prod.yml exec -T api \
    python scripts/migrate_vectors_to_local.py

# 5) Supabase SQL Editor 执行 sql/migrate_vector_local_supabase.sql
```

#### 设计要点

- **只迁向量表**（`script_dm_chunks` / `script_dm_qa`），
  `documents` / `jobs` / `stories` / `highlights` / `questions` 与 GoTrue 鉴权留在 Supabase。
- 本地副本**去掉指向 `scripts` / `script_dm_documents` 的外键**（跨库无外键），
  只保留 `script_id` / `document_id` 做过滤；级联删除由 `purge_local_document` /
  `purge_local_script` 负责。
- 新增 `dm_documents` **影子表**（id / script_id / script_code / object_key /
  is_active / deleted_at / created_at），替代检索时对主表的 join。
  同步失败只记日志不阻断 —— 主表仍是真相源，影子表可重建。
- `qa.chunk_id` 的外键保留（同库），`list_dm_qa_titles` 一并搬到本地，
  否则标题树会读到迁走前的旧数据。
- 21MB 数据全量常驻 shared_buffers，HNSW 图也在内存，检索 3~5ms。

#### 回滚

Supabase 侧存量数据**只搬不删**，所以回滚是改一行配置：

```bash
sed -i 's/^VECTOR_BACKEND=local/VECTOR_BACKEND=supabase/' .env
docker compose -f docker-compose.prod.yml --env-file .env up -d api worker
```

⚠️ 必须是 `up -d` 而不是 `restart` —— compose 的 `restart` 不重读 `.env`。

回滚后新写入的数据只在本地库，需要重新跑一次迁移脚本（反向）。

#### 怎么查看本地向量库

pgvector 只监听 `127.0.0.1:5432`（容器端口没映射到公网），所以**外网直连连不上**，
这是刻意的安全设计。三种查看方式：

**1）服务器命令行（最快）**

```bash
cd /opt/jbs

./scripts/vec_shell.sh                     # 进入交互式 psql
./scripts/vec_shell.sh stats               # 行数 / 体积 / 索引 / 各剧本分布
./scripts/vec_shell.sh docs                # 已导入手册清单（含各自的 chunk / qa 数）
./scripts/vec_shell.sh "select ..."        # 临时 SQL
./scripts/vec_shell.sh probe "凶手是谁"     # 用真实 embedding 跑一次检索，验证召回
```

`probe` 会真的调 SiliconFlow 生成查询向量再检索，是判断"检索到底准不准"最直接的方式。

**2）本地 GUI（DBeaver / TablePlus）—— 用 DBeaver 内置 SSH 隧道（推荐）**

⚠️ 不要在本地另开 `ssh -fN -L` 外置隧道再连 5433：那种后台进程会随终端会话退出被杀，
表现就是过一会儿 `Connection refused`。让 DBeaver 自己管隧道，重启/掉线自动恢复。

连接设置两步：

- **SSH 标签**（点 `+ SSH`）：Host `101.34.58.31`，Port `22`，用户 `ubuntu`，
  认证 `Public Key`，私钥 `~/.ssh/id_rsa`，口令留空。
- **主要标签**：主机 `127.0.0.1`，端口 **`5432`**（注意不是 5433 ——
  走 DBeaver 隧道时填的是"服务器视角"的地址），库 `jbsvector`，用户 `postgres`，
  密码取服务器 `.env` 里的 `PGVECTOR_PASSWORD`：

```bash
ssh jbs "grep '^PGVECTOR_PASSWORD' /opt/jbs/.env | cut -d= -f2"
```

实测 PostgreSQL 16.15 连通（1.6s 首连）。断线重连时隧道可能挂，重跑一次 `ssh -fN` 即可。

**3）Web UI（可选，占内存）**

想用 pgAdmin / Cloudbeaver 这类网页端，可以加容器，但**务必只绑 127.0.0.1**，
再用上面同样的隧道访问 —— 数据库带全部向量数据，别暴露到公网。

**常用 SQL**

```sql
-- 各剧本数据量
select script_code, count(*) from public.script_dm_chunks group by 1 order by 2 desc;

-- 某剧本抽几条 chunk 看内容
select left(content, 80) from public.script_dm_chunks
 where script_code = '<剧本 code>' limit 5;

-- 有没有漏掉向量的行（正常应为 0）
select count(*) from public.script_dm_qa where embedding is null;

-- 索引体积与是否启用
select indexrelname, pg_size_pretty(pg_relation_size(indexrelid))
  from pg_stat_user_indexes where relname like 'script_dm_%';
```

### 迁完后实测（2026-09-08，3975 条 QA / 1114 条 chunk）

| 场景 | Supabase | 本地 pgvector | 提升 |
| --- | --- | --- | --- |
| `match_chunks` 按剧本过滤（真实主路径） | 199 ms | **1.8 ms** | 110× |
| `match_qa` 按剧本过滤（真实主路径） | 215 ms | **3.3 ms** | 65× |
| 不带剧本过滤的全局检索 | 199 / 215 ms | 12 / 27 ms | 8~16× |

#### 一个反直觉的坑：小数据量下 PG 会主动放弃 HNSW

排查时看到 `match_dm_qa` 走了 40ms 而不是预期的几毫秒，`explain` 发现：

```
带 join dm_documents 时：
  Bitmap Heap Scan on dm_documents → Nested Loop → Bitmap Index Scan on idx_dm_qa_doc
  → 取出全部 3975 行 → Sort top-N heapsort        = 27~40 ms
不带 join、只有向量排序 + 阈值过滤时：
  Index Scan using idx_dm_qa_embedding_hnsw       = 0.96 ms
```

**只要 WHERE 里带上 `document_id` / `script_code` / `ANY(数组)` / `EXISTS` 这类过滤，
规划器就会认为「先按 btree 取行再排序」更便宜，从而放弃 HNSW。**

但实测结论反而是好事：

- **带剧本过滤时**（真实主路径）：候选集缩小到几百行，btree 取行 + 精确排序只要 3ms，
  而且是**精确检索（无 HNSW 近似误差）**，比走 HNSW 更准；
- **不带过滤时**：走全表精确排序 12~27ms —— 数据量小时仍然比 HNSW 遍历更快
  （HNSW 要随机访问图节点，3975 行规模下不如顺序扫描）。

所以当前是**规划器自动选到了更优解**，不是 bug。等数据量涨到十万行级别，
全表扫描代价超过 HNSW，规划器会自己切回索引扫描，无需人工干预。

试过但**无效**的手段（别再浪费时间）：

- 把过滤改成子查询「先取 top-N 再过滤」→ 规划器改走 Seq Scan，反而 24ms；
- `set local enable_seqscan = off` 强制禁用 → 仍然 26ms，没走 HNSW；
- `document_id = ANY(数组)` / `EXISTS` 两种写法 → 都是 24~27ms。

### 方案 C：内存暴力检索（数据量小时反而最快）

5446 行 × 1024 维，一次全量点积约 5.6M 次浮点运算，numpy 约 2ms，
比走 HNSW 图还快，且**召回率 100%（精确检索，无近似误差）**。

适合「先不动架构、马上要快」的过渡：启动时拉全量 → 常驻内存 →
Redis 存版本号做失效通知 → 重新导入手册时刷新。

缺点是随规模线性吃内存（1 万行 ≈ 40MB），且每个 worker 进程各存一份。

## 五、不推荐的选项

- **调 HNSW 的 m / ef_construction**：当前 5000 行规模下召回与耗时都无瓶颈，改了没感觉。
- **IVFFlat**：需要训练数据，增量写入还得定期重建，不如 HNSW。
- **换 Supabase 区域到美西/欧洲**：只会更慢。74ms 说明现已在亚太，够好了。
- **Supabase 读副本**：副本也在境外，解决不了跨洋 RTT。

## 六、后续可选：降维 / 量化

数据量涨到十万行以上再考虑：

- `halfvec`（pgvector 0.7+）：1024 维 float32 → float16，存储减半、检索快 1.5~2x，
  需重建索引并**重算存量向量**。
- 改用 768 维模型（如 `bge-base-zh`）：存储再降 25%，但要重跑全部 ingestion。

当前 21MB 规模下，这些都不划算。
