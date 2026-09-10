# 站内消息（user_messages）

> 集中管理「与我有关」的事件：用户不该自己去翻列表才发现。

## 为什么需要它

站内发生的事会改变用户能否继续往下走，但生产链路是分叉的：

| 事件 | 触发点 | 走同步 / 异步 |
| --- | --- | --- |
| 你提的问题被真人回答了 | `POST /dm-guide/questions/{id}/answer` | 异步（API） |
| 你求解析的剧本被导入并解析完成 | `dm.finalize`（Celery worker） | 同步（worker） |

两条路径上事件源完全不同，状态机也独立。消息系统把它们**对接到一个统一的收件箱**里，让用户用「未读 → 已读 → 删除」三态统一处理。

## 一、设计要点

1. **幂等是底线**。同一事件可能从两条路径同时打过来（worker 跑完后用户又打开列表触发惰性同步），用 `(user_id, dedup_key)` 部分唯一索引 + `push_user_message` 的 `insert ... on conflict do nothing` 保证「同一事件只留一条消息」。数据库里只有一份去重实现，调用方不用各自做。
2. **写入只走 RPC**。`push_user_message(...)` 是一切的入口：异步 API 走 `SupabaseClient.rpc`，同步 worker 走 `SyncMessageClient.rpc`（直接 `httpx.Client`，不起 event loop），与 `dm_store.DMStore` 的同步 client 同思路。
3. **旁路能力**。投递失败只记日志，绝不能把主流程（解答问题、解析完成）拖成失败 —— `notify_*` 全部吞 `DatabaseError`。
4. **消息是只读收件箱**。没有「发消息」接口，写入完全由后端在事件发生时触发；用户能做的只有读、标记已读、删除。
5. **上下文在 data 里**。`data.scriptId / scriptCode / scriptTitle / questionId / requestId` 一行取齐，前端点消息直接跳详情页，不必再拿 id 反查一遍。

## 二、表与 RPC（sql/user_messages.sql）

### `public.user_messages`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | uuid | 主键 |
| `user_id` | uuid | 接收者（不建外键，避免 auth.users 跨 schema 的级联约束） |
| `type` | text | `system` / `question_answered` / `script_parsed` |
| `title` | text | 卡片标题 |
| `content` | text | 摘要正文（~60 字），全文去详情页 |
| `actor_id` | uuid | 触发者（系统消息为空） |
| `data` | jsonb | 事件上下文 |
| `dedup_key` | text | 幂等键；非空时与 user_id 联合唯一 |
| `read_at` | timestamptz | 已读时间；NULL = 未读 |
| `created_at` | timestamptz | 创建时间 |

索引：

- `uq_user_messages_dedup (user_id, dedup_key) where dedup_key is not null` —— 幂等的落点
- `idx_user_messages_user (user_id, created_at desc)` —— 我的消息主查询
- `idx_user_messages_unread (user_id) where read_at is null` —— 未读计数

RLS：select / update / delete 三条 owner 策略，写入不授予（后端 service_role 独占）。

### RPC

- **`push_user_message(p_user_id, p_type, p_title, p_content, p_actor_id, p_data, p_dedup_key) returns uuid`** —— 幂等推送；命中去重返回 NULL。
- **`settle_script_requests(p_script_id, p_script_code, p_match_keys) returns setof script_requests`** —— 剧本解析完成时一次性把匹配到的 pending 诉求置 completed，并回填 script_id / script_code，**返回这些行**供调用方发消息。匹配规则与异步惰性同步一致（`script_id = p_script_id` 或 `match_key = any(p_match_keys)`），逻辑只有这一份。

## 三、API（`/api/v1/messages`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/messages` | 我的消息列表，支持 `type` / `unreadOnly` / 分页；返回里 `unreadCount` 是全量未读数 |
| GET | `/messages/unread-count` | 未读数（tabbar 红点轻量轮询） |
| POST | `/messages/read-all` | 全部已读，返回 updated / 剩余未读 |
| PATCH | `/messages/{id}/read` | 单条已读，幂等（重复 updated=0） |
| POST | `/messages/read-batch` | 批量已读，body `{messageIds: [uuid...]}`（最多 100） |
| DELETE | `/messages/{id}` | 物理删除（只能删自己的，否则 404） |

## 四、埋点位置

| 事件 | 调用方 | 备注 |
| --- | --- | --- |
| `question_answered` | `DMGuideService.answer_question`（异步） | 自问自答不通知；剧本标题取不到就退化为不带剧名文案 |
| `script_parsed` | `dm_ingest.finalize`（**同步 worker**）+ `ScriptRequestService._sync_completed`（**异步兜底**） | 两条路径上 dedup_key = `script_parsed:{request_id}`，保证只留一条 |

`finalize` 主动结算剧本标题后再传给 `_run_synthesis` —— 顺手省一次重复查询（合成文章阶段本来就要查）。

## 五、排查手册

| 现象 | 看什么 | 怎么修 |
| --- | --- | --- |
| 用户收不到「问题被回答」 | `user_messages` 是否有 `dedup_key=question_answered:{qid}` 的行 | 缺则查 `DMGuideService.answer_question` / `_notify_question_answered` 异常日志 |
| 用户收不到「剧本已解析」 | 走 `settle_script_requests` 看 `script_requests` 是否被结算 | 如果 RPC 报错（`settle_script_requests` 函数不存在），先在 Supabase 重跑 `sql/user_messages.sql` |
| 出现重复消息 | 看 dedup_key | 若没有 dedup_key（如 system 广播没带），PostgREST 会允许重复；调用方必须给 dedup_key |
| tabbar 红点一直转 | 看未读数是不是从 `user_messages` 实时数出来的 | 走 `MessageRepository.count_unread`，直接命中 `idx_user_messages_unread` 部分索引 |
| RPC `push_user_message` 报不存在 | `pgrst, 'reload schema'` 是否执行 | Supabase Dashboard -> SQL Editor 重跑 `sql/user_messages.sql` 即可，文件末尾已带 `notify pgrst, 'reload schema'` |

## 六、上线步骤

1. 在 Supabase Dashboard -> SQL Editor 执行 `sql/user_messages.sql`
   （或本地用 `scripts/apply_sql_remote.py sql/user_messages.sql`，需 `SUPABASE_ACCESS_TOKEN`）
2. 跑端到端自检：
   ```
   PYTHONPATH=. .venv/Scripts/python.exe scripts/_probe_messages.py
   ```
   期望全部 `[ok]`，重点看「重复投递 -> None」和「标记已读 updated=1」
3. 部署后端（`scripts/deploy.sh`）即可
