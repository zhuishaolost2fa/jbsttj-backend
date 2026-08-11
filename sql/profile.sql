-- ============================================================
-- 用户个人信息表 profiles
-- 存储昵称、头像等可编辑的公开资料，与 auth.users 通过 id 关联。
-- 后端用 service_role key 直连，绕过 RLS；此处同时保留 RLS 策略，
-- 供前端用 anon key 直连时只能读写自己的资料（auth.uid() = id）。
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可。
-- ============================================================

create table if not exists public.profiles (
    id           uuid primary key,
    -- 不建外键：本项目鉴权走「服务间通道」，user_id 不强制绑定 auth.users，
    -- 与 upload_tasks / files 表保持一致，避免 service_role 写入受 FK 约束。
    nickname     text,
    avatar_url   text,
    -- 默认头像配色：未设置 avatar_url 时，前端按此索引渲染渐变首字母头像（0~7）
    avatar_color integer not null default 0 check (avatar_color between 0 and 7),
    bio          text,
    -- 性别：male / female / other（other 也用于「不愿透露」）
    gender       text check (gender is null or gender in ('male', 'female', 'other')),
    -- 生日，存日期即可，格式 YYYY-MM-DD
    birthday     date,
    -- 地区，存「省份 城市」拼接串，便于展示与检索
    region       text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

-- 兼容老表：若已存在旧 profiles 表，补上新增字段（已存在则不报错）
do $$
begin
    if not exists (select 1 from information_schema.columns where table_name = 'profiles' and column_name = 'avatar_color') then
        alter table public.profiles add column avatar_color integer not null default 0 check (avatar_color between 0 and 7);
    end if;
    if not exists (select 1 from information_schema.columns where table_name = 'profiles' and column_name = 'gender') then
        alter table public.profiles add column gender text check (gender is null or gender in ('male', 'female', 'other'));
    end if;
    if not exists (select 1 from information_schema.columns where table_name = 'profiles' and column_name = 'birthday') then
        alter table public.profiles add column birthday date;
    end if;
    if not exists (select 1 from information_schema.columns where table_name = 'profiles' and column_name = 'region') then
        alter table public.profiles add column region text;
    end if;
end $$;

-- updated_at 自动维护（touch_updated_at 已在 schema.sql 中定义）
drop trigger if exists trg_profiles_updated on public.profiles;
create trigger trg_profiles_updated
    before update on public.profiles
    for each row execute function public.touch_updated_at();

create index if not exists idx_profiles_updated on public.profiles (updated_at desc);

-- ------------------------------------------------------------
-- 行级安全策略
-- ------------------------------------------------------------
alter table public.profiles enable row level security;

drop policy if exists "own profile read" on public.profiles;
create policy "own profile read" on public.profiles
    for select
    using (auth.uid() = id);

drop policy if exists "own profile write" on public.profiles;
create policy "own profile write" on public.profiles
    for all
    using (auth.uid() = id)
    with check (auth.uid() = id);
