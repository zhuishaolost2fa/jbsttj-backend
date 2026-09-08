"""批量重跑 DM 手册 · 第二步（服务器容器内执行）：清旧数据 + 触发解析。

必须在服务器容器内跑：chunks/qa 已下沉到容器网络里的 pgvector，本机连不到。
用法（容器内）：
    docker cp _reimport_trigger.py jbs-worker:/app/_reimport_trigger.py
    docker exec jbs-worker python _reimport_trigger.py
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx

from app.core.config import settings
from app.schemas.dm_guide import IngestRequest
from app.services.dm_service import DMGuideService
from app.services.dm_store import get_dm_store

# 由 scripts/_reimport_upload.py --apply 产出，需与第一步保持一致
TARGETS: List[Dict[str, str]] = [
    {"script_id": "94de9957-7392-411a-8c7a-62a735335af9", "title": "樱再会霜汐子"},
    {"script_id": "4313d539-ead1-4b9f-a2d0-c2511689ba62", "title": "众念成形"},
    {"script_id": "0460e827-7043-4598-8bb6-84b582572180", "title": "六角馆的谋杀鉴赏"},
    {"script_id": "a59f0bd0-622d-43d7-ae4b-340fa5412fdb", "title": "如是我观"},
    {"script_id": "97e68fa6-52de-4034-a277-0e44c4acf7c9", "title": "撕裂2愚者的无冕之作"},
    {"script_id": "45021d96-5be9-4310-a4a2-2a0699fc2b95", "title": "无相人考古奇案"},
    {"script_id": "6b630de6-faed-48ff-93fb-b5265151005a", "title": "月落洼"},
    {"script_id": "4aa23a61-b796-48e9-b095-8213771fd8e1", "title": "木夕僧之戏"},
    {"script_id": "11267665-48a5-4135-8972-f5bd21ffca2d", "title": "死者在幻夜中醒来"},
    {"script_id": "e5aef168-3de2-4166-9e7f-d490c1eee160", "title": "病娇男孩的精分日记"},
    {"script_id": "b81951ff-11ba-43fa-a59d-5765c937b7bf", "title": "神乐汤"},
    {"script_id": "7e017a81-a4cb-47ae-8db3-47ce3039bbda", "title": "紫藤夫人"},
    {"script_id": "2b197314-6aa2-4729-bcc8-d72a63416302", "title": "山鬼母"},
    {"script_id": "c2439d48-f42f-40dd-bb46-d50c7f70138d", "title": "雪乡连环杀人事件"},
    {"script_id": "6e8e8664-820d-41b5-a00f-c32092757a31", "title": "风的误算"},
    {"script_id": "a8b0a9c5-98df-4801-974a-25e74f124c62", "title": "鬼河怒放"},
]


def _fetch_row(script_id: str) -> Dict[str, Any]:
    key = settings.supabase_service_role_key
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/scripts",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"id": f"eq.{script_id}", "select": "id,title,code,extra"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise RuntimeError(f"剧本不存在: {script_id}")
    return rows[0]


async def main() -> int:
    store = get_dm_store()
    svc = DMGuideService()
    dispatched: List[Dict[str, Any]] = []

    for t in TARGETS:
        sid, title = t["script_id"], t["title"]
        try:
            row = _fetch_row(sid)
        except Exception as exc:  # noqa: BLE001
            print(f"!! [{title}] 读取剧本失败: {exc}")
            continue

        # 1) 清掉旧解析产物（Supabase 元数据 + 本地 pgvector 向量）
        try:
            store.purge_script_side_effects(sid)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻��派发，force 重跑会覆盖
            print(f"!! [{title}] 清理旧产物失败（继续）: {exc}")

        # 2) 触发解析（force=True：忽略内容指纹，全流程重跑）
        script = SimpleNamespace(
            id=row["id"], title=row.get("title") or "", extra=row.get("extra") or {}
        )
        try:
            resp = await svc.trigger_ingest(
                script, IngestRequest(force=True), user_id=""
            )
        except Exception as exc:  # noqa: BLE001
            print(f"!! [{title}] 派发失败: {exc}")
            continue
        print(f"[ok] {title:<16} job={resp.job_id} status={resp.status} reused={resp.reused}")
        dispatched.append({"script_id": sid, "title": title, "job_id": resp.job_id})

    print(f"\n派发完成 {len(dispatched)}/{len(TARGETS)}")
    print(json.dumps(dispatched, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
