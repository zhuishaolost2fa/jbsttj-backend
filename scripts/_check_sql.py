"""用 pglast（libpg_query）校验 SQL 文件的语法。

离线校验，不连数据库；能抓出 DDL/DML 与 plpgsql 函数体里的语法错误，
抓不出「表/列不存在」这类语义问题（那部分只能去 SQL Editor 跑）。

依赖（装在隔离 venv，不污染项目环境）：
    pip install pglast

用法：
    python scripts/_check_sql.py sql/script_delete.sql
    python scripts/_check_sql.py sql/dm_story.sql
"""
import sys

import pglast

path = sys.argv[1]
sql = open(path, encoding="utf-8").read()

try:
    stmts = pglast.parse_sql(sql)
    print(f"OK parse_sql: {len(stmts)} statements")
except Exception as exc:  # noqa: BLE001
    print(f"FAIL parse_sql: {exc}")
    sys.exit(1)

try:
    pl = pglast.parse_plpgsql(sql)
except Exception as exc:  # noqa: BLE001
    print(f"FAIL parse_plpgsql: {exc}")
    sys.exit(1)

for fn in pl:
    print(f"OK plpgsql: {fn}")
