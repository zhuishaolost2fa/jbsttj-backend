-- ============================================================
-- DM 主持人手册 · 向量检索（RAG）表结构
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/scripts.sql（本模块的 script_id 外键指向 public.scripts）
--
-- 设计要点：
--   1. 「文档—分块—问答」三级结构：一个剧本可以有多版 DM 手册（重新上传即新版本），
--      每版手册切成若干 chunk，每个 chunk 派生若干问答对，删除手册可级联清理；
--   2. 去重下沉到数据库：content_hash 上的唯一约束是**最后一道防线**，
--      应用层 Redis 判重挂掉时也不会写出重复向量；
--   3. 向量列固定 vector(1024)，对应 BAAI/bge-large-zh-v1.5 的输出维度。
--      换模型必须同步改这里的维度、重建索引，并把存量向量全部重算；
--   4. HNSW 而非 IVFFlat：手册是持续增量写入的，IVFFlat 需要预先有代表性数据才能
--      训练出好的聚类中心，冷启动召回差；HNSW 无需训练、增量写入友好；
--   5. 进度计数走 SQL 函数原子自增：四个任务在不同 worker 上并行回写同一行，
--      读-改-写会丢更新。
-- ============================================================

-- ------------------------------------------------------------
-- 0. 扩展
-- ------------------------------------------------------------
create extension if not exists vector;
create extension if not exists pg_trgm;

-- ------------------------------------------------------------
-- 1. DM 手册文档表
--    以「文件内容指纹」为幂等键：同一份 PDF 重复提交不会重复建档，
--    直接复用已完成的解析结果，省掉整条流水线的算力与 API 花销。
-- ------------------------------------------------------------
create table if not exists public.script_dm_documents (
    id            uuid primary key default gen_random_uuid(),
    script_id     uuid not null references public.scripts (id) on delete cascade,
    -- 基于剧本中文名派生的稳定业务编码：同名剧本被拆成多个 script_id 时，用它聚合检索
    script_code   text not null default '',

    -- OSS 定位信息，来自 scripts.extra->'dmGuide'
    file_id       text,
    object_key    text not null,
    file_name     text,
    file_size     bigint,
    -- 文件内容指纹（PDF 字节流 SHA256），幂等键
    content_hash  text not null,

    -- 解析产物统计
    total_pages   integer not null default 0,
    total_chunks  integer not null default 0,
    total_qa      integer not null default 0,
    -- 被去重丢弃的块数，用于评估手册的重复率（版式噪声多的手册这个值会很高）
    dropped_chunks integer not null default 0,

    -- 同一剧本可有多版手册，只有 is_active 的那版参与检索
    version       integer not null default 1,
    is_active     boolean not null default true,

    embed_model   text,
    chat_model    text,

    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    deleted_at    timestamptz,

    constraint uq_dm_doc_hash unique (script_id, content_hash)
);

-- 兼容早于本版本建好的库：老表没有 script_code，需要显式补列。
alter table public.script_dm_documents add column if not exists script_code text not null default '';

update public.script_dm_documents d
   set script_code = s.code
  from public.scripts s
 where d.script_id = s.id
   and coalesce(d.script_code, '') = '';

create index if not exists idx_dm_doc_script
    on public.script_dm_documents (script_id, is_active)
    where deleted_at is null;

create index if not exists idx_dm_doc_script_code
    on public.script_dm_documents (script_code, is_active)
    where deleted_at is null;

create index if not exists idx_dm_doc_object_key
    on public.script_dm_documents (object_key)
    where deleted_at is null;

-- ------------------------------------------------------------
-- 2. 分块表（正文向量）
-- ------------------------------------------------------------
create table if not exists public.script_dm_chunks (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references public.script_dm_documents (id) on delete cascade,
    -- 冗余 script_id：检索时按剧本过滤是最高频路径，避免每次都 join 文档表
    script_id     uuid not null references public.scripts (id) on delete cascade,
    -- 冗余 script_code：同名剧本拆成多个 script_id 时，可按业务 code 聚合检索
    script_code   text not null default '',

    chunk_index   integer not null,
    content       text not null,
    -- 归一化文本的 SHA256，精确去重用
    content_hash  text not null,
    -- SimHash 64 位指纹（存为有符号 bigint），近似去重与事后审计用
    simhash       bigint,

    -- 溯源信息：命中后前端可以直接跳到手册第几页
    page_start    integer,
    page_end      integer,
    -- 章节路径，如 {'第三章 案件还原','3.2 关键物证'}，作为检索结果的面包屑
    section_path  text[] not null default '{}',
    -- 块类型：body 正文 / heading 标题段 / table 表格 / list 列表
    block_type    text not null default 'body',

    char_count    integer not null default 0,
    embedding     vector(1024),

    created_at    timestamptz not null default now(),

    -- 同一文档内内容指纹唯一：数据库侧的去重兜底
    constraint uq_dm_chunk_hash unique (document_id, content_hash),
    constraint ck_dm_chunk_block_type check (block_type in ('body', 'heading', 'table', 'list'))
);

alter table public.script_dm_chunks add column if not exists script_code text not null default '';

update public.script_dm_chunks c
   set script_code = s.code
  from public.scripts s
 where c.script_id = s.id
   and coalesce(c.script_code, '') = '';

create index if not exists idx_dm_chunk_doc
    on public.script_dm_chunks (document_id, chunk_index);

create index if not exists idx_dm_chunk_script
    on public.script_dm_chunks (script_id);

create index if not exists idx_dm_chunk_script_code
    on public.script_dm_chunks (script_code);

-- SimHash 分段索引：跨文档找近似重复段落时按高 16 位快速圈定候选
create index if not exists idx_dm_chunk_simhash
    on public.script_dm_chunks (simhash)
    where simhash is not null;

-- 正文全文模糊搜索，与向量检索组成混合召回
create index if not exists idx_dm_chunk_content_trgm
    on public.script_dm_chunks using gin (content gin_trgm_ops);

-- ★ HNSW 向量索引
--   m=16：每个节点的双向连接数，16 是召回率与内存占用的平衡点；
--   ef_construction=64：建图时的候选队列长度，越大图质量越好、建索引越慢。
--   余弦距离（vector_cosine_ops）匹配 bge 系列的训练目标，不要换成 L2。
create index if not exists idx_dm_chunk_embedding_hnsw
    on public.script_dm_chunks using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- ------------------------------------------------------------
-- 3. 问答对表（问题向量）
--    问答对与正文块分开建索引：用户提问与「问题」的语义距离，
--    通常比与「正文段落」的距离更近，两路召回融合后准确率明显高于单路。
-- ------------------------------------------------------------
create table if not exists public.script_dm_qa (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references public.script_dm_documents (id) on delete cascade,
    script_id     uuid not null references public.scripts (id) on delete cascade,
    -- 基于剧本中文名派生的稳定业务编码：同名剧本被拆成多个 script_id 时，用它聚合检索
    script_code   text not null default '',
    -- 来源块，允许为空（块被删除后问答对仍可保留）
    chunk_id      uuid references public.script_dm_chunks (id) on delete set null,

    question      text not null,
    answer        text not null,
    -- 归一化问题的 SHA256，防止不同块生成出重复问题
    question_hash text not null,

    page_start    integer,
    page_end      integer,
    section_path  text[] not null default '{}',
    -- 问答对类别：rule 规则 / plot 剧情 / role 角色 / clue 线索 / flow 流程 / other
    category      text not null default 'other',

    embedding     vector(1024),
    created_at    timestamptz not null default now(),

    constraint uq_dm_qa_hash unique (document_id, question_hash)
);

alter table public.script_dm_qa add column if not exists script_code text not null default '';

update public.script_dm_qa q
   set script_code = s.code
  from public.scripts s
 where q.script_id = s.id
   and coalesce(q.script_code, '') = '';

-- 提供者：记录是谁上传的手册产出了这条问答（与 script_dm_jobs.created_by 同源）。
-- 冗余到问答对层面，便于按用户追溯「这条问答出自谁上传的剧本」。
-- 历史问答没有对应的上传记录，留 NULL 即可。
alter table public.script_dm_qa add column if not exists created_by uuid;

create index if not exists idx_dm_qa_doc      on public.script_dm_qa (document_id);
create index if not exists idx_dm_qa_script   on public.script_dm_qa (script_id);
create index if not exists idx_dm_qa_code     on public.script_dm_qa (script_code);
create index if not exists idx_dm_qa_chunk    on public.script_dm_qa (chunk_id);
create index if not exists idx_dm_qa_category on public.script_dm_qa (category);
create index if not exists idx_dm_qa_created_by on public.script_dm_qa (created_by);

create index if not exists idx_dm_qa_question_trgm
    on public.script_dm_qa using gin (question gin_trgm_ops);

create index if not exists idx_dm_qa_embedding_hnsw
    on public.script_dm_qa using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- ------------------------------------------------------------
-- 4. 流水线任务表
--    四个 Celery 任务分布在不同 worker 上并行回写，所有计数字段
--    都必须通过下面的 bump_dm_job_progress 原子自增。
-- ------------------------------------------------------------
create table if not exists public.script_dm_jobs (
    id             uuid primary key default gen_random_uuid(),
    script_id      uuid not null references public.scripts (id) on delete cascade,
    script_code    text not null default '',
    document_id    uuid references public.script_dm_documents (id) on delete set null,

    object_key     text not null,
    file_name      text,
    celery_task_id text,
    created_by     uuid,

    -- pending 排队 / downloading 下载 / extracting 提取 / chunking 分块
    -- / generating_qa 生成问答 / embedding 向量化
    -- / completed 完成 / failed 失败 / cancelled 被取消或被强制重跑取代 / skipped 命中幂等跳过
    status         text not null default 'pending',
    stage_detail   text,

    total_pages       integer not null default 0,
    processed_pages   integer not null default 0,
    total_shards      integer not null default 0,
    finished_shards   integer not null default 0,
    total_chunks      integer not null default 0,
    dropped_chunks    integer not null default 0,
    total_qa          integer not null default 0,
    embedded_chunks   integer not null default 0,
    embedded_qa       integer not null default 0,

    error_message  text,
    retry_count    integer not null default 0,

    started_at     timestamptz,
    finished_at    timestamptz,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint ck_dm_job_status check (status in (
        'pending', 'downloading', 'extracting', 'chunking',
        'generating_qa', 'embedding',
        'completed', 'failed', 'cancelled', 'skipped'
    ))
);

-- 兼容早于本版本建好的库：create table if not exists 对已存在的表不生效，
-- 列和 check 约束的变更必须显式补。缺了这段，老库会在 QA 阶段写状态时
-- 撞上 ck_dm_job_status 直接失败 —— 而且是任务跑了十几分钟之后才失败。
alter table public.script_dm_jobs add column if not exists script_code text not null default '';
alter table public.script_dm_jobs add column if not exists created_by uuid;

update public.script_dm_jobs j
   set script_code = s.code
  from public.scripts s
 where j.script_id = s.id
   and coalesce(j.script_code, '') = '';
alter table public.script_dm_jobs drop constraint if exists ck_dm_job_status;
alter table public.script_dm_jobs add constraint ck_dm_job_status check (status in (
    'pending', 'downloading', 'extracting', 'chunking',
    'generating_qa', 'embedding',
    'completed', 'failed', 'cancelled', 'skipped'
));

create index if not exists idx_dm_job_script  on public.script_dm_jobs (script_id, created_at desc);
create index if not exists idx_dm_job_script_code on public.script_dm_jobs (script_code, created_at desc);
drop index if exists public.idx_dm_job_status;
create index if not exists idx_dm_job_status  on public.script_dm_jobs (status)
    where status not in ('completed', 'failed', 'cancelled', 'skipped');
create index if not exists idx_dm_job_task_id on public.script_dm_jobs (celery_task_id);

-- ------------------------------------------------------------
-- 5. updated_at 自动维护
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

drop trigger if exists trg_dm_doc_updated on public.script_dm_documents;
create trigger trg_dm_doc_updated
    before update on public.script_dm_documents
    for each row execute function public.touch_updated_at();

drop trigger if exists trg_dm_job_updated on public.script_dm_jobs;
create trigger trg_dm_job_updated
    before update on public.script_dm_jobs
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 6. 进度原子自增
--    并行 worker 直接 update ... set x = x + n，避免读-改-写丢更新。
--    只传需要变更的计数，其余留 0 即可。
-- ------------------------------------------------------------
create or replace function public.bump_dm_job_progress(
    p_job_id           uuid,
    p_status           text default null,
    p_stage_detail     text default null,
    p_processed_pages  integer default 0,
    p_finished_shards  integer default 0,
    p_total_chunks     integer default 0,
    p_dropped_chunks   integer default 0,
    p_total_qa         integer default 0,
    p_embedded_chunks  integer default 0,
    p_embedded_qa      integer default 0
)
returns public.script_dm_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    result public.script_dm_jobs;
begin
    update public.script_dm_jobs
       set status          = coalesce(p_status, status),
           stage_detail    = coalesce(p_stage_detail, stage_detail),
           processed_pages = processed_pages + coalesce(p_processed_pages, 0),
           finished_shards = finished_shards + coalesce(p_finished_shards, 0),
           total_chunks    = total_chunks    + coalesce(p_total_chunks, 0),
           dropped_chunks  = dropped_chunks  + coalesce(p_dropped_chunks, 0),
           total_qa        = total_qa        + coalesce(p_total_qa, 0),
           embedded_chunks = embedded_chunks + coalesce(p_embedded_chunks, 0),
           embedded_qa     = embedded_qa     + coalesce(p_embedded_qa, 0),
           started_at      = case when started_at is null and p_status is not null
                                       and p_status <> 'pending'
                                  then now() else started_at end,
           finished_at     = case when p_status in ('completed', 'failed', 'skipped')
                                  then now() else finished_at end
     where id = p_job_id
    returning * into result;

    return result;
end;
$$;

-- ------------------------------------------------------------
-- 7. 向量检索函数
--    PostgREST 走 POST /rest/v1/rpc/match_dm_chunks 调用，
--    query_embedding 传 '[0.1,0.2,...]' 形式的字符串即可自动 cast。
--
--    ef_search 是查询期的候选队列长度：调大召回更全但更慢。
--    这里按 top_k 动态放大，并保证不低于 40 —— 带 script_id 过滤时，
--    HNSW 会先按向量距离取候选再过滤，候选太少会出现「明明有数据却返回空」。
-- ------------------------------------------------------------
create or replace function public.match_dm_chunks(
    query_embedding      vector(1024),
    p_script_id          uuid default null,
    p_document_id        uuid default null,
    p_script_code        text default null,
    match_count          integer default 8,
    similarity_threshold double precision default 0.25
)
returns table (
    id           uuid,
    document_id  uuid,
    script_id    uuid,
    script_code  text,
    chunk_index  integer,
    content      text,
    page_start   integer,
    page_end     integer,
    section_path text[],
    block_type   text,
    similarity   double precision
)
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
    execute format('set local hnsw.ef_search = %s', greatest(match_count * 8, 40));

    return query
    select c.id,
           c.document_id,
           c.script_id,
           c.script_code,
           c.chunk_index,
           c.content,
           c.page_start,
           c.page_end,
           c.section_path,
           c.block_type,
           (1 - (c.embedding <=> query_embedding))::double precision as similarity
      from public.script_dm_chunks c
      join public.script_dm_documents d on d.id = c.document_id
     where c.embedding is not null
       and d.deleted_at is null
       and d.is_active
       and (p_script_id   is null or c.script_id   = p_script_id)
       and (p_script_code is null or c.script_code = p_script_code)
       and (p_document_id is null or c.document_id = p_document_id)
       and (1 - (c.embedding <=> query_embedding)) >= similarity_threshold
     order by c.embedding <=> query_embedding
     limit match_count;
end;
$$;

create or replace function public.match_dm_qa(
    query_embedding      vector(1024),
    p_script_id          uuid default null,
    p_document_id        uuid default null,
    p_script_code        text default null,
    p_category           text default null,
    match_count          integer default 8,
    similarity_threshold double precision default 0.25
)
returns table (
    id           uuid,
    document_id  uuid,
    script_id    uuid,
    script_code  text,
    chunk_id     uuid,
    question     text,
    answer       text,
    category     text,
    page_start   integer,
    page_end     integer,
    section_path text[],
    similarity   double precision
)
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
    execute format('set local hnsw.ef_search = %s', greatest(match_count * 8, 40));

    return query
    select q.id,
           q.document_id,
           q.script_id,
           q.script_code,
           q.chunk_id,
           q.question,
           q.answer,
           q.category,
           q.page_start,
           q.page_end,
           q.section_path,
           (1 - (q.embedding <=> query_embedding))::double precision as similarity
      from public.script_dm_qa q
      join public.script_dm_documents d on d.id = q.document_id
     where q.embedding is not null
       and d.deleted_at is null
       and d.is_active
       and (p_script_id   is null or q.script_id   = p_script_id)
       and (p_script_code is null or q.script_code = p_script_code)
       and (p_document_id is null or q.document_id = p_document_id)
       and (p_category    is null or q.category    = p_category)
       and (1 - (q.embedding <=> query_embedding)) >= similarity_threshold
     order by q.embedding <=> query_embedding
     limit match_count;
end;
$$;

-- ------------------------------------------------------------
-- 8. 跨文档近似重复检测（运维排查用）
--    应用层已用 Redis 做在线去重，这个函数用于事后审计：
--    抽查某个块在库里是否还有汉明距离很近的孪生兄弟。
-- ------------------------------------------------------------
create or replace function public.find_similar_simhash(
    p_simhash    bigint,
    p_max_distance integer default 3,
    p_limit      integer default 20
)
returns table (
    id       uuid,
    document_id uuid,
    content  text,
    distance integer
)
language sql
stable
as $$
    -- 用 XOR 后 popcount 算汉明距离；bigint 无内建 popcount，
    -- 转成 bit(64) 后借助 length(replace(...)) 数 1 的个数
    select c.id,
           c.document_id,
           c.content,
           length(replace((p_simhash # c.simhash)::bit(64)::text, '0', '')) as distance
      from public.script_dm_chunks c
     where c.simhash is not null
       and length(replace((p_simhash # c.simhash)::bit(64)::text, '0', '')) <= p_max_distance
     order by distance
     limit p_limit;
$$;

-- ------------------------------------------------------------
-- 9. 清理函数：重新索引前先清空旧版本
--
--    ⚠️ 本函数已被 sql/script_delete.sql 覆盖（那里是含 stories/highlights
--       与容错判断的最终版）。要改清理逻辑请去 script_delete.sql，
--       并保证本文件的执行顺序排在其之前，否则会把旧版本覆盖回去。
-- ------------------------------------------------------------
create or replace function public.purge_dm_document(p_document_id uuid)
returns void
language sql
security definer
set search_path = public
as $$
    delete from public.script_dm_qa     where document_id = p_document_id;
    delete from public.script_dm_chunks where document_id = p_document_id;
    update public.script_dm_documents
       set total_chunks = 0, total_qa = 0, dropped_chunks = 0
     where id = p_document_id;
$$;

-- ------------------------------------------------------------
-- 10. 行级安全策略
--     手册内容属于剧本的付费/机密资料，**不开放给 anon 直读**：
--     前端一律走后端接口，由后端用 service_role 读取并做业务鉴权。
--     这里只启用 RLS 且不建 select 策略，等于默认全部拒绝。
-- ------------------------------------------------------------
alter table public.script_dm_documents enable row level security;
alter table public.script_dm_chunks    enable row level security;
alter table public.script_dm_qa        enable row level security;
alter table public.script_dm_jobs      enable row level security;

-- 如需允许登录用户直接查询任务进度，取消下面这条策略的注释：
-- drop policy if exists "dm jobs readable by authenticated" on public.script_dm_jobs;
-- create policy "dm jobs readable by authenticated" on public.script_dm_jobs
--     for select to authenticated using (true);

-- ------------------------------------------------------------
-- 11. 便捷视图：手册索引概览
-- ------------------------------------------------------------
create or replace view public.script_dm_overview as
select s.id            as script_id,
       coalesce(nullif(d.script_code, ''), s.code) as script_code,
       s.title         as script_title,
       d.id            as document_id,
       d.file_name,
       d.total_pages,
       d.total_chunks,
       d.total_qa,
       d.dropped_chunks,
       case when d.total_chunks + d.dropped_chunks > 0
            then round(d.dropped_chunks::numeric
                       / (d.total_chunks + d.dropped_chunks) * 100, 1)
            else 0 end as dedup_rate_percent,
       d.embed_model,
       d.is_active,
       d.created_at,
       d.updated_at
  from public.script_dm_documents d
  join public.scripts s on s.id = d.script_id
 where d.deleted_at is null;
