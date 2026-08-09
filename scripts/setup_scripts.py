"""剧本库 · 建表 + 灌数据迁移脚本。

与 `setup_script_options.py` 是同一套路子（PostgREST 不能执行 DDL，所以需要它），
差别在于剧本表依赖字典表，脚本会先检查字典是否已就绪，没就绪就直接拦下 ——
否则建表 SQL 里的校验触发器会把每一条剧本都判为「未知编码」。

运行方式（按可用凭据选择）：
  1. 直连数据库（推荐，建表 + 灌数据一步到位）：
       SUPABASE_DB_URL='postgresql://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres' \
       python scripts/setup_scripts.py
  2. 只打印建表 SQL（无直连凭据时，复制去 Supabase Dashboard -> SQL Editor 执行）：
       python scripts/setup_scripts.py --print-sql
  3. 表已存在、只想灌/刷剧本数据：
       python scripts/setup_scripts.py --seed-only
  4. 只做种子数据自检，不连数据库：
       python scripts/setup_scripts.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap

# 让脚本能 import 到 app 包（脚本位于 scripts/ 下）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.data.scripts_seed import SCRIPTS, iter_script_rows, validate_seed  # noqa: E402
from scripts.setup_script_options import run_ddl_direct, split_sql_statements  # noqa: E402

SQL_FILE = os.path.join(ROOT, "sql", "scripts.sql")


def load_sql() -> str:
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        return f.read()


def check_seed() -> None:
    """种子自检：编码合法性、区间方向、code 唯一性。不通过直接退出。"""
    problems = validate_seed()
    print(f"[check] 剧本条数: {len(SCRIPTS)}")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        raise SystemExit(f"[check] 种子数据有 {len(problems)} 处问题，已中止")
    print("[check] 种子数据校验通过 ✅")


async def seed_via_postgrest() -> int:
    """通过 PostgREST（service_role）按 code 幂等写入剧本。"""
    from app.services.repository import ScriptOptionRepository, ScriptRepository
    from app.services.supabase import supabase as sb

    await sb.startup()
    try:
        # 字典没灌好的话，剧本表的校验触发器会把每条记录都拒掉，
        # 与其让 35 条记录一起爆一堆看不懂的错，不如在这里给出明确指引。
        opt_repo = ScriptOptionRepository(sb)
        categories = await opt_repo.list_categories()
        if not categories:
            raise SystemExit(
                "字典表为空，请先执行： python scripts/setup_script_options.py\n"
                "（剧本的玩法/题材/发行方式/难度都引用字典编码，字典缺失会导致校验触发器全部拒绝写入）"
            )

        repo = ScriptRepository(sb)
        rows = iter_script_rows()
        # 分批写入：单次请求体过大时 PostgREST 容易超时，35 条本来不多，
        # 但这里留好批量能力，后续扩到几百条也不用改代码。
        written = 0
        batch_size = 50
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            await repo.upsert_many(batch)
            written += len(batch)
            print(f"  已写入 {written}/{len(rows)}")
        return written
    finally:
        await sb.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="剧本库 · 建表与灌数据")
    parser.add_argument("--print-sql", action="store_true", help="仅打印建表 SQL，不连接数据库")
    parser.add_argument("--seed-only", action="store_true", help="假设表已存在，仅灌种子数据")
    parser.add_argument("--check", action="store_true", help="仅做种子自检与 SQL 切分校验")
    args = parser.parse_args()

    sql = load_sql()

    if args.check:
        check_seed()
        stmts = split_sql_statements(sql)
        fn_ok = any("validate_script_option_codes" in s and "raise exception" in s for s in stmts)
        print(f"[check] SQL 语句数={len(stmts)}  校验触发器函数体完整={fn_ok}")
        for i, s in enumerate(stmts, 1):
            print(f"  {i:2d}. {textwrap.shorten(' '.join(s.split()), width=80, placeholder=' …')}")
        if not fn_ok:
            raise SystemExit("[check] 校验触发器被错误切分，SQL 有问题")
        print("[check] 通过 ✅")
        return

    if args.print_sql:
        print(sql)
        print(
            "\n# 请先确保已执行 sql/script_options.sql（字典表），再整段执行以上 SQL，"
            "随后运行： python scripts/setup_scripts.py --seed-only"
        )
        return

    check_seed()

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if args.seed_only:
        if not db_url:
            print("[seed] 仅灌数据模式：使用 PostgREST service_role 写入（需表已存在）")
        count = asyncio.run(seed_via_postgrest())
        print(f"[seed] 已 upsert 剧本 {count} 部 ✅")
        return

    if not db_url:
        print(
            "未检测到 SUPABASE_DB_URL 环境变量，无法直连执行 DDL。\n"
            "可选方案：\n"
            "  A. 提供数据库连接串后重试：\n"
            "     SUPABASE_DB_URL='postgresql://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres' \\\n"
            "     python scripts/setup_scripts.py\n"
            "  B. 打印建表 SQL 去 SQL Editor 执行： python scripts/setup_scripts.py --print-sql\n"
            "     （执行后再用： python scripts/setup_scripts.py --seed-only 灌数据）"
        )
        raise SystemExit(2)

    # 直连：建表 + 灌数据。run_ddl_direct 复用字典脚本里已验证过的实现，
    # 但它固定读字典 SQL 文件，这里临时把目标文件指过来。
    import scripts.setup_script_options as opt_setup

    original = opt_setup.SQL_FILE
    opt_setup.SQL_FILE = SQL_FILE
    try:
        run_ddl_direct(db_url)
    finally:
        opt_setup.SQL_FILE = original

    count = asyncio.run(seed_via_postgrest())
    print(f"[done] 建表 + 灌数据完成：剧本 {count} 部 ✅")


if __name__ == "__main__":
    main()
