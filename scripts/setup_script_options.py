"""剧本杀筛选维度字典 · 建表 + 灌数据迁移脚本。

为什么需要这个脚本？
  Supabase 的 PostgREST 只能做增删改查（DML），无法执行建表（DDL）。
  而字典接口又依赖两张表，因此本脚本负责「真正把表建出来并灌入种子数据」。

运行方式（按可用凭据选择）：
  1. 直连数据库（推荐，可真正执行 DDL 建表 + 灌数据）：
       SUPABASE_DB_URL='postgresql://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres' \
       python scripts/setup_script_options.py
  2. 只打印建表 SQL（无直连凭据时，复制去 Supabase Dashboard -> SQL Editor 执行）：
       python scripts/setup_script_options.py --print-sql
  3. 表已存在、只想用 PostgREST 灌/刷种子数据：
       python scripts/setup_script_options.py --seed-only

行为说明：
  - 直连模式下，脚本先用 pg8000 执行 sql/script_options.sql 建表、索引、RLS、视图；
    随后复用业务层（ScriptOptionRepository + PostgREST service_role）把种子数据 upsert 进去。
  - 直连不可用时，降级为：打印建表 SQL，并提示用户去 SQL Editor 执行；
    若已手动建表，可加 --seed-only 走 PostgREST 灌数据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import textwrap

# 让脚本能 import 到 app 包（脚本位于 scripts/ 下）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.data.script_options_seed import iter_category_rows, iter_option_rows  # noqa: E402

SQL_FILE = os.path.join(ROOT, "sql", "script_options.sql")


# ------------------------------------------------------------
# SQL 语句切分：必须正确识别 $$ 美元引用与 ' 字符串，避免把函数体内的 ; 误切。
# ------------------------------------------------------------
def split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    cur: list[str] = []
    i, n = 0, len(sql)
    in_single = False
    dollar_tag = None  # 当前处于 $$ 或 $tag$ 引用块内

    while i < n:
        ch = sql[i]

        # 在美元引用块内：只寻找闭合标签
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                cur.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            cur.append(ch)
            i += 1
            continue

        # 在单引号字符串内
        if in_single:
            cur.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":  # 转义的 ''
                    cur.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        # 行注释：跳过到行尾
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue

        # 块注释：跳过到 */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue

        # 单引号字符串开始
        if ch == "'":
            in_single = True
            cur.append(ch)
            i += 1
            continue

        # 美元引用开始：$$ 或 $tag$
        if ch == "$":
            m = re.match(r"\$([A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                cur.append(dollar_tag)
                i += len(dollar_tag)
                continue
            cur.append(ch)
            i += 1
            continue

        # 语句结束
        if ch == ";":
            stmt = "".join(cur).strip()
            if stmt:
                statements.append(stmt)
            cur = []
            i += 1
            continue

        cur.append(ch)
        i += 1

    tail = "".join(cur).strip()
    if tail:
        statements.append(tail)
    return statements


def load_sql() -> str:
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------
# 直连数据库建表
# ------------------------------------------------------------
def run_ddl_direct(db_url: str) -> None:
    try:
        import pg8000.dbapi as dbapi  # 纯 Python 驱动，Windows 免编译
    except ImportError:
        raise SystemExit(
            "未安装 pg8000，请先： pip install pg8000  （或直接用 --print-sql 去 SQL Editor 建表）"
        )

    sql = load_sql()
    statements = split_sql_statements(sql)
    print(f"[DDL] 切分出 {len(statements)} 条语句，开始执行...")

    conn = dbapi.connect(dsn=db_url)
    try:
        with conn.cursor() as cur:
            for idx, stmt in enumerate(statements, start=1):
                head = " ".join(stmt.split())[:70]
                print(f"  ({idx}/{len(statements)}) {head}")
                cur.execute(stmt)
        conn.commit()
        print("[DDL] 建表完成 ✅")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------
# 通过 PostgREST（复用业务层）灌种子数据
# ------------------------------------------------------------
async def seed_via_postgrest() -> tuple[int, int]:
    from app.services.supabase import supabase as sb
    from app.services.repository import ScriptOptionRepository

    await sb.startup()
    try:
        repo = ScriptOptionRepository(sb)
        cat_rows = iter_category_rows()
        opt_rows = iter_option_rows()
        await repo.upsert_categories(cat_rows)
        await repo.upsert_options(opt_rows)
        return len(cat_rows), len(opt_rows)
    finally:
        await sb.shutdown()


# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="剧本杀筛选字典 · 建表与灌数据")
    parser.add_argument("--print-sql", action="store_true", help="仅打印建表 SQL，不连接数据库")
    parser.add_argument("--seed-only", action="store_true", help="假设表已存在，仅用 PostgREST 灌种子数据")
    parser.add_argument("--check", action="store_true", help="仅校验建表 SQL 可被正确切分，不连接数据库")
    args = parser.parse_args()

    sql = load_sql()

    if args.check:
        stmts = split_sql_statements(sql)
        fn_ok = any("touch_updated_at" in s and "return new" in s for s in stmts)
        print(f"[check] 语句数={len(stmts)}  触发器函数体完整={fn_ok}")
        for i, s in enumerate(stmts, 1):
            print(f"  {i:2d}. {textwrap.shorten(' '.join(s.split()), width=80, placeholder=' …')}")
        if not fn_ok:
            raise SystemExit("[check] 触发器函数被错误切分，SQL 有问题")
        print("[check] 通过 ✅")
        return

    if args.print_sql:
        print(sql)
        print(
            "\n# 请将以上 SQL 整段复制粘贴到 Supabase Dashboard -> SQL Editor 执行，"
            "随后运行： python scripts/setup_script_options.py --seed-only"
        )
        return

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if args.seed_only:
        if not db_url:
            print("[seed] 仅灌数据模式：使用 PostgREST service_role 写入（需表已存在）")
        cats, opts = asyncio.run(seed_via_postgrest())
        print(f"[seed] 已 upsert 维度 {cats} 个、选项 {opts} 个 ✅")
        return

    if not db_url:
        print(
            "未检测到 SUPABASE_DB_URL 环境变量，无法直连执行 DDL。\n"
            "可选方案：\n"
            "  A. 提供数据库连接串后重试：\n"
            "     SUPABASE_DB_URL='postgresql://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres' \\\n"
            "     python scripts/setup_script_options.py\n"
            "  B. 直接打印建表 SQL 去 SQL Editor 执行： python scripts/setup_script_options.py --print-sql\n"
            "     （执行后再用： python scripts/setup_script_options.py --seed-only 灌数据）"
        )
        raise SystemExit(2)

    # 直连：建表 + 灌数据
    run_ddl_direct(db_url)
    cats, opts = asyncio.run(seed_via_postgrest())
    print(f"[done] 建表 + 灌数据完成：维度 {cats} 个、选项 {opts} 个 ✅")


if __name__ == "__main__":
    main()
