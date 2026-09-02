"""
验证 POST /files/simple-upload 的 **API 层**行为（service 层由 check_simple_upload_prefix.py 覆盖）。

用 TestClient + dependency_overrides 打桩 get_current_user / get_file_service，
不发真实请求到 DB/OSS，只验证：
  1. upload_type=temporary -> service 收到 prefix="temp"
  2. upload_type=permanent -> service 收到 prefix=None
  3. 不传 upload_type     -> 默认 permanent（prefix=None）
  4. upload_type 非法值    -> 4xx 且不调用 service
  5. filename 覆盖原始文件名

注意：故意**不用** `with TestClient(app)`，避免触发 lifespan（会去连 Supabase）。
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient

from app.core.security import get_current_user
from app.main import app
from app.schemas.file import FileInfo
from app.services.file_service import get_file_service

# ---------- 打桩 ----------
calls = []


class FakeUser:
    id = "user-abc-123"
    email = "tester@example.com"
    role = "user"


class FakeService:
    async def simple_upload(
        self,
        user,
        *,
        filename,
        content_type,
        data,
        max_size=0,
        prefix=None,
        acl=None,
        content_disposition=None,
    ):
        calls.append(
            {
                "user_id": user.id,
                "filename": filename,
                "prefix": prefix,
                "max_size": max_size,
                "size": len(data),
            }
        )
        return FileInfo(
            id="f" * 32,
            filename=filename,
            object_key=f"{prefix or 'uploads'}/{user.id}/2026/09/fake.docx",
            bucket="jbs-store",
            content_type=content_type,
            file_size=len(data),
            file_hash="h" * 64,
            etag="fake-etag",
        )


app.dependency_overrides[get_current_user] = lambda: FakeUser()
app.dependency_overrides[get_file_service] = lambda: FakeService()

client = TestClient(app)

# ---------- 定位真实路由 ----------
# ⚠️ 本项目用的 FastAPI 版本，include_router 会在 app.routes 里放 `_IncludedRouter`
#    占位对象（延迟展开），没有 path 属性，遍历 app.routes 取不到路径。
#    改从 OpenAPI schema 里拿最终路径。
path = None
for route_path in app.openapi().get("paths", {}):
    if route_path.endswith("/files/simple-upload"):
        path = route_path
        break
if not path:
    print("❌ 找不到 /files/simple-upload 路由")
    sys.exit(1)
print(f"路由: {path}\n")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


FILE_BYTES = b"fake-docx-content"


def post(upload_type, filename=None):
    data = {}
    if upload_type is not None:
        data["upload_type"] = upload_type
    if filename is not None:
        data["filename"] = filename
    return client.post(
        path,
        files={"file": ("原始名.docx", FILE_BYTES, "application/msword")},
        data=data,
    )


# 1) temporary
calls.clear()
resp = post("temporary")
check(
    "upload_type=temporary -> prefix='temp'",
    resp.status_code in (200, 201) and calls and calls[-1]["prefix"] == "temp",
    f"HTTP {resp.status_code} prefix={calls[-1]['prefix'] if calls else 'N/A'}",
)

# 2) permanent
calls.clear()
resp = post("permanent")
check(
    "upload_type=permanent -> prefix=None",
    resp.status_code in (200, 201) and calls and calls[-1]["prefix"] is None,
    f"HTTP {resp.status_code} prefix={calls[-1]['prefix'] if calls else 'N/A'}",
)

# 3) 不传 -> 默认 permanent
calls.clear()
resp = post(None)
check(
    "不传 upload_type -> 默认 permanent (prefix=None)",
    resp.status_code in (200, 201) and calls and calls[-1]["prefix"] is None,
    f"HTTP {resp.status_code} prefix={calls[-1]['prefix'] if calls else 'N/A'}",
)

# 4) 非法值
calls.clear()
resp = post("bogus")
check(
    "upload_type 非法值 -> 4xx 且不调用 service",
    resp.status_code >= 400 and len(calls) == 0,
    f"HTTP {resp.status_code} service 调用次数={len(calls)} body={resp.text[:100]}",
)

# 5) filename 覆盖
calls.clear()
resp = post("temporary", filename="自定义名.docx")
check(
    "filename 覆盖原始文件名",
    resp.status_code in (200, 201) and calls and calls[-1]["filename"] == "自定义名.docx",
    f"HTTP {resp.status_code} filename={calls[-1]['filename'] if calls else 'N/A'}",
)

# 6) 不传 filename -> 用上传文件名
calls.clear()
resp = post("temporary")
check(
    "不传 filename -> 回退到上传文件名",
    resp.status_code in (200, 201) and calls and calls[-1]["filename"] == "原始名.docx",
    f"HTTP {resp.status_code} filename={calls[-1]['filename'] if calls else 'N/A'}",
)

# 7) max_size 是 20MB 上限
calls.clear()
resp = post("temporary")
check(
    "service 收到 max_size=20MB",
    calls and calls[-1]["max_size"] == 20 * 1024 * 1024,
    f"max_size={calls[-1]['max_size'] if calls else 'N/A'}",
)

# ---------- 输出 ----------
passed = sum(1 for _, ok, _ in results if ok)
print("=" * 62)
for name, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"         {detail}")
print("=" * 62)
print(f"{passed}/{len(results)} 通过")
sys.exit(0 if passed == len(results) else 1)
