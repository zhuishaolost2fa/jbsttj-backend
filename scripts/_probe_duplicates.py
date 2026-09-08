"""临时探查：列出重名剧本条目及各自真实挂载数据（精确过滤，避免批量查询截断）。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/_probe_duplicates.py
只读取、不修改。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List

import httpx

from app.core.config import settings


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/{path}",
        headers=_headers(),
        params=params,
        timeout=60,
    )
    if resp.status_code >= 400:
        print(f"  !! {path} -> {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def _count(path: str, **params: Any) -> int:
    """用 HEAD + Prefer: count=exact 拿总数，判断有没有被 limit 截断。"""
    h = dict(_headers())
    h["Prefer"] = "count=exact"
    resp = httpx.head(
        f"{settings.supabase_url}/rest/v1/{path}",
        headers=h,
        params=params,
        timeout=60,
    )
    cr = resp.headers.get("content-range") or ""
    return int(cr.split("/")[-1]) if "/" in cr else -1


def main() -> int:
    scripts = _get("scripts", select="id,title,code,status,extra,created_at",
                   order="created_at.asc", limit="500")
    print(f"剧本 {_count('scripts')} 条，取到 {len(scripts)}")
    for t in ("script_dm_documents", "script_dm_jobs", "script_dm_highlights",
              "script_dm_stories", "script_dm_synthesis"):
        print(f"  {t}: 共 {_count(t)} 条")

    names = Counter(str(s.get("title") or "").strip() for s in scripts)
    dups = [n for n, c in names.items() if c > 1 and n]
    print(f"\n重名标题 {len(dups)} 组\n")

    for name in dups:
        print(f"## {name}")
        for s in scripts:
            if str(s.get("title") or "").strip() != name:
                continue
            sid = s["id"]
            docs = _get("script_dm_documents", select="id,script_id,script_code,file_name,"
                        "total_chunks,total_qa,total_stories,is_active,created_at",
                        script_id=f"eq.{sid}")
            docs += _get("script_dm_documents", select="id,script_id,script_code,file_name,"
                         "total_chunks,total_qa,total_stories,is_active,created_at",
                         script_code=f"eq.{s.get('code')}")
            seen = set()
            uniq = []
            for d in docs:
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                uniq.append(d)
            chk = sum(int(d.get("total_chunks") or 0) for d in uniq)
            qa = sum(int(d.get("total_qa") or 0) for d in uniq)
            sto = sum(int(d.get("total_stories") or 0) for d in uniq)
            jobs = _get("script_dm_jobs", select="id,status,created_at",
                        script_id=f"eq.{sid}", order="created_at.desc", limit="3")
            hl = _count("script_dm_highlights", script_id=f"eq.{sid}")
            stor = _count("script_dm_stories", script_id=f"eq.{sid}")
            syn = _count("script_dm_synthesis", script_id=f"eq.{sid}")
            guide = (s.get("extra") or {}).get("dmGuide")
            print(
                f"   {str(s.get('code')):<34} status={str(s.get('status')):<10} "
                f"docs={len(uniq)} chunk={chk:<5} qa={qa:<5} story={sto:<5} "
                f"stories行={stor:<5} 划线={hl:<4} 合成={syn} "
                f"job={jobs[0]['status'] if jobs else '-':<10} {str(s['created_at'])[:10]}"
            )
            for d in uniq:
                print(f"        doc {d['id'][:8]} {d.get('file_name') or '-'} "
                      f"chunk={d.get('total_chunks')} qa={d.get('total_qa')} "
                      f"story={d.get('total_stories')} active={d.get('is_active')} "
                      f"{str(d.get('created_at'))[:10]}")
            print(f"        手册objectKey: "
                  f"{(guide if isinstance(guide, str) else (guide or {}).get('objectKey')) if guide else '-'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
