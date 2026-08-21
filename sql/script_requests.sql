-- ============================================================
-- 剧本「求解析」诉求表 · Supabase 表结构
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/scripts.sql（可选外键）、sql/dm_rag.sql（判定「已解析」）
--
-- 业务语义：
--   1. 「求解析」= 用户希望某个剧本尽快出 DM 主持人手册（解析入库）。
--      目标剧本可能已在剧本库（关联 script_id），也可能还没有（只留标题）；
--   2. **去重**：同一用户对同一剧本只能求一次 —— 唯一约束建在
--      (user_id, match_key) 上：库中剧本 match_key=script_id，
--      库外剧本 match_key=归一化标题键（与导入去重同一套规则）；
--   3. 状态机：pending（待解析）→ completed（剧本已解析完成）/
--      cancelled（用户主动取消）。取消是软取消，不删行；
--   4. 「剧本已被解析」的判定不在这张表里 —— 后端读取时对照
--      script_dm_documents（is_active=true 且 total_chunks>0）惰性同步，
--      解析流水线无需反向耦合本表。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 求解析主表
-- ------------------------------------------------------------
create table if not exists public.script_requests (
    id           uuid primary key default gen_random_uuid(),

    -- 发起人：auth.users.id
    user_id      uuid not null,

    -- 目标剧本：库中剧本关联 script_id（并冗余 code），
    -- 库外剧本 script_id 为空、只留 script_title 自由文本
    script_id    uuid,
    script_code  text,
    script_title text not null,

    -- 去重键：script_id（库中剧本）或 normalize_title_key(标题)（库外剧本）。
    -- 唯一约束建在这里，保证「同一用户对同一剧本只有一条求解析」
    match_key    text not null,

    -- 期望解析的原因 / 补充说明（可选）
    reason       text,

    -- 状态机：pending 待解析 / completed 已完成 / cancelled 已取消
    status       text not null default 'pending',
    cancelled_at timestamptz,
    completed_at timestamptz,

    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),

    constraint uq_script_requests_user_script unique (user_id, match_key),
    constraint ck_script_requests_status check (status in ('pending', 'completed', 'cancelled'))
);

-- script_id 仅在存在时引用剧本主表；剧本软删不影响诉求记录
alter table public.script_requests
    drop constraint if exists fk_script_requests_script;
alter table public.script_requests
    add constraint fk_script_requests_script
    foreign key (script_id) references public.scripts (id);

-- ------------------------------------------------------------
-- 2. 索引
-- ------------------------------------------------------------
-- 我的求解析列表：按用户 + 创建时间倒序
create index if not exists idx_script_requests_user
    on public.script_requests (user_id, created_at desc);

-- 排行榜 / 已完成同步：按状态过滤 + 剧本聚合
create index if not exists idx_script_requests_status
    on public.script_requests (status, created_at desc);

-- 惰性同步「剧本已解析 → 请求置 completed」时按 script_id 批量更新
create index if not exists idx_script_requests_script
    on public.script_requests (script_id)
    where script_id is not null;

-- ------------------------------------------------------------
-- 3. updated_at 自动维护（与 scripts.sql 同款函数，单独执行时兜底创建）
-- ------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_script_requests_updated on public.script_requests;
create trigger trg_script_requests_updated
    before update on public.script_requests
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 4. 行级安全策略
--    求解析记录是用户私有数据：authenticated 只能读自己的行；
--    排行榜聚合由后端用 service_role key 完成（绕过 RLS），
--    因此不需要给 anon 开「读所有人」的策略。
--    写入不授予任何策略 —— 后端独占。
-- ------------------------------------------------------------
alter table public.script_requests enable row level security;

drop policy if exists "script_requests readable by owner" on public.script_requests;
create policy "script_requests readable by owner" on public.script_requests
    for select
    using (auth.uid() = user_id);
