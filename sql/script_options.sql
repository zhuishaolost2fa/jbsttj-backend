-- ============================================================
-- 剧本杀筛选维度字典 · Supabase 表结构
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 设计要点：
--   1. 维度（玩法/题材/...）与选项拆成两张表，新增维度无需改代码与表结构；
--   2. 人数、时长这类「区间型」选项额外存 min_value / max_value，
--      后端可把选项直接翻译成范围查询，前端不需要硬编码任何数字；
--   3. 字典数据对所有人只读开放，写入仅限 service_role（后端）。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 筛选维度表
--    code 直接做主键：维度编码是对外 API 的一部分，稳定且可读，
--    比额外造一个 uuid 更利于前端按 /script-options/{code} 取数。
-- ------------------------------------------------------------
create table if not exists public.script_option_categories (
    code         text primary key,
    name         text not null,
    description  text,
    -- 前端筛选器是否允许多选，纯展示语义，由后端下发避免前端写死
    multi_select boolean not null default true,
    sort_order   integer not null default 0,
    is_active    boolean not null default true,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists idx_script_option_categories_sort
    on public.script_option_categories (sort_order, code)
    where is_active;

-- ------------------------------------------------------------
-- 2. 选项表
--    (category_code, code) 唯一：这是 PostgREST upsert 的前提，
--    灌数据脚本可重复执行而不产生重复行。
-- ------------------------------------------------------------
create table if not exists public.script_options (
    id            uuid primary key default gen_random_uuid(),
    category_code text not null
                  references public.script_option_categories (code)
                  on update cascade on delete cascade,
    code          text not null,
    label         text not null,
    -- 别名：用于关键词搜索命中「欢乐本」「哭哭本」这类口语叫法
    aliases       text[] not null default '{}',
    description   text,
    -- 区间语义：人数(unit=person) 与 时长(unit=minute) 使用，其余维度为 null
    min_value     integer,
    max_value     integer,
    unit          text check (unit is null or unit in ('person', 'minute')),
    sort_order    integer not null default 0,
    is_hot        boolean not null default false,
    is_active     boolean not null default true,
    extra         jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint uq_script_options_cat_code unique (category_code, code),
    -- 有区间必须成对出现且方向正确，防止脏数据流入范围查询
    constraint ck_script_options_range check (
        (min_value is null and max_value is null)
        or (min_value is not null and max_value is not null and min_value <= max_value)
    )
);

-- 列表查询主索引：按维度取、按 sort_order 排、只看启用项
create index if not exists idx_script_options_category
    on public.script_options (category_code, sort_order, code)
    where is_active;

create index if not exists idx_script_options_hot
    on public.script_options (category_code, sort_order)
    where is_active and is_hot;

-- 别名数组检索（aliases @> '{"哭哭本"}'）
create index if not exists idx_script_options_aliases
    on public.script_options using gin (aliases);

-- 标签模糊搜索加速（ilike），pg_trgm 在 schema.sql 中已启用，这里兜底
create extension if not exists pg_trgm;
create index if not exists idx_script_options_label_trgm
    on public.script_options using gin (label gin_trgm_ops);

-- ------------------------------------------------------------
-- 3. updated_at 自动维护
--    复用 schema.sql 里的 touch_updated_at()；若单独执行本文件则在此创建。
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

drop trigger if exists trg_script_option_categories_updated on public.script_option_categories;
create trigger trg_script_option_categories_updated
    before update on public.script_option_categories
    for each row execute function public.touch_updated_at();

drop trigger if exists trg_script_options_updated on public.script_options;
create trigger trg_script_options_updated
    before update on public.script_options
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 4. 行级安全策略
--    字典数据不含用户隐私，对 anon / authenticated 开放只读，
--    这样前端即使直连 Supabase 也能渲染筛选器；
--    写入不授予任何策略 —— 后端用 service_role key 绕过 RLS 独占写权限。
-- ------------------------------------------------------------
alter table public.script_option_categories enable row level security;
alter table public.script_options           enable row level security;

drop policy if exists "script option categories readable" on public.script_option_categories;
create policy "script option categories readable" on public.script_option_categories
    for select
    using (true);

drop policy if exists "script options readable" on public.script_options;
create policy "script options readable" on public.script_options
    for select
    using (true);

-- ------------------------------------------------------------
-- 5. 便捷视图：一次性拿到「维度 + 其下选项」的聚合结构
--    前端首屏渲染筛选器只需查这一个视图。
-- ------------------------------------------------------------
create or replace view public.script_option_tree as
select
    c.code           as category_code,
    c.name           as category_name,
    c.description    as category_description,
    c.multi_select,
    c.sort_order     as category_sort_order,
    coalesce(
        (
            select jsonb_agg(
                       jsonb_build_object(
                           'code',        o.code,
                           'label',       o.label,
                           'aliases',     o.aliases,
                           'description', o.description,
                           'min_value',   o.min_value,
                           'max_value',   o.max_value,
                           'unit',        o.unit,
                           'sort_order',  o.sort_order,
                           'is_hot',      o.is_hot
                       )
                       order by o.sort_order, o.code
                   )
            from public.script_options o
            where o.category_code = c.code
              and o.is_active
        ),
        '[]'::jsonb
    ) as options
from public.script_option_categories c
where c.is_active
order by c.sort_order, c.code;
