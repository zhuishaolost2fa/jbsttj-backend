# 只读接口缓存（Redis）

剧本详情页一次打开会并发打七八个只读接口，而这些接口的源数据只在两种时刻变化：

1. **ingest 流水线跑完** —— QA 树、故事卡片、合成文章、索引状态、检索结果整体换血；
2. **用户写了一条内容** —— 划线评论、真人解答、新沉淀的提问。

所以全部按 **cache-aside + scope 版本号** 缓存：key 里拼入该 scope 的版本号，
写侧 bump 版本号即整体失效，不需要扫描删除；Redis 不可用时版本号读成 0、
读写全部降级为直查数据库，接口行为不变（`app/services/redis_cache.py`）。

## 覆盖范围

| 接口 | scope | TTL | 失效时机 |
| --- | --- | --- | --- |
| `GET /scripts/{id_or_code}` 剧本详情 | 全局 `version` | 60s | 任何剧本写操作（编辑 / 上下架 / 删除） |
| `GET /scripts` 剧本列表 | 全局 `version` | 60s | 同上 |
| `GET /dm-guide/qa-titles` 问答标题链 | `dm-qa-titles:<code>` | 600s | 流水线收尾、剧本删除 |
| `GET /dm-guide/stories` 故事卡片 | `dm-stories:<code>` | 600s | 流水线收尾、划线增删改（列表带公开划线数） |
| `GET /dm-guide/synthesis` 合成文章 | `dm-synthesis:<code>` | 600s | 流水线收尾、合成文章写库后 |
| `GET /dm-guide/stories/{id}` 条目详情 | `dm-story:<story_id>` | 60s | 该条目的划线增删改 |
| `GET /dm-guide/highlights` 共读时间线 | `dm-highlights:<code|story_id>` | 60s | 划线增删改 |
| `GET /dm-guide/questions` 用户提问 | `dm-questions:<code>` | 60s | 真人解答、新问题沉淀 |
| `GET /dm-guide/guide-questions` 引导问题 | `dm-guide-questions:<code>` | 60s | 同上 |
| `GET /dm-guide/search`、`POST /dm-guide/ask` | `dm-search:<code>` | 300s | 流水线收尾（向量库换血） |
| `GET .../dm-guide` 手册状态 / `import-status` | `dm-status:<code>` | 15s | 触发解析、流水线收尾、剧本删除 |
| `GET .../dm-guide/jobs/{id}` 任务进度 | `dm-job:<job_id>` | 15s | 仅终态缓存，解析中不缓存 |

TTL 由配置控制：`script_list_cache_ttl` / `dm_qa_cache_ttl` / `dm_content_cache_ttl` /
`dm_search_cache_ttl` / `dm_ugc_cache_ttl` / `dm_status_cache_ttl`。

## 三条硬规则

1. **解析中绝不缓存状态**。状态与进度接口只有进入终态
   （无任务，或任务 `completed` / `skipped` / `failed` / `cancelled`）才写缓存；
   否则前端会看到卡住的百分比。
2. **UGC 缓存必须配写侧失效**。划线 / 提问这类用户随手就能改的数据，TTL 只是兜底，
   真正的失效点在各写接口（`_invalidate_highlight_caches` / `_invalidate_question_caches`）。
3. **ask 命中缓存不重复沉淀问题**。同一个问题在缓存期内被反复问只记录一次，
   沉淀的意义是「有人问过」，重复计数没有价值。

## 运维

```bash
# 看某个剧本当前缓存了哪些 key
redis-cli -n 1 --scan --pattern "dm-*mao-dao*"

# 手动失效某剧本的全部解析产出缓存（版本号 +1，旧 key 靠 TTL 自然过期）
redis-cli -n 1 incr jbs:cache:ver:dm-stories:mao-dao-mou-sha-xun-huan

# 清空全部业务缓存（版本号键 + 数据键）
redis-cli -n 1 --scan --pattern "jbs:cache:*" | xargs redis-cli -n 1 del
```

端到端自检脚本（需本机 Redis）：

```bash
.venv/Scripts/python.exe scripts/_probe_cache_e2e.py
```
