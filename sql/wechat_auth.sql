-- ============================================================
-- 微信小程序登录：第三方身份绑定表
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（幂等，可重复执行）。
--
-- 设计要点：
--   1. 本项目所有业务表的 user_id 都来自 Supabase auth.users(id)，
--      小程序用户必须映射到一个真实的 auth.users 行，才能复用现有 RLS /
--      profiles / script_requests 等全部逻辑，业务代码零改动。
--   2. 因此本表只做「openid → Supabase uuid」的映射，不承载登录态。
--      登录态仍然完全由 Supabase 签发的 JWT 承载。
--   3. 不建外键到 auth.users：与 profiles / upload_tasks 等表保持一致
--      （跨 schema 外键会让 service_role 写入受约束，且本项目允许
--       「服务间通道」写入不存在的 user_id）。
-- ============================================================

create table if not exists public.user_identities (
    id           uuid primary key default gen_random_uuid(),
    -- 对应的 Supabase auth.users(id)，即业务侧的 user_id
    user_id      uuid not null,
    -- 身份提供方：目前只有 'wechat'，预留 provider 字段便于后续扩展
    provider     text not null,
    -- 提供方内的唯一标识：小程序即 openid
    provider_uid text not null,
    -- 开放平台唯一标识：同一主体下多端（小程序 / 公众号 / App）打通用
    union_id     text,
    -- wx.login 换来的 session_key，用于解密加密数据（如旧版手机号解密）。
    -- 敏感：绝不下发给前端；新版 getPhoneNumber 走 code 方式，不需要它。
    session_key  text,
    session_key_updated_at timestamptz,
    -- code2session 原始响应（去掉 secret 等敏感项后的快照），便于排障
    raw          jsonb,
    created_at   timestamptz not null default now(),
    last_login_at timestamptz,
    constraint uq_user_identities unique (provider, provider_uid)
);

comment on table public.user_identities is '第三方登录身份绑定：openid/unionid -> auth.users(id)';
comment on column public.user_identities.session_key is '微信 session_key，服务端专用，禁止下发给客户端';

create index if not exists idx_user_identities_user on public.user_identities (user_id);
create index if not exists idx_user_identities_union on public.user_identities (union_id)
    where union_id is not null;

-- ------------------------------------------------------------
-- 行级安全策略
-- 后端用 service_role 直连（绕过 RLS）；这里保留策略，供前端用 anon key
-- 直连时只能看到自己的绑定关系。session_key 含敏感信息，任何情况下都
-- 不应被客户端读取 —— 前端没有理由直连本表，策略直接收紧到「仅自己」。
-- ------------------------------------------------------------
alter table public.user_identities enable row level security;

drop policy if exists "own identity read" on public.user_identities;
create policy "own identity read" on public.user_identities
    for select
    using (auth.uid() = user_id);

-- 写入只允许服务端（service_role）进行，客户端无 insert / update / delete 策略
-- ——RLS 默认拒绝未显式授权的操作，这里不建策略即是禁止。

-- ------------------------------------------------------------
-- profiles 补一列：登录来源。
-- 用途：微信用户没有真实邮箱，前端据此隐藏「修改邮箱 / 修改密码」入口，
-- 避免展示 wx_xxxxxxxx@wechat.local 这类占位邮箱。
-- ------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_name = 'profiles' and column_name = 'provider'
    ) then
        alter table public.profiles add column provider text;
    end if;
end $$;
