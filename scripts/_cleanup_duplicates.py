"""临时脚本：清理重名剧本里的空壳条目（同一剧本被重复建了 2-3 次）。

默认**只预览不删除**；加 --apply 才真正调用 soft_delete_script。

判定为「空壳」的标准（全部满足才进待删清单）：
  - 存在同名且带手册（extra.dmGuide）的兄弟条目 → 它是重复品
  - 自己没有 script_dm_documents / stories / highlights / synthesis
  - 没有正在跑的 job

用法：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/_cleanup_duplicates.py
  PYTHONPATH=. .venv/Scripts/python.exe scripts/_cleanup_duplicates.py --apply
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Any, Dict, List

import httpx

from app.core.config import settings


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/{path}",
        headers=_headers(), params=params, timeout=60,
    )
    if resp.status_code >= 400:
        print(f"  !! GET {path} -> {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def _count(path: str, **params: Any) -> int:
    h = dict(_headers())
    h["Prefer"] = "count=exact"
    resp = httpx.head(
        f"{settings.supabase_url}/rest/v1/{path}",
        headers=h, params=params, timeout=60,
    )
    cr = resp.headers.get("content-range") or ""
    return int(cr.split("/")[-1]) if "/" in cr else -1


def _rpc(name: str, payload: Dict[str, Any]) -> Any:
    resp = httpx.post(
        f"{settings.supabase_url}/rest/v1/rpc/{name}",
        headers=_headers(), json=payload, timeout=60,
    )
    if resp.status_code >= 400:
        return {"_error": resp.status_code, "_body": resp.text[:300]}
    return resp.json()


def main() -> int:
    apply = "--apply" in sys.argv

    scripts = _get("scripts", select="id,title,code,status,extra,created_by,created_at",
                   order="created_at.asc", limit="500")
    names = Counter(str(s.get("title") or "").strip() for s in scripts)

    targets: List[Dict[str, Any]] = []
    for name, cnt in names.items():
        if cnt < 2 or not name:
            continue
        group = [s for s in scripts if str(s.get("title") or "").strip() == name]
        has_guide = [s for s in group if (s.get("extra") or {}).get("dmGuide")]
        if not has_guide:
            print(f"## {name}: 全都没手册，跳过（需要人工判断）")
            continue
        for s in group:
            if (s.get("extra") or {}).get("dmGuide"):
                continue  # 保留带手册的
            sid = s["id"]
            docs = _count("script_dm_documents", script_id=f"eq.{sid}")
            stor = _count("script_dm_stories", script_id=f"eq.{sid}")
            hl = _count("script_dm_highlights", script_id=f"eq.{sid}")
            syn = _count("script_dm_synthesis", script_id=f"eq.{sid}")
            jobs = _get("script_dm_jobs", select="id,status", script_id=f"eq.{sid}")
            running = [j for j in jobs if j.get("status") in
                       ("pending", "extracting", "generating_qa", "embedding")]
            safe = (docs == 0 and stor == 0 and hl == 0 and syn == 0 and not running)
            targets.append({"script": s, "docs": docs, "stories": stor,
                            "highlights": hl, "synthesis": syn,
                            "running": running, "safe": safe})
            flag = "待删" if safe else "!! 有数据/任务，跳过"
            print(f"## {name}")
            print(f"   {s.get('code'):<36} status={s.get('status'):<10} "
                  f"docs={docs} stories={stor} 划线={hl} 合成={syn} "
                  f"在跑任务={len(running)}  -> {flag}")

    todo = [t for t in targets if t["safe"]]
    print(f"\n合计 {len(targets)} 条重复品，其中 {len(todo)} 条可安全删除")

    if not apply:
        print("\n（预览模式，未做任何改动。加 --apply 执行删除）")
        for t in todo:
            print(f"   将删除 {t['script'].get('code')}  ({t['script'].get('title')})")
        return 0

    print("\n=== 执行删除 ===")
    ok, fail = 0, 0
    for t in todo:
        s = t["script"]
        prev = _rpc("preview_script_dm_purge", {"p_script_id": s["id"]})
        print(f"  preview {s.get('code')}: {prev}")
        res = _rpc("soft_delete_script", {"p_script_id": s["id"]})
        if isinstance(res, dict) and res.get("ok"):
            ok += 1
            print(f"  ✓ 已删 {s.get('code')}  dm_purged={res.get('dm_purged')} "
                  f"file_soft_deleted={res.get('file_soft_deleted')} "
                  f"oss_delete_required={res.get('oss_delete_required')}")
        else:
            fail += 1
            print(f"  ✗ 失败 {s.get('code')}: {res}")
    print(f"\n完成：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
