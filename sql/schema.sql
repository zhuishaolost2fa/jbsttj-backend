-- ============================================================
-- 文件上传服务 · Supabase 表结构
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可
-- ============================================================

-- ------------------------------------------------------------
-- 1. 上传任务表
-- ------------------------------------------------------------
create table if not exists public.upload_tasks (
    id            uuid primary key default gen_random_uuid(),
    -- 本项目鉴权走自建「服务间通道」(X-API-Key + X-User-Id)，user_id 为业务自定义标识，
    -- 不强制绑定 Supabase auth.users，故此处不建外键，避免后端用 service_role 写入时受 FK 约束。
    user_id       uuid not null,
    bucket        text not null,
    object_key    text not null,
    filename      text not null,
    content_type  text,
    file_size     bigint  not null check (file_size > 0),
    chunk_size    integer not null check (chunk_size >= 102400),
    total_parts   integer not null default 0,
    -- OSS 返回的 multipart uploadId，秒传任务为空
    upload_id     text,
    -- 客户端计算的内容指纹，秒传与断点续传的匹配依据
    file_hash     text,
    status        text not null default 'uploading'
                  check (status in ('uploading', 'completed', 'aborted', 'failed')),
    error_message text,
    metadata      jsonb not null default '{}'::jsonb,
    expires_at    timestamptz,
    completed_at  timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create index if not exists idx_upload_tasks_user       on public.upload_tasks (user_id, created_at desc);
create index if not exists idx_upload_tasks_status     on public.upload_tasks (status);
-- 断点续传查询的核心索引
create index if not exists idx_upload_tasks_resume     on public.upload_tasks (user_id, file_hash, file_size, status);

-- ------------------------------------------------------------
-- 2. 分片记录表
--    唯一约束是 PostgREST upsert(on_conflict=task_id,part_number) 的前提
-- ------------------------------------------------------------
create table if not exists public.upload_parts (
    id          bigserial primary key,
    task_id     uuid    not null references public.upload_tasks (id) on delete cascade,
    part_number integer not null check (part_number between 1 and 10000),
    etag        text    not null,
    size        bigint  not null default 0,
    created_at  timestamptz not null default now(),
    constraint uq_upload_parts_task_part unique (task_id, part_number)
);

create index if not exists idx_upload_parts_task on public.upload_parts (task_id, part_number);

-- ------------------------------------------------------------
-- 3. 文件表
--    object_key 刻意不加唯一约束：秒传时多条记录会指向同一个 OSS 对象，
--    删除时按引用计数判断是否物理删除。
-- ------------------------------------------------------------
create table if not exists public.files (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null,
    task_id      uuid references public.upload_tasks (id) on delete set null,
    bucket       text not null,
    object_key   text not null,
    filename     text not null,
    content_type text,
    file_size    bigint not null,
    file_hash    text,
    etag         text,
    is_public    boolean not null default false,
    metadata     jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    deleted_at   timestamptz
);

create index if not exists idx_files_user       on public.files (user_id, created_at desc);
create index if not exists idx_files_hash       on public.files (user_id, file_hash) where deleted_at is null;
create index if not exists idx_files_object_key on public.files (object_key) where deleted_at is null;
create index if not exists idx_files_task       on public.files (task_id);
-- 文件名模糊搜索（ilike）加速，需要 pg_trgm 扩展
create extension if not exists pg_trgm;
create index if not exists idx_files_filename_trgm on public.files using gin (filename gin_trgm_ops);

-- ------------------------------------------------------------
-- 4. updated_at 自动维护
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

drop trigger if exists trg_upload_tasks_updated on public.upload_tasks;
create trigger trg_upload_tasks_updated
    before update on public.upload_tasks
    for each row execute function public.touch_updated_at();

drop trigger if exists trg_files_updated on public.files;
create trigger trg_files_updated
    before update on public.files
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 5. 行级安全策略
--    后端用 service_role key，天然绕过 RLS；
--    以下策略保证前端即使拿 anon key 直连也只能看到自己的数据。
-- ------------------------------------------------------------
alter table public.upload_tasks enable row level security;
alter table public.upload_parts enable row level security;
alter table public.files        enable row level security;

drop policy if exists "own tasks" on public.upload_tasks;
create policy "own tasks" on public.upload_tasks
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "own parts" on public.upload_parts;
create policy "own parts" on public.upload_parts
    for all
    using (
        exists (
            select 1 from public.upload_tasks t
            where t.id = upload_parts.task_id and t.user_id = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.upload_tasks t
            where t.id = upload_parts.task_id and t.user_id = auth.uid()
        )
    );

drop policy if exists "own files" on public.files;
create policy "own files" on public.files
    for all
    using (auth.uid() = user_id or is_public = true)
    with check (auth.uid() = user_id);

-- ------------------------------------------------------------
-- 6. 运维辅助：清理僵尸任务
--    配合 pg_cron 定时执行，把长期未完成的任务标记掉。
--    对应的 OSS 分片碎片建议同时在 Bucket 上配置
--    「删除未完成的分片上传任务」生命周期规则来清理。
-- ------------------------------------------------------------
create or replace function public.expire_stale_upload_tasks(stale_hours integer default 24)
returns integer
language plpgsql
security definer
as $$
declare
    affected integer;
begin
    update public.upload_tasks
       set status = 'failed',
           error_message = coalesce(error_message, '任务超时未完成，已自动作废')
     where status = 'uploading'
       and updated_at < now() - make_interval(hours => stale_hours);
    get diagnostics affected = row_count;
    return affected;
end;
$$;

-- 示例（需先启用 pg_cron 扩展）：每天凌晨 3 点清理
-- select cron.schedule('expire-upload-tasks', '0 3 * * *',
--                      $$select public.expire_stale_upload_tasks(24)$$);
