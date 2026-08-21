"""剧本库业务层。

职责：
  1. **字典校验** —— 提交的玩法/题材/发行方式/难度必须是字典里真实存在的 code。
     数据库触发器也会拦，但那层报错是 PostgREST 的英文原文，不适合直接给前端；
     在这里拦掉可以给出「未知的玩法编码: xxx，可选值：...」这种能直接照着改的提示。
  2. **code 生成** —— 新增时不传 code 就按标题经 pinyin 派生；仅当标题含数字时
     code 才带数字，不自动追加 2、3 这类序号。
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

from app.core.exceptions import DatabaseError, NotFoundError, ValidationError
from app.data import script_options_seed as opt_seed
from app.schemas.common import Pagination
from app.schemas.script import (
    LabeledCode,
    ScriptAutocompleteItem,
    ScriptCreate,
    ScriptItem,
    ScriptItemCamel,
    ScriptListResult,
    ScriptSearchByNameResult,
    ScriptUpdate,
)
from app.services.repository import ScriptRepository, normalize_title_key
from app.services.script_option_service import ScriptOptionService, get_script_option_service
from app.schemas.dm_guide import DMGuideRef

logger = logging.getLogger("app.script")

MAX_PAGE_SIZE = 100

# 补充更新时绝不改动的身份 / 系统字段
_IDENTITY_FIELDS = ("id", "code", "created_by", "created_at", "deleted_at")
# 补充更新时按「并集」合并的数组字段（避免覆盖库存信息）
_ARRAY_FIELDS = ("aliases", "playstyles", "themes", "tags")


def slugify(title: str) -> str:
    """把剧本名转成 URL 友好的 code 片段。

    中文优先转拼音（pypinyin），英文/数字原样保留，再用连字符规整。
    例：雾都疑影 -> wu-du-yi-ying；山母鬼3 -> shan-mu-gui-3（标题里的数字会保留进 code）。
    若转拼音后为空（极端情况，如纯符号标题），调用方会退回到随机短码。
    """
    # 逐词转拼音：中文变拼音，非中文（字母/数字/标点）原样保留
    tokens = lazy_pinyin(title, errors="default")
    raw = "-".join(t for t in tokens if t)
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug[:48]


def _is_unique_violation(exc: DatabaseError, constraint: str) -> bool:
    """判断数据库异常是否由指定唯一约束冲突引起。

    PostgREST 在撞唯一约束时返回的 body 形如：
    ``{"code":"23505","details":"Key (code)=(xxx) already exists.",
       "message":"duplicate key value violates unique constraint \\"uq_scripts_code\\""}``
    这里按「约束名」精确识别（PostgreSQL 总会把约束名带在 message 里），
    避免把其它表的 23505 误判成本表的 code 冲突。
    """
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        text = f"{details.get('message', '')} {details.get('details', '')}"
    else:
        text = str(details)
    return constraint in text


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
        has_guide: Optional[bool] = None,
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
            has_guide=has_guide,
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

    # ================= 新增 / 导入 =================
    async def create_script(
        self, payload: ScriptCreate, *, user_id: Optional[str] = None
    ) -> Tuple[ScriptItem, bool]:
        """新增剧本，但导入场景下会先做**去重关联**。

        返回 `(剧本项, was_created)`：

        - 剧本库已存在同名（标题精确 / 别名精确）剧本 -> 不新建，把导入信息
          **关联**到已有剧本上（挂 DM 手册、补全缺失字段、置为已上架），
          `was_created=False`；
        - 剧本库没有 -> 正常新建，`was_created=True`。

        这样「导入 DM 指南」流程就不会再产出重复的剧本行：库里本来有一份
        「雾都疑影」（无手册），导入它的手册后直接挂上去，而不是再建一份。
        """
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        await self._assert_codes(
            playstyles=data.get("playstyles"),
            themes=data.get("themes"),
            release_type=data.get("release_type"),
            difficulty=data.get("difficulty"),
        )
        self._check_gender_sum(data)

        # 1) 先看剧本库是否已有同名剧本（导入去重的核心）
        existing = await self.repo.find_existing(payload.title)
        if existing is not None:
            merged = self._merge_for_link(existing, data)
            row = await self.repo.update(existing["id"], merged)
            if row is None:
                # 极端竞态：查到之后被软删了，退化为新建，避免导入静默失败
                return await self._create_or_supplement(payload, data, user_id=user_id)
            logger.info("导入关联到已有剧本 %s (%s)", row.get("title"), row.get("code"))
            return self._to_item(row, await self._label_map()), False

        # 2) 库里没有 -> 新建（若 code 撞唯一约束则自动转为补充更新）
        return await self._create_or_supplement(payload, data, user_id=user_id)

    @staticmethod
    def _merge_upsert(existing: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """把提交的字段「补充」进已有剧本（code 冲突时的更新语义）。

        - 身份 / 系统字段（id / code / created_by / created_at / deleted_at）绝不改动；
        - 标量字段：以本次提交值为准（update 语义，提交即覆盖）；
        - 数组字段（别名 / 玩法 / 题材 / 标签）：与已有值取并集，避免覆盖掉库存信息；
        - extra(jsonb)：按 key 深合并，保留已有键、覆盖传入键。
        """
        merged: Dict[str, Any] = {}
        existing_extra = dict(existing.get("extra") or {})
        incoming_extra = dict(data.get("extra") or {})
        for key, value in data.items():
            if key in _IDENTITY_FIELDS:
                continue
            if key == "extra":
                merged["extra"] = {**existing_extra, **incoming_extra}
                continue
            if key in _ARRAY_FIELDS and isinstance(value, list):
                base = list(existing.get(key) or [])
                for item in value:
                    if item not in base:
                        base.append(item)
                merged[key] = base
                continue
            merged[key] = value
        return merged

    async def _create_or_supplement(
        self, payload: ScriptCreate, data: Dict[str, Any], *, user_id: Optional[str] = None
    ) -> Tuple[ScriptItem, bool]:
        """新增剧本；若 code 与 `uq_scripts_code` 冲突，则转为「补充更新」而非报错。

        这是 POST /api/v1/scripts 幂等化的关键：重复提交同一 code 时不再 500，
        而是把本次提交合并进已有行（身份字段不动），返回 `was_created=False`，
        语义与「导入去重关联」保持一致。
        """
        # 不再做 code 唯一性校验：code 直接由标题经 pinyin 派生（仅标题含数字才带数字），
        # 生成后即使用。若与已有 code 撞车，不报错、也不造「假续集」编码，
        # 而是把本次提交「补充」到已有行上（见下方冲突处理）。
        code = data.get("code") or await self._generate_code(payload.title)
        data["code"] = code

        # 把归一化标题键写入别名，使后续「山母鬼.pdf」之类的重传能反向匹配到本行
        key = normalize_title_key(payload.title)
        if key:
            aliases = list(data.get("aliases") or [])
            if key not in aliases:
                aliases.append(key)
            if aliases:
                data["aliases"] = aliases

        data.setdefault("status", "published")
        if user_id:
            data["created_by"] = user_id

        # 1) 主动查重：code 已存在则说明是重复提交，直接转为「补充更新」
        existing = await self.repo.get_by_code(code)
        if existing is not None:
            merged = self._merge_upsert(existing, data)
            row = await self.repo.update(existing["id"], merged)
            # 竞态：查到后该行被软删，退化为按 code 复活更新
            if row is None:
                row = await self.repo.update_by_code(code, merged, include_deleted=True)
            logger.info("code 已存在，补充更新剧本 %s (%s)", row.get("title"), row.get("code"))
            return self._to_item(row, await self._label_map()), False

        # 2) 库里没有该 code -> 新建；若并发写入仍撞唯一约束，兜底转补充更新
        try:
            row = await self.repo.create(data)
        except DatabaseError as exc:
            if not _is_unique_violation(exc, "uq_scripts_code"):
                raise
            logger.info("code 并发冲突，转为补充更新: %s", code)
            existing = await self.repo.get_by_code(code, include_deleted=True)
            if existing is None:
                raise
            merged = self._merge_upsert(existing, data)
            row = await self.repo.update_by_code(code, merged, include_deleted=True)
            if row is None:
                raise
            return self._to_item(row, await self._label_map()), False

        logger.info("新增剧本 %s (%s)", row.get("title"), row.get("code"))
        return self._to_item(row, await self._label_map()), True

    @staticmethod
    def _merge_for_link(existing: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """把导入信息合并进已有剧本，规则：

        - DM 手册（extra.dmGuide）**总是挂上**——这是导入的核心目标；
        - 其余字段只在「库里该字段为空」时补全，绝不覆盖剧本库已有数据，
          避免导入方随手填的字段把运营维护的权威信息冲掉；
        - 标题 / code / created_by 等身份字段不动；
        - 关联即视为「可对外可见」：若原状态是 draft / offline 则提升为 published。
        """
        # 不随导入覆盖的身份 / 系统字段
        skip = {"title", "code", "created_by", "id", "deleted_at"}

        merged: Dict[str, Any] = {}

        # 把本次导入的归一化标题键并入已有剧本的别名：同一本剧本用过的各种标题
        # 写法（山母鬼 / 山母鬼.pdf / 山母鬼 副本）都能在后续重传时被匹配到
        key = normalize_title_key(data.get("title") or "")
        if key:
            existing_aliases = list(existing.get("aliases") or [])
            if key not in existing_aliases:
                existing_aliases.append(key)
                merged["aliases"] = existing_aliases

        # 1) 挂 DM 手册
        existing_extra = dict(existing.get("extra") or {})
        incoming_extra = dict(data.get("extra") or {})
        dm = incoming_extra.get("dmGuide") or incoming_extra.get("dm_guide")
        if dm is not None:
            existing_extra["dmGuide"] = dm
            merged["extra"] = existing_extra

        # 2) 补全缺失字段
        for key, value in data.items():
            if key in skip or key == "extra":
                continue
            if value is None:
                continue
            current = existing.get(key)
            if current in (None, "", [], {}):
                merged[key] = value

        # 3) 导入即视为可用：草稿 / 下架提升为已上架
        if existing.get("status") in (None, "draft", "offline"):
            merged["status"] = "published"

        return merged

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
        """按标题经 pinyin 派生剧本 code。

        规则：
        - 通过 pypinyin 把标题转拼音（英文/数字原样保留），再用连字符规整；
        - **仅当标题本身含数字时** code 才带数字（数字直接来自标题原文，例如
          「山母鬼3」→ ``shan-mu-gui-3``），不做任何自增序号；
        - **不再自动追加 2、3 这类序号**来规避重名，也不再对 code 做唯一性校验；
          code 直接由标题派生后使用。若数据库唯一约束兜底拦下重复 code，会按数据库
          错误返回，而不会悄悄加 ``-2`` 凭空造出一个「假续集」编码。
        """
        code = slugify(title)
        if not code:
            # 极端情况（纯符号标题）退化为随机短码，同样不含自增式数字
            code = f"script-{uuid.uuid4().hex[:8]}"
        return code

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

    # ================= 自动补全（联想） =================
    async def autocomplete(self, q: str, *, limit: int = 8) -> List[ScriptAutocompleteItem]:
        """边输入边查的轻量剧本联想。

        只召回已上架剧本的精简字段（id / code / title / author / cover_url / has_guide），
        不拉字典标签、不拼展示文案，保证下拉框实时响应。找不到返回空列表。
        """
        q = (q or "").strip()
        if not q:
            return []
        rows = await self.repo.autocomplete(q, limit=max(1, min(limit, 20)))
        return [self._to_autocomplete_item(r) for r in rows]

    @staticmethod
    def _to_autocomplete_item(row: Dict[str, Any]) -> ScriptAutocompleteItem:
        return ScriptAutocompleteItem(
            id=str(row.get("id") or ""),
            code=row.get("code") or "",
            title=row.get("title") or "",
            author=row.get("author"),
            cover_url=row.get("cover_url"),
            has_guide=bool(DMGuideRef.from_extra(row.get("extra"))),
        )

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
