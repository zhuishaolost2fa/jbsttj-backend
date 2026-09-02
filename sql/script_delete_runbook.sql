-- ============================================================
-- 删除单个剧本及其副作用 · 操作手册（Runbook）
--
-- 与 sql/script_delete.sql 的区别：
--   script_delete.sql  = **建函数**（幂等，只需执行 1 次，之后长期有效）
--   本文件             = **用函数**（每次删剧本时照着跑，不建任何对象）
--
-- 前置条件（第一次用之前做一次，之后不用重复）：
--   1. 在 Supabase SQL Editor 整段执行 sql/script_delete.sql；
--   2. 执行自检，确认安装正确：
--        select public.check_script_delete_installed();
--      期望：all_installed = true，purge_dm_document_language = 'plpgsql'
--      （若为 'sql'，说明 dm_rag.sql / dm_story.sql 被重跑过、旧版覆盖回来了，
--        需再执行一次 script_delete.sql）
--
-- 安全约定：
--   本文件里凡是**会改数据**的语句都放在注释块中 —— 必须先替换掉
--   <...> 占位符、再单独复制出来执行。整段选中执行不会误删任何东西
--   （未替换的占位符会因 uuid 语法错误直接报错终止）。
--
-- 权限：SQL Editor 以 postgres 角色执行，绕过 RLS；
--       若走 HTTP（PostgREST）调用，函数只授权了 service_role，
--       必须带 service_role key，用 anon key 会 401/404。
-- ============================================================


-- ------------------------------------------------------------
-- ① 先找到要删的剧本（可直接执行，把关键词换成实际内容）
-- ------------------------------------------------------------
select id,
       code,
       title,
       status,
       created_by,
       deleted_at,
       extra -> 'dmGuide' ->> 'objectKey' as guide_object_key,   -- OSS 对象，可能要物理删
       extra -> 'dmGuide' ->> 'fileId'    as guide_file_id       -- files 表记录，会被软删
  from public.scripts
 where title ilike '%关键词%'      -- 按标题模糊查
    or code  = 'juben-code'        -- 或按编码精确查
 order by updated_at desc
 limit 20;


-- ------------------------------------------------------------
-- ② 删前预览：只数不删（可直接执行，替换 script_id）
--
--    返回各张 DM 表「将要删掉多少行」，确认无误再执行第 ③ 步。
--    没有任何 DM 表时返回空对象 {} —— 说明这本剧本没导过手册，直接删即可。
-- ------------------------------------------------------------
-- select public.preview_script_dm_purge('<script_id>'::uuid);


-- ------------------------------------------------------------
-- ③ 执行删除
--
--    两个必读前提：
--      · 剧本行是**软删**（deleted_at + status=offline），可随时恢复（第 ⑥ 步）；
--      · 一次调用完成：取消在跑任务 → 清 7 张 DM 表 → 软删 files 记录 →
--        数引用 → 软删剧本行并摘掉 extra.dmGuide。
--
--    参数：
--      p_script_id  剧本 id（必填）
--      p_user_id    剧本创建者 id（可选）。**传了才会软删 files 里的手册记录**，
--                   让文件从「我的文件」列表消失；不传则跳过这一步
--                   （跨用户删除场景下绝不错删别人的文件）。
--                   拿不到就从 ① 的 created_by 列取。
--      p_purge_dm   默认 true。传 false = 只下架剧本、保留解析产物。
-- ------------------------------------------------------------

-- 3-A 标准删除（推荐：剧本 + DM 产物 + 手册文件记录一起处理）
-- select public.soft_delete_script('<script_id>'::uuid, '<user_id>'::uuid);

-- 3-B 不传 user_id（只清 DM 产物，files 记录保持不动）
-- select public.soft_delete_script('<script_id>'::uuid);

-- 3-C 只下架剧本、保留解析产物（例如只是想临时从列表隐藏）
-- select public.soft_delete_script('<script_id>'::uuid, '<user_id>'::uuid, false);

-- 返回示例：
--   {
--     "ok": true,
--     "script_id": "...", "code": "...", "title": "...",
--     "dm_purged": { "deleted": {
--         "public.script_dm_highlights": 12, "public.script_dm_stories": 86,
--         "public.script_dm_chunks": 240,   "public.script_dm_qa": 158,
--         "public.script_dm_questions": 3,  "public.script_dm_documents": 1,
--         "public.script_dm_jobs": 1,       "jobs_cancelled": 0
--     }},
--     "file_soft_deleted": true,
--     "object_key": "uploads/dm/xxx.pdf",
--     "refs_remaining": 0,
--     "oss_delete_required": true        -- ★ 见第 ⑤ 步
--   }
-- 失败返回（ok=false）的三种情况：
--   script_not_found  剧本不存在
--   already_deleted   已经删过了（幂等保护，重复点删除不会二次清理）
--   （抛异常）        script_dm_* 外键阻塞，看报错表名


-- ------------------------------------------------------------
-- ④ 校验清理结果（可直接执行，替换 script_id）
-- ------------------------------------------------------------
-- 剧本行应为 deleted_at 非空、status=offline、extra 里已无 dmGuide
-- select id, code, title, status, deleted_at, extra
--   from public.scripts where id = '<script_id>'::uuid;

-- DM 产物应全部归零
-- select public.preview_script_dm_purge('<script_id>'::uuid);

-- 巡检视图里不应再出现这本剧本
-- select * from public.v_dm_orphan_scripts where script_id = '<script_id>'::uuid;


-- ------------------------------------------------------------
-- ⑤ OSS 对象：SQL 删不掉，需要单独处理
--
--    数据库里没有 OSS 凭据，所以函数只负责数引用，不负责删对象。
--    当第 ③ 步返回 oss_delete_required = true 时，说明没有任何 files 记录
--    和其它剧本再引用这个对象，可以安全物理删除：
--
--    方式一（推荐，走后端）：
--      await oss.delete_object(object_key)     -- 后端拿到返回值后调用
--
--    方式二（运维手动，ossutil）：
--      ossutil rm oss://jbs-store/<object_key>
--
--    refs_remaining > 0 时**不要删对象** —— 秒传会让同一份 PDF 被多本剧本
--    共用，删了会让其它剧本的手册变成死链。
--    残留对象可留着不管（files 记录已软删，业务上不可见），
--    或走 OSS 生命周期规则自动清理。
-- ------------------------------------------------------------


-- ------------------------------------------------------------
-- ⑥ 误删恢复（可直接执行，替换 script_id）
--
--    软删只是下架语义，恢复后剧本原样回到列表。
--    若该 code 已被新剧本占用，返回 code_conflict 而不是报错。
-- ------------------------------------------------------------
-- select public.restore_script('<script_id>'::uuid);              -- 恢复为 published
-- select public.restore_script('<script_id>'::uuid, 'draft');     -- 恢复为草稿

-- 注意：恢复**不会**还原 DM 解析产物和 files 记录 —— 那些已被物理删/软删，
--       需要重新导入手册跑一遍流水线。


-- ------------------------------------------------------------
-- ⑦ 彻底物理删除（慎用，前端不要接）
--
--    软删留墓碑行是为了保住 code 唯一约束下的复活能力与可追溯性，
--    只有清理测试脏数据、或用户按合规要求要求「彻底删除」时才走这里。
--    script_requests.script_id 外键是 NO ACTION，直接删行会被阻塞，
--    函数默认先 detach（置空 script_id、保留标题文本）再删行。
-- ------------------------------------------------------------
-- select public.hard_delete_script('<script_id>'::uuid, '<user_id>'::uuid);


-- ------------------------------------------------------------
-- ⑧ 批量 / 兜底清理
--
--    后端清理失败（worker 挂了、Redis 不可用、中途异常）会留下
--    「剧本已软删、DM 产物还在」的孤儿数据 —— 既占向量索引，
--    也可能让检索命中已下架剧本的内容。
-- ------------------------------------------------------------
-- 看有哪些残骸
-- select * from public.v_dm_orphan_scripts;

-- 按删除时间从早到晚清理最多 N 本（默认 200）
-- select public.purge_orphan_dm_data(200);

-- 建议挂 pg_cron 每天凌晨 4 点自动跑：
--   select cron.schedule('purge-dm-orphans', '0 4 * * *',
--                        $$select public.purge_orphan_dm_data(200)$$);


-- ------------------------------------------------------------
-- ⑨ 走 HTTP 调用（后端 / curl，等价第 ③ 步）
--
--    函数已通过 PostgREST 暴露，只能带 service_role key 调用。
-- ------------------------------------------------------------
-- curl -X POST "https://ratcjkjvynubglofrvkt.supabase.co/rest/v1/rpc/soft_delete_script" \
--   -H "apikey: <SERVICE_ROLE_KEY>" \
--   -H "Authorization: Bearer <SERVICE_ROLE_KEY>" \
--   -H "Content-Type: application/json" \
--   -d '{"p_script_id": "<script_id>", "p_user_id": "<user_id>"}'
--
-- 其它端点同一路径换函数名即可：
--   /rpc/preview_script_dm_purge        {"p_script_id": "..."}
--   /rpc/purge_script_dm_side_effects   {"p_script_id": "..."}
--   /rpc/restore_script                 {"p_script_id": "..."}
--   /rpc/check_script_delete_installed  {}
-- ============================================================
