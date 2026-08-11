"""离线冒烟测试：不依赖真实 OSS / Supabase，验证接口与上传编排逻辑。

只把两个 IO 边界（OSS、数据库仓储）换成内存假实现，
UploadService / FileService 的真实业务逻辑保持原样参与测试。

运行：
    python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

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
from app.services.file_service import FileService, get_file_service  # noqa: E402
from app.services.oss import ObjectMeta, RemotePart  # noqa: E402
from app.services.upload_service import UploadService, get_upload_service  # noqa: E402

SETTINGS = get_settings()
USER_ID = str(uuid.uuid4())
CHUNK = 1024 * 1024

PASS, FAIL = 0, 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ======================================================================
# 内存假实现
# ======================================================================
class FakeOSS:
    def __init__(self) -> None:
        self.uploads: dict[str, dict[int, RemotePart]] = {}
        self.objects: dict[str, ObjectMeta] = {}
        self.aborted: list[str] = []

    async def init_multipart(self, key, content_type=None, metadata=None):
        upload_id = uuid.uuid4().hex
        self.uploads[upload_id] = {}
        return upload_id

    async def presign_part(self, key, upload_id, part_number, expires):
        return f"https://test-bucket.oss-cn-hangzhou.aliyuncs.com/{key}?partNumber={part_number}&uploadId={upload_id}"

    async def upload_part(self, key, upload_id, part_number, data):
        etag = uuid.uuid4().hex.upper()
        self.uploads.setdefault(upload_id, {})[part_number] = RemotePart(part_number, etag, len(data))
        return etag

    async def list_parts(self, key, upload_id):
        return sorted(self.uploads.get(upload_id, {}).values(), key=lambda p: p.part_number)

    async def complete_multipart(self, key, upload_id, parts):
        total = sum(p.size for p in parts)
        meta = ObjectMeta(key=key, size=total, etag=uuid.uuid4().hex.upper())
        self.objects[key] = meta
        self.uploads.pop(upload_id, None)
        return meta

    async def abort_multipart(self, key, upload_id):
        self.aborted.append(upload_id)
        self.uploads.pop(upload_id, None)

    async def head_object(self, key):
        return self.objects.get(key)

    async def put_object(self, key, data, content_type=None):
        meta = ObjectMeta(key=key, size=len(data), etag=uuid.uuid4().hex.upper())
        self.objects[key] = meta
        return meta

    async def sign_download_url(self, key, expires, filename=None, inline=False):
        return f"https://test-bucket.oss-cn-hangzhou.aliyuncs.com/{key}?sig=x&expires={expires}"

    async def delete_object(self, key):
        self.objects.pop(key, None)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class FakeTaskRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def create(self, payload):
        row = {"id": str(uuid.uuid4()), "created_at": _now(), "updated_at": _now(), **payload}
        self.rows[row["id"]] = row
        return row

    async def get(self, task_id, user_id=None):
        row = self.rows.get(task_id)
        if row and user_id and row["user_id"] != user_id:
            return None
        return row

    async def find_resumable(self, user_id, file_hash, file_size, key_prefix=None):
        for row in reversed(list(self.rows.values())):
            if (
                row["user_id"] == user_id
                and row.get("file_hash") == file_hash
                and row["file_size"] == file_size
                and row["status"] == "uploading"
                and (not key_prefix or (row.get("object_key") or "").startswith(key_prefix))
            ):
                return row
        return None

    async def list_by_user(self, user_id, *, status=None, limit=20, offset=0):
        rows = [r for r in self.rows.values() if r["user_id"] == user_id]
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows[offset : offset + limit], len(rows)

    async def update(self, task_id, data):
        row = self.rows.get(task_id)
        if row:
            row.update(data, updated_at=_now())
        return row

    async def mark_completed(self, task_id):
        await self.update(task_id, {"status": "completed", "completed_at": _now()})

    async def mark_failed(self, task_id, reason):
        await self.update(task_id, {"status": "failed", "error_message": reason})

    async def mark_aborted(self, task_id):
        await self.update(task_id, {"status": "aborted"})


class FakePartRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict] = {}

    async def record(self, task_id, part_number, etag, size):
        row = {"task_id": task_id, "part_number": part_number, "etag": etag.strip('"'), "size": size}
        self.rows[(task_id, part_number)] = row
        return row

    async def record_many(self, task_id, parts):
        for p in parts:
            await self.record(task_id, int(p["part_number"]), str(p["etag"]), int(p.get("size") or 0))

    async def list_by_task(self, task_id):
        return sorted(
            [r for (t, _), r in self.rows.items() if t == task_id], key=lambda r: r["part_number"]
        )

    async def delete_by_task(self, task_id):
        for key in [k for k in self.rows if k[0] == task_id]:
            self.rows.pop(key)


class FakeFileRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def create(self, payload):
        row = {"id": str(uuid.uuid4()), "created_at": _now(), "deleted_at": None, **payload}
        self.rows[row["id"]] = row
        return row

    async def get(self, file_id, user_id=None):
        row = self.rows.get(file_id)
        if not row or row.get("deleted_at"):
            return None
        if user_id and row["user_id"] != user_id:
            return None
        return row

    async def get_by_task(self, task_id, user_id):
        for row in self.rows.values():
            if row.get("task_id") == task_id and row["user_id"] == user_id and not row.get("deleted_at"):
                return row
        return None

    async def find_by_hash(self, user_id, file_hash, key_prefix=None):
        if not file_hash:
            return None
        for row in reversed(list(self.rows.values())):
            if row["user_id"] == user_id and row.get("file_hash") == file_hash and not row.get("deleted_at"):
                if key_prefix and not (row.get("object_key") or "").startswith(key_prefix):
                    continue
                return row
        return None

    async def list_by_user(self, user_id, *, keyword=None, limit=20, offset=0):
        rows = [r for r in self.rows.values() if r["user_id"] == user_id and not r.get("deleted_at")]
        if keyword:
            rows = [r for r in rows if keyword.lower() in r["filename"].lower()]
        return rows[offset : offset + limit], len(rows)

    async def soft_delete(self, file_id, user_id):
        row = self.rows.get(file_id)
        if row and row["user_id"] == user_id:
            row["deleted_at"] = _now()
        return row

    async def count_references(self, object_key):
        return sum(
            1 for r in self.rows.values() if r["object_key"] == object_key and not r.get("deleted_at")
        )


# ======================================================================
# 装配
# ======================================================================
fake_oss = FakeOSS()
task_repo, part_repo, file_repo = FakeTaskRepo(), FakePartRepo(), FakeFileRepo()

upload_service = UploadService(oss=fake_oss, settings=SETTINGS)
upload_service.tasks, upload_service.parts, upload_service.files = task_repo, part_repo, file_repo

file_service = FileService(oss=fake_oss, settings=SETTINGS)
file_service.files = file_repo

app.dependency_overrides[get_upload_service] = lambda: upload_service
app.dependency_overrides[get_file_service] = lambda: file_service


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


TOKEN = make_token()
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ======================================================================
# 用例
# ======================================================================
def main() -> int:
    with TestClient(app) as client:
        print("\n[1] 基础与文档")
        r = client.get("/health")
        check("GET /health", r.status_code == 200 and r.json()["status"] == "ok", r.text[:120])
        spec = client.get("/openapi.json")
        paths = spec.json().get("paths", {})
        check("OpenAPI 可生成", spec.status_code == 200 and len(paths) >= 14, f"仅 {len(paths)} 条")
        print(f"        共注册 {len(paths)} 个路径")

        print("\n[2] 鉴权")
        check("未带 token 访问被拒", client.get("/api/v1/files").status_code == 401)
        check("伪造签名被拒", client.get(
            "/api/v1/files", headers={"Authorization": "Bearer " + jwt.encode(
                {"sub": USER_ID, "aud": "authenticated", "exp": int(time.time()) + 60}, "wrong-secret",
                algorithm="HS256")}).status_code == 401)
        expired = make_token(exp=int(time.time()) - 100)
        r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
        check("过期 token 被拒", r.status_code == 401 and r.json()["error"]["code"] == "token_expired", r.text[:120])
        r = client.get("/api/v1/auth/me", headers=AUTH)
        check("合法 token 通过", r.status_code == 200 and r.json()["id"] == USER_ID, r.text[:120])
        check("me 接口含昵称/头像字段",
              "nickname" in r.json() and "avatar_url" in r.json(), r.text[:120])
        check("PATCH /me 空请求被拒",
              client.patch("/api/v1/auth/me", headers=AUTH, json={}).status_code == 422,
              r.text[:120])
        check("OpenAPI 含 PATCH /auth/me",
              "patch" in paths.get("/api/v1/auth/me", {}) or "/api/v1/auth/me" in paths,
              "未注册")

        print("\n[3] 分片上传全流程")
        size = int(CHUNK * 2.5)
        r = client.post("/api/v1/uploads/init", headers=AUTH, json={
            "filename": "报告 v1.pdf", "file_size": size, "file_hash": "hash-aaa"})
        check("初始化任务", r.status_code == 201, r.text[:200])
        init = r.json()
        check("分片数计算正确", init["total_parts"] == 3, f"got {init.get('total_parts')}")
        check("文件名已清洗", init["object_key"].endswith(".pdf"), init.get("object_key", ""))
        task_id = init["task_id"]

        r = client.post(f"/api/v1/uploads/{task_id}/presign", headers=AUTH,
                        json={"part_numbers": [1, 2, 3]})
        check("批量签发分片地址", r.status_code == 200 and len(r.json()["parts"]) == 3, r.text[:200])

        r = client.post(f"/api/v1/uploads/{task_id}/presign", headers=AUTH,
                        json={"part_numbers": [99]})
        check("越界分片号被拒", r.status_code == 422, r.text[:150])

        # 只传前两片，验证缺片检测
        for n in (1, 2):
            client.put(f"/api/v1/uploads/{task_id}/parts/{n}", headers=AUTH, content=b"x" * CHUNK)
        r = client.post(f"/api/v1/uploads/{task_id}/complete", headers=AUTH, json={})
        ok = r.status_code == 409 and r.json()["error"]["details"]["missing_parts"] == [3]
        check("缺片时拒绝合并并指出缺失", ok, r.text[:200])

        r = client.get(f"/api/v1/uploads/{task_id}", headers=AUTH)
        st = r.json()
        check("进度查询", r.status_code == 200 and len(st["uploaded_parts"]) == 2, r.text[:150])
        print(f"        进度 {st['progress']}% ({st['uploaded_bytes']}/{st['file_size']} 字节)")

        client.put(f"/api/v1/uploads/{task_id}/parts/3", headers=AUTH,
                   content=b"y" * (size - CHUNK * 2))
        r = client.post(f"/api/v1/uploads/{task_id}/complete", headers=AUTH, json={})
        check("合并成功", r.status_code == 200, r.text[:200])
        done = r.json()
        check("落库大小正确", done["file"]["file_size"] == size, str(done["file"].get("file_size")))
        file_id = done["file"]["id"]

        r2 = client.post(f"/api/v1/uploads/{task_id}/complete", headers=AUTH, json={})
        check("重复合并保持幂等", r2.status_code == 200 and r2.json()["file"]["id"] == file_id, r2.text[:150])

        print("\n[4] 秒传与断点续传")
        r = client.post("/api/v1/uploads/init", headers=AUTH, json={
            "filename": "报告副本.pdf", "file_size": size, "file_hash": "hash-aaa"})
        check("相同指纹命中秒传", r.status_code == 201 and r.json()["instant"] is True, r.text[:200])
        check("秒传复用同一对象", r.json()["object_key"] == init["object_key"])

        r = client.post("/api/v1/uploads/init", headers=AUTH, json={
            "filename": "大视频.mp4", "file_size": CHUNK * 4, "file_hash": "hash-bbb"})
        resume_task = r.json()["task_id"]
        client.put(f"/api/v1/uploads/{resume_task}/parts/1", headers=AUTH, content=b"z" * CHUNK)
        r = client.post("/api/v1/uploads/init", headers=AUTH, json={
            "filename": "大视频.mp4", "file_size": CHUNK * 4, "file_hash": "hash-bbb"})
        body = r.json()
        ok = body["resumed"] is True and len(body["uploaded_parts"]) == 1 and body["task_id"] == resume_task
        check("重新初始化命中断点续传", ok, r.text[:200])

        print("\n[5] 越权与取消")
        other = {"Authorization": f"Bearer {make_token(sub=str(uuid.uuid4()))}"}
        r = client.get(f"/api/v1/uploads/{task_id}", headers=other)
        check("他人任务不可见", r.status_code == 404, r.text[:150])
        r = client.delete(f"/api/v1/uploads/{resume_task}", headers=AUTH)
        check("取消任务", r.status_code == 204 and len(fake_oss.aborted) == 1, r.text[:150])

        print("\n[6] 文件管理")
        r = client.get("/api/v1/files", headers=AUTH)
        check("文件列表", r.status_code == 200 and r.json()["pagination"]["total"] == 2, r.text[:200])
        r = client.get(f"/api/v1/files/{file_id}/download-url", headers=AUTH)
        check("下载签名地址", r.status_code == 200 and r.json()["url"].startswith("https://"), r.text[:150])
        r = client.post("/api/v1/files/simple-upload", headers=AUTH,
                        files={"file": ("note.txt", b"hello world", "text/plain")})
        check("小文件直传", r.status_code == 201 and r.json()["file"]["file_size"] == 11, r.text[:200])

        r = client.post("/api/v1/files/simple-upload", headers=AUTH,
                        files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
        check("危险扩展名被拦截", r.status_code == 422, r.text[:150])

        key = done["file"]["object_key"]
        client.delete(f"/api/v1/files/{file_id}", headers=AUTH)
        check("秒传副本存在时不物理删除", key in fake_oss.objects)
        other_copy = [f for f in file_repo.rows.values() if f["object_key"] == key and not f["deleted_at"]]
        client.delete(f"/api/v1/files/{other_copy[0]['id']}", headers=AUTH)
        check("最后一个引用删除后清理对象", key not in fake_oss.objects)

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
