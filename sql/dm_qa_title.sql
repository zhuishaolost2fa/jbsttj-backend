-- ============================================================
-- DM 问答对「标题」字段 + 标题链查询
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行）
--
-- 依赖：sql/dm_rag.sql
--
-- 设计要点：
--   1. title = 该问答对来源块的「最近一级标题」（section_path 末级），
--      多个 QA 共享同一标题，前端可按标题分组展示；
--   2. 完整层级仍在 section_path 里，title 只是冗余出来的末级，
--      标题链树的层级关系由应用层按 section_path 重建；
--   3. list_dm_qa_titles 按「文档创建时间 → 块序号 → QA 创建时间」输出，
--      即手册的原始行文顺序，应用层无需再排序。
-- ============================================================

-- ------------------------------------------------------------
-- 1. title 列与回填
-- ------------------------------------------------------------
alter table public.script_dm_qa add column if not exists title text not null default '';

-- 存量数据回填：末级标题即 section_path 最后一个元素
-- （空数组时 cardinality=0，取下标 0 得 NULL，保持 '' 由应用层归入「未分节」）
update public.script_dm_qa
   set title = section_path[cardinality(section_path)]
 where title = ''
   and cardinality(section_path) > 0;

create index if not exists idx_dm_qa_title
    on public.script_dm_qa (script_code, title);

-- ------------------------------------------------------------
-- 2. 标题链查询：按剧本业务 code 取出全部 QA，行文顺序输出
--    应用层按 section_path 把扁平行组装成嵌套标题树。
--    只取 is_active 文档（与向量检索的口径一致）。
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
security definer
set search_path = public
as $$
    select q.section_path,
           -- 兼容未回填/未走新入库逻辑的行：title 为空时退到 section_path 末级
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
      join public.script_dm_documents d on d.id = q.document_id
      left join public.script_dm_chunks c on c.id = q.chunk_id
     where q.script_code = p_script_code
       and d.is_active
       and d.deleted_at is null
     order by d.created_at,
              c.chunk_index nulls last,
              q.created_at
$$;
