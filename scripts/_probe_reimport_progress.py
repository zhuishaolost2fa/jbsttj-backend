"""批量重跑 DM 手册 · 进度监控（本机只读，查 Supabase）。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/_probe_reimport_progress.py
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

import httpx

from app.core.config import settings

JOBS: List[Dict[str, str]] = [
    {"script_id": "94de9957-7392-411a-8c7a-62a735335af9", "title": "樱再会霜汐子",
     "job_id": "ae4097dd-739c-4e71-863b-50741c7abfbf"},
    {"script_id": "4313d539-ead1-4b9f-a2d0-c2511689ba62", "title": "众念成形",
     "job_id": "948c9fdc-54a2-4918-98c4-bbf8fdeb4a36"},
    {"script_id": "0460e827-7043-4598-8bb6-84b582572180", "title": "六角馆的谋杀鉴赏",
     "job_id": "cf70db08-b6b1-4f93-925e-71bed05cd2a9"},
    {"script_id": "a59f0bd0-622d-43d7-ae4b-340fa5412fdb", "title": "如是我观",
     "job_id": "791d1b11-fd75-47bc-972c-c0e7353accc2"},
    {"script_id": "97e68fa6-52de-4034-a277-0e44c4acf7c9", "title": "撕裂2愚者",
     "job_id": "7c574431-4a4d-4bd0-94d7-5d27dd240163"},
    {"script_id": "45021d96-5be9-4310-a4a2-2a0699fc2b95", "title": "无相人考古奇案",
     "job_id": "27c20d42-bde0-480b-b505-c9d0e155d591"},
    {"script_id": "6b630de6-faed-48ff-93fb-b5265151005a", "title": "月落洼",
     "job_id": "4db1b20c-8a03-4cd5-81fc-16532fec95f3"},
    {"script_id": "4aa23a61-b796-48e9-b095-8213771fd8e1", "title": "木夕僧之戏",
     "job_id": "9d666082-2667-493d-8729-9e67af43a46b"},
    {"script_id": "11267665-48a5-4135-8972-f5bd21ffca2d", "title": "死者在幻夜",
     "job_id": "1f2e62e0-e1ee-4080-a527-853f5d5d9b0a"},
    {"script_id": "e5aef168-3de2-4166-9e7f-d490c1eee160", "title": "病娇男孩",
     "job_id": "41f94b05-3d0b-49fe-ae06-4154bb3609df"},
    {"script_id": "b81951ff-11ba-43fa-a59d-5765c937b7bf", "title": "神乐汤",
     "job_id": "1c448006-fcdd-4cb4-8a55-09fc06cdbd54"},
    {"script_id": "7e017a81-a4cb-47ae-8db3-47ce3039bbda", "title": "紫藤夫人",
     "job_id": "39218ed4-b871-4fb2-bfa6-f09615fe52fe"},
    {"script_id": "2b197314-6aa2-4729-bcc8-d72a63416302", "title": "山鬼母",
     "job_id": "751fbe93-7b4d-4f78-abb7-0796745fb7d5"},
    {"script_id": "c2439d48-f42f-40dd-bb46-d50c7f70138d", "title": "雪乡连环",
     "job_id": "9ff4bdc3-c62d-46bc-945d-56701c47bcc9"},
    {"script_id": "6e8e8664-820d-41b5-a00f-c32092757a31", "title": "风的误算",
     "job_id": "c266e871-f689-46c1-9a9e-a921b54e5a81"},
    {"script_id": "a8b0a9c5-98df-4801-974a-25e74f124c62", "title": "鬼河怒放",
     "job_id": "df4b1033-228b-4dc7-b968-11cf84878391"},
]


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/{path}", headers=_headers(), params=params, timeout=60
    )
    if resp.status_code >= 400:
        print(f"!! {path} -> {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def main() -> int:
    job_ids = ",".join(j["job_id"] for j in JOBS)
    jobs = {
        r["id"]: r
        for r in _get(
            "script_dm_jobs",
            select="id,script_id,script_code,status,stage_detail,total_pages,processed_pages,"
                   "total_chunks,embedded_chunks,total_qa,total_stories,error_message,updated_at",
            id=f"in.({job_ids})",
        )
    }
    script_ids = ",".join(j["script_id"] for j in JOBS)
    docs = _get(
        "script_dm_documents",
        select="script_id,total_chunks,total_qa,total_stories,synthesis_status",
        script_id=f"in.({script_ids})",
    )
    doc_by_script = {str(d.get("script_id")): d for d in docs}

    print(f"{'剧本':<12} {'状态':<12} {'页':>9} {'块':>9} {'QA':>6} {'故事':>5} {'合成':<8} 说明")
    print("-" * 92)
    counts: Dict[str, int] = {}
    for j in JOBS:
        row = jobs.get(j["job_id"], {})
        status = str(row.get("status") or "?")
        counts[status] = counts.get(status, 0) + 1
        pages = f"{row.get('processed_pages') or 0}/{row.get('total_pages') or 0}"
        chunks = f"{row.get('embedded_chunks') or 0}/{row.get('total_chunks') or 0}"
        doc = doc_by_script.get(j["script_id"], {})
        qa = doc.get("total_qa") or 0
        stories = doc.get("total_stories") or 0
        synth = str(doc.get("synthesis_status") or "-")
        detail = str(row.get("stage_detail") or row.get("error_message") or "")[:28]
        print(
            f"{j['title']:<12} {status:<12} {pages:>9} {chunks:>9} {qa:>6} {stories:>5} "
            f"{synth:<8} {detail}"
        )
    print("-" * 92)
    print("汇总: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
