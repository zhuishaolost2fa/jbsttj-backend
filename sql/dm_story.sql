-- ============================================================
-- DM 手册 · 故事还原 + 用户划线评论 表结构
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/dm_rag.sql（script_dm_documents / script_dm_chunks）
--
-- 设计要点：
--   1. 「故事还原」是与问答对并列的 LLM 采集产物：T3 generate_qa 在生成
--      问答对的同时，识别手册中的还原类内容（时间线 / 真相 / 角色背景 /
--      线索关联 / 结局），落成结构化条目，可独立向量化参与 RAG 检索；
--   2. 「用户划线评论」采用 W3C Web Annotation 风格的**文本锚点**：
--      quote（划线原文）+ start/end_offset（字符偏移）+ prefix/suffix
--      （前后文指纹）。手册 force 重跑或换新版本后，story 行会重建，
--      划线靠 quote 重新模糊锚定，**用户数据不随重跑丢失**；
--   3. story_id 用 on delete set null 而非 cascade：purge 重跑时旧 story
--      被删，划线保留为 orphaned 状态，由 reanchor_dm_highlights 重新挂接；
--   4. user_id 沿用项目惯例：自建「服务间通道」鉴权（X-API-Key +
--      X-User-Id），不绑 Supabase auth.users，不建外键；
--   5. 可见性分 private / public 两档：public 划线聚合出「共读还原」
--      时间线，private 只自己可见。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 故事还原条目表（LLM 采集）
-- ------------------------------------------------------------
create table if not exists public.script_dm_stories (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references public.script_dm_documents (id) on delete cascade,
    -- 冗余 script_id / script_code：与 chunks/qa 同理，最高频过滤路径避免 join
    script_id     uuid not null references public.scripts (id) on delete cascade,
    script_code   text not null default '',
    -- 来源块；块被清理后条目仍保留（与 script_dm_qa.chunk_id 同语义）
    chunk_id      uuid references public.script_dm_chunks (id) on delete set null,

    story_index   integer not null default 0,
    -- timeline 时间线 / truth 真相还原 / role 角色背景 /
    -- clue 线索关联 / ending 结局收束 / other
    story_type    text not null default 'other',
    title         text not null default '',
    -- 还原正文（LLM 从手册原文整理，非逐字摘抄）
    content       text not null,
    -- 一句话摘要，用于列表页与检索结果卡片
    summary       text,
    -- 结构化补充：时间线事件列表、人物关系对、伏笔-回收映射等
    -- 例：{"events": [{"when": "案发前夜 23:00", "what": "…"}],
    --       "roles": ["沈墨", "温言"]}
    meta          jsonb not null default '{}'::jsonb,

    -- 归一化内容的 SHA256：同一文档内去重兜底（幂等键）
    content_hash  text not null,

    page_start    integer,
    page_end      integer,
    section_path  text[] not null default '{}',
    char_count    integer not null default 0,
    embedding     vector(1024),

    created_at    timestamptz not null default now(),

    constraint uq_dm_story_hash unique (document_id, content_hash),
    constraint ck_dm_story_type check (story_type in (
        'timeline', 'truth', 'role', 'clue', 'ending', 'other'
    ))
);

create index if not exists idx_dm_story_doc
    on public.script_dm_stories (document_id, story_index);
create index if not exists idx_dm_story_script
    on public.script_dm_stories (script_id, story_type);
create index if not exists idx_dm_story_code
    on public.script_dm_stories (script_code, story_type);
create index if not exists idx_dm_story_chunk
    on public.script_dm_stories (chunk_id);
create index if not exists idx_dm_story_content_trgm
    on public.script_dm_stories using gin (content gin_trgm_ops);
-- HNSW 参数与 chunks/qa 保持一致（bge-large-zh 1024 维，余弦距离）
create index if not exists idx_dm_story_embedding_hnsw
    on public.script_dm_stories using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- ------------------------------------------------------------
-- 2. 用户划线评论表
--    一行 = 一次划线；comment 可空（纯划线收藏）。
--    锚点三要素：quote / (start_offset, end_offset) / (prefix, suffix)。
--    偏移量按 story.content 的字符数（前端用 Array.from 统计，避免 surrogate pair 错位）。
-- ------------------------------------------------------------
create table if not exists public.script_dm_highlights (
    id            uuid primary key default gen_random_uuid(),
    script_id     uuid not null references public.scripts (id) on delete cascade,
    script_code   text not null default '',
    -- 划线诞生时的文档版本；跨版本重锚定的定位范围
    document_id   uuid not null references public.script_dm_documents (id) on delete cascade,
    -- 当前挂接的故事条目；on delete set null —— 重跑清 story 时划线不删，
    -- 留待 reanchor_dm_highlights 按 quote 重新挂接
    story_id      uuid references public.script_dm_stories (id) on delete set null,

    -- 自建鉴权通道的 user_id（X-User-Id），不绑 auth.users，不建外键
    user_id       uuid not null,

    -- ---- 锚点信息 ----
    quote         text not null,
    start_offset  integer not null,
    end_offset    integer not null,
    -- 前后文各存 ≤64 字符，偏移漂移时做模糊重锚
    prefix        text not null default '',
    suffix        text not null default '',

    -- ---- 评论内容 ----
    comment       text,
    -- private 仅自己可见 / public 进入共读时间线
    visibility    text not null default 'private',

    -- active 正常 / orphaned 所属 story 已被重跑删除或内容变化，待重锚
    status        text not null default 'active',
    like_count    integer not null default 0,

    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    deleted_at    timestamptz,

    constraint ck_dm_hl_range check (end_offset > start_offset and start_offset >= 0),
    constraint ck_dm_hl_visibility check (visibility in ('private', 'public')),
    constraint ck_dm_hl_status check (status in ('active', 'orphaned')),
    -- 同一用户在同一条目上不重复划同一段（story_id 为 null 的 orphaned 不受限）
    constraint uq_dm_hl_user_story_range unique (user_id, story_id, start_offset, end_offset)
);

-- story 级聚合：条目详情页取「该条目的公开划线」
create index if not exists idx_dm_hl_story
    on public.script_dm_highlights (story_id, visibility, created_at desc)
    where deleted_at is null;
-- 剧本级聚合：「共读还原」公开时间线（跨条目按时间倒序）
create index if not exists idx_dm_hl_script_public
    on public.script_dm_highlights (script_id, visibility, created_at desc)
    where deleted_at is null and visibility = 'public';
-- 「我的划线」列表
create index if not exists idx_dm_hl_user
    on public.script_dm_highlights (user_id, created_at desc)
    where deleted_at is null;
-- 重锚定扫描入口：按文档版本批量找 orphaned
create index if not exists idx_dm_hl_document
    on public.script_dm_highlights (document_id, status)
    where deleted_at is null;

-- updated_at 自动维护
drop trigger if exists trg_dm_hl_updated on public.script_dm_highlights;
create trigger trg_dm_hl_updated
    before update on public.script_dm_highlights
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 3. 文档表 / 任务表补列（故事还原计数）
-- ------------------------------------------------------------
alter table public.script_dm_documents add column if not exists total_stories integer not null default 0;
alter table public.script_dm_jobs    add column if not exists total_stories    integer not null default 0;
alter table public.script_dm_jobs    add column if not exists embedded_stories  integer not null default 0;

-- bump 函数扩展故事计数（默认 0，旧调用方不受影响）
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
    p_embedded_qa      integer default 0,
    p_total_stories    integer default 0,
    p_embedded_stories integer default 0
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
           total_stories   = total_stories   + coalesce(p_total_stories, 0),
           embedded_stories = embedded_stories + coalesce(p_embedded_stories, 0),
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
-- 4. purge 扩展：force 重跑时清掉旧 story
--    注意 story 上的划线是 set null 不是级联删除 —— 用户划线保留，
--    等 T4 写完新 story 后调 reanchor_dm_highlights 重新挂接。
--
--    ⚠️ 本函数已被 sql/script_delete.sql 覆盖（那里是含容错判断的最终版，
--       并对未建 story 表的库做 to_regclass 判空）。要改清理逻辑请去
--       script_delete.sql，且本文件必须排在其之前执行。
-- ------------------------------------------------------------
create or replace function public.purge_dm_document(p_document_id uuid)
returns void
language sql
security definer
set search_path = public
as $$
    delete from public.script_dm_qa     where document_id = p_document_id;
    delete from public.script_dm_chunks where document_id = p_document_id;
    delete from public.script_dm_stories where document_id = p_document_id;
    update public.script_dm_highlights
       set status = 'orphaned'
     where document_id = p_document_id
       and deleted_at is null
       and status = 'active';
    update public.script_dm_documents
       set total_chunks = 0, total_qa = 0, dropped_chunks = 0, total_stories = 0
     where id = p_document_id;
$$;

-- ------------------------------------------------------------
-- 5. 划线重锚定：手册重跑后，按 quote 把 orphaned 划线挂回新 story
--    两级匹配：
--      ① 精确：新 story.content 中完整包含 quote；
--      ② 模糊：quote 完整包含于 content 且 prefix/suffix 命中前后文
--         （容忍少量字符增删导致的偏移漂移）。
--    匹配不到的保持 orphaned，前端展示时降级为「原文卡片」而非跳转锚点。
--    PostgREST 调法：POST /rest/v1/rpc/reanchor_dm_highlights
--                    body {"p_document_id": "…"}（后端 service_role 调用）
-- ------------------------------------------------------------
create or replace function public.reanchor_dm_highlights(
    p_document_id uuid
)
returns table (relined integer, still_orphaned integer)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_relinked integer := 0;
    v_orphaned integer := 0;
    hl record;
    v_story uuid;
    v_pos integer;
begin
    for hl in
        select h.id, h.quote, h.prefix, h.suffix
          from public.script_dm_highlights h
         where h.document_id = p_document_id
           and h.status = 'orphaned'
           and h.deleted_at is null
    loop
        -- ① 同文档的活跃 story 中找精确包含 quote 的条目
        select s.id, position(hl.quote in s.content)
          into v_story, v_pos
          from public.script_dm_stories s
          join public.script_dm_documents d on d.id = s.document_id
         where s.document_id = p_document_id
           and d.is_active and d.deleted_at is null
           and s.content like '%' || hl.quote || '%'
         order by s.story_index
         limit 1;

        -- ② 精确未命中：prefix+quote 或 quote+suffix 的拼接模糊匹配
        if v_story is null and (hl.prefix <> '' or hl.suffix <> '') then
            select s.id, position(hl.quote in s.content)
              into v_story, v_pos
              from public.script_dm_stories s
              join public.script_dm_documents d on d.id = s.document_id
             where s.document_id = p_document_id
               and d.is_active and d.deleted_at is null
               and (
                    (hl.prefix <> '' and s.content like '%' || hl.prefix || hl.quote || '%')
                 or (hl.suffix <> '' and s.content like '%' || hl.quote || hl.suffix || '%')
               )
             order by s.story_index
             limit 1;
        end if;

        if v_story is not null then
            update public.script_dm_highlights
               set story_id = v_story,
                   start_offset = coalesce(v_pos, start_offset),
                   end_offset = coalesce(v_pos, start_offset) + length(hl.quote),
                   status = 'active',
                   updated_at = now()
             where id = hl.id;
            v_relinked := v_relinked + 1;
        else
            v_orphaned := v_orphaned + 1;
        end if;
    end loop;

    return query select v_relinked, v_orphaned;
end;
$$;

-- ------------------------------------------------------------
-- 6. 故事还原向量检索（与 match_dm_chunks / match_dm_qa 同构）
-- ------------------------------------------------------------
create or replace function public.match_dm_stories(
    query_embedding      vector(1024),
    p_script_id          uuid default null,
    p_document_id        uuid default null,
    p_script_code        text default null,
    p_story_type         text default null,
    match_count          integer default 8,
    similarity_threshold double precision default 0.25
)
returns table (
    id           uuid,
    document_id  uuid,
    script_id    uuid,
    script_code  text,
    chunk_id     uuid,
    story_index  integer,
    story_type   text,
    title        text,
    content      text,
    summary      text,
    meta         jsonb,
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
    select s.id,
           s.document_id,
           s.script_id,
           s.script_code,
           s.chunk_id,
           s.story_index,
           s.story_type,
           s.title,
           s.content,
           s.summary,
           s.meta,
           s.page_start,
           s.page_end,
           s.section_path,
           (1 - (s.embedding <=> query_embedding))::double precision as similarity
      from public.script_dm_stories s
      join public.script_dm_documents d on d.id = s.document_id
     where s.embedding is not null
       and d.deleted_at is null
       and d.is_active
       and (p_script_id   is null or s.script_id   = p_script_id)
       and (p_script_code is null or s.script_code = p_script_code)
       and (p_document_id is null or s.document_id = p_document_id)
       and (p_story_type  is null or s.story_type  = p_story_type)
       and (1 - (s.embedding <=> query_embedding)) >= similarity_threshold
     order by s.embedding <=> query_embedding
     limit match_count;
end;
$$;

-- ------------------------------------------------------------
-- 7. 概览视图补故事计数
--    注意：CREATE OR REPLACE VIEW 只能在**末尾追加**列，不能改列序/列名；
--    这里在中间插入了 total_stories（total_qa 与 dropped_chunks 之间），
--    列序已变，必须先 DROP 再重建，否则报 42P16。
-- ------------------------------------------------------------
drop view if exists public.script_dm_overview;
create view public.script_dm_overview as
select s.id            as script_id,
       coalesce(nullif(d.script_code, ''), s.code) as script_code,
       s.title         as script_title,
       d.id            as document_id,
       d.file_name,
       d.total_pages,
       d.total_chunks,
       d.total_qa,
       d.total_stories,
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

-- ------------------------------------------------------------
-- 8. 行级安全
--    stories：手册衍生内容，同 chunks/qa，service_role 专用，anon 默认拒绝；
--    highlights：用户 UGC，authenticated 可读公开划线、管理自己的划线
--    （前端实际走后端接口，这里的策略是给未来直连 Supabase 留的兜底）。
-- ------------------------------------------------------------
alter table public.script_dm_stories    enable row level security;
alter table public.script_dm_highlights enable row level security;

drop policy if exists "dm highlights readable" on public.script_dm_highlights;
create policy "dm highlights readable" on public.script_dm_highlights
    for select to authenticated
    using (visibility = 'public' or user_id = auth.uid());

drop policy if exists "dm highlights insertable by owner" on public.script_dm_highlights;
create policy "dm highlights insertable by owner" on public.script_dm_highlights
    for insert to authenticated
    with check (user_id = auth.uid());

drop policy if exists "dm highlights updatable by owner" on public.script_dm_highlights;
create policy "dm highlights updatable by owner" on public.script_dm_highlights
    for update to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid());

drop policy if exists "dm highlights deletable by owner" on public.script_dm_highlights;
create policy "dm highlights deletable by owner" on public.script_dm_highlights
    for delete to authenticated
    using (user_id = auth.uid());

-- ------------------------------------------------------------
-- 9. 剧本故事还原列表（RPC）
--    服务层 DMStore.list_stories 调用的就是它。
--    与 list_dm_qa_titles 同构：按 script_code 聚合活跃文档下的故事条目，
--    每行附带「公开划线数」（共读时间线用），支持 story_type 过滤与分页。
--    PostgREST 调法：POST /rest/v1/rpc/list_dm_stories
-- ------------------------------------------------------------
create or replace function public.list_dm_stories(
    p_script_code text,
    p_story_type  text default null,
    p_limit       integer default 50,
    p_offset      integer default 0
)
returns json
language plpgsql
stable
as $$
declare
    v_total integer;
    v_items json;
    v_code  text;
begin
    v_code := lower(trim(coalesce(p_script_code, '')));
    if v_code = '' then
        return json_build_object('items', '[]'::json, 'total', 0);
    end if;

    select count(*)
      into v_total
      from public.script_dm_stories s
      join public.script_dm_documents d on d.id = s.document_id
     where s.script_code = v_code
       and d.is_active = true
       and (p_story_type is null or p_story_type = '' or s.story_type = p_story_type);

    select coalesce(json_agg(row_to_json(t) order by t.story_index), '[]'::json)
      into v_items
      from (
          select s.id,
                 s.document_id,
                 s.script_code,
                 s.chunk_id,
                 s.story_index,
                 s.story_type,
                 s.title,
                 s.content,
                 s.summary,
                 s.meta,
                 s.section_path,
                 s.page_start,
                 s.page_end,
                 s.char_count,
                 s.created_at,
                 coalesce(h.public_highlights, 0) as public_highlights
            from public.script_dm_stories s
            join public.script_dm_documents d on d.id = s.document_id
            left join lateral (
                select count(*)::int as public_highlights
                  from public.script_dm_highlights h
                 where h.story_id = s.id
                   and h.visibility = 'public'
                   and h.status = 'active'
                   and h.deleted_at is null
            ) h on true
           where s.script_code = v_code
             and d.is_active = true
             and (p_story_type is null or p_story_type = '' or s.story_type = p_story_type)
           order by s.story_index
           limit greatest(0, p_limit) offset greatest(0, p_offset)
      ) t;

    return json_build_object('items', v_items, 'total', v_total);
end;
$$;
