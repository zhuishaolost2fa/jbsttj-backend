-- ============================================================
-- DM 手册 · 故事还原「合成文章」表
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/dm_story.sql（script_dm_stories）
--
-- 设计要点：
--   1. 「合成文章」(script_dm_synthesis) 与现有 stories 是**互补关系**：
--      - script_dm_stories：按 chunk（~800 字）逐段抽出的颗粒化条目（按手册行文顺序）
--        —— 适合检索、划线锚定、共读时间线，但读起来像碎片卡片。
--      - script_dm_synthesis：所有 chunks 入库后，LLM 拿全量 StoryItem 二次
--        加工成的「整本剧本脉络文章」，分 5 节（梗概/诡计/时间线/角色/结局），
--        —— 适合主持人复盘、玩家快速理解整本故事。
--   2. 一篇文档对应一篇合成文章（document_id 唯一），force 重跑时 upsert 覆盖；
--   3. 5 节拆 5 列而不是塞 jsonb：方便前端按 section 渲染、利于向量检索按节定向；
--   4. anchor_stories jsonb 记每节引用的 StoryItem 标题列表（按标题匹配，document_id
--      范围内），便于前端做「细节抽屉」跳转；
--   5. synthesis_status 给 jobs / documents 加列：pending / generating / ready / failed
--      —— 状态机独立于 stories 落库，与 job.status 解耦（生成失败不挡 finalize）。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 合成文章表
-- ------------------------------------------------------------
create table if not exists public.script_dm_synthesis (
    id              uuid primary key default gen_random_uuid(),
    document_id     uuid not null references public.script_dm_documents (id) on delete cascade,
    script_id       uuid not null references public.scripts (id) on delete cascade,
    script_code     text not null default '',

    -- 5 节正文（每节 150~400 字，整篇合计 800~1500 字）
    synopsis        text not null default '',  -- 剧本梗概（背景、核心矛盾、人物群像）
    trick           text not null default '',  -- 核心诡计（剧本最核心的真相揭示）
    timeline        text not null default '',  -- 时间线（案发前 → 案发 → 后续）
    roles           text not null default '',  -- 角色命运（每个角色的关键抉择与归宿）
    ending          text not null default '',  -- 结局（剧本落幕时的整体收束）

    -- 关联回现有故事条目（按 title 模糊匹配，document_id 范围内）
    anchor_stories  jsonb not null default '{}'::jsonb,
    -- 形如：
    -- {"synopsis": ["秀吉背景", "和歌家血脉"],
    --  "trick":    ["日记覆盖规则", "循环谜面成立条件"],
    --  "timeline": ["案发前夜 23:00 沈墨潜入书房", ...],
    --  "roles":    ["沈墨", "温言"],
    --  "ending":   ["桃山未明自白"]}

    model           text not null default '',  -- 实际调用的模型
    prompt_version  text not null default 'v1',

    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    constraint uq_dm_synthesis_doc unique (document_id)
);

create index if not exists idx_dm_synthesis_script
    on public.script_dm_synthesis (script_id);
create index if not exists idx_dm_synthesis_code
    on public.script_dm_synthesis (script_code);

-- updated_at 自动维护
drop trigger if exists trg_dm_synthesis_updated on public.script_dm_synthesis;
create trigger trg_dm_synthesis_updated
    before update on public.script_dm_synthesis
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------
-- 2. 文档表 / 任务表加合成状态
-- ------------------------------------------------------------
alter table public.script_dm_documents
    add column if not exists synthesis_status   text not null default 'pending';
alter table public.script_dm_jobs
    add column if not exists synthesis_status   text not null default 'pending';

-- ------------------------------------------------------------
-- 3. upsert RPC（finalize 后调）：合成文章以 document_id 唯一，重跑即覆盖
--    PostgREST 调法：POST /rest/v1/rpc/upsert_dm_synthesis
--                    body { "p_document_id": "...", "p_synopsis": "...", ... }
-- ------------------------------------------------------------
create or replace function public.upsert_dm_synthesis(
    p_document_id    uuid,
    p_synopsis       text,
    p_trick          text,
    p_timeline       text,
    p_roles          text,
    p_ending         text,
    p_anchor_stories jsonb default '{}'::jsonb,
    p_model          text default '',
    p_prompt_version text default 'v1'
)
returns public.script_dm_synthesis
language plpgsql
security definer
set search_path = public
as $$
declare
    v_script_id   uuid;
    v_script_code text;
    v_row         public.script_dm_synthesis;
begin
    -- 由 document_id 反推 script_id / script_code，避免调用方再传一遍
    select script_id, coalesce(nullif(script_code, ''), '')
      into v_script_id, v_script_code
      from public.script_dm_documents
     where id = p_document_id;

    if v_script_id is null then
        raise exception 'document_id % not found', p_document_id;
    end if;

    insert into public.script_dm_synthesis (
        document_id, script_id, script_code,
        synopsis, trick, timeline, roles, ending,
        anchor_stories, model, prompt_version
    ) values (
        p_document_id, v_script_id, v_script_code,
        coalesce(p_synopsis, ''), coalesce(p_trick, ''),
        coalesce(p_timeline, ''), coalesce(p_roles, ''),
        coalesce(p_ending, ''), coalesce(p_anchor_stories, '{}'::jsonb),
        coalesce(p_model, ''), coalesce(p_prompt_version, 'v1')
    )
    on conflict (document_id) do update set
        synopsis       = excluded.synopsis,
        trick          = excluded.trick,
        timeline       = excluded.timeline,
        roles          = excluded.roles,
        ending         = excluded.ending,
        anchor_stories = excluded.anchor_stories,
        model          = excluded.model,
        prompt_version = excluded.prompt_version,
        updated_at     = now()
    returning * into v_row;

    return v_row;
end;
$$;

-- ------------------------------------------------------------
-- 4. 读取 RPC：按 script_code 拿当前活跃文档下的合成文章
--    PostgREST 调法：POST /rest/v1/rpc/get_dm_synthesis
--                    body { "p_script_code": "..." }
-- ------------------------------------------------------------
create or replace function public.get_dm_synthesis(p_script_code text)
returns public.script_dm_synthesis
language plpgsql
stable
security definer
set search_path = public
as $$
declare
    v_code text := lower(trim(coalesce(p_script_code, '')));
    v_row  public.script_dm_synthesis;
begin
    if v_code = '' then
        return null;
    end if;

    select s.*
      into v_row
      from public.script_dm_synthesis s
      join public.script_dm_documents d on d.id = s.document_id
     where s.script_code = v_code
       and d.is_active = true
       and d.deleted_at is null
     order by d.created_at desc
     limit 1;

    return v_row;
end;
$$;

-- ------------------------------------------------------------
-- 5. 行级安全：同 chunks/qa，service_role 专用，anon 默认拒绝
-- ------------------------------------------------------------
alter table public.script_dm_synthesis enable row level security;