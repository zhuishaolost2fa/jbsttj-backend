"""剧本库业务层。

职责：
  1. **字典校验** —— 提交的玩法/题材/发行方式/难度必须是字典里真实存在的 code。
     数据库触发器也会拦，但那层报错是 PostgREST 的英文原文，不适合直接给前端；
     在这里拦掉可以给出「未知的玩法编码: xxx，可选值：...」这种能直接照着改的提示。
  2. **slug 生成** —— 新增时不传 code 就按标题自动派生，重名自动加后缀。
  3. **展示文案** —— 人数「6人（3男3女）」、时长「4-5小时」这类拼装放在后端，
     避免每个前端各写一套格式化逻辑。
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pypinyin import lazy_pinyin

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.data import script_options_seed as opt_seed
from app.schemas.common import Pagination
from app.schemas.script import (
    LabeledCode,
    ScriptCreate,
    ScriptItem,
    ScriptItemCamel,
    ScriptListResult,
    ScriptSearchByNameResult,
    ScriptUpdate,
)
from app.services.repository import ScriptRepository
from app.services.script_option_service import ScriptOptionService, get_script_option_service
from app.schemas.dm_guide import DMGuideRef

logger = logging.getLogger("app.script")

MAX_PAGE_SIZE = 100


def slugify(title: str) -> str:
    """把剧本名转成 URL 友好的 slug。

    中文优先转拼音（pypinyin），英文/数字原样保留，再用连字符规整。
    例：雾都疑影 -> wu-du-yi-ying；雾都疑影 2nd -> wu-du-yi-ying-2nd。
    若转拼音后为空（极端情况，如纯符号标题），调用方会退回到随机短码。
    """
    # 逐词转拼音：中文变拼音，非中文（字母/数字/标点）原样保留
    tokens = lazy_pinyin(title, errors="default")
    raw = "-".join(t for t in tokens if t)
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug[:48]


class ScriptService:
    def __init__(
        self,
        repo: Optional[ScriptRepository] = None,
        option_service: Optional[ScriptOptionService] = None,
    ) -> None:
        self.repo = repo or ScriptRepository()
        self.options = option_service or get_script_option_service()

    # ================= 查询 =================
    async def list_scripts(
        self,
        *,
        keyword: Optional[str] = None,
        playstyles: Optional[List[str]] = None,
        themes: Optional[List[str]] = None,
        release_types: Optional[List[str]] = None,
        difficulties: Optional[List[str]] = None,
        players: Optional[int] = None,
        duration: Optional[int] = None,
        min_rating: Optional[float] = None,
        recommended_only: bool = False,
        status: Optional[str] = "published",
        created_by: Optional[str] = None,
        sort: str = "hot",
        limit: int = 20,
        offset: int = 0,
    ) -> ScriptListResult:
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, offset)

        # 筛选条件里的编码同样校验，避免前端传错字典 code 时静默返回空列表
        await self._assert_codes(
            playstyles=playstyles,
            themes=themes,
            release_type=release_types,
            difficulty=difficulties,
        )

        rows, total = await self.repo.list_scripts(
            keyword=keyword,
            playstyles=playstyles,
            themes=themes,
            release_types=release_types,
            difficulties=difficulties,
            players=players,
            duration=duration,
            min_rating=min_rating,
            recommended_only=recommended_only,
            status=status,
            created_by=created_by,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        labels = await self._label_map()
        items = [self._to_item(row, labels) for row in rows]
        return ScriptListResult(
            items=items,
            pagination=Pagination(
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
            ),
        )

    async def get_script(self, id_or_code: str) -> ScriptItem:
        """按 UUID 或业务 code 取详情，前端拿哪个都能查。"""
        row = None
        if _looks_like_uuid(id_or_code):
            row = await self.repo.get(id_or_code)
        if row is None:
            row = await self.repo.get_by_code(id_or_code.lower())
        if row is None:
            raise NotFoundError(f"剧本不存在: {id_or_code}", code="script_not_found")
        return self._to_item(row, await self._label_map())

    # ================= 新增 =================
    async def create_script(self, payload: ScriptCreate, *, user_id: Optional[str] = None) -> ScriptItem:
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        await self._assert_codes(
            playstyles=data.get("playstyles"),
            themes=data.get("themes"),
            release_type=data.get("release_type"),
            difficulty=data.get("difficulty"),
        )
        self._check_gender_sum(data)

        code = data.get("code") or await self._generate_code(payload.title)
        if await self.repo.get_by_code(code, include_deleted=True):
            raise ConflictError(f"剧本编码已存在: {code}", code="script_code_exists")
        data["code"] = code

        data.setdefault("status", "published")
        if user_id:
            data["created_by"] = user_id

        row = await self.repo.create(data)
        logger.info("新增剧本 %s (%s)", row.get("title"), row.get("code"))
        return self._to_item(row, await self._label_map())

    # ================= 修改 =================
    async def update_script(self, script_id: str, payload: ScriptUpdate) -> ScriptItem:
        """局部更新。

        用 `exclude_unset=True` 区分「没传」与「传了 null」：
        没传的字段保持原值，显式传 null 的字段会被清空 —— 这正是 PATCH 该有的语义。

        **code 创建后不可变**：详情页 URL 用 code 拼接，运行时改 code 会让已分享/
        收藏的旧链接 404。误传了与现有不同的 code 时明确报错，而不是静默放行或忽略。
        """
        existing = await self.repo.get(script_id)
        if existing is None:
            raise NotFoundError(f"剧本不存在: {script_id}", code="script_not_found")

        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise ValidationError("请求体为空，没有需要更新的字段", code="empty_payload")

        await self._assert_codes(
            playstyles=data.get("playstyles"),
            themes=data.get("themes"),
            release_type=data.get("release_type"),
            difficulty=data.get("difficulty"),
        )
        # 区间字段：只改一侧时要拿库里的旧值一起校验，否则会写出半截区间
        self._check_partial_ranges(existing, data)
        self._check_gender_sum({**existing, **data})

        # code 创建后不可变：详情页 URL 用 code 拼接，运行时改 code 会让已分享/
        # 收藏的旧链接 404。误传不同 code 时明确报错，而不是静默放行或静默忽略。
        incoming_code = data.pop("code", None)
        if incoming_code and incoming_code != existing.get("code"):
            raise ValidationError("剧本编码(code)创建后不可修改", code="code_immutable")

        row = await self.repo.update(script_id, data)
        if row is None:
            raise NotFoundError(f"剧本不存在: {script_id}", code="script_not_found")
        logger.info("更新剧本 %s，字段: %s", script_id, ", ".join(sorted(data)))
        return self._to_item(row, await self._label_map())

    async def delete_script(self, script_id: str) -> None:
        row = await self.repo.soft_delete(script_id)
        if row is None:
            raise NotFoundError(f"剧本不存在: {script_id}", code="script_not_found")
        logger.info("下架剧本 %s", script_id)

    # ================= 内部：校验 =================
    async def _assert_codes(
        self,
        *,
        playstyles: Optional[List[str]] = None,
        themes: Optional[List[str]] = None,
        release_type: Optional[Any] = None,
        difficulty: Optional[Any] = None,
    ) -> None:
        checks: List[Tuple[str, List[str], str]] = []
        if playstyles:
            checks.append((opt_seed.CATEGORY_PLAYSTYLE, playstyles, "玩法"))
        if themes:
            checks.append((opt_seed.CATEGORY_THEME, themes, "题材"))
        if release_type:
            values = release_type if isinstance(release_type, list) else [release_type]
            checks.append((opt_seed.CATEGORY_RELEASE, values, "发行方式"))
        if difficulty:
            values = difficulty if isinstance(difficulty, list) else [difficulty]
            checks.append((opt_seed.CATEGORY_DIFFICULTY, values, "难度"))
        if not checks:
            return

        valid = await self._valid_codes()
        for category, values, label in checks:
            allowed = valid.get(category, set())
            unknown = [v for v in values if v not in allowed]
            if unknown:
                raise ValidationError(
                    f"未知的{label}编码: {', '.join(unknown)}",
                    code="unknown_option_code",
                    details={
                        "category": category,
                        "invalid": unknown,
                        "allowed": sorted(allowed),
                    },
                )

    @staticmethod
    def _check_partial_ranges(existing: Dict[str, Any], data: Dict[str, Any]) -> None:
        for lo, hi, label in (
            ("player_min", "player_max", "人数"),
            ("duration_min", "duration_max", "时长"),
        ):
            if lo not in data and hi not in data:
                continue
            lo_val = data.get(lo, existing.get(lo))
            hi_val = data.get(hi, existing.get(hi))
            if (lo_val is None) != (hi_val is None):
                raise ValidationError(f"{label}区间必须同时提供上下限", code="invalid_range")
            if lo_val is not None and lo_val > hi_val:
                raise ValidationError(f"{label}区间方向错误：{lo_val} > {hi_val}", code="invalid_range")

    @staticmethod
    def _check_gender_sum(data: Dict[str, Any]) -> None:
        """男女配置之和不能超过最大人数，否则前端角色卡渲染会对不上。"""
        male = data.get("male_count")
        female = data.get("female_count")
        flexible = data.get("flexible_count") or 0
        player_max = data.get("player_max")
        if male is None and female is None:
            return
        total = (male or 0) + (female or 0) + flexible
        if player_max is not None and total > player_max:
            raise ValidationError(
                f"角色性别配置之和 {total} 超过最大人数 {player_max}",
                code="invalid_gender_config",
            )

    async def _valid_codes(self) -> Dict[str, set]:
        """字典中所有合法 code，按维度分组。走字典服务，天然带缓存与离线兜底。"""
        tree = await self.options.get_tree()
        return {c.code: {o.code for o in c.options} for c in tree.categories}

    async def _label_map(self) -> Dict[str, Dict[str, str]]:
        """{维度: {code: 中文标签}}，用于把出参里的编码翻成标签。"""
        tree = await self.options.get_tree()
        return {c.code: {o.code: o.label for o in c.options} for c in tree.categories}

    async def _generate_code(self, title: str) -> str:
        """按标题派生拼音 slug；极端情况下退化为随机短码，冲突则追加序号。"""
        base = slugify(title) or f"script-{uuid.uuid4().hex[:8]}"
        candidate = base
        for suffix in range(2, 12):
            if not await self.repo.get_by_code(candidate, include_deleted=True):
                return candidate
            candidate = f"{base}-{suffix}"
        return f"{base}-{uuid.uuid4().hex[:6]}"

    # ================= 按名称查找 =================
    async def search_by_name(
        self, name: str, *, limit: int = 10
    ) -> Tuple[List[ScriptItemCamel], bool]:
        """按名称查找剧本，返回（小驼峰剧本列表, 是否找到）。

        先在库里用 `title` / `aliases` 做模糊召回，再在内存里按匹配质量排序：
        标题精确命中 > 别名精确命中 > 标题前缀命中 > 标题包含命中。
        找不到返回空列表（found=False），**不抛 404**，方便前端直接消费。
        """
        name = (name or "").strip()
        if not name:
            return [], False

        rows = await self.repo.search_by_name(name, limit=max(1, min(limit, 50)))
        labels = await self._label_map()

        name_l = name.lower()

        def _score(r: Dict[str, Any]) -> int:
            title = (r.get("title") or "").lower()
            if title == name_l:
                return 0
            if any((a or "").lower() == name_l for a in (r.get("aliases") or [])):
                return 1
            if title.startswith(name_l):
                return 2
            return 3

        ranked = sorted(rows, key=lambda r: (_score(r), -(r.get("play_count") or 0)))
        items = [self._to_item(r, labels, ScriptItemCamel) for r in ranked]
        return items, bool(items)

    # ================= 内部：出参组装 =================
    def _to_item(
        self,
        row: Dict[str, Any],
        labels: Dict[str, Dict[str, str]],
        item_cls=ScriptItem,
    ) -> ScriptItem:
        playstyle_labels = labels.get(opt_seed.CATEGORY_PLAYSTYLE, {})
        theme_labels = labels.get(opt_seed.CATEGORY_THEME, {})
        release_labels = labels.get(opt_seed.CATEGORY_RELEASE, {})
        difficulty_labels = labels.get(opt_seed.CATEGORY_DIFFICULTY, {})

        playstyles = list(row.get("playstyles") or [])
        themes = list(row.get("themes") or [])

        return item_cls(
            id=str(row.get("id") or ""),
            code=row.get("code") or "",
            title=row.get("title") or "",
            aliases=list(row.get("aliases") or []),
            summary=row.get("summary"),
            author=row.get("author"),
            publisher=row.get("publisher"),
            release_type=row.get("release_type"),
            difficulty=row.get("difficulty"),
            playstyles=playstyles,
            themes=themes,
            tags=list(row.get("tags") or []),
            release_label=release_labels.get(row.get("release_type") or ""),
            difficulty_label=difficulty_labels.get(row.get("difficulty") or ""),
            playstyle_labels=[
                LabeledCode(code=c, label=playstyle_labels.get(c, c)) for c in playstyles
            ],
            theme_labels=[LabeledCode(code=c, label=theme_labels.get(c, c)) for c in themes],
            player_min=row.get("player_min"),
            player_max=row.get("player_max"),
            male_count=row.get("male_count"),
            female_count=row.get("female_count"),
            flexible_count=int(row.get("flexible_count") or 0),
            allow_gender_swap=row.get("allow_gender_swap"),
            player_text=_format_players(row),
            duration_min=row.get("duration_min"),
            duration_max=row.get("duration_max"),
            duration_text=_format_duration(row.get("duration_min"), row.get("duration_max")),
            rating=float(row["rating"]) if row.get("rating") is not None else None,
            rating_count=int(row.get("rating_count") or 0),
            play_count=int(row.get("play_count") or 0),
            published_year=row.get("published_year"),
            cover_url=row.get("cover_url"),
            is_recommended=bool(row.get("is_recommended")),
            status=row.get("status") or "published",
            source=row.get("source"),
            extra=dict(row.get("extra") or {}),
            has_guide=bool(DMGuideRef.from_extra(row.get("extra"))),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _format_players(row: Dict[str, Any]) -> Optional[str]:
    """拼「6人（3男3女）」「6-7人」这类文案。"""
    pmin, pmax = row.get("player_min"), row.get("player_max")
    if pmin is None or pmax is None:
        return None
    head = f"{pmin}人" if pmin == pmax else f"{pmin}-{pmax}人"

    parts: List[str] = []
    if row.get("male_count"):
        parts.append(f"{row['male_count']}男")
    if row.get("female_count"):
        parts.append(f"{row['female_count']}女")
    if row.get("flexible_count"):
        parts.append(f"{row['flexible_count']}任意")
    return f"{head}（{''.join(parts)}）" if parts else head


def _format_duration(dmin: Optional[int], dmax: Optional[int]) -> Optional[str]:
    """分钟转「4-5小时」「90分钟」，整点小时不带小数。"""
    if dmin is None or dmax is None:
        return None

    def fmt(minutes: int) -> str:
        if minutes < 60:
            return f"{minutes}分钟"
        hours = minutes / 60
        return f"{int(hours)}小时" if hours.is_integer() else f"{hours:.1f}小时"

    if dmin == dmax:
        return fmt(dmin)
    if dmin >= 60 and dmax >= 60:
        lo = dmin / 60
        hi = dmax / 60
        lo_s = f"{int(lo)}" if lo.is_integer() else f"{lo:.1f}"
        hi_s = f"{int(hi)}" if hi.is_integer() else f"{hi:.1f}"
        return f"{lo_s}-{hi_s}小时"
    return f"{fmt(dmin)}-{fmt(dmax)}"


_service: Optional[ScriptService] = None


def get_script_service() -> ScriptService:
    global _service
    if _service is None:
        _service = ScriptService()
    return _service
