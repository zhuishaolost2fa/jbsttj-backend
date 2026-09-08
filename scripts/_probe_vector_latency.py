"""向量链路体检：测 Supabase 往返延迟、表规模、一次真实检索耗时。

只读，不写任何数据。跑之前去掉沙箱注入的代理环境变量。
"""
from __future__ import annotations

import os
import random
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env() -> tuple[str, str]:
    url = key = ""
    for name in (".env", ".env.local"):
        p = ROOT / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "SUPABASE_URL":
                url = v
            elif k == "SUPABASE_SERVICE_ROLE_KEY":
                key = v
    return url, key


URL, KEY = load_env()
if not URL or not KEY:
    sys.exit("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

HEADERS = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}

TABLES = [
    "script_dm_chunks",
    "script_dm_qa",
    "script_dm_documents",
    "script_dm_stories",
]


def count_rows(client: httpx.Client, table: str) -> int | None:
    """用 PostgREST 的 count=exact 拿行数；表不存在返回 None。"""
    try:
        r = client.head(
            f"{URL}/rest/v1/{table}?select=id",
            headers={**HEADERS, "Prefer": "count=exact", "Range-Unit": "items"},
            timeout=20.0,
        )
        cr = r.headers.get("content-range", "")
        if "/" in cr:
            return int(cr.split("/")[1])
        # stories 表可能没建
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"  {table}: 查询失败 {exc}")
        return None


def main() -> None:
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    with httpx.Client(timeout=30.0, limits=limits, headers=HEADERS) as client:
        # 1. 冷连接延迟（含 TLS 握手）
        t0 = time.perf_counter()
        client.get(f"{URL}/rest/v1/", timeout=20.0)
        cold = (time.perf_counter() - t0) * 1000

        # 2. 热连接往返（连接池复用，最接近真实稳态）
        warm: list[float] = []
        for _ in range(10):
            t0 = time.perf_counter()
            client.get(f"{URL}/rest/v1/scripts?select=id&limit=1", timeout=20.0)
            warm.append((time.perf_counter() - t0) * 1000)

        print("=" * 52)
        print(f"Supabase: {URL}")
        print(f"冷连接（含 TLS 握手）: {cold:.0f} ms")
        print(f"热往返 10 次: 中位 {statistics.median(warm):.0f} ms "
              f"最小 {min(warm):.0f} / 最大 {max(warm):.0f} ms")
        print("=" * 52)

        # 3. 表规模
        print("表规模：")
        total_vec = 0
        for t in TABLES:
            n = count_rows(client, t)
            if n is None:
                print(f"  {t}: 不存在或不可读")
            else:
                print(f"  {t}: {n} 行")
                if t in ("script_dm_chunks", "script_dm_qa", "script_dm_stories"):
                    total_vec += n
        print(f"  向量行合计 ≈ {total_vec}")
        # 1024 维 float32 = 4KB/行（未计 HNSW 图与 TOAST 开销）
        print(f"  裸向量体积 ≈ {total_vec * 1024 * 4 / 1024 / 1024:.1f} MB")

        # 4. 一次真实检索（随机向量，测的是 DB 侧耗时 + 网络）
        random.seed(42)
        vec = "[" + ",".join(f"{random.uniform(-1, 1):.6f}" for _ in range(1024)) + "]"
        print("-" * 52)
        print("检索耗时（随机 query，无 script 过滤，最坏情况）：")
        for rpc in ("match_dm_chunks", "match_dm_qa"):
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                r = client.post(
                    f"{URL}/rest/v1/rpc/{rpc}",
                    json={
                        "query_embedding": vec,
                        "match_count": 8,
                        "similarity_threshold": 0.0,
                    },
                    timeout=30.0,
                )
                times.append((time.perf_counter() - t0) * 1000)
            ok = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
            print(f"  {rpc}: 中位 {statistics.median(times):.0f} ms "
                  f"(min {min(times):.0f} / max {max(times):.0f}) [{ok}]")

        # 5. 单次 embedding 写入的 HTTP 开销（用一条最小 upsert 到不存在的表会报错，
        #    这里只测「POST 一个 4KB body」的往返，用 rpc 打一个空操作函数即可跳过）
        print("-" * 52)
        print(f"query 向量 payload 大小: {len(vec) / 1024:.1f} KB/次")


if __name__ == "__main__":
    main()
