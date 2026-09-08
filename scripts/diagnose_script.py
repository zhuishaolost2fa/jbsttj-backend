"""诊断单个剧本的「解析失败」：按 script_id / code 查剧本行 + 解析任务 + 文档。

用法：
    .venv/Scripts/python.exe scripts/diagnose_script.py <script_id 或 code>

查三件事：
1. scripts 行：title / code / status / extra.dmGuide（是否挂手册、objectKey）
2. script_dm_jobs：最近任务状态、error_message、计数
3. script_dm_documents：文档是否激活、chunks/qa 产出、embed 模型
"""

from pathlib import Path
import sys
import httpx

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


env = load_env()
SUPABASE_URL = env.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
REST_URL = f"{SUPABASE_URL}/rest/v1"
HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def fetch(path, params=None):
    url = f"{REST_URL}/{path}"
    resp = httpx.get(url, headers=HEADERS, params=params, timeout=60)
    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text[:400]}")
        return []
    data = resp.json()
    return data if isinstance(data, list) else [data]


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/diagnose_script.py <script_id 或 code>")
        sys.exit(1)
    target = sys.argv[1].strip()
    print(f"目标: {target}\n")

    # 1) scripts 行
    print("=" * 72)
    print("[1] 剧本行 scripts")
    print("=" * 72)
    if "-" in target and len(target) >= 32:  # 粗略判 UUID
        rows = fetch("scripts", params={
            "select": "id,code,title,status,created_by,extra,deleted_at,created_at",
            "id": f"eq.{target}",
        })
        script_id = target
    else:
        rows = fetch("scripts", params={
            "select": "id,code,title,status,created_by,extra,deleted_at,created_at",
            "code": f"eq.{target.lower()}",
        })
        script_id = rows[0]["id"] if rows else None
    if not rows:
        print("  ❌ 未找到剧本行（可能已软删，或 code/id 有误）")
        # 尝试 include deleted
        rows = fetch("scripts", params={
            "select": "id,code,title,status,created_by,extra,deleted_at,created_at",
            "id": f"eq.{target}",
        })
        if rows:
            print(f"  （找到 deleted_at={rows[0].get('deleted_at')} 的软删行）")
            script_id = rows[0]["id"]
    for r in rows:
        extra = r.get("extra") or {}
        dm = extra.get("dmGuide") or extra.get("dm_guide")
        print(f"  id        = {r.get('id')}")
        print(f"  code      = {r.get('code')}")
        print(f"  title     = {r.get('title')}")
        print(f"  status    = {r.get('status')}")
        print(f"  deleted   = {r.get('deleted_at')}")
        print(f"  created_by= {r.get('created_by')}")
        if dm:
            print(f"  dmGuide.objectKey = {dm.get('objectKey') or dm.get('object_key')}")
            print(f"  dmGuide.fileId    = {dm.get('fileId') or dm.get('file_id')}")
            print(f"  dmGuide.fileName  = {dm.get('fileName') or dm.get('file_name')}")
        else:
            print("  dmGuide   = (空)  ← 剧本没挂手册，解析无从谈起")
    if not script_id:
        print("\n（无法确定 script_id，后续查询跳过）")
        return

    # 2) jobs
    print()
    print("=" * 72)
    print("[2] 解析任务 script_dm_jobs（最近 10 条）")
    print("=" * 72)
    jobs = fetch("script_dm_jobs", params={
        "select": "id,status,stage_detail,error_message,object_key,total_pages,"
                  "processed_pages,total_chunks,embedded_chunks,total_qa,embedded_qa,"
                  "retry_count,created_at,updated_at,finished_at",
        "script_id": f"eq.{script_id}",
        "order": "created_at.desc",
        "limit": "10",
    })
    if not jobs:
        print("  ⚠ 无任务记录 → 自动触发(maybe_trigger)可能被静默吞，查后端日志 / 手动 ingest")
    for j in jobs:
        print(f"  job={j['id'][:8]} status={j.get('status',''):11s} "
              f"chunks={j.get('total_chunks')}/{j.get('embedded_chunks')} "
              f"qa={j.get('total_qa')}/{j.get('embedded_qa')}")
        print(f"      key={str(j.get('object_key') or '')[:50]}")
        print(f"      stage={str(j.get('stage_detail') or '')[:60]}")
        err = j.get("error_message")
        if err:
            print(f"      ❗ error_message = {err}")
        print(f"      created={str(j.get('created_at',''))[:19]} "
              f"updated={str(j.get('updated_at',''))[:19]} "
              f"finished={str(j.get('finished_at',''))[:19]} retry={j.get('retry_count')}")

    # 3) documents
    print()
    print("=" * 72)
    print("[3] 索引文档 script_dm_documents")
    print("=" * 72)
    docs = fetch("script_dm_documents", params={
        "select": "id,is_active,version,file_name,object_key,total_pages,total_chunks,"
                  "total_qa,dropped_chunks,embed_model,chat_model,content_hash,deleted_at,created_at",
        "script_id": f"eq.{script_id}",
        "order": "created_at.desc",
    })
    if not docs:
        print("  ⚠ 无文档记录")
    for d in docs:
        print(f"  doc={d['id'][:8]} active={d.get('is_active')} ver={d.get('version')} "
              f"file={str(d.get('file_name') or '')[:26]}")
        print(f"      pages={d.get('total_pages')} chunks={d.get('total_chunks')} "
              f"qa={d.get('total_qa')} dropped={d.get('dropped_chunks')}")
        print(f"      embed_model={d.get('embed_model')} chat_model={d.get('chat_model')}")
        print(f"      deleted={d.get('deleted_at')} created={str(d.get('created_at',''))[:19]}")

    print()
    print("=" * 72)
    print("[4] 结论速查")
    print("=" * 72)
    print("  - 未挂手册(dmGuide 空)          → 上传手册并写 extra.dmGuide 后重新触发")
    print("  - job failed                    → 看 error_message 字段")
    print("  - 无任务记录                    → 自动触发被吞，手动 POST /dm-guides/ingest")
    print("  - doc active=false              → 检索查不到，重新触发解析")
    print("  - chunks=0 且 job completed     → 退化索引，force 重跑")
    print("  - 任务卡在中间态且 updated 很久 → worker 死了 / MQ 断了")


if __name__ == "__main__":
    main()
