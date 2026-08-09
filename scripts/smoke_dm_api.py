"""DM 主持人手册 4 个接口的离线冒烟测试（打桩，无需 Supabase / MQ / LLM）。

用内存假实现替换 DMStore / LLM / MQ 派发三个 IO 边界，**真实 HTTP 路由与业务编排
参与测试**，不消耗任何云服务或配额。覆盖 7 个场景：

  1. 首次触发 → 202 + 已派发到队列
  2. 重复触发 → 复用同一在跑任务
  3. force=true → 取消旧任务并重新派发
  4. GET 状态（hasGuide / indexed / 进度）
  5. GET 任务进度（含不存在任务返回 404）
  6. 混合检索（面包屑解析 / 相似度保留 4 位 / 页码透出 / 非法 mode / 空查询）
  7. 剧本没挂 dmGuide → 422 + dm_guide_missing

运行：python scripts/smoke_dm_api.py
"""
from __future__ import annotations

import sys
import types
import uuid
import warnings
from pathlib import Path

# 让脚本能直接 `python scripts/smoke_dm_api.py` 运行（脚本目录默认不在 import 路径上）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

import app.services.dm_store as store_mod
import app.services.dm_service as dm_service_mod
from app.core.config import get_settings

SCRIPT_ID = str(uuid.uuid4())
DOC_ID = str(uuid.uuid4())
JOBS: dict = {}
DISPATCHED: list = []


class StubStore:
    def find_active_job(self, script_id):
        for j in JOBS.values():
            if j["script_id"] == script_id and j["status"] not in store_mod.TERMINAL_STATES:
                return j
        return None

    def create_job(self, payload):
        JOBS[payload["id"]] = {**payload, "total_pages": 0, "processed_pages": 0}
        return JOBS[payload["id"]]

    def get_job(self, job_id):
        return JOBS.get(job_id)

    def update_job(self, job_id, patch):
        JOBS.setdefault(job_id, {}).update(patch)

    def fail_job(self, job_id, msg):
        self.update_job(job_id, {"status": "failed", "error_message": msg})

    def latest_job(self, script_id):
        rows = [j for j in JOBS.values() if j["script_id"] == script_id]
        return rows[-1] if rows else None

    def get_active_document(self, script_id):
        return {
            "id": DOC_ID, "script_id": script_id, "object_key": "scripts/x/dm.pdf",
            "file_name": "主持人手册.pdf", "total_pages": 386, "total_chunks": 742,
            "total_qa": 1980, "version": 2,
        }

    def match_chunks(self, embedding, **kw):
        assert len(embedding) == 1024, "查询向量维度不对"
        return [{
            "id": str(uuid.uuid4()), "document_id": DOC_ID,
            "content": "搜证阶段每位玩家限搜查两次，主持人须严格记录搜查顺序。",
            "section_path": ["第二章 搜证阶段", "2.1 流程要点"],
            "page_start": 47, "page_end": 48, "similarity": 0.8123456,
        }]

    def match_qa(self, embedding, **kw):
        return [{
            "id": str(uuid.uuid4()), "document_id": DOC_ID,
            "question": "玩家搜证次数用完了还想搜怎么办？",
            "answer": "礼貌拒绝并提示可以通过交换情报获取线索。",
            "category": "流程控制", "chunk_id": str(uuid.uuid4()), "similarity": 0.9012345,
        }]


class StubLLM:
    async def aembed_query(self, text, **kw):
        return [0.01] * 1024


def main() -> int:
    # ---- 打桩配置：让 dm_rag_enabled 为 True ----
    s = get_settings()
    s.siliconflow_api_key = "sk-test"
    s.supabase_url = "https://stub.supabase.co"
    s.supabase_service_role_key = "stub-key"
    s.celery_broker_url = "amqp://stub"
    s.celery_result_backend = "redis://stub"

    _stub = StubStore()
    store_mod.get_dm_store = lambda: _stub
    dm_service_mod.store_mod = store_mod

    # 打桩 LLM
    import app.services.llm as llm_mod
    llm_mod.get_llm_client = lambda: StubLLM()

    # 打桩 dispatch_pipeline（不真的投递 MQ）
    fake_tasks = types.ModuleType("app.tasks.dm_ingest")
    fake_tasks.dispatch_pipeline = lambda **kw: DISPATCHED.append(kw)
    sys.modules["app.tasks.dm_ingest"] = fake_tasks

    # ---- 打桩剧本服务与鉴权 ----
    from app.main import app
    from app.core.security import CurrentUser, get_current_user
    from app.services.script_service import get_script_service

    class StubScript:
        id = SCRIPT_ID
        title = "如是我观"
        extra = {"dmGuide": {"objectKey": "scripts/x/dm.pdf", "fileName": "主持人手册.pdf",
                             "fileSize": 12 * 1024 * 1024, "fileId": "f-1"}}

    class StubScriptService:
        async def get_script(self, id_or_code):
            return StubScript()

    app.dependency_overrides[get_script_service] = lambda: StubScriptService()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(uuid.uuid4()), email="dm@test.com"
    )

    client = TestClient(app)
    BASE = f"/api/v1/scripts/{SCRIPT_ID}/dm-guide"
    fails = []

    def check(name, cond, extra=""):
        print(f"   {'OK  ' if cond else 'FAIL'} {name}" + (f"  {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    print("\n[1] POST /dm-guide/ingest 首次触发")
    r = client.post(f"{BASE}/ingest", json={"force": False})
    print(f"     {r.status_code} {r.json()}")
    check("返回 202", r.status_code == 202)
    check("未复用", r.json().get("reused") is False)
    check("已派发到队列", len(DISPATCHED) == 1)
    check("派发参数带 objectKey", DISPATCHED and DISPATCHED[0]["object_key"] == "scripts/x/dm.pdf")
    job_id = r.json()["jobId"]
    check("响应字段是小驼峰", "jobId" in r.json())

    print("\n[2] 重复触发应复用在跑的任务")
    r2 = client.post(f"{BASE}/ingest", json={"force": False})
    check("reused=true", r2.json().get("reused") is True)
    check("未重复派发", len(DISPATCHED) == 1, f"dispatched={len(DISPATCHED)}")
    check("返回同一个 jobId", r2.json()["jobId"] == job_id)

    print("\n[3] force=true 取消旧任务并重新派发")
    r3 = client.post(f"{BASE}/ingest", json={"force": True})
    check("重新派发", len(DISPATCHED) == 2)
    check("旧任务被置为 cancelled", JOBS[job_id]["status"] == "cancelled",
          f"实际={JOBS[job_id]['status']}")
    check("force 透传", DISPATCHED[1]["force"] is True)

    print("\n[4] GET /dm-guide 状态")
    r4 = client.get(BASE)
    d = r4.json()
    print(f"     {r4.status_code} indexed={d.get('indexed')} chunks={d.get('totalChunks')} "
          f"qa={d.get('totalQa')} job={d.get('job', {}).get('status')}")
    check("200", r4.status_code == 200)
    check("hasGuide=true", d.get("hasGuide") is True)
    check("indexed=true", d.get("indexed") is True)
    check("带最近任务进度", d.get("job") is not None)

    print("\n[5] GET /dm-guide/jobs/{job_id}")
    r5 = client.get(f"{BASE}/jobs/{job_id}")
    check("200", r5.status_code == 200)
    check("状态为 cancelled", r5.json().get("status") == "cancelled")
    r5b = client.get(f"{BASE}/jobs/{uuid.uuid4()}")
    check("不存在的任务返回 404", r5b.status_code == 404, f"实际={r5b.status_code}")

    print("\n[6] GET /dm-guide/search")
    r6 = client.get(BASE + "/search", params={"q": "搜证次数用完了怎么办", "mode": "hybrid"})
    d6 = r6.json()
    print(f"     {r6.status_code} chunks={len(d6.get('chunks', []))} qa={len(d6.get('qa', []))} "
          f"took={d6.get('tookMs')}ms")
    check("200", r6.status_code == 200)
    check("hybrid 两类都有", len(d6.get("chunks", [])) == 1 and len(d6.get("qa", [])) == 1)
    check("面包屑已解析", d6["chunks"][0]["sectionPath"] == ["第二章 搜证阶段", "2.1 流程要点"])
    check("相似度已保留 4 位", d6["qa"][0]["similarity"] == 0.9012)
    check("页码透出", d6["chunks"][0]["pageStart"] == 47)

    r6b = client.get(BASE + "/search", params={"q": "x", "mode": "bogus"})
    check("非法 mode 返回 422", r6b.status_code == 422, f"实际={r6b.status_code}")
    r6c = client.get(BASE + "/search", params={"q": "   "})
    check("空查询被拒", r6c.status_code == 422, f"实际={r6c.status_code}")

    print("\n[7] 剧本没挂 dmGuide 时应 422")

    class NoGuideScript(StubScript):
        extra = {}

    class NoGuideService:
        async def get_script(self, id_or_code):
            return NoGuideScript()

    app.dependency_overrides[get_script_service] = lambda: NoGuideService()
    JOBS.clear()
    r7 = client.post(f"{BASE}/ingest", json={})
    check("422", r7.status_code == 422, f"实际={r7.status_code}")
    check("错误码为 dm_guide_missing",
          r7.json().get("error", {}).get("code") == "dm_guide_missing", str(r7.json())[:110])

    print("\n" + "=" * 60)
    print("全部通过" if not fails else f"{len(fails)} 项失败: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
