-- ============================================================
-- 剧本库 · 按 code 查该剧本所有「问题 + 回答」
-- 适用：Supabase Dashboard -> SQL Editor（整段执行，只读）
-- 用法：只改参数区的 script_code，整段执行即可
-- ============================================================

-- ===================== 参数区（只改这一行） =====================
drop table if exists _p;
create temp table _p (script_code text);
insert into _p values ('nian-lun');   -- ← 改成你要查的剧本 code

-- ===================== 查询：该剧本所有问答 =====================
select case q.category
         when 'rule'  then '规则'
         when 'plot'  then '剧情'
         when 'role'      then '角色'
         when 'character' then '人物'
         when 'clue'      then '线索'
         when 'flow'      then '流程'
         when 'timeline'  then '时间线'
         when 'other'     then '其他'
         else q.category
       end                              as 分类,
       q.question                        as 问题,
       q.answer                          as 回答,
       q.page_start || '-' || q.page_end as 页码
from public.script_dm_qa q
join public.scripts s on s.id = q.script_id
where s.code = (select script_code from _p)
  and s.deleted_at is null
order by q.category, q.created_at;
