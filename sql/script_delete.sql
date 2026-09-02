-- ============================================================
-- 剧本删除 · 统一清理能力（Supabase SQL Editor 整段执行，可重复执行）
--
-- 为什么单独抽一个文件？
--   「删除一本剧本」不是一个 delete 语句，而是横跨 4 组表的组合动作：
--     1. public.scripts        —— 软删（deleted_at + status=offline，保留 code 唯一约束下的复活能力）
--     2. public.script_dm_*    —— 物理删（jobs/documents/chunks/qa/questions/stories/highlights）
--     3. public.files          —— 软删手册文件记录（带 user_id 过滤，绝不错删别人的文件）
--     4. OSS 对象              —— 仅在引用计数归零时物理删（秒传会让同一对象被多本剧本共用）
--   这套语义原本散落在 sql/scripts.sql（软删列）、sql/dm_rag.sql 与 sql/dm_story.sql
--   （purge_dm_document）以及后端 app/services/script_service.py::delete_script 里，
--   改一处容易漏另外两处。本文件把它们收敛成一组函数，作为**唯一权威实现**。
--
-- 依赖与执行顺序：
--   sql/scripts.sql → sql/schema.sql(files) → sql/dm_rag.sql → sql/dm_story.sql
--   → sql/dm_questions.sql →【本文件】
--   本文件会覆盖 dm_rag.sql / dm_story.sql 里的 purge_dm_document，
--   因此必须排在它们之后执行，否则会被旧版本覆盖回去。
--
-- 设计原则：
--   1. 幂等：全部 create or replace，重复执行不产生副作用；
--   2. 容错：对可能尚未建表的模块（如未执行 dm_story.sql 的库）用 to_regclass 判存在再删；
--   3. 可观测：清理类函数统一返回 jsonb 报告（各表删除行数 / 剩余引用数），
--      便于后端记日志与写测试断言，而不是「删完不知道删了什么」；
--   4. 默认只授权 service_role：删除是高危动作，anon / authenticated 一律不可调用。
--
-- 执行后自检：select public.check_script_delete_installed();
--   all_installed = true 且 purge_dm_document_language = 'plpgsql' 即为正确状态。
--
-- 后端接入：全部函数走 PostgREST RPC
--   POST /rest/v1/rpc/soft_delete_script   body {"p_script_id": "…", "p_user_id": "…"}
--   POST /rest/v1/rpc/purge_script_dm_side_effects  body {"p_script_id": "…"}
-- ============================================================


-- ------------------------------------------------------------
-- 1. DM 解析产物清理
-- ------------------------------------------------------------

-- 1.1 单文档清理（重新解析 / force 重跑前调用）
--     语义区别于剧本删除：用户划线（highlights）**不删**，只置 orphaned，
--     等新 story 写完由 reanchor_dm_highlights 按 quote 重新挂接。
--
--     这里覆盖 dm_rag.sql / dm_story.sql 里的同名函数（旧版只清 chunks+qa，
--     或只多清 stories），实现从 language sql 换成 plpgsql 以做「表是否存在」
--     的容错判断 —— 先显式 drop 再建，避开跨语言替换的边界情况。
drop function if exists public.purge_dm_document(uuid);
create or replace function public.purge_dm_document(p_document_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if to_regclass('public.script_dm_qa') is not null then
        delete from public.script_dm_qa where document_id = p_document_id;
    end if;

    if to_regclass('public.script_dm_chunks') is not null then
        delete from public.script_dm_chunks where document_id = p_document_id;
    end if;

    if to_regclass('public.script_dm_stories') is not null then
        delete from public.script_dm_stories where document_id = p_document_id;
    end if;

    -- 划线保留、降级为 orphaned（不物理删除，用户笔记不因重跑而丢失）
    if to_regclass('public.script_dm_highlights') is not null then
        update public.script_dm_highlights
           set status = 'orphaned',
               updated_at = now()
         where document_id = p_document_id
           and status = 'active'
           and deleted_at is null;
    end if;

    if to_regclass('public.script_dm_documents') is not null then
        update public.script_dm_documents
           set total_chunks = 0,
               total_qa = 0,
               dropped_chunks = 0,
               total_stories = 0,
               updated_at = now()
         where id = p_document_id;
    end if;
end;
$$;


-- 1.2 剧本级清理：物理删除某剧本的全部导入副作用
--     剧本行李软删除，不会触发 script_id 外键级联，必须手动清。
--     两个关键顺序：
--       - 先取消在跑任务：行删掉后 worker 进度上报会落到 0 行，
--         提前置 cancelled 能让卡在守卫点位的任务尽早停下、少烧 API 额度；
--       - 再从叶子到根删行（highlights → stories → chunks → qa →
--         questions → documents → jobs），即使某个库漏配了级联也能安全删。
create or replace function public.purge_script_dm_side_effects(p_script_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_deleted jsonb := '{}'::jsonb;
    v_tbl     text;
    v_n       integer;
begin
    -- 1) 取消未终结的流水线任务
    if to_regclass('public.script_dm_jobs') is not null then
        update public.script_dm_jobs
           set status = 'cancelled',
               error_message = coalesce(error_message, '剧本已删除，解析任务取消'),
               updated_at = now()
         where script_id = p_script_id
           and status not in ('completed', 'failed', 'cancelled', 'skipped');
        get diagnostics v_n = row_count;
        v_deleted := v_deleted || jsonb_build_object('jobs_cancelled', v_n);
    end if;

    -- 2) 从叶子到根物理删除（表名即返回报告里的键名）
    foreach v_tbl in array array[
        'public.script_dm_highlights',
        'public.script_dm_stories',
        'public.script_dm_chunks',
        'public.script_dm_qa',
        'public.script_dm_questions',
        'public.script_dm_documents',
        'public.script_dm_jobs'
    ]
    loop
        if to_regclass(v_tbl) is not null then
            execute format('delete from %s where script_id = $1', v_tbl) using p_script_id;
            get diagnostics v_n = row_count;
            v_deleted := v_deleted || jsonb_build_object(v_tbl, v_n);
        end if;
    end loop;

    return jsonb_build_object('script_id', p_script_id, 'deleted', v_deleted);
end;
$$;


-- 1.3 干跑预览：只统计「将要删掉多少行」，不改任何数据。
--     删除前给运营/后端一个确认面板用，避免误删大剧本。
create or replace function public.preview_script_dm_purge(p_script_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
    v_counts jsonb := '{}'::jsonb;
    v_tbl    text;
    v_n      integer;
begin
    foreach v_tbl in array array[
        'public.script_dm_highlights',
        'public.script_dm_stories',
        'public.script_dm_chunks',
        'public.script_dm_qa',
        'public.script_dm_questions',
        'public.script_dm_documents',
        'public.script_dm_jobs'
    ]
    loop
        if to_regclass(v_tbl) is not null then
            execute format('select count(*) from %s where script_id = $1', v_tbl)
              into v_n using p_script_id;
            v_counts := v_counts || jsonb_build_object(v_tbl, v_n);
        end if;
    end loop;

    return jsonb_build_object('script_id', p_script_id, 'rows', v_counts);
end;
$$;


-- ------------------------------------------------------------
-- 2. OSS 对象引用计数
--    object_key 在 files 表刻意没有唯一约束：秒传时多条记录指向同一对象，
--    所以物理删除前必须「数引用」，而不是「删记录」。
-- ------------------------------------------------------------

create or replace function public.count_file_references(p_object_key text)
returns integer
language sql
stable
security definer
set search_path = public
as $$
    select count(*)::integer
      from public.files
     where object_key = p_object_key
       and deleted_at is null;
$$;


-- 仍把该 OSS 对象当作 DM 手册引用的未删除剧本数（可剔除正在删除的那一本）
create or replace function public.count_dm_guide_refs(
    p_object_key text,
    p_exclude_script_id uuid default null
)
returns integer
language sql
stable
security definer
set search_path = public
as $$
    select count(*)::integer
      from public.scripts s
     where s.deleted_at is null
       and s.extra -> 'dmGuide' ->> 'objectKey' = p_object_key
       and (p_exclude_script_id is null or s.id <> p_exclude_script_id);
$$;


-- ------------------------------------------------------------
-- 3. 剧本软删除主入口（对应后端 ScriptService.delete_script）
--
--    一次 RPC 完成：取消任务 → 清 DM 产物 → 软删 files 记录 →
--    数引用 → 软删剧本行并摘掉 extra.dmGuide。
--    OSS 对象的物理删除**不在这里做**（SQL 层没有 OSS 凭据），
--    由后端拿到返回的 oss_delete_required 后调 OSS SDK 删除。
-- ------------------------------------------------------------
create or replace function public.soft_delete_script(
    p_script_id   uuid,
    p_user_id     uuid   default null,   -- 为空则跳过 files 软删（跨用户场景不错删）
    p_purge_dm    boolean default true    -- 为 false 时只软删剧本行，保留解析产物
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row          public.scripts;
    v_guide        jsonb;
    v_object_key   text;
    v_file_id      text;
    v_file_uuid    uuid := null;
    v_file_deleted boolean := false;
    v_refs         integer := 0;
    v_dm           jsonb := '{}'::jsonb;
    v_n            integer;
begin
    -- 行锁：并发重复点删除时，第二次会看到 deleted_at 已被置上
    select * into v_row from public.scripts where id = p_script_id for update;

    if not found then
        return jsonb_build_object(
            'ok', false, 'reason', 'script_not_found', 'script_id', p_script_id
        );
    end if;

    if v_row.deleted_at is not null then
        return jsonb_build_object(
            'ok', false, 'reason', 'already_deleted',
            'script_id', p_script_id, 'deleted_at', v_row.deleted_at
        );
    end if;

    -- 手册引用：兼容 extra.dmGuide 与历史写法 extra.dm_guide
    v_guide := coalesce(v_row.extra -> 'dmGuide', v_row.extra -> 'dm_guide');
    if v_guide is not null then
        v_object_key := nullif(v_guide ->> 'objectKey', '');
        v_file_id    := nullif(coalesce(v_guide ->> 'fileId', v_guide ->> 'file_id'), '');
    end if;

    -- 1) DM 解析产物
    if p_purge_dm then
        v_dm := public.purge_script_dm_side_effects(p_script_id);
    end if;

    -- 2) 手册文件记录软删：带 user_id 过滤，不属于当前用户的自然 no-op
    if v_file_id is not null and p_user_id is not null then
        begin
            v_file_uuid := v_file_id::uuid;
        exception when others then
            v_file_uuid := null;   -- extra 里塞了非 uuid 的脏数据：跳过，不让删除失败
        end;

        if v_file_uuid is not null then
            update public.files
               set deleted_at = now(),
                   updated_at = now()
             where id = v_file_uuid
               and user_id = p_user_id
               and deleted_at is null;
            get diagnostics v_n = row_count;
            v_file_deleted := v_n > 0;
        end if;
    end if;

    -- 3) 引用计数 = files 未删记录 + 其它剧本的 dmGuide 引用，全为 0 才可物理删对象
    if v_object_key is not null then
        v_refs := public.count_file_references(v_object_key)
                + public.count_dm_guide_refs(v_object_key, p_script_id);
    end if;

    -- 4) 软删剧本行，并摘掉 extra.dmGuide（墓碑行不残留指向已删文件的指针）
    update public.scripts
       set deleted_at = now(),
           updated_at = now(),
           status     = 'offline',
           extra      = case
                            when extra is not null
                             and (extra -> 'dmGuide' is not null or extra -> 'dm_guide' is not null)
                                then (extra - 'dmGuide') - 'dm_guide'
                            else extra
                        end
     where id = p_script_id;

    return jsonb_build_object(
        'ok',                 true,
        'script_id',          p_script_id,
        'code',               v_row.code,
        'title',              v_row.title,
        'dm_purged',          v_dm,
        'file_soft_deleted',  v_file_deleted,
        'object_key',         v_object_key,
        'refs_remaining',     v_refs,
        'oss_delete_required', v_object_key is not null and v_refs = 0
    );
end;
$$;


-- ------------------------------------------------------------
-- 4. 复活（撤销软删除）
--    软删只是下架语义，误删后可直接恢复；code 唯一约束下若同名剧本
--    已重新上架，返回 conflict 而不是抛约束错误（便于后端友好提示）。
-- ------------------------------------------------------------
create or replace function public.restore_script(
    p_script_id uuid,
    p_status    text default 'published'
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row public.scripts;
begin
    select * into v_row from public.scripts where id = p_script_id for update;

    if not found then
        return jsonb_build_object('ok', false, 'reason', 'script_not_found', 'script_id', p_script_id);
    end if;
    if v_row.deleted_at is null then
        return jsonb_build_object('ok', false, 'reason', 'not_deleted', 'script_id', p_script_id);
    end if;

    begin
        update public.scripts
           set deleted_at = null,
               status     = coalesce(p_status, 'published'),
               updated_at = now()
         where id = p_script_id;
    exception when unique_violation then
        return jsonb_build_object(
            'ok', false, 'reason', 'code_conflict',
            'script_id', p_script_id, 'code', v_row.code
        );
    end;

    return jsonb_build_object('ok', true, 'script_id', p_script_id, 'code', v_row.code);
end;
$$;


-- ------------------------------------------------------------
-- 5. 物理删除（运维专用，前端不要接）
--    软删剧本留着墓碑行是为了保住 code 唯一约束下的复活能力与数据可追溯性，
--    只有清理测试脏数据 / 用户要求彻底删除（合规诉求）时才走这里。
--
--    注意 script_requests.script_id 外键是 NO ACTION，物理删剧本会被阻塞，
--    因此默认先 detach（把 script_id 置空、保留标题文本），再删行。
-- ------------------------------------------------------------
create or replace function public.hard_delete_script(
    p_script_id          uuid,
    p_user_id            uuid   default null,
    p_detach_requests    boolean default true,
    p_soft_delete_files  boolean default true
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row        public.scripts;
    v_guide      jsonb;
    v_object_key text;
    v_file_id    text;
    v_file_uuid  uuid := null;
    v_refs       integer := 0;
    v_dm         jsonb := '{}'::jsonb;
    v_requests   integer := 0;
begin
    select * into v_row from public.scripts where id = p_script_id for update;
    if not found then
        return jsonb_build_object('ok', false, 'reason', 'script_not_found', 'script_id', p_script_id);
    end if;

    v_guide := coalesce(v_row.extra -> 'dmGuide', v_row.extra -> 'dm_guide');
    if v_guide is not null then
        v_object_key := nullif(v_guide ->> 'objectKey', '');
        v_file_id    := nullif(coalesce(v_guide ->> 'fileId', v_guide ->> 'file_id'), '');
    end if;

    -- 1) DM 产物：先显式清一遍（表上虽有 on delete cascade，但显式清能拿到行数报告）
    v_dm := public.purge_script_dm_side_effects(p_script_id);

    -- 2) 解绑求解析记录（否则外键 NO ACTION 会阻塞删行）
    if p_detach_requests and to_regclass('public.script_requests') is not null then
        update public.script_requests
           set script_id = null,
               updated_at = now()
         where script_id = p_script_id;
        get diagnostics v_requests = row_count;
    end if;

    -- 3) 手册文件记录软删（同步软删除语义，避免「文件还在、剧本没了」）
    if p_soft_delete_files and v_file_id is not null and p_user_id is not null then
        begin
            v_file_uuid := v_file_id::uuid;
        exception when others then
            v_file_uuid := null;
        end;
        if v_file_uuid is not null then
            update public.files
               set deleted_at = now(),
                   updated_at = now()
             where id = v_file_uuid
               and user_id = p_user_id
               and deleted_at is null;
        end if;
    end if;

    if v_object_key is not null then
        v_refs := public.count_file_references(v_object_key)
                + public.count_dm_guide_refs(v_object_key, p_script_id);
    end if;

    -- 4) 删行（script_dm_* 由外键 cascade 兜底）
    delete from public.scripts where id = p_script_id;

    return jsonb_build_object(
        'ok',                  true,
        'script_id',           p_script_id,
        'code',                v_row.code,
        'dm_purged',           v_dm,
        'requests_detached',   v_requests,
        'object_key',          v_object_key,
        'refs_remaining',      v_refs,
        'oss_delete_required', v_object_key is not null and v_refs = 0
    );
end;
$$;


-- ------------------------------------------------------------
-- 6. 兜底清理：清掉「剧本已软删、DM 产物还留着」的孤儿数据
--    后端清理失败（worker 挂了 / Redis 不可用 / 中途异常）时会留下这类残骸，
--    既占向量索引空间，也可能让检索命中已下架剧本的内容。
--    建议挂 pg_cron 每天跑一次：
--      select cron.schedule('purge-dm-orphans', '0 4 * * *',
--                           $$select public.purge_orphan_dm_data(200)$$);
-- ------------------------------------------------------------
create or replace function public.purge_orphan_dm_data(p_limit integer default 200)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id     uuid;
    v_purged integer := 0;
    v_report jsonb := '[]'::jsonb;
begin
    for v_id in
        select s.id
          from public.scripts s
         where s.deleted_at is not null
         order by s.deleted_at
         limit greatest(coalesce(p_limit, 200), 1)
    loop
        v_report := v_report || public.purge_script_dm_side_effects(v_id);
        v_purged := v_purged + 1;
    end loop;

    return jsonb_build_object('scanned', v_purged, 'details', v_report);
end;
$$;


-- 诊断视图：哪些已删剧本还残留 DM 数据（日常巡检用）
--
-- 动态生成：只为当前库里**真实存在**的 script_dm_* 表生成统计列。
-- 这样未执行 dm_rag.sql / dm_story.sql 的库不会因缺表而让整段脚本报错；
-- 也避免写死列清单后，新增 story/highlight 表时巡检口径悄悄落后于 purge 口径
-- （purge_script_dm_side_effects 清 7 张表，视图必须统计同样 7 张表）。
do $$
declare
    v_short text;
    v_table text;
    v_cols  text := '';
    v_conds text := '';
begin
    foreach v_short in array array[
        'documents', 'stories', 'highlights', 'chunks', 'qa', 'questions'
    ]
    loop
        v_table := 'public.script_dm_' || v_short;
        if to_regclass(v_table) is not null then
            v_cols := v_cols || format(
                ', (select count(*) from %s t where t.script_id = s.id) as %I',
                v_table, v_short
            );
            v_conds := v_conds
                || case when v_conds = '' then '' else ' or ' end
                || format('exists (select 1 from %s t where t.script_id = s.id)', v_table);
        end if;
    end loop;

    -- 一张 DM 表都没有：视图无从建起，跳过（不报错，保持整段脚本可重复执行）
    if v_conds = '' then
        return;
    end if;

    execute format(
        'create or replace view public.v_dm_orphan_scripts as '
        || 'select s.id as script_id, s.code, s.title, s.deleted_at%s '
        || 'from public.scripts s where s.deleted_at is not null and (%s)',
        v_cols, v_conds
    );
end;
$$;


-- ------------------------------------------------------------
-- 7. 安装自检：确认「最终版」没被旧文件覆盖回去
--
--    purge_dm_document 在 dm_rag.sql / dm_story.sql 里各有一份 language sql 的旧实现，
--    本文件靠「排在其后执行」取胜；一旦有人事后重跑那两个文件，旧版会静默覆盖回来
--    （Postgres 的 create or replace 允许换语言，只删 chunks+qa，stories 会漏清）。
--    跑一次这个自检即可确认现状，建议加进部署后的验收清单。
--      select public.check_script_delete_installed();
--    → {"all_installed": true, "purge_dm_document_language": "plpgsql", ...}
-- ------------------------------------------------------------
create or replace function public.check_script_delete_installed()
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
    with expected(name, args) as (
        values
            ('purge_dm_document',             'uuid'),
            ('purge_script_dm_side_effects',  'uuid'),
            ('preview_script_dm_purge',       'uuid'),
            ('count_file_references',         'text'),
            ('count_dm_guide_refs',           'text, uuid'),
            ('soft_delete_script',            'uuid, uuid, boolean'),
            ('restore_script',                'uuid, text'),
            ('hard_delete_script',            'uuid, uuid, boolean, boolean'),
            ('purge_orphan_dm_data',          'integer')
    ),
    installed as (
        select e.name,
               p.oid is not null            as ok,
               l.lanname                    as lang
          from expected e
          left join pg_proc     p
                 on p.proname = e.name
                and p.pronamespace = 'public'::regnamespace
                and pg_get_function_identity_arguments(p.oid) = e.args
          left join pg_language l on l.oid = p.prolang
    )
    select jsonb_build_object(
               'all_installed', bool_and(ok),
               'purge_dm_document_language',
                   max(lang) filter (where name = 'purge_dm_document'),
               'functions',
                   jsonb_agg(jsonb_build_object('name', name, 'installed', ok, 'language', lang)
                             order by name)
           )
      from installed;
$$;


-- ------------------------------------------------------------
-- 8. 权限：默认全部回收，只把执行权交给 service_role
--    删除类函数一旦对 anon 开放，等于任何人都能清空 DM 库；
--    巡检视图会暴露已下架剧本的残留情况，同样只给 service_role。
--    用动态 SQL 遍历函数清单，避免漏授/漏收；角色不存在时跳过（非 Supabase 环境）。
-- ------------------------------------------------------------
do $$
declare
    v_fn   text;
    v_role text;
begin
    foreach v_fn in array array[
        'public.purge_dm_document(uuid)',
        'public.purge_script_dm_side_effects(uuid)',
        'public.preview_script_dm_purge(uuid)',
        'public.count_file_references(text)',
        'public.count_dm_guide_refs(text, uuid)',
        'public.soft_delete_script(uuid, uuid, boolean)',
        'public.restore_script(uuid, text)',
        'public.hard_delete_script(uuid, uuid, boolean, boolean)',
        'public.purge_orphan_dm_data(integer)',
        'public.check_script_delete_installed()'
    ]
    loop
        -- 回收：public 是「所有角色」的隐式集合，必须先收
        foreach v_role in array array['public', 'anon', 'authenticated']
        loop
            if exists (select 1 from pg_roles where rolname = v_role) then
                execute format('revoke execute on function %s from %I', v_fn, v_role);
            end if;
        end loop;

        if exists (select 1 from pg_roles where rolname = 'service_role') then
            execute format('grant execute on function %s to service_role', v_fn);
        end if;
    end loop;

    -- 巡检视图：默认对 public 开放，必须显式收回后再单独授权
    if to_regclass('public.v_dm_orphan_scripts') is not null then
        foreach v_role in array array['public', 'anon', 'authenticated']
        loop
            if exists (select 1 from pg_roles where rolname = v_role) then
                execute format('revoke all on public.v_dm_orphan_scripts from %I', v_role);
            end if;
        end loop;

        if exists (select 1 from pg_roles where rolname = 'service_role') then
            execute 'grant select on public.v_dm_orphan_scripts to service_role';
        end if;
    end if;
end;
$$;


-- ------------------------------------------------------------
-- 9. 用法速查
-- ------------------------------------------------------------
-- ① 删除剧本（后端主路径）：
--      select public.soft_delete_script('<script_id>', '<user_id>');
--    → {"ok":true, "dm_purged":{"deleted":{...}}, "object_key":"uploads/...",
--       "refs_remaining":0, "oss_delete_required":true}
--    oss_delete_required = true 时，后端再调 OSS delete_object(object_key)。
--
-- ② 只清 DM 产物（保留剧本行，用于「重新解析」）：
--      select public.purge_script_dm_side_effects('<script_id>');
--
-- ③ 删前预览（运营确认面板）：
--      select public.preview_script_dm_purge('<script_id>');
--
-- ④ 误删恢复：
--      select public.restore_script('<script_id>');           -- 恢复为 published
--      select public.restore_script('<script_id>', 'draft');  -- 恢复为草稿
--
-- ⑤ 巡检 & 兜底：
--      select * from public.v_dm_orphan_scripts;
--      select public.purge_orphan_dm_data(200);
--
-- ⑥ 彻底物理删除（慎用）：
--      select public.hard_delete_script('<script_id>', '<user_id>');
--
-- ⑦ 部署后自检（确认函数齐全、purge_dm_document 没被旧文件覆盖回 sql 版）：
--      select public.check_script_delete_installed();
--    期望：all_installed = true，purge_dm_document_language = 'plpgsql'
-- ============================================================
