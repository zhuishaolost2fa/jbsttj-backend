"""批量重跑 DM 手册 · 第一步（本机）：上传手册到 OSS 并挂到剧本 extra.dmGuide。

只做「上传 + 写挂载」，不碰解析数据 —— 旧 chunks/qa 在**服务器本地 pgvector** 里，
本机连不到，清理与触发放在第二步的服务器脚本里做。

用法（dry-run 先看计划）：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/_reimport_upload.py
    PYTHONPATH=. .venv/Scripts/python.exe scripts/_reimport_upload.py --apply
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.services.oss import get_oss_service

SRC_DIR = Path(r"C:\Users\Administrator\Desktop\剧本杀文档")
OUT = Path(__file__).with_name("_reimport_uploaded.json")

# (文件名, 主条目 script_id, 剧本名, [需要清掉 dmGuide 的重复条目 id])
PLAN: List[Dict[str, Any]] = [
    {"file": "《樱再会霜汐子》手册.docx", "script_id": "94de9957-7392-411a-8c7a-62a735335af9",
     "title": "樱再会霜汐子", "clear": ["83ba9467-00ce-42a0-a0f9-c93a4be55603"]},
    {"file": "众念成形.docx", "script_id": "4313d539-ead1-4b9f-a2d0-c2511689ba62",
     "title": "众念成形", "clear": []},
    {"file": "六角馆的谋杀鉴赏完整.docx", "script_id": "0460e827-7043-4598-8bb6-84b582572180",
     "title": "六角馆的谋杀鉴赏", "clear": []},
    {"file": "如是我观主持人手册.docx", "script_id": "a59f0bd0-622d-43d7-ae4b-340fa5412fdb",
     "title": "如是我观", "clear": ["1ed847e0-ec30-452b-9126-c12b95b385f7"]},
    {"file": "撕裂2愚者的无冕之作.docx", "script_id": "97e68fa6-52de-4034-a277-0e44c4acf7c9",
     "title": "撕裂2愚者的无冕之作", "clear": []},
    {"file": "无相人考古奇案 .docx", "script_id": "45021d96-5be9-4310-a4a2-2a0699fc2b95",
     "title": "无相人考古奇案", "clear": ["848ea663-9bd4-4cbc-8c2b-9501f9e054ce"]},
    {"file": "月落洼.docx", "script_id": "6b630de6-faed-48ff-93fb-b5265151005a",
     "title": "月落洼", "clear": []},
    {"file": "木夕僧之戏.docx", "script_id": "4aa23a61-b796-48e9-b095-8213771fd8e1",
     "title": "木夕僧之戏", "clear": []},
    {"file": "死者在幻夜中醒来.docx", "script_id": "11267665-48a5-4135-8972-f5bd21ffca2d",
     "title": "死者在幻夜中醒来", "clear": ["d0c0dec1-d286-4acf-b382-5aef839e0326"]},
    {"file": "病娇男孩的精分日记..docx", "script_id": "e5aef168-3de2-4166-9e7f-d490c1eee160",
     "title": "病娇男孩的精分日记", "clear": []},
    {"file": "神乐汤-组织者手册.docx", "script_id": "b81951ff-11ba-43fa-a59d-5765c937b7bf",
     "title": "神乐汤", "clear": []},
    {"file": "紫藤夫人.docx", "script_id": "7e017a81-a4cb-47ae-8db3-47ce3039bbda",
     "title": "紫藤夫人", "clear": []},
    {"file": "豪门46山鬼母.docx", "script_id": "2b197314-6aa2-4729-bcc8-d72a63416302",
     "title": "山鬼母", "clear": ["2f8d2d64-5c4e-4bd9-8336-68efdb166f6a"]},
    {"file": "雪乡连环杀人事件.docx", "script_id": "c2439d48-f42f-40dd-bb46-d50c7f70138d",
     "title": "雪乡连环杀人事件", "clear": ["df9ef12d-9728-4583-b0af-3688302a952b",
                                          "00bbfd6e-875c-4055-b3d1-67e375d0b37a"]},
    {"file": "风的误算(1).docx", "script_id": "6e8e8664-820d-41b5-a00f-c32092757a31",
     "title": "风的误算", "clear": []},
    {"file": "鬼河怒放红花镇求生指南（组织者手册）.docx",
     "script_id": "a8b0a9c5-98df-4801-974a-25e74f124c62",
     "title": "鬼河怒放", "clear": []},
]

_CONTENT_TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  ".doc": "application/msword", ".pdf": "application/pdf"}


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_script(script_id: str) -> Optional[Dict[str, Any]]:
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/scripts",
        headers=_headers(),
        params={"id": f"eq.{script_id}", "select": "id,title,code,extra"},
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  !! 读取剧本失败 {script_id}: {resp.status_code} {resp.text[:160]}")
        return None
    rows = resp.json()
    return rows[0] if rows else None


def _patch_extra(script_id: str, extra: Dict[str, Any]) -> bool:
    resp = httpx.patch(
        f"{settings.supabase_url}/rest/v1/scripts",
        headers=_headers(),
        params={"id": f"eq.{script_id}"},
        json={"extra": extra},
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  !! 写回 extra 失败 {script_id}: {resp.status_code} {resp.text[:160]}")
        return False
    return True


async def _upload(path: Path, key: str) -> int:
    oss_svc = get_oss_service()
    data = path.read_bytes()
    await oss_svc.put_object(key, data, content_type=_CONTENT_TYPES.get(path.suffix.lower()))
    return len(data)


async def main(apply: bool) -> int:
    results: List[Dict[str, Any]] = []
    for item in PLAN:
        path = SRC_DIR / item["file"]
        title, script_id = item["title"], item["script_id"]
        if not path.exists():
            print(f"!! 文件不存在: {path}")
            continue
        size = path.stat().st_size
        ext = path.suffix.lower()
        key = f"scripts/{script_id}/dm-guide/{uuid.uuid4().hex}{ext}"
        print(f"[{title}] {path.name} ({size/1024/1024:.1f}MB) -> {key}")

        if not apply:
            results.append({**item, "object_key": key, "size": size})
            continue

        row = _get_script(script_id)
        if row is None:
            continue
        await _upload(path, key)
        extra = dict(row.get("extra") or {})
        extra.pop("dm_guide", None)
        extra["dmGuide"] = {
            "objectKey": key,
            "fileName": path.name,
            "fileSize": size,
        }
        if not _patch_extra(script_id, extra):
            continue
        print("   上传 + 挂载完成")

        for dup_id in item.get("clear") or []:
            dup = _get_script(dup_id)
            if not dup:
                continue
            dextra = dict(dup.get("extra") or {})
            if "dmGuide" in dextra or "dm_guide" in dextra:
                dextra.pop("dmGuide", None)
                dextra.pop("dm_guide", None)
                if _patch_extra(dup_id, dextra):
                    print(f"   重复条目 {dup_id[:8]} 已摘除 dmGuide")

        results.append({**item, "object_key": key, "size": size})

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(results)} 条 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
