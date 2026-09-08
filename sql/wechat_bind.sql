-- 微信账号与邮箱账号互通（增量变更，可重复执行）
--
-- 背景：微信登录原本依赖「占位邮箱 + 确定性密码」走 password grant。
-- 但 GoTrue 一个账号只有一个密码，微信用户一旦绑定真实邮箱并自设密码，
-- 两种登录方式就会互相顶掉。现改为 magiclink 免密签发（不依赖密码），
-- 于是微信与邮箱可以作为两条独立凭证共存于同一个 user_id 下。
--
-- 本文件只做增量，不重建既有表；已执行过 wechat_auth.sql 的实例直接跑即可。

-- ------------------------------------------------------------
-- 1. 绑定表补「邮箱快照」
--    签发 magiclink 时 GoTrue 只认 email（实测传 user_id 会 400），
--    所以每次登录都需要拿到该用户当前邮箱。这里缓存一份，
--    省掉一次 admin 接口调用；真正的权威来源仍是 auth.users。
-- ------------------------------------------------------------
alter table public.user_identities
    add column if not exists email_snapshot text;

comment on column public.user_identities.email_snapshot is
    '建号/绑定时的 auth.users.email 快照；仅作缓存，权威来源为 auth.users';

-- ------------------------------------------------------------
-- 2. 按 user_id + provider 的查询是主路径，补复合索引
-- ------------------------------------------------------------
create index if not exists idx_user_identities_user_provider
    on public.user_identities (user_id, provider);

-- ------------------------------------------------------------
-- 3. profiles 补「微信是否已绑定」的冗余标记
--    前端安全页要据此决定显示「绑定微信」还是「已绑定」。
--    权威来源是 user_identities，这里只是让 /auth/me 少查一次表。
-- ------------------------------------------------------------
alter table public.profiles
    add column if not exists wechat_bound boolean not null default false;

comment on column public.profiles.wechat_bound is
    '是否已绑定微信身份；权威来源为 user_identities，本列只供 /auth/me 快速读取';
