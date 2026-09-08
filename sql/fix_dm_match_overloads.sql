-- ============================================================
-- 修复：match_dm_* 函数重载导致 PostgREST 返回 300 / PGRST203
--
-- 【症状】
--   调用 /rest/v1/rpc/match_dm_chunks 返回 HTTP 300：
--   {"code":"PGRST203","message":"Could not choose the best candidate
--    function between: public.match_dm_chunks(...5参...),
--    public.match_dm_chunks(...6参...)"}
--   DMStore.rpc 拿到的不是数组，match_chunks 静默返回空列表，
--   RAG 检索「查得到但永远返回空」，只能走 LLM 兜底。
--
-- 【根因】
--   sql/dm_rag.sql 用 create or replace function 加过参数
--   （新增 p_script_code / p_category）。PG 的 or replace 要求
--   参数类型列表完全一致才视为同一函数，签名一变就是**新建**，
--   旧签名的函数仍然留在库里。PostgREST 遇到同名多签名无法选，
--   直接 300。
--
--   ⚠️ 注意：vector(1024) 的 typmod 不参与函数签名，
--      pg_proc 里参数类型就是 vector，所以 drop 时写 vector 即可。
--
-- 【做法】
--   同名函数只保留「参数最多且含 p_script_code」的那一个（即 dm_rag.sql
--   最新版），其余历史签名全部 drop，然后通知 PostgREST 刷新 schema 缓存。
--
-- 在 Supabase Dashboard -> SQL Editor 整段执行即可（可重复执行）
-- ============================================================

do $$
declare
    r           record;
    last_name   text := '';
    keep_note   text;
begin
    for r in
        select p.proname,
               p.oid::regprocedure as sig,
               p.pronargs,
               (p.proargnames @> array['p_script_code']::text[]) as has_code
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public'
           and p.proname in ('match_dm_chunks', 'match_dm_qa', 'match_dm_stories')
         order by p.proname,
                  -- 优先保留含 p_script_code 的（dm_rag.sql 最新版）
                  (p.proargnames @> array['p_script_code']::text[]) desc,
                  -- 其次保留参数最多的
                  p.pronargs desc,
                  p.oid::regprocedure::text
    loop
        if r.proname <> last_name then
            -- 每个函数名的第一行 = 要保留的那个
            last_name := r.proname;
            raise notice '保留 %（% 个参数，含 p_script_code=%）',
                         r.sig, r.pronargs, r.has_code;
            continue;
        end if;

        raise notice '删除历史重载 %（% 个参数）', r.sig, r.pronargs;
        execute format('drop function %s', r.sig);
    end loop;
end $$;

-- 让 PostgREST 立刻刷新 schema 缓存，否则 300 还会持续到缓存自然过期
notify pgrst, 'reload schema';

-- ------------------------------------------------------------
-- 验证：下面这条查询应该每个函数名只剩 1 行
-- ------------------------------------------------------------
select p.proname,
       p.pronargs,
       p.oid::regprocedure as signature
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public'
   and p.proname in ('match_dm_chunks', 'match_dm_qa', 'match_dm_stories')
 order by p.proname, p.pronargs;
