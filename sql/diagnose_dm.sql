-- ============================================================
-- DM 手册解析 · 数据库诊断脚本
-- ------------------------------------------------------------
-- 适用：Supabase Dashboard -> SQL Editor（整段选中执行）
-- 权限：需要能读 public 表的权限（Dashboard 默认有；anon 不行，走后端）
-- 性质：只读，不修改任何数据
-- 用法：只改下面「参数区」那一行，其余整段执行即可
-- ============================================================

-- ===================== 参数区（只改这一行） =====================
drop table if exists _diag_params;
create temp table _diag_params (script_id text, script_code text);
-- 第一个值填剧本 UUID，或留 ''；第二个值填业务 code（如 nian-lun），或留 ''
insert into _diag_params values (''::text, 'nian-lun'::text);

-- 解析出目标剧本（后续所有查询都基于这张临时表，无需再改参数）
drop table if exists _diag_script;
create temp table _diag_script as
select s.id, s.code, s.title, s.status,
       s.extra->'dmGuide' as dm_guide
from public.scripts s, _diag_params p
where (p.script_id = '' or s.id::text = p.script_id)
  and (p.script_code = '' or s.code = p.script_code);

do $$
begin
  if not exists (select 1 from _diag_script) then
    raise notice '⚠ 没有匹配到剧本，请检查参数区的 script_id / script_code';
  end if;
end $$;


-- ===================== ① 剧本概要：是否挂了手册 =====================
-- 关注：dm_guide 是否为 null、是否缺 objectKey、status 是否为 published
select id, code, title, status,
       dm_guide,
       (dm_guide ->> 'objectKey') is null as missing_object_key
from _diag_script;


-- ===================== ② 流水线任务（核心） =====================
-- status 含义：
--   pending/downloading/extracting/chunking/generating_qa/embedding = 还在跑
--   completed = 成功   failed = 看 error_message   skipped = 命中旧版复用
--   cancelled = 被 force 重跑顶掉
-- since_update 很大且计数不涨 => worker 死了 / RabbitMQ 断了（卡住）
-- 若这一行【完全为空】=> 自动触发被 maybe_trigger 静默吞了，去看后端日志
select j.id, j.status, j.stage_detail, j.error_message,
       j.total_pages, j.processed_pages,
       j.total_chunks, j.embedded_chunks,
       j.total_qa, j.embedded_qa,
       j.dropped_chunks, j.retry_count,
       j.started_at, j.finished_at, j.updated_at,
       now() - j.updated_at as since_update
from public.script_dm_jobs j
join _diag_script s on j.script_id = s.id
order by j.created_at desc
limit 10;


-- ===================== ③ 索引文档 =====================
-- is_active=false => 没被激活，检索查不到
-- total_chunks/total_qa=0 => 解析没产出，回看 ② 的 failed
-- dropped_chunks 很高 => 手册版式噪声多（去重狠），属正常但可关注
select d.id, d.is_active, d.version,
       d.total_pages, d.total_chunks, d.total_qa, d.dropped_chunks,
       d.embed_model, d.chat_model,
       d.content_hash, d.created_at
from public.script_dm_documents d
join _diag_script s on d.script_id = s.id
where d.deleted_at is null
order by d.created_at desc;


-- ===================== ④ 向量落库缺口 =====================
-- null_embed > 0 => 部分向量化没跑完/失败，检索会漏数据
select 'chunks' as obj,
       count(*) as total,
       count(*) filter (where embedding is null) as null_embed
from public.script_dm_chunks c
join _diag_script s on c.script_id = s.id
union all
select 'qa',
       count(*),
       count(*) filter (where embedding is null)
from public.script_dm_qa q
join _diag_script s on q.script_id = s.id;


-- ===================== ⑤ 一键概览（官方视图） =====================
select *
from public.script_dm_overview
where script_code = (select code from _diag_script);


-- ===================== ⑥ 健康体检（自动标问题） =====================
-- 一行看完：有没有任务、最新状态、文档是否激活、向量是否缺、手册是否挂了
with latest as (
  select distinct on (script_id) *
  from public.script_dm_jobs
  where script_id = (select id from _diag_script)
  order by script_id, created_at desc
),
doc as (
  select distinct on (script_id) *
  from public.script_dm_documents
  where script_id = (select id from _diag_script)
    and deleted_at is null
  order by script_id, created_at desc
)
select s.code, s.title,
       (select count(*) from public.script_dm_jobs
         where script_id = s.id) as job_count,
       (select status from latest) as latest_job_status,
       (select error_message from latest) as latest_error,
       (select is_active from doc) as doc_active,
       (select total_chunks from doc) as doc_chunks,
       (select total_qa from doc) as doc_qa,
       (select count(*) filter (where embedding is null)
          from public.script_dm_chunks where script_id = s.id) as chunks_null_embed,
       (select count(*) filter (where embedding is null)
          from public.script_dm_qa where script_id = s.id) as qa_null_embed,
       case
         when (select dm_guide from _diag_script) is null
           then '❌ 未挂 DM 手册（extra.dmGuide 缺失）'
         when (select dm_guide->>'objectKey' from _diag_script) is null
           then '❌ 手册缺 objectKey'
         when (select count(*) from public.script_dm_jobs
                where script_id = s.id) = 0
           then '⚠ 无任务记录：自动触发可能被静默吞（查后端日志 / 手动 ingest）'
         when (select status from latest) = 'failed'
           then '❌ 任务失败：' || (select error_message from latest)
         when (select is_active from doc) is not true
           then '⚠ 文档未激活，检索查不到'
         when (select count(*) filter (where embedding is null)
                from public.script_dm_chunks where script_id = s.id) > 0
           then '⚠ 分块向量有缺口'
         when (select count(*) filter (where embedding is null)
                from public.script_dm_qa where script_id = s.id) > 0
           then '⚠ QA 向量有缺口'
         else '✅ 正常'
       end as verdict
from _diag_script s;


-- ============================================================
-- 附：症状 -> 根因 速查（对照 ② ③ ④ 的结果）
-- ------------------------------------------------------------
-- 手册完全没解析            -> ① dm_guide 为 null / 缺 objectKey；或 ② 无任务记录（自动触发被吞）
-- 进度一直 0 不动           -> ② since_update 很大且计数不涨 => Celery worker 没起 / RabbitMQ 断
-- 任务 failed              -> ② error_message（PDF 超限 / OSS key 错 / 向量维度不匹配 / RAG 配置缺）
-- job completed 但搜不到   -> ③ is_active=false；或 ④ embedding is null > 0
-- 检索结果很杂             -> 去重率高但阈值低，调 minSimilarity（<0.25 基本是噪声）
-- ============================================================
