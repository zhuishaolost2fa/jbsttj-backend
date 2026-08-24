-- ============================================================
-- 剧本库 · Supabase 表结构
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/script_options.sql（本表的玩法/题材/发行方式/难度均引用字典编码）
--       请先执行字典表 SQL，再执行本文件。
--
-- 设计要点：
--   1. 「字典驱动」：release_type / difficulty / playstyles / themes 存的都是
--      script_options 里的 code，不存中文，前端筛选时直接拿字典 code 回传即可；
--   2. 人数与时长都拆成 min/max 两列，字典里「6人」「4-6小时」这类区间选项
--      可以直接翻译成 player_min<=X<=player_max 的范围查询，无需前端硬编码；
--   3. 编码合法性由触发器统一校验（数组字段无法用外键），错误信息为中文；
--   4. code 唯一：这是 PostgREST upsert 的前提，灌数据脚本可重复执行不产生重复行；
--   5. 软删除：deleted_at 而非物理删除，避免误删已被订单/评价引用的剧本。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 剧本主表
-- ------------------------------------------------------------
create table if not exists public.scripts (
    id           uuid primary key default gen_random_uuid(),

    -- 业务编码（英文 slug），对外稳定标识，也是 upsert 的冲突键
    code         text not null,
    title        text not null,
    -- 别名 / 副标题 / 系列名，用于搜索命中
    aliases      text[] not null default '{}',
    summary      text,
    author       text,
    publisher    text,

    -- ---- 字典维度（存 script_options.code） ----
    release_type text,                              -- 发行方式：boxed / city_limited / exclusive ...
    difficulty   text,                              -- 难度：beginner / intermediate / advanced / expert
    playstyles   text[] not null default '{}',      -- 玩法，可多值
    themes       text[] not null default '{}',      -- 题材，可多值
    -- 自由标签：字典里没有、但值得展示的关键词（如「暴风雪山庄」「叙诡」）
    tags         text[] not null default '{}',

    -- ---- 人数配置 ----
    player_min      integer,
    player_max      integer,
    male_count      integer,
    female_count    integer,
    -- 「任意性别」位数量，部分剧本有 1 个不限性别的角色
    flexible_count  integer not null default 0,
    allow_gender_swap boolean,                      -- 是否可反串，null 表示未知

    -- ---- 时长（分钟） ----
    duration_min integer,
    duration_max integer,

    -- ---- 口碑数据 ----
    rating       numeric(3, 1),                     -- 评分，0.0 ~ 10.0
    rating_count integer not null default 0,
    play_count   integer not null default 0,        -- 玩过/标记人数，用于热度排序
    published_year integer,

    cover_url    text,
    is_recommended boolean not null default false,
    -- 上下架状态：published 前台可见 / draft 草稿 / offline 已下架
    status       text not null default 'published',

    -- 数据来源说明，标注该条记录的信息出处，便于后续核对与替换
    source       text,
    extra        jsonb not null default '{}'::jsonb,

    created_by   uuid,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    deleted_at   timestamptz,

    constraint uq_scripts_code unique (code),
    -- slug 规范：小写字母、数字、连字符，避免出现空格与大小写歧义
    constraint ck_scripts_code_format check (code ~ '^[a-z0-9][a-z0-9-]{1,63}$'),
    constraint ck_scripts_status check (status in ('published', 'draft', 'offline')),
    constraint ck_scripts_players check (
        (player_min is null and player_max is null)
        or (player_min is not null and player_max is not null
            and player_min >= 1 and player_min <= player_max and player_max <= 50)
    ),
    constraint ck_scripts_duration check (
        (duration_min is null and duration_max is null)
        or (duration_min is not null and duration_max is not null
            and duration_min >= 0 and duration_min <= duration_max and duration_max <= 2880)
    ),
    constraint ck_scripts_rating check (rating is null or (rating >= 0 and rating <= 10))
);

-- ------------------------------------------------------------
-- 2. 索引
-- ------------------------------------------------------------
-- 列表页主索引：只看已上架未删除，按热度/评分排
create index if not exists idx_scripts_listing
    on public.scripts (play_count desc, rating desc nulls last)
    where deleted_at is null and status = 'published';

create index if not exists idx_scripts_rating
    on public.scripts (rating desc nulls last)
    where deleted_at is null and status = 'published';

create index if not exists idx_scripts_created
    on public.scripts (created_at desc)
    where deleted_at is null;

-- 人数 / 时长范围过滤
create index if not exists idx_scripts_player_range
    on public.scripts (player_min, player_max)
    where deleted_at is null;

create index if not exists idx_scripts_duration_range
    on public.scripts (duration_min, duration_max)
    where deleted_at is null;

-- 标量字典维度
create index if not exists idx_scripts_release_type
    on public.scripts (release_type)
    where deleted_at is null;

create index if not exists idx_scripts_difficulty
    on public.scripts (difficulty)
    where deleted_at is null;

-- 数组维度用 GIN，支持 playstyles && '{happy,emotional}' 这类「任一命中」查询
create index if not exists idx_scripts_playstyles on public.scripts using gin (playstyles);
create index if not exists idx_scripts_themes     on public.scripts using gin (themes);
create index if not exists idx_scripts_tags       on public.scripts using gin (tags);
create index if not exists idx_scripts_aliases    on public.scripts using gin (aliases);

-- 标题模糊搜索
create extension if not exists pg_trgm;
create index if not exists idx_scripts_title_trgm
    on public.scripts using gin (title gin_trgm_ops);

-- ------------------------------------------------------------
-- 3. updated_at 自动维护（复用字典表里的同名函数，单独执行本文件时在此兜底创建）
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

drop trigger if exists trg_scripts_updated on public.scripts;
create trigger trg_scripts_updated
    before update on public.scripts
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 4. 字典编码合法性校验
--    标量字段本可以用外键，但 playstyles / themes 是数组，外键管不了；
--    为了「一处校验、错误信息统一」，四个字段全部走同一个触发器。
-- ------------------------------------------------------------
create or replace function public.validate_script_option_codes()
returns trigger
language plpgsql
as $$
declare
    invalid text;
begin
    if new.release_type is not null and not exists (
        select 1 from public.script_options o
        where o.category_code = 'release' and o.code = new.release_type
    ) then
        raise exception '未知的发行方式编码: %', new.release_type using errcode = '23514';
    end if;

    if new.difficulty is not null and not exists (
        select 1 from public.script_options o
        where o.category_code = 'difficulty' and o.code = new.difficulty
    ) then
        raise exception '未知的难度编码: %', new.difficulty using errcode = '23514';
    end if;

    select string_agg(c, ', ') into invalid
    from unnest(new.playstyles) as c
    where not exists (
        select 1 from public.script_options o
        where o.category_code = 'playstyle' and o.code = c
    );
    if invalid is not null then
        raise exception '未知的玩法编码: %', invalid using errcode = '23514';
    end if;

    select string_agg(c, ', ') into invalid
    from unnest(new.themes) as c
    where not exists (
        select 1 from public.script_options o
        where o.category_code = 'theme' and o.code = c
    );
    if invalid is not null then
        raise exception '未知的题材编码: %', invalid using errcode = '23514';
    end if;

    return new;
end;
$$;

drop trigger if exists trg_scripts_validate_codes on public.scripts;
create trigger trg_scripts_validate_codes
    before insert or update of release_type, difficulty, playstyles, themes
    on public.scripts
    for each row execute function public.validate_script_option_codes();

-- ------------------------------------------------------------
-- 5. 行级安全策略
--    剧本库是公开内容，anon / authenticated 可读「已上架且未删除」的记录；
--    写入不授予任何策略 —— 后端用 service_role key 绕过 RLS 独占写权限。
-- ------------------------------------------------------------
alter table public.scripts enable row level security;

drop policy if exists "scripts readable" on public.scripts;
create policy "scripts readable" on public.scripts
    for select
    using (deleted_at is null and status = 'published');

-- ------------------------------------------------------------
-- 6. 便捷视图：把字典编码翻译成中文标签
--    给运营后台、导出报表用；前端列表接口走后端 API，不必依赖本视图。
-- ------------------------------------------------------------
create or replace view public.scripts_labeled as
select
    s.id,
    s.code,
    s.title,
    s.author,
    s.publisher,
    s.summary,
    s.player_min,
    s.player_max,
    s.male_count,
    s.female_count,
    s.duration_min,
    s.duration_max,
    s.rating,
    s.play_count,
    s.published_year,
    s.status,
    (select o.label from public.script_options o
      where o.category_code = 'release' and o.code = s.release_type)    as release_label,
    (select o.label from public.script_options o
      where o.category_code = 'difficulty' and o.code = s.difficulty)   as difficulty_label,
    coalesce((
        select array_agg(o.label order by o.sort_order)
        from public.script_options o
        where o.category_code = 'playstyle' and o.code = any (s.playstyles)
    ), '{}')                                                            as playstyle_labels,
    coalesce((
        select array_agg(o.label order by o.sort_order)
        from public.script_options o
        where o.category_code = 'theme' and o.code = any (s.themes)
    ), '{}')                                                            as theme_labels,
    s.tags,
    s.created_at,
    s.updated_at
from public.scripts s
where s.deleted_at is null;

-- ------------------------------------------------------------
-- 7. 浏览量（view_count）
--    列表接口透出、详情接口每次访问 +1。
--    计数走 RPC 原子自增（increment_script_view），避免「读-改-写」竞态。
--    函数默认只授权 service_role：anon / authenticated 直接调会被刷浏览量。
-- ------------------------------------------------------------
alter table public.scripts add column if not exists view_count integer not null default 0;

create or replace function public.increment_script_view(p_script_id uuid)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    update public.scripts
       set view_count = view_count + 1
     where id = p_script_id
       and deleted_at is null
    returning view_count into v_count;
    return coalesce(v_count, 0);
end;
$$;

revoke execute on function public.increment_script_view(uuid) from public;
grant execute on function public.increment_script_view(uuid) to service_role;
