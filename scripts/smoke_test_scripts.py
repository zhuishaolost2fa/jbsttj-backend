"""剧本库接口冒烟测试：不依赖真实 Supabase / OSS，验证列表、详情、新增、修改、下架。

只把两个 IO 边界（数据库仓储、字典仓储）换成内存假实现，
ScriptService 的真实业务编排（字典校验、slug 生成、区间校验、展示文案）保持原样参与测试。

运行：
    python scripts/smoke_test_scripts.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前注入配置
os.environ.update(
    {
        "APP_ENV": "test",
        "DEBUG": "false",
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key",
        "SUPABASE_SERVICE_ROLE_KEY": "service-key",
        "SUPABASE_JWT_SECRET": "test-secret-at-least-32-characters-long!!",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "OSS_ACCESS_KEY_ID": "test-ak",
        "OSS_ACCESS_KEY_SECRET": "test-sk",
        "OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
        "OSS_REGION": "cn-hangzhou",
        "OSS_BUCKET": "test-bucket",
        "UPLOAD_CHUNK_SIZE": str(1024 * 1024),
    }
)

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.data import script_options_seed as opt_seed  # noqa: E402
from app.data import scripts_seed  # noqa: E402
from app.schemas.script_option import ScriptOptionItem  # noqa: E402
from app.services.script_option_service import ScriptOptionService  # noqa: E402
from app.services.script_service import ScriptService, get_script_service  # noqa: E402

SETTINGS = get_settings()
USER_ID = str(uuid.uuid4())

PASS, FAIL = 0, 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ======================================================================
# 内存假实现
# ======================================================================
class FakeOptionRepo:
    """字典仓储假实现：直接把内存种子数据吐出来，避免触碰 Supabase。"""

    def __init__(self) -> None:
        self._cats = [dict(c) for c in opt_seed.iter_category_rows()]
        self._opts = [dict(o) for o in opt_seed.iter_option_rows()]

    async def list_categories(self, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        rows = self._cats
        if not include_inactive:
            rows = [c for c in rows if c.get("is_active", True)]
        return [dict(c) for c in rows]

    async def list_options(
        self,
        *,
        category_code: Optional[str] = None,
        only_hot: bool = False,
        keyword: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        rows = self._opts
        if not include_inactive:
            rows = [o for o in rows if o.get("is_active", True)]
        if category_code:
            rows = [o for o in rows if o.get("category_code") == category_code]
        if only_hot:
            rows = [o for o in rows if o.get("is_hot")]
        return [dict(o) for o in rows]


class FakeScriptRepo:
    """剧本仓储假实现：在内存里忠实复刻 PostgREST 的过滤 / 排序 / 分页语义。

    这样冒烟测试能真正验证 ScriptService + 接口层的过滤逻辑，而不只是走个过场。
    """

    SORTS = {
        "hot": lambda r: (-(r.get("play_count") or 0), -(r.get("rating") or 0)),
        "rating": lambda r: (-(r.get("rating") or 0), -(r.get("play_count") or 0)),
        "newest": lambda r: r.get("created_at") or "",
        "year": lambda r: (-(r.get("published_year") or 0), -(r.get("play_count") or 0)),
        "title": lambda r: (r.get("title") or "").lower(),
    }

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        for r in rows or []:
            row = dict(r)
            row.setdefault("id", str(uuid.uuid4()))
            self.rows[row["id"]] = row

    async def get(self, script_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        row = self.rows.get(script_id)
        if row is None:
            return None
        if not include_deleted and row.get("deleted_at"):
            return None
        return dict(row)

    async def get_by_code(self, code: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        for row in self.rows.values():
            if row.get("code") == code:
                if not include_deleted and row.get("deleted_at"):
                    return None
                return dict(row)
        return None

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
        sort: str = "hot",
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        rows = [dict(r) for r in self.rows.values() if not r.get("deleted_at")]
        if status:
            rows = [r for r in rows if r.get("status") == status]

        if playstyles:
            wanted = set(playstyles)
            rows = [r for r in rows if wanted & set(r.get("playstyles") or [])]
        if themes:
            wanted = set(themes)
            rows = [r for r in rows if wanted & set(r.get("themes") or [])]
        if release_types:
            rows = [r for r in rows if r.get("release_type") in release_types]
        if difficulties:
            rows = [r for r in rows if r.get("difficulty") in difficulties]
        if players is not None:
            rows = [
                r
                for r in rows
                if (r.get("player_min") or 0) <= players <= (r.get("player_max") or 0)
            ]
        if duration is not None:
            rows = [
                r
                for r in rows
                if (r.get("duration_min") or 0) <= duration <= (r.get("duration_max") or 0)
            ]
        if min_rating is not None:
            rows = [r for r in rows if (r.get("rating") or 0) >= min_rating]
        if recommended_only:
            rows = [r for r in rows if r.get("is_recommended")]

        if keyword:
            kw = keyword.lower()
            kept = []
            for r in rows:
                if any(
                    kw in (str(r.get(f) or "")).lower()
                    for f in ("title", "summary", "author", "publisher")
                ):
                    kept.append(r)
                    continue
                if any(kw in (str(a or "")).lower() for a in (r.get("aliases") or [])):
                    kept.append(r)
            rows = kept

        # hot/rating/year 的 key 已取负，reverse=False 即得到降序；
        # newest 用时间戳字符串，需要 reverse=True；title 升序 reverse=False。
        reverse = sort == "newest"
        key = self.SORTS.get(sort, self.SORTS["hot"])
        rows.sort(key=key, reverse=reverse)

        total = len(rows)
        page = rows[offset : offset + limit]
        return page, total

    async def search_by_name(
        self, name: str, *, status: Optional[str] = "published", limit: int = 10
    ) -> List[Dict[str, Any]]:
        """与真实仓储一致：仅按 title / aliases 召回候选，service 层再做质量排序。"""
        if not name:
            return []
        kw = name.lower()
        rows = [dict(r) for r in self.rows.values() if not r.get("deleted_at")]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        kept = [
            r
            for r in rows
            if kw in (r.get("title") or "").lower()
            or any(kw in (str(a or "")).lower() for a in (r.get("aliases") or []))
        ]
        kept.sort(key=lambda r: -(r.get("play_count") or 0))
        return kept[: max(1, min(limit, 50))]

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(payload)
        if not row.get("id"):
            row["id"] = str(uuid.uuid4())
        row["created_at"] = _now()
        row["updated_at"] = _now()
        self.rows[row["id"]] = row
        return dict(row)

    async def update(self, script_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        row = self.rows.get(script_id)
        if row is None or row.get("deleted_at"):
            return None
        row.update(data)
        row["updated_at"] = _now()
        return dict(row)

    async def soft_delete(self, script_id: str) -> Optional[Dict[str, Any]]:
        row = self.rows.get(script_id)
        if row is None or row.get("deleted_at"):
            return None
        row["deleted_at"] = _now()
        row["status"] = "offline"
        row["updated_at"] = _now()
        return dict(row)


# ======================================================================
# 装配
# ======================================================================
seed_rows = scripts_seed.iter_script_rows()
option_service = ScriptOptionService(repo=FakeOptionRepo())
script_repo = FakeScriptRepo(rows=seed_rows)
script_service = ScriptService(repo=script_repo, option_service=option_service)

app.dependency_overrides[get_script_service] = lambda: script_service


def make_token(**overrides) -> str:
    payload = {
        "sub": USER_ID,
        "aud": "authenticated",
        "role": "authenticated",
        "email": "tester@example.com",
        "exp": int(time.time()) + 3600,
        **overrides,
    }
    return jwt.encode(payload, SETTINGS.supabase_jwt_secret, algorithm="HS256")


AUTH = {"Authorization": f"Bearer {make_token()}"}
SEED_TOTAL = len(seed_rows)


# ======================================================================
# 用例
# ======================================================================
def main() -> int:
    with TestClient(app) as client:
        print("\n[A] 列表与筛选")
        r = client.get("/api/v1/scripts")
        check("列表默认返回全部种子", r.status_code == 200 and r.json()["pagination"]["total"] == SEED_TOTAL,
              f"total={r.json().get('pagination', {}).get('total')} expect={SEED_TOTAL}")
        body = r.json()
        check("列表项带出参展示字段(player_text)", all("player_text" in it for it in body["items"]))

        # 玩法多选：emotional 命中任意一个即匹配
        r = client.get("/api/v1/scripts", params={"playstyle": "hardcore"})
        ok = r.status_code == 200
        if ok:
            for it in r.json()["items"]:
                ok = ok and ("hardcore" in it["playstyles"])
        check("按玩法筛选(hardcore)", ok, r.text[:160])
        hardcore_count = r.json()["pagination"]["total"]
        check("玩法筛选确实缩小了结果集", 0 < hardcore_count < SEED_TOTAL, f"count={hardcore_count}")

        # 题材
        r = client.get("/api/v1/scripts", params={"theme": "modern"})
        ok = r.status_code == 200 and all("modern" in it["themes"] for it in r.json()["items"])
        check("按题材筛选(modern)", ok, r.text[:160])

        # 人数区间匹配：6 人命中「6-7人」之类
        r = client.get("/api/v1/scripts", params={"players": 6})
        ok = r.status_code == 200
        if ok:
            for it in r.json()["items"]:
                pmin, pmax = it["player_min"], it["player_max"]
                ok = ok and (pmin is not None and pmin <= 6 <= pmax)
        check("按人数匹配(players=6)", ok, r.text[:160])

        # 时长匹配（分钟）
        r = client.get("/api/v1/scripts", params={"duration": 240})
        ok = r.status_code == 200
        if ok:
            for it in r.json()["items"]:
                dmin, dmax = it["duration_min"], it["duration_max"]
                ok = ok and (dmin is not None and dmin <= 240 <= dmax)
        check("按时长匹配(duration=240)", ok, r.text[:160])

        # 关键词：命中标题
        r = client.get("/api/v1/scripts", params={"keyword": "年轮"})
        items = r.json()["items"] if r.status_code == 200 else []
        ok = r.status_code == 200 and any(it["title"] == "年轮" for it in items)
        check("关键词搜索命中标题(年轮)", ok, r.text[:160])

        # 排序：rating 降序
        r = client.get("/api/v1/scripts", params={"sort": "rating"})
        ratings = [it["rating"] for it in r.json()["items"] if it["rating"] is not None]
        check("评分排序(rating desc)", ratings == sorted(ratings, reverse=True), f"{ratings[:5]}")

        # 分页
        r = client.get("/api/v1/scripts", params={"limit": 10, "offset": 0})
        body = r.json()
        check("分页 limit=10", r.status_code == 200 and len(body["items"]) == 10, f"got {len(body['items'])}")
        check("分页 has_more 正确", body["pagination"]["has_more"] is True)

        print("\n[A2] 按名称查找（小驼峰返回）")
        sample = next(iter(scripts_seed.iter_script_rows()))
        sample_title = sample["title"]
        # 精确名称
        r = client.get("/api/v1/scripts/byname", params={"name": sample_title})
        check("按名称精确查找返回 200", r.status_code == 200, r.text[:160])
        if r.status_code == 200:
            body = r.json()
            check("found=true 且 items 非空", body["found"] and len(body["items"]) >= 1)
            check("回显 query 与入参一致", body["query"] == sample_title)
            check("count 与 items 长度一致", body["count"] == len(body["items"]))
            first = body["items"][0]
            check("返回字段为小驼峰: releaseType", "releaseType" in first)
            check("返回字段为小驼峰: playerMin", "playerMin" in first)
            check("返回字段为小驼峰: createdAt", "createdAt" in first)
            check("小驼峰结果不含下划线 snake 字段", "release_type" not in first)

        # 模糊名称（取标题前一半做子串）
        partial = sample_title[: max(1, len(sample_title) // 2)]
        r2 = client.get("/api/v1/scripts/byname", params={"name": partial})
        check("模糊名称查找返回 200", r2.status_code == 200, r2.text[:160])
        check("模糊匹配也能命中样本剧本", r2.status_code == 200 and r2.json()["found"] is True)

        # 找不到：返回 200 + found=false + 空数组，而非 404
        r3 = client.get("/api/v1/scripts/byname", params={"name": "绝对不存在的剧本名zzz"})
        check("找不到时返回 200 而非 404", r3.status_code == 200, str(r3.status_code))
        if r3.status_code == 200:
            b3 = r3.json()
            check("找不到时 found=false 且 items=[]", b3["found"] is False and b3["items"] == [])

        print("\n[B] 详情")
        # 从列表拿一个 code 做详情校验
        detail_code = "nian-lun"
        r = client.get(f"/api/v1/scripts/{detail_code}")
        ok = r.status_code == 200 and r.json().get("code") == detail_code
        check("按 code 取详情(年轮)", ok, r.text[:160])
        # 不存在的 code -> 404
        r = client.get("/api/v1/scripts/does-not-exist")
        check("不存在的剧本返回 404", r.status_code == 404 and r.json()["error"]["code"] == "script_not_found",
              r.text[:160])

        print("\n[C] 鉴权")
        r = client.post("/api/v1/scripts", json={"title": "越权新增"})
        check("新增接口未带 token 被拒(401)", r.status_code == 401)

        print("\n[D] 新增")
        # 非法字典编码 -> 422，且 details.allowed 给出可选值
        r = client.post(
            "/api/v1/scripts",
            headers=AUTH,
            json={"title": "测试非法编码", "playstyles": ["not_a_real_code"]},
        )
        ok = r.status_code == 422 and "allowed" in r.json().get("error", {}).get("details", {})
        check("非法玩法编码被拒并提示可选值", ok, r.text[:200])

        # 合法新增，code 自动生成
        r = client.post(
            "/api/v1/scripts",
            headers=AUTH,
            json={
                "title": "测试用新增剧本",
                "summary": "冒烟测试创建",
                "release_type": "boxed",
                "difficulty": "intermediate",
                "playstyles": ["hardcore"],
                "themes": ["modern"],
                "player_min": 4,
                "player_max": 6,
                "male_count": 2,
                "female_count": 2,
                "duration_min": 180,
                "duration_max": 240,
            },
        )
        ok = r.status_code == 201
        created = r.json() if ok else {}
        check("合法新增返回 201", ok, r.text[:200])
        if ok:
            check("code 已自动生成", bool(created.get("code")), str(created.get("code")))
            check("人数展示文案正确", created.get("player_text") == "4-6人（2男2女）",
                  str(created.get("player_text")))
            check("时长展示文案正确", created.get("duration_text") == "3-4小时", str(created.get("duration_text")))
            check("出参标签已翻译", created.get("release_label") == "盒装", str(created.get("release_label")))

        # 性别配置之和超过最大人数 -> 422
        r = client.post(
            "/api/v1/scripts",
            headers=AUTH,
            json={"title": "性别超限", "player_min": 4, "player_max": 4, "male_count": 5, "female_count": 0},
        )
        check("性别配置超员被拒(422)", r.status_code == 422, r.text[:160])

        # 人数区间只给一侧 -> 422
        r = client.post(
            "/api/v1/scripts",
            headers=AUTH,
            json={"title": "半截区间", "player_min": 4},
        )
        check("新增人数区间必须成对(422)", r.status_code == 422, r.text[:160])

        print("\n[E] 修改")
        script_id = created.get("id")
        if script_id:
            # 局部更新 summary
            r = client.patch(f"/api/v1/scripts/{script_id}", headers=AUTH,
                             json={"summary": "局部更新后的简介"})
            ok = r.status_code == 200 and r.json().get("summary") == "局部更新后的简介"
            check("PATCH 局部更新字段", ok, r.text[:200])
            if ok:
                # 未被修改的字段应保持
                check("PATCH 未传字段保持原值", r.json().get("title") == "测试用新增剧本",
                      str(r.json().get("title")))

            # 半截区间被拒
            r = client.patch(f"/api/v1/scripts/{script_id}", headers=AUTH, json={"player_min": 5})
            check("PATCH 半截区间被拒(422)", r.status_code == 422, r.text[:160])

            # 显式传 null 清空字段
            r = client.patch(f"/api/v1/scripts/{script_id}", headers=AUTH, json={"author": None})
            ok = r.status_code == 200 and r.json().get("author") is None
            check("PATCH 传 null 清空字段", ok, r.text[:160])

            # PUT 等价于 PATCH
            r = client.put(f"/api/v1/scripts/{script_id}", headers=AUTH, json={"summary": "PUT 更新"})
            check("PUT 等价于 PATCH", r.status_code == 200 and r.json().get("summary") == "PUT 更新", r.text[:160])

            print("\n[F] 下架（软删除）")
            r = client.delete(f"/api/v1/scripts/{script_id}", headers=AUTH)
            ok = r.status_code == 200 and r.json().get("message") == "剧本已下架"
            check("下架返回 200 与确认文案", ok, r.text[:160])
            r = client.get(f"/api/v1/scripts/{script_id}")
            check("下架后详情 404", r.status_code == 404, r.text[:160])
            # 列表总数应减少 1
            r = client.get("/api/v1/scripts")
            check("下架后列表总数减 1", r.status_code == 200
                  and r.json()["pagination"]["total"] == SEED_TOTAL, f"total={r.json().get('pagination', {}).get('total')}")
        else:
            check("PATCH/PUT/DELETE 前置新增成功", False, "未拿到 script_id")

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
