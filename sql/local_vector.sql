-- ============================================================
-- 本地 pgvector：DM 向量表结构（容器启动时自动执行）
--
-- 挂载到 /docker-entrypoint-initdb.d/ 后，首次初始化数据目录时自动执行，
-- 已在库里存在时不会重复跑（create table if not exists 保证可重复执行）。
--
-- 【与 Supabase 侧 sql/dm_rag.sql 的差异】
--   1. **去掉所有指向 public.scripts 的外键** —— scripts 主表仍在 Supabase，
--      本地是独立库，无法跨库建外键。script_id 仅作过滤列，级联删除由应用层负责。
--   2. qa.chunk_id 的外键**保留**（chunks 和 qa 都在本地，同库可用）。
--   3. 新增影子表 dm_documents：检索要按 is_active / deleted_at 过滤，
--      主表在 Supabase 无法 join，本地只同步过滤需要的四个字段。
--   4. 表结构、索引参数、检索函数签名与 Supabase 侧**保持一致**，
--      保证 VECTOR_BACKEND 开关切换时应用层无感。
-- ============================================================

create extension if not exists vector;

-- ------------------------------------------------------------
-- 0. 文档影子表（Supabase script_dm_documents 的投影）
--    只留检索过滤需要的列，由应用层在写文档/版本切换/删除时同步。
-- ------------------------------------------------------------
create table if not exists public.dm_documents (
    id          uuid primary key,
    script_id   uuid,
    script_code text not null default '',
    -- 换文件重传场景要按 object_key 下线旧文档，影子表必须带上它
    object_key  text not null default '',
    is_active   boolean not null default true,
    deleted_at  timestamptz,
    -- 标题链查询按「文档创建时间 → 块序号 → QA 创建时间」排序，需要文档的创建时间
    created_at  timestamptz not null default now()
);

create index if not exists idx_local_doc_script_code
    on public.dm_documents (script_code)
    where deleted_at is null;

-- ------------------------------------------------------------
-- 1. 分块表（正文向量）
-- ------------------------------------------------------------
create table if not exists public.script_dm_chunks (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null,
    -- 冗余 script_id / script_code：检索按剧本过滤是最高频路径
    script_id     uuid not null,
    script_code   text not null default '',

    chunk_index   integer not null,
    content       text not null,
    content_hash  text not null,
    simhash       bigint,

    page_start    integer,
    page_end      integer,
    section_path  text[] not null default '{}',
    block_type    text not null default 'body',

    char_count    integer not null default 0,
    embedding     vector(1024),

    created_at    timestamptz not null default now(),

    constraint uq_dm_chunk_hash unique (document_id, content_hash),
    constraint ck_dm_chunk_block_type check (block_type in ('body', 'heading', 'table', 'list'))
);

create index if not exists idx_dm_chunk_doc
    on public.script_dm_chunks (document_id, chunk_index);
create index if not exists idx_dm_chunk_script
    on public.script_dm_chunks (script_id);
create index if not exists idx_dm_chunk_script_code
    on public.script_dm_chunks (script_code);

-- HNSW 向量索引：与 Supabase 侧同样参数，m=16 / ef_construction=64，余弦距离
create index if not exists idx_dm_chunk_embedding_hnsw
    on public.script_dm_chunks using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- ------------------------------------------------------------
-- 2. 问答对表（问题向量）
-- ------------------------------------------------------------
create table if not exists public.script_dm_qa (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null,
    script_id     uuid not null,
    script_code   text not null default '',
    -- 同库外键，块被删除后问答对仍保留
    chunk_id      uuid references public.script_dm_chunks (id) on delete set null,

    question      text not null,
    answer        text not null,
    question_hash text not null,

    page_start    integer,
    page_end      integer,
    section_path  text[] not null default '{}',
    category      text not null default 'other',
    -- 来源块的最近一级标题，前端按标题分组展示（与 dm_qa_title.sql 对齐）
    title         text not null default '',
    created_by    uuid,

    embedding     vector(1024),
    created_at    timestamptz not null default now(),

    constraint uq_dm_qa_hash unique (document_id, question_hash)
);

create index if not exists idx_dm_qa_doc       on public.script_dm_qa (document_id);
create index if not exists idx_dm_qa_script    on public.script_dm_qa (script_id);
create index if not exists idx_dm_qa_code      on public.script_dm_qa (script_code);
create index if not exists idx_dm_qa_chunk     on public.script_dm_qa (chunk_id);
create index if not exists idx_dm_qa_category  on public.script_dm_qa (category);

create index if not exists idx_dm_qa_embedding_hnsw
    on public.script_dm_qa using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- ------------------------------------------------------------
-- 3. 检索函数
--    签名与返回列与 Supabase 侧完全一致，应用层切换无感。
--    ef_search 按 top_k 动态放大，保证带 script 过滤时不会「有数据却返回空」。
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
      join public.dm_documents d on d.id = c.document_id
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
      join public.dm_documents d on d.id = q.document_id
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
-- 3b. 标题链查询（与 Supabase 侧 list_dm_qa_titles 同签名）
--     QA 表已迁到本地，这个函数必须在本地执行，否则标题树会读到迁走前的旧数据。
-- ------------------------------------------------------------
create or replace function public.list_dm_qa_titles(p_script_code text)
returns table (
    section_path text[],
    title        text,
    qa_id        uuid,
    question     text,
    answer       text,
    category     text,
    page_start   integer,
    page_end     integer
)
language sql
stable
as $$
    select q.section_path,
           coalesce(nullif(q.title, ''),
                    q.section_path[cardinality(q.section_path)],
                    '') as title,
           q.id,
           q.question,
           q.answer,
           q.category,
           q.page_start,
           q.page_end
      from public.script_dm_qa q
      join public.dm_documents d on d.id = q.document_id
      left join public.script_dm_chunks c on c.id = q.chunk_id
     where q.script_code = p_script_code
       and d.is_active
       and d.deleted_at is null
     order by d.created_at,
              c.chunk_index nulls last,
              q.created_at
$$;

-- ------------------------------------------------------------
-- 4. 清理：按文档 / 按剧本物理删除
--    Supabase 侧的 purge_dm_document 由 script_delete.sql 统一管理，
--    本地这份只负责向量表，应用层两边各调一次。
-- ------------------------------------------------------------
create or replace function public.purge_local_document(p_document_id uuid)
returns void
language sql
as $$
    delete from public.script_dm_qa     where document_id = p_document_id;
    delete from public.script_dm_chunks where document_id = p_document_id;
    delete from public.dm_documents     where id = p_document_id;
$$;

create or replace function public.purge_local_script(p_script_id uuid)
returns void
language sql
as $$
    delete from public.script_dm_qa     where script_id = p_script_id;
    delete from public.script_dm_chunks where script_id = p_script_id;
    delete from public.dm_documents     where script_id = p_script_id;
$$;
