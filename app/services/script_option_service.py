"""剧本杀筛选维度业务层。

职责：
  1. 从字典表读维度与选项，组装成前端友好的结构；
  2. 带进程内 TTL 缓存 —— 字典是低频变更的参考数据，没必要每次请求都打库；
  3. 数据库不可用（未配置 / 未建表 / 网络抖动）时，自动降级到内存种子数据，
     保证「列表接口永远有东西返回」，前端筛选器不会白屏。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from app.core.exceptions import DatabaseError, NotFoundError
from app.data import script_options_seed as seed
from app.schemas.script_option import (
    ScriptOptionCategory,
    ScriptOptionGroup,
    ScriptOptionItem,
    ScriptOptionListResult,
    ScriptOptionTree,
)
from app.services.repository import ScriptOptionRepository

logger = logging.getLogger("app.script_option")

# 字典缓存 TTL（秒）：低频数据，10 分钟足够
_CACHE_TTL = 600


class _TreeCache:
    """整棵字典树的进程内缓存。"""

    def __init__(self) -> None:
        self._data: Optional[List[Dict]] = None
        self._fetched_at: float = 0.0

    def get(self) -> Optional[List[Dict]]:
        if self._data is not None and time.monotonic() - self._fetched_at <= _CACHE_TTL:
            return self._data
        return None

    def set(self, data: List[Dict]) -> None:
        self._data = data
        self._fetched_at = time.monotonic()

    def clear(self) -> None:
        self._data = None
        self._fetched_at = 0.0


_cache = _TreeCache()


class ScriptOptionService:
    def __init__(self, repo: Optional[ScriptOptionRepository] = None) -> None:
        self.repo = repo or ScriptOptionRepository()

    # ---------------- 对外方法 ----------------
    async def get_tree(self, *, only_hot: bool = False) -> ScriptOptionTree:
        """全量筛选器结构：维度 + 其下选项。"""
        groups_raw = await self._load_tree()
        groups: List[ScriptOptionGroup] = []
        total_options = 0
        for cat in groups_raw:
            items = [
                ScriptOptionItem(**opt)
                for opt in cat["options"]
                if (opt["is_hot"] if only_hot else True)
            ]
            total_options += len(items)
            groups.append(
                ScriptOptionGroup(
                    code=cat["code"],
                    name=cat["name"],
                    description=cat.get("description"),
                    multi_select=cat.get("multi_select", True),
                    sort_order=cat.get("sort_order", 0),
                    option_count=len(items),
                    options=items,
                )
            )
        return ScriptOptionTree(
            categories=groups,
            total_categories=len(groups),
            total_options=total_options,
        )

    async def list_categories(self) -> List[ScriptOptionCategory]:
        """仅维度清单（不含选项），供前端先渲染 Tab。"""
        groups_raw = await self._load_tree()
        return [
            ScriptOptionCategory(
                code=cat["code"],
                name=cat["name"],
                description=cat.get("description"),
                multi_select=cat.get("multi_select", True),
                sort_order=cat.get("sort_order", 0),
                option_count=len(cat["options"]),
            )
            for cat in groups_raw
        ]

    async def list_options(
        self, category_code: str, *, only_hot: bool = False, keyword: Optional[str] = None
    ) -> ScriptOptionListResult:
        """单维度选项列表。"""
        groups_raw = await self._load_tree()
        cat = next((c for c in groups_raw if c["code"] == category_code), None)
        if cat is None:
            raise NotFoundError(
                f"未知的筛选维度: {category_code}", code="unknown_category"
            )

        kw = (keyword or "").strip().lower()
        items: List[ScriptOptionItem] = []
        for opt in cat["options"]:
            if only_hot and not opt["is_hot"]:
                continue
            if kw:
                haystack = " ".join(
                    [opt["label"], opt.get("description") or "", " ".join(opt.get("aliases") or [])]
                ).lower()
                if kw not in haystack:
                    continue
            items.append(ScriptOptionItem(**opt))

        category = ScriptOptionCategory(
            code=cat["code"],
            name=cat["name"],
            description=cat.get("description"),
            multi_select=cat.get("multi_select", True),
            sort_order=cat.get("sort_order", 0),
            option_count=len(cat["options"]),
        )
        return ScriptOptionListResult(category=category, items=items, total=len(items))

    def invalidate_cache(self) -> None:
        _cache.clear()

    # ---------------- 内部：加载 + 缓存 + 降级 ----------------
    async def _load_tree(self) -> List[Dict]:
        cached = _cache.get()
        if cached is not None:
            return cached

        data: Optional[List[Dict]] = None
        try:
            data = await self._load_from_db()
        except DatabaseError as exc:
            logger.warning("字典表读取失败，降级到内存种子数据: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("字典表读取异常，降级到内存种子数据: %s", exc)

        if not data:
            # 未配置 Supabase / 未建表 / 表为空 → 用内存种子兜底
            data = self._load_from_seed()

        _cache.set(data)
        return data

    async def _load_from_db(self) -> List[Dict]:
        categories = await self.repo.list_categories()
        if not categories:
            return []
        options = await self.repo.list_options()

        grouped: Dict[str, List[Dict]] = {}
        for opt in options:
            grouped.setdefault(opt["category_code"], []).append(
                ScriptOptionItem.from_row(opt).model_dump()
            )

        tree: List[Dict] = []
        for cat in categories:
            tree.append(
                {
                    "code": cat["code"],
                    "name": cat["name"],
                    "description": cat.get("description"),
                    "multi_select": bool(cat.get("multi_select", True)),
                    "sort_order": int(cat.get("sort_order") or 0),
                    "options": grouped.get(cat["code"], []),
                }
            )
        return tree

    def _load_from_seed(self) -> List[Dict]:
        options_by_cat: Dict[str, List[Dict]] = {}
        for row in seed.iter_option_rows():
            options_by_cat.setdefault(row["category_code"], []).append(
                ScriptOptionItem.from_row(row).model_dump()
            )
        tree: List[Dict] = []
        for cat in seed.iter_category_rows():
            tree.append(
                {
                    "code": cat["code"],
                    "name": cat["name"],
                    "description": cat.get("description"),
                    "multi_select": bool(cat.get("multi_select", True)),
                    "sort_order": int(cat.get("sort_order") or 0),
                    "options": options_by_cat.get(cat["code"], []),
                }
            )
        return tree


_service: Optional[ScriptOptionService] = None


def get_script_option_service() -> ScriptOptionService:
    global _service
    if _service is None:
        _service = ScriptOptionService()
    return _service
