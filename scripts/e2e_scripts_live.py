"""剧本库接口 · 真实数据库连通性验证（e2e）。

与 scripts/smoke_test_scripts.py 的区别：
  - smoke_test_scripts.py：内存假仓储，离线跑，验证「业务逻辑」是否正确（30 项）。
  - 本脚本：打真实 Supabase，验证「线上链路」是否通——建表 SQL、RLS、触发器、
    PostgREST 过滤语法、索引是否与代码里的假设一致。

写操作使用服务身份通道（X-API-Key + X-User-Id），走真实鉴权，不做 mock。
脚本自建的测试剧本会在结束时用 service_role 硬删清理，不污染业务数据。

运行：
    python scripts/e2e_scripts_live.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PASSED = 0
FAILED = 0
TEST_CODE = f"e2e-test-{uuid.uuid4().hex[:8]}"
TEST_USER_ID = str(uuid.uuid4())


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name} {extra}")


def svc_headers() -> dict:
    """服务身份鉴权头：走真实 get_current_user 的 service 分支。"""
    return {"X-API-Key": settings.service_api_key, "X-User-Id": TEST_USER_ID}


async def cleanup() -> None:
    """用 service_role 硬删测试数据（软删只是标记，不能留在库里）。"""
    from app.services.supabase import supabase as sb

    await sb.startup()
    try:
        await sb.delete("scripts", filters={"code": f"eq.{TEST_CODE}"})
        print(f"[cleanup] 已硬删测试剧本 {TEST_CODE}")
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanup] 清理失败（需手动删除 {TEST_CODE}）: {exc}")
    finally:
        await sb.shutdown()


def main() -> int:
    if not settings.service_api_key:
        print("未配置 SERVICE_API_KEY，无法测试写接口。请在 .env 中设置后重试。")
        return 2

    with TestClient(app) as client:
        # ---------- 读接口：全部公开，不带任何鉴权头 ----------
        print("\n=== 列表与筛选（匿名访问，验证 RLS 公开读）===")
        r = client.get("/api/v1/scripts", params={"limit": 5})
        ok = r.status_code == 200
        check("GET /scripts 列表 200", ok, r.text[:200])
        if ok:
            body = r.json()
            items = body.get("items", [])
            total = body.get("pagination", {}).get("total")
            check("种子 35 部已可查", total == 35, f"total={total}")
            check("分页 limit=5 生效", len(items) == 5, f"len={len(items)}")
            if items:
                it = items[0]
                check(
                    "出参含展示文案 player_text/duration_text",
                    "player_text" in it and "duration_text" in it,
                    str(list(it.keys()))[:200],
                )
                check(
                    "出参含中文标签 release_label",
                    "release_label" in it,
                    str(list(it.keys()))[:200],
                )
                print(
                    f"       样例: {it.get('title')} | {it.get('player_text')} | "
                    f"{it.get('duration_text')} | {it.get('release_label')} | 评分 {it.get('rating')}"
                )

        # 数组列重叠查询（GIN 索引 + ov. 语法）
        r = client.get("/api/v1/scripts", params={"playstyle": "硬核推理"})
        check("非法玩法名（传中文而非 code）被拦截 422", r.status_code == 422, f"{r.status_code} {r.text[:150]}")

        base = r_total = None
        r = client.get("/api/v1/scripts", params={"playstyle": "hardcore", "limit": 50})
        ok = r.status_code == 200
        check("按玩法 code 筛选 200（ov. 数组重叠）", ok, r.text[:200])
        if ok:
            n = r.json()["pagination"]["total"]
            check("硬核推理筛出子集（非全量 35）", 0 < n < 35, f"total={n}")
            print(f"       玩法=硬核推理: {n} 部")

        # 多选：命中任意一个即算匹配，结果应 >= 单选
        r2 = client.get("/api/v1/scripts", params=[("playstyle", "hardcore"), ("playstyle", "emotional")])
        if r2.status_code == 200 and ok:
            n2 = r2.json()["pagination"]["total"]
            check("玩法多选结果 >= 单选", n2 >= n, f"多选={n2} 单选={n}")
            print(f"       玩法=硬核推理 或 情感: {n2} 部")

        r = client.get("/api/v1/scripts", params={"theme": "nonexistent-theme"})
        ok = r.status_code == 422
        check("未知题材编码被拦截 422 并列出可选值", ok and "allowed" in r.text, r.text[:150])

        r = client.get("/api/v1/scripts", params={"theme": "republic", "limit": 50})
        ok = r.status_code == 200
        check("按题材筛选 200", ok, r.text[:200])
        if ok:
            n = r.json()["pagination"]["total"]
            check("题材筛出子集", n < 35, f"total={n}")
            print(f"       题材=民国: {n} 部 -> {[i['title'] for i in r.json()['items']]}")

        r = client.get("/api/v1/scripts", params={"release": "boxed", "limit": 50})
        if r.status_code == 200:
            n = r.json()["pagination"]["total"]
            check("按发行方式筛选生效", 0 < n < 35, f"total={n}")
            print(f"       发行=盒装: {n} 部")

        # 人数：传具体数值，后端按剧本 [player_min, player_max] 做包含匹配
        r = client.get("/api/v1/scripts", params={"players": 6, "limit": 50})
        ok = r.status_code == 200
        check("按人数=6 筛选 200", ok, r.text[:200])
        if ok:
            items = r.json()["items"]
            good = all(
                (i.get("player_min") is None or i["player_min"] <= 6)
                and (i.get("player_max") is None or i["player_max"] >= 6)
                for i in items
            )
            bad = [
                (i["title"], i.get("player_min"), i.get("player_max")) for i in items
                if not ((i.get("player_min") is None or i["player_min"] <= 6)
                        and (i.get("player_max") is None or i["player_max"] >= 6))
            ]
            check("人数筛选结果区间正确", good, str(bad[:3]))
            print(f"       6 人本: {r.json()['pagination']['total']} 部")

        # 时长：传分钟数，后端按 [duration_min, duration_max] 包含匹配
        r = client.get("/api/v1/scripts", params={"duration": 300, "limit": 50})
        ok = r.status_code == 200
        check("按时长=300 分钟筛选 200", ok, r.text[:200])
        if ok:
            items = r.json()["items"]
            good = all(
                (i.get("duration_min") is None or i["duration_min"] <= 300)
                and (i.get("duration_max") is None or i["duration_max"] >= 300)
                for i in items
            )
            check("时长筛选结果区间正确", good)
            check("时长筛出子集", r.json()["pagination"]["total"] < 35)
            print(f"       时长含 300 分钟: {r.json()['pagination']['total']} 部")

        # 最低评分
        r = client.get("/api/v1/scripts", params={"min_rating": 8.8, "limit": 50})
        if r.status_code == 200:
            items = r.json()["items"]
            check("最低评分筛选生效", all(i["rating"] >= 8.8 for i in items if i.get("rating")))
            print(f"       评分 >= 8.8: {r.json()['pagination']['total']} 部")

        # 组合筛选
        r = client.get(
            "/api/v1/scripts",
            params={"playstyle": "emotional", "players": 6, "sort": "rating", "limit": 5},
        )
        if r.status_code == 200:
            n = r.json()["pagination"]["total"]
            check("组合筛选（情感+6人+评分排序）可用", n >= 0, f"total={n}")
            print(f"       情感本 & 6人: {n} 部 -> {[i['title'] for i in r.json()['items']]}")

        # 关键词模糊搜索（pg_trgm 索引）
        r = client.get("/api/v1/scripts", params={"keyword": "馆"})
        ok = r.status_code == 200
        check("关键词模糊搜索 200（ilike）", ok, r.text[:200])
        if ok:
            titles = [i["title"] for i in r.json()["items"]]
            check("搜到含「馆」的剧本", len(titles) > 0, str(titles))
            print(f"       关键词「馆」: {titles}")

        # 排序
        r = client.get("/api/v1/scripts", params={"sort": "rating", "page_size": 3})
        ok = r.status_code == 200
        check("按评分排序 200", ok, r.text[:200])
        if ok:
            rs = [i.get("rating") for i in r.json()["items"] if i.get("rating") is not None]
            check("评分降序正确", rs == sorted(rs, reverse=True), str(rs))
            print(f"       评分 TOP3: {[(i['title'], i.get('rating')) for i in r.json()['items']]}")

        r = client.get("/api/v1/scripts", params={"sort": "no-such-sort"})
        check("非法排序字段被拦截 422", r.status_code == 422, f"{r.status_code}")

        # ---------- 详情 ----------
        print("\n=== 详情 ===")
        r = client.get("/api/v1/scripts/nian-lun")
        ok = r.status_code == 200
        check("按业务 code 查详情 200", ok, r.text[:200])
        script_uuid = None
        if ok:
            d = r.json()
            script_uuid = d.get("id")
            check("标题正确", d.get("title") == "年轮", str(d.get("title")))
            print(f"       {d.get('title')} | {d.get('player_text')} | {d.get('duration_text')} | {d.get('playstyle_labels')}")

        if script_uuid:
            r = client.get(f"/api/v1/scripts/{script_uuid}")
            check("按 UUID 查详情 200", r.status_code == 200, r.text[:200])

        r = client.get("/api/v1/scripts/no-such-script-xyz")
        check("不存在的剧本返回 404", r.status_code == 404, f"{r.status_code}")

        # ---------- 写接口 ----------
        print("\n=== 新增 / 修改 / 下架（服务身份鉴权）===")
        payload = {
            "code": TEST_CODE,
            "title": f"E2E 连通性测试本 {TEST_CODE[-4:]}",
            "summary": "由 e2e_scripts_live.py 自动创建，运行结束会自动删除。",
            "release_type": "boxed",
            "difficulty": "intermediate",
            "playstyles": ["hardcore", "restore"],
            "themes": ["modern"],
            "player_min": 6,
            "player_max": 6,
            "male_count": 3,
            "female_count": 3,
            "duration_min": 240,
            "duration_max": 300,
            "rating": 8.0,
            "source": "e2e-test",
        }

        r = client.post("/api/v1/scripts", json=payload)
        check("未带鉴权新增被拦截 401", r.status_code == 401, f"{r.status_code} {r.text[:150]}")

        r = client.post("/api/v1/scripts", json=payload, headers=svc_headers())
        ok = r.status_code in (200, 201)
        check("新增剧本成功", ok, f"{r.status_code} {r.text[:300]}")
        new_id = None
        if ok:
            d = r.json()
            new_id = d.get("id")
            check("返回的 code 与入参一致", d.get("code") == TEST_CODE, str(d.get("code")))
            check("人数文案生成正确", d.get("player_text") == "6人（3男3女）", str(d.get("player_text")))
            check("时长文案生成正确", "4" in str(d.get("duration_text")), str(d.get("duration_text")))
            print(f"       新增 id={new_id} | {d.get('player_text')} | {d.get('duration_text')}")

        # 触发器：非法字典编码应被数据库/服务层拦截
        bad = dict(payload, code=f"{TEST_CODE}-bad", playstyles=["no-such-playstyle"])
        r = client.post("/api/v1/scripts", json=bad, headers=svc_headers())
        check("非法玩法编码被拦截 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")

        # code 重复
        r = client.post("/api/v1/scripts", json=payload, headers=svc_headers())
        check("重复 code 被拦截 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")

        if new_id:
            r = client.patch(
                f"/api/v1/scripts/{new_id}",
                json={"rating": 9.2, "difficulty": "expert", "tags": ["e2e", "临时"]},
                headers=svc_headers(),
            )
            ok = r.status_code == 200
            check("局部更新成功", ok, f"{r.status_code} {r.text[:250]}")
            if ok:
                d = r.json()
                check("评分已更新", float(d.get("rating")) == 9.2, str(d.get("rating")))
                check("难度已更新", d.get("difficulty") == "expert", str(d.get("difficulty")))
                check("标题未被误改", d.get("title") == payload["title"], str(d.get("title")))

            r = client.patch(f"/api/v1/scripts/{new_id}", json={"rating": None}, headers=svc_headers())
            ok = r.status_code == 200
            check("传 null 清空字段", ok and r.json().get("rating") is None, f"{r.status_code} {r.text[:200]}")

            r = client.patch(f"/api/v1/scripts/{new_id}", json={"player_min": 8}, headers=svc_headers())
            check("半截人数区间被拦截 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")

            r = client.delete(f"/api/v1/scripts/{new_id}", headers=svc_headers())
            check("软删下架成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

            r = client.get(f"/api/v1/scripts/{TEST_CODE}")
            check("下架后公开查询已不可见 404", r.status_code == 404, f"{r.status_code}")

            r = client.get("/api/v1/scripts", params={"limit": 1})
            if r.status_code == 200:
                total = r.json()["pagination"]["total"]
                check("下架后列表总数回到 35", total == 35, f"total={total}")

    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    code = main()
    asyncio.run(cleanup())
    sys.exit(code)
