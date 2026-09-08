"""临时探查：列出库里所有剧本 + 手册挂载情况 + DM 文档解析状态。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/_probe_scripts_state.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import httpx

from app.core.config import settings


def _headers() -> Dict[str, str]:
    return {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Accept": "application/json",
    }


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    url = f"{settings.supabase_url}/rest/v1/{path}"
    resp = httpx.get(url, headers=_headers(), params=params, timeout=30)
    if resp.status_code >= 400:
        print(f"  !! {path} -> {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def main() -> int:
    scripts = _get(
        "scripts",
        select="id,title,code,status,extra,created_at",
        order="created_at.asc",
        limit="200",
    )
    print(f"剧本总数: {len(scripts)}")

    docs = _get(
        "script_dm_documents",
        select="id,script_id,script_code,file_name,total_chunks,total_qa,total_stories,is_active,synthesis_status,created_at",
        order="created_at.desc",
        limit="500",
    )
    by_script: Dict[str, List[Dict[str, Any]]] = {}
    for d in docs:
        by_script.setdefault(str(d.get("script_id") or d.get("script_code")), []).append(d)

    jobs = _get(
        "script_dm_jobs",
        select="id,script_id,script_code,status,created_at",
        order="created_at.desc",
        limit="500",
    )
    jobs_by_script: Dict[str, Dict[str, Any]] = {}
    for j in jobs:  # 已按时间倒序，第一条即最近一次
        jobs_by_script.setdefault(str(j.get("script_id")), j)

    print()
    header = f"{'标题':<22} {'code':<26} {'手册':<5} {'chunks':>7} {'qa':>6} {'story':>6} {'合成':<8} {'最近任务':<12}"
    print(header)
    print("-" * len(header) * 2)
    for s in scripts:
        title = str(s.get("title") or "")[:20]
        code = str(s.get("code") or "")[:24]
        extra = s.get("extra") or {}
        guide = extra.get("dmGuide") or extra.get("dm_guide")
        has_guide = "有" if guide else "无"
        rows = by_script.get(str(s.get("id")), [])
        act = next((r for r in rows if r.get("is_active")), rows[0] if rows else None)
        chunks = act.get("total_chunks") if act else None
        qa = act.get("total_qa") if act else None
        story = act.get("total_stories") if act else None
        synth = str(act.get("synthesis_status") or "-") if act else "-"
        job = jobs_by_script.get(str(s.get("id")), {})
        print(
            f"{title:<22} {code:<26} {has_guide:<5} "
            f"{str(chunks or 0):>7} {str(qa or 0):>6} {str(story or 0):>6} {synth:<8} "
            f"{str(job.get('status') or '-'):<12}"
        )
        if guide:
            key = guide if isinstance(guide, str) else (guide.get("objectKey") or "")
            print(f"      objectKey: {key}")

    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        print()
        print(json.dumps(scripts, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
