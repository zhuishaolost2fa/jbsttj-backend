-- ============================================================
-- 用户消息（站内信 / 收件箱）
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：无（user_id 不建外键，避免 auth.users 与 public 跨 schema 的级联约束）
--
-- 业务背景：
--   站内发生「与我有关的事」时，用户不该自己去翻列表才发现：
--     1. 我提过的问题被别人回答了（script_dm_questions 真人解答）；
--     2. 我求过解析的剧本，被别人导入并解析完成了（script_requests → completed）；
--     3. system：运营 / 系统公告（预留）。
--   这些事件的产生点分散在流水线（同步 Celery worker）与 API（异步 FastAPI）
--   两处，因此本表的设计重心是「**幂等 + 旁路**」。
--
-- 设计要点：
--   1. **幂等写入**：dedup_key 非空时对 (user_id, dedup_key) 建**部分唯一索引**，
--      由 push_user_message 用 insert ... on conflict do nothing 落库。
--      同一事件重复触发（worker 重试、惰性同步补发）只会留下一条消息；
--   2. **两条写入路径**：异步 API 侧走 PostgREST，同步 worker 侧走 httpx 直连，
--      都只调 push_user_message 这一个 RPC —— 幂等逻辑只有一份，在数据库里；
--   3. **已读是软状态**：read_at 空即未读，不删行；未读计数走部分索引
--      idx_user_messages_unread，用户量级下代价可忽略；
--   4. **data 存事件上下文**（jsonb）：剧本 id/code/标题、问题 id 等，
--      前端点消息直接跳详情页，不需要再拿 id 反查一遍；
--   5. 消息是**只读收件箱**：只有后端能写，用户只能读 / 标记已读 / 删除自己的。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 消息主表
-- ------------------------------------------------------------
create table if not exists public.user_messages (
    id         uuid primary key default gen_random_uuid(),

    -- 接收者（消息归属人）。不建外键：auth.users 在另一个 schema，
    -- 用户被删后消息自然成为孤儿，由清理任务兜底，不让删除被关系卡住
    user_id    uuid not null,

    -- system 系统公告 / question_answered 问题被回答 / script_parsed 求的剧本已解析
    type       text not null default 'system',
    title      text not null,
    content    text not null default '',

    -- 触发者（如回答问题的人）；系统消息为空
    actor_id   uuid,

    -- 事件上下文：{scriptId, scriptCode, scriptTitle, questionId, requestId, ...}
    data       jsonb not null default '{}'::jsonb,

    -- 幂等键：非空时 (user_id, dedup_key) 唯一，重复事件只留一条
    dedup_key  text,

    read_at    timestamptz,
    created_at timestamptz not null default now(),

    constraint ck_user_messages_type check (
        type in ('system', 'question_answered', 'script_parsed')
    )
);

-- 幂等的落点：部分唯一索引（dedup_key 为空的系统广播不受约束）
create unique index if not exists uq_user_messages_dedup
    on public.user_messages (user_id, dedup_key)
    where dedup_key is not null;

-- 收件箱主查询：我的消息，时间倒序
create index if not exists idx_user_messages_user
    on public.user_messages (user_id, created_at desc);

-- 未读计数：只索引未读行，已读消息再多也不影响计数查询
create index if not exists idx_user_messages_unread
    on public.user_messages (user_id)
    where read_at is null;

-- ------------------------------------------------------------
-- 2. 幂等推送（唯一的写入口）
--    API 侧（异步 PostgREST）与 worker 侧（同步 httpx）都只调这个函数，
--    保证「同一事件只发一条」这件事在数据库里有且只有一份实现。
--    返回新消息 id；命中幂等（重复事件）时返回 NULL —— 调用方据此判断是否真发了。
-- ------------------------------------------------------------
create or replace function public.push_user_message(
    p_user_id    uuid,
    p_type       text,
    p_title      text,
    p_content    text default '',
    p_actor_id   uuid default null,
    p_data       jsonb default '{}'::jsonb,
    p_dedup_key  text default null
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    if p_user_id is null then
        return null;
    end if;

    if p_dedup_key is not null and p_dedup_key <> '' then
        insert into public.user_messages (user_id, type, title, content, actor_id, data, dedup_key)
        values (p_user_id, p_type, p_title, coalesce(p_content, ''), p_actor_id,
                coalesce(p_data, '{}'::jsonb), p_dedup_key)
        on conflict (user_id, dedup_key) where dedup_key is not null do nothing
        returning id into v_id;
    else
        insert into public.user_messages (user_id, type, title, content, actor_id, data, dedup_key)
        values (p_user_id, p_type, p_title, coalesce(p_content, ''), p_actor_id,
                coalesce(p_data, '{}'::jsonb), null)
        returning id into v_id;
    end if;

    return v_id;
end;
$$;

-- ------------------------------------------------------------
-- 3. 剧本解析完成 → 结算「求解析」诉求
--    把该剧本下全部 pending 诉求一次性置 completed，并返回这些行
--    （含 user_id / script_title），调用方据此逐条发消息。
--
--    匹配规则与后端惰性同步一致：
--      - 已关联剧本：script_id = p_script_id；
--      - 库外诉求（只填了标题）：match_key = 归一化标题键 ∈ p_match_keys。
--    放在 SQL 里的原因：worker 侧是同步代码、API 侧是异步代码，
--    两边各写一遍匹配必然走偏 —— 结算逻辑只有这一份。
-- ------------------------------------------------------------
create or replace function public.settle_script_requests(
    p_script_id   uuid,
    p_script_code text default null,
    p_match_keys  text[] default '{}'
)
returns setof public.script_requests
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_script_id is null and coalesce(array_length(p_match_keys, 1), 0) = 0 then
        return;
    end if;

    return query
    update public.script_requests r
       set status       = 'completed',
           completed_at = now(),
           updated_at   = now(),
           script_id    = coalesce(r.script_id, p_script_id),
           script_code  = coalesce(r.script_code, p_script_code)
     where r.status = 'pending'
       and (
           (p_script_id is not null and r.script_id = p_script_id)
           or r.match_key = any (p_match_keys)
       )
    returning *;
end;
$$;

-- ------------------------------------------------------------
-- 4. 行级安全策略
--    消息是用户私有数据：只能读 / 改 / 删自己的行；
--    写入不授予任何策略 —— 后端用 service_role 独占（绕过 RLS）。
-- ------------------------------------------------------------
alter table public.user_messages enable row level security;

drop policy if exists "user_messages readable by owner" on public.user_messages;
create policy "user_messages readable by owner" on public.user_messages
    for select
    using (auth.uid() = user_id);

-- 标记已读：只允许改自己的行
drop policy if exists "user_messages updatable by owner" on public.user_messages;
create policy "user_messages updatable by owner" on public.user_messages
    for update
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "user_messages deletable by owner" on public.user_messages;
create policy "user_messages deletable by owner" on public.user_messages
    for delete
    using (auth.uid() = user_id);

-- ------------------------------------------------------------
-- 5. 刷新 PostgREST schema 缓存
--    新建的表 / 函数不会自动出现在 PostgREST 的缓存里（默认要等缓存过期），
--    不 reload 的话接口会报 PGRST205「Could not find the table」。
-- ------------------------------------------------------------
notify pgrst, 'reload schema';
