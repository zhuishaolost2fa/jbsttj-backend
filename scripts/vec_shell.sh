#!/usr/bin/env bash
# 本地 pgvector 向量库快速查看（在服务器上执行）
#
#   ./vec_shell.sh                进入交互式 psql
#   ./vec_shell.sh "select ..."   执行一条 SQL 后退出
#   ./vec_shell.sh stats          常用统计速查（行数 / 体积 / 索引 / 各剧本分布）
#   ./vec_shell.sh docs           列出已导入的手册文档
#   ./vec_shell.sh probe "凶手是谁"  用真实 embedding 跑一次检索，验证召回是否正常
#
# 注意：pgvector 只监听 127.0.0.1，公网连不上，只能在服务器本机或通过 SSH 隧道访问。

set -euo pipefail

CONTAINER="${CONTAINER:-jbs-pgvector}"
DB="${DB:-jbsvector}"
USER="${PGUSER:-postgres}"

psql_run() {
    docker exec -i "$CONTAINER" psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"
}

STATS_SQL="
select 'chunks' as 表名, count(*) as 行数 from public.script_dm_chunks
union all select 'qa', count(*) from public.script_dm_qa
union all select 'documents(影子)', count(*) from public.dm_documents
union all select 'chunks 无向量', count(*) from public.script_dm_chunks where embedding is null
union all select 'qa 无向量', count(*) from public.script_dm_qa where embedding is null;
select pg_size_pretty(pg_total_relation_size('public.script_dm_chunks')) as chunks_含索引,
       pg_size_pretty(pg_total_relation_size('public.script_dm_qa')) as qa_含索引,
       pg_size_pretty(pg_database_size(current_database())) as 整库;
select script_code, count(*) as chunk数 from public.script_dm_chunks group by 1 order by 2 desc limit 10;
select indexrelname as 索引名, pg_size_pretty(pg_relation_size(indexrelid)) as 体积
  from pg_stat_user_indexes where relname like 'script_dm_%' order by 2 desc limit 5;
"

DOCS_SQL="
select left(d.id::text, 8) as id短, d.script_code, d.is_active,
       d.deleted_at is not null as 已删除,
       (select count(*) from public.script_dm_chunks c where c.document_id = d.id) as chunk数,
       (select count(*) from public.script_dm_qa q where q.document_id = d.id) as qa数,
       d.created_at::date as 导入日期
  from public.dm_documents d order by d.created_at desc limit 20;
"

case "${1:-}" in
    stats)
        psql_run -c "$STATS_SQL"
        ;;
    docs)
        psql_run -c "$DOCS_SQL"
        ;;
    probe)
        QUERY="${2:-凶手是谁}"
        # 在 api 容器里跑：需要 SiliconFlow 的 embedding 客户端，pgvector 容器里没有
        docker compose -f /opt/jbs/docker-compose.prod.yml exec -T api python - "$QUERY" <<'PY'
import sys, time
from app.services.vector_store import get_vector_store
from app.services.llm import get_llm_client
import asyncio
store = get_vector_store()
q = sys.argv[1] if len(sys.argv) > 1 else "凶手是谁"
vec = asyncio.run(get_llm_client().aembed_query(q))
for name, fn in (("match_chunks", store.match_chunks), ("match_qa", store.match_qa)):
    t0 = time.perf_counter()
    rows = fn(vec, match_count=5, similarity_threshold=0.2)
    ms = (time.perf_counter() - t0) * 1000
    print(f"[{name}] {ms:.2f} ms  命中 {len(rows)} 条")
    for r in rows[:3]:
        text = (r.get("content") or r.get("question") or "").replace("\n", " ")
        print(f"    {r['similarity']:.4f}  {text[:56]}")
PY
        ;;
    "")
        docker exec -it "$CONTAINER" psql -U "$USER" -d "$DB"
        ;;
    *)
        psql_run -c "$1"
        ;;
esac
