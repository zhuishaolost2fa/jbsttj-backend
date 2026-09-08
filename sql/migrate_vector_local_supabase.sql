-- ============================================================
-- 向量表下沉本地 pgvector —— Supabase 侧的配套改动
--
-- 前置：已执行 sql/fix_dm_match_overloads.sql（修 PGRST203）
-- 在 Supabase Dashboard -> SQL Editor 整段执行即可（可重复执行）
--
-- 【背景】
--   script_dm_chunks / script_dm_qa 两张向量表已下沉到同机 pgvector，
--   新导入的手册只会写本地库。但 script_dm_stories 仍在 Supabase，
--   它的 chunk_id 有外键指向 script_dm_chunks —— 新 chunk 的 id 在
--   Supabase 侧不存在，导入故事条目时会直接撞外键报错。
--
-- 【做法】
--   把这条外键降级为「应用层维护的软引用」：stories 仍在 Supabase，
--   chunk_id 只作溯源用，真正的内容在本地库。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 解除 stories -> chunks 的跨库外键
--    名字取 PG 默认的 <表>_<列>_fkey；若历史上改过名，先跑下面的查询确认：
--
--    select conname from pg_constraint
--     where conrelid = 'public.script_dm_stories'::regclass and contype = 'f';
-- ------------------------------------------------------------
alter table public.script_dm_stories
    drop constraint if exists script_dm_stories_chunk_id_fkey;

-- ------------------------------------------------------------
-- 2. 存量向量数据先留着，别急着删
--
--    迁移脚本只搬不删。确认新检索链路稳定（对比几次问答结果一致）之后，
--    再决定是否清理。清理前务必先备份：
--
--    -- 可选：清空 Supabase 侧向量表（不可回滚，谨慎！）
--    -- delete from public.script_dm_qa;
--    -- delete from public.script_dm_chunks;
--
--    注意 script_dm_stories.chunk_id 仍引用着存量 chunk 的 id（外键已解除，
--    不会阻止删除），一旦清空，老故事条目的溯源会指向空。
-- ------------------------------------------------------------

-- ------------------------------------------------------------
-- 3. 验证：stories 表应只剩指向 documents / scripts 的外键
-- ------------------------------------------------------------
select conname,
       pg_get_constraintdef(oid) as definition
  from pg_constraint
 where conrelid = 'public.script_dm_stories'::regclass
   and contype = 'f'
 order by conname;
