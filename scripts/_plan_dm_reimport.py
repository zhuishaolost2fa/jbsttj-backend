"""临时脚本：生成「桌面手册文件 → 库内剧本条目」的导入计划（只输出，不改数据）。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/_plan_dm_reimport.py
输出：scripts/_reimport_plan.json
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

SRC_DIR = Path(r"C:\Users\Administrator\Desktop\剧本杀文档")
OUT = Path(__file__).with_name("_reimport_plan.json")

# 文件名 → 剧本名（去掉「手册」「组织者手册」「主持人手册」等后缀与序号前缀）
_NOISE = [
    "组织者手册", "主持人手册", "DM手册", "dm手册", "手册",
]
_BRACKET = re.compile(r"[《》\[\]()（）]")


def _normalize(name: str) -> str:
    """文件名 → 用于匹配的剧本名关键词。"""
    stem = Path(name).stem
    stem = _BRACKET.sub("", stem)
    stem = re.sub(r"^\d+[_.\-]", "", stem)  # 开头的 1_ / 2.
    for noise in _NOISE:
        stem = stem.replace(noise, "")
    return stem.strip(" .-_")


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/{path}",
        headers=_headers(), params=params, timeout=60,
    )
    if resp.status_code >= 400:
        print(f"!! {path} -> {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def _pick_files() -> List[Dict[str, Any]]:
    """每个剧本挑一份文件：优先 docx，其次 pdf。"""
    grouped: Dict[str, Dict[str, Path]] = {}
    for p in sorted(SRC_DIR.iterdir()):
        if p.suffix.lower() not in (".docx", ".doc", ".pdf"):
            continue
        key = _normalize(p.name)
        if not key:
            continue
        grouped.setdefault(key, {})[p.suffix.lower()] = p

    picked: List[Dict[str, Any]] = []
    for key, variants in grouped.items():
        path = variants.get(".docx") or variants.get(".doc") or variants.get(".pdf")
        if path is None:
            continue
        picked.append(
            {
                "match_key": key,
                "file": str(path),
                "file_name": path.name,
                "ext": path.suffix.lower(),
                "size": path.stat().st_size,
                "size_mb": round(path.stat().st_size / 1024 / 1024, 1),
                "variants": {k: v.name for k, v in variants.items()},
            }
        )
    picked.sort(key=lambda x: x["match_key"])
    return picked


def main() -> int:
    scripts = _get(
        "scripts",
        select="id,title,aliases,code,status,extra,created_at",
        order="created_at.asc",
        limit="500",
    )
    docs = _get(
        "script_dm_documents",
        select="id,script_id,script_code,total_chunks,total_qa,total_stories,is_active,synthesis_status",
        limit="1000",
    )
    doc_by_script: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        sid = str(d.get("script_id"))
        cur = doc_by_script.get(sid)
        # 优先留激活的、其次留 chunk 最多的
        if cur is None or (d.get("is_active") and not cur.get("is_active")) or (
            int(d.get("total_chunks") or 0) > int(cur.get("total_chunks") or 0)
        ):
            doc_by_script[sid] = d

    files = _pick_files()

    # 文件 ↔ 剧本匹配：按标题包含关系（双向）
    plan: List[Dict[str, Any]] = []
    for f in files:
        key = f["match_key"]
        cands: List[Dict[str, Any]] = []
        for s in scripts:
            title = str(s.get("title") or "")
            aliases = [str(a) for a in (s.get("aliases") or [])]
            names = [title] + aliases
            hit = False
            for n in names:
                if not n:
                    continue
                if n == key or key in n or n in key:
                    hit = True
                    break
            if hit:
                cands.append(s)
        plan.append({**f, "candidates": [
            {
                "id": c["id"],
                "title": c.get("title"),
                "code": c.get("code"),
                "chunks": int(doc_by_script.get(c["id"], {}).get("total_chunks") or 0),
                "qa": int(doc_by_script.get(c["id"], {}).get("total_qa") or 0),
                "stories": int(doc_by_script.get(c["id"], {}).get("total_stories") or 0),
                "has_guide": bool((c.get("extra") or {}).get("dmGuide")),
            }
            for c in cands
        ]})

    OUT.write_text(
        json.dumps({"files": plan, "script_count": len(scripts)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"文件数 {len(files)}，剧本数 {len(scripts)} -> {OUT}\n")
    for row in plan:
        c = row["candidates"]
        flag = "OK " if len(c) == 1 else ("多 " if len(c) > 1 else "无 ")
        ids = ", ".join(f"{x['title']}({x['chunks']}/{x['qa']}/{x['stories']})" for x in c) or "—"
        print(f"{flag}{row['match_key']:<20} {row['ext']:<6} {row['size_mb']:>7}MB -> {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
