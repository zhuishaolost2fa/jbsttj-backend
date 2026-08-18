-- ============================================================
-- DM 主持人手册 · 用户提问沉淀与真人解答
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/scripts.sql、sql/dm_rag.sql（本模块的 script_id 外键指向 public.scripts）
--
-- 业务背景：
--   /dm-guide/ask 检索到的最高相似度过低时，透出的答案没有意义，
--   但「用户问了这个、手册没覆盖」本身就是高价值信息 ——
--   它说明手册/QA 库有盲区。这里把这类问题按剧本维度沉淀下来：
--     1. 同一剧本同一问题只存一行，重复提问原子累加 ask_count；
--     2. 真人（主持人/运营）通过后台接口补充答案，问题转为 answered；
--     3. 按剧本取 ask_count 最高的前 N 条作为「引导问题」，
--        展示在剧本问答页，减少重复提问、沉淀真人经验。
--
-- 设计要点：
--   1. 幂等下沉到数据库：record_dm_question 用 insert ... on conflict
--      原子自增，两个用户同时问同一问题不会写出重复行；
--   2. 聚合维度是 script_code 而非 script_id：同名剧本拆成多个分片时，
--      用户问题应该归并到「这部剧本」下，与 dm_rag.sql 的聚合口径一致；
--   3. 与 script_dm_qa 刻意分开：那里是手册自动生成的 QA（带向量），
--      这里是用户真实提问（低相似度才落库、带人气计数），
--      两者的生命周期与质量水位完全不同，混在一张表里会互相污染。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 用户提问表
-- ------------------------------------------------------------
create table if not exists public.script_dm_questions (
    id            uuid primary key default gen_random_uuid(),
    script_id     uuid not null references public.scripts (id) on delete cascade,
    -- 基于剧本中文名派生的稳定业务编码：同名分片聚合，与 script_dm_qa.script_code 同口径
    script_code   text not null default '',

    question      text not null,
    -- 归一化问题的 SHA256（与 dedup.content_hash 同算法），同一剧本内去重键
    question_hash text not null,

    -- 被问次数：重复提问由 record_dm_question 原子 +1，是「引导问题」的排序依据
    ask_count     integer not null default 1,
    -- 历次提问中检索到的最高原始相似度，用于评估手册盲区严重程度
    best_similarity double precision not null default 0,

    -- pending 待解答 / answered 真人已解答 / dismissed 无效问题（广告、闲聊等）
    status        text not null default 'pending',

    -- 真人解答：answered 时必填
    answer        text,
    answered_by   uuid,
    answered_at   timestamptz,

    -- 首次提问的用户（ask 接口要求登录，正常不会为空；匿名历史数据留 NULL）
    created_by    uuid,

    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    constraint uq_dm_question_hash unique (script_code, question_hash),
    constraint ck_dm_question_status check (status in ('pending', 'answered', 'dismissed'))
);

-- ------------------------------------------------------------
-- 1.1 提问者 / 解答者昵称与头像：**不冗余存储**，读取时由服务层
--     按 created_by / answered_by 批量关联 profiles 实时合并。
--     （曾经实现过「快照列 + MQ 同步」方案，对这个数据量级属于
--     过度设计，已回退；下面是清理语句，重复执行即可收敛旧库。）
-- ------------------------------------------------------------
alter table public.script_dm_questions drop column if exists created_by_nickname;
alter table public.script_dm_questions drop column if exists created_by_avatar_url;
alter table public.script_dm_questions drop column if exists created_by_avatar_color;
alter table public.script_dm_questions drop column if exists answered_by_nickname;
alter table public.script_dm_questions drop column if exists answered_by_avatar_url;
alter table public.script_dm_questions drop column if exists answered_by_avatar_color;
drop index if exists public.idx_dm_question_created_by;
drop index if exists public.idx_dm_question_answered_by;

-- 「待解答列表」与「引导问题」的最高频查询都是按剧本维度过滤
create index if not exists idx_dm_question_code_status
    on public.script_dm_questions (script_code, status);

-- 引导问题排序：人气优先
create index if not exists idx_dm_question_code_hot
    on public.script_dm_questions (script_code, ask_count desc, created_at desc);

create index if not exists idx_dm_question_script
    on public.script_dm_questions (script_id, created_at desc);

-- ------------------------------------------------------------
-- 2. updated_at 自动维护（复用 dm_rag.sql 里的 touch_updated_at）
-- ------------------------------------------------------------
drop trigger if exists trg_dm_question_updated on public.script_dm_questions;
create trigger trg_dm_question_updated
    before update on public.script_dm_questions
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 3. 提问记录（幂等 + 原子计数）
--    同一剧本同一问题：ask_count + 1，best_similarity 取历史最大，
--    created_by 只记录首个提问者，重复提问不覆盖。
--    已解答的问题被再次问到时不重置状态 —— 答案仍然有效，
--    只是人气继续累计（说明这个答案展示得不够显眼）。
-- ------------------------------------------------------------
-- 旧版快照方案曾给本函数加过 3 个资料参数：create or replace 不换签名会产生
-- 重载残留，这里先按旧签名显式 drop，再重建 6 参数版本。
drop function if exists public.record_dm_question(uuid, text, text, text, double precision, uuid, text, text, integer);

create or replace function public.record_dm_question(
    p_script_id       uuid,
    p_script_code     text,
    p_question        text,
    p_question_hash   text,
    p_best_similarity double precision default 0,
    p_created_by      uuid default null
)
returns public.script_dm_questions
language plpgsql
security definer
set search_path = public
as $$
declare
    result public.script_dm_questions;
begin
    insert into public.script_dm_questions (
        script_id, script_code, question, question_hash,
        ask_count, best_similarity, created_by
    )
    values (
        p_script_id, p_script_code, p_question, p_question_hash,
        1, greatest(coalesce(p_best_similarity, 0), 0), p_created_by
    )
    on conflict (script_code, question_hash) do update
       set ask_count       = public.script_dm_questions.ask_count + 1,
           best_similarity = greatest(public.script_dm_questions.best_similarity,
                                      greatest(coalesce(p_best_similarity, 0), 0))
    returning * into result;

    return result;
end;
$$;

-- 旧版「快照 + MQ 同步」方案的刷新函数，已随方案回退一并删除
drop function if exists public.sync_dm_question_user(uuid, text, text, integer);

-- ------------------------------------------------------------
-- 4. 行级安全策略
--    与 dm_rag 各表一致：不开放 anon 直读，一律走后端 service_role。
-- ------------------------------------------------------------
alter table public.script_dm_questions enable row level security;
