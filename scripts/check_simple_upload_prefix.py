"""
验证 FileService.simple_upload 的前缀隔离与秒传命名空间（不碰真实 DB / OSS）。

用假 OSS 与假 Repository，只测 service 层的分支逻辑：
  1. prefix=temp     -> object_key 落在 temp/
  2. prefix=None     -> object_key 落在 uploads/（默认前缀）
  3. 同内容跨前缀不复用（永久对象不会被临时上传秒传命中）
  4. 同前缀内重复内容命中秒传（不再 put 对象）
  5. 超过 20MB / 空文件 / 黑名单扩展名被拦截
  6. 秒传查询确实带上了 key_prefix
"""

import asyncio
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings
from app.core.exceptions import ValidationError
from app.services.file_service import FileService

settings = get_settings()
USER_ID = "user-abc-123"


class FakeUser:
    id = USER_ID


class FakeOSS:
    def __init__(self) -> None:
        self.put_calls = []

    async def put_object(self, key, data, content_type=None, content_disposition=None):
        self.put_calls.append(key)
        return types.SimpleNamespace(etag="fake-etag")

    async def head_object(self, key):
        # 模拟对象确实存在于 OSS（否则秒传分支会退回真实上传）
        return types.SimpleNamespace(etag="fake-etag")

    async def set_object_acl(self, key, acl):
        return None


class FakeRepo:
    def __init__(self) -> None:
        self.rows = []
        self.hash_queries = []  # (hash前8位, key_prefix)

    async def find_by_hash(self, user_id, file_hash, key_prefix=None):
        self.hash_queries.append((file_hash[:8], key_prefix))
        for row in self.rows:
            if row.get("file_hash") != file_hash:
                continue
            if row.get("user_id") != user_id:
                continue
            # 与真实 SQL 的 `object_key like {prefix}/*` 对齐
            if key_prefix and not row.get("object_key", "").startswith(f"{key_prefix}/"):
                continue
            return row
        return None

    async def create(self, payload):
        row = dict(payload)
        row.setdefault("id", "f" * 32)
        self.rows.append(row)
        return row


def new_service():
    oss, repo = FakeOSS(), FakeRepo()
    return FileService(oss=oss, settings=settings, files=repo), oss, repo


async def main():
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    # 1) temporary 前缀
    svc, oss, repo = new_service()
    info = await svc.simple_upload(
        FakeUser(),
        filename="DM手册.docx",
        content_type=None,
        data=b"hello-dm-guide",
        prefix=settings.temp_upload_prefix,
    )
    check("temporary 落 temp/ 前缀", info.object_key.startswith("temp/"), info.object_key)

    # 2) permanent（prefix=None → 默认 upload_prefix）
    svc2, oss2, _ = new_service()
    info2 = await svc2.simple_upload(
        FakeUser(),
        filename="DM手册.docx",
        content_type=None,
        data=b"hello-dm-guide",
        prefix=None,
    )
    check(
        "permanent 落 uploads/ 前缀",
        info2.object_key.startswith(f"{settings.upload_prefix}/"),
        info2.object_key,
    )

    # 3) 跨前缀不复用：先永久传一次，再临时传同内容
    svc3, oss3, repo3 = new_service()
    await svc3.simple_upload(
        FakeUser(), filename="a.docx", content_type=None, data=b"same-content", prefix=None
    )
    puts_before = len(oss3.put_calls)
    info3 = await svc3.simple_upload(
        FakeUser(),
        filename="b.docx",
        content_type=None,
        data=b"same-content",
        prefix=settings.temp_upload_prefix,
    )
    check(
        "临时上传不复用永久对象（仍真实 put）",
        len(oss3.put_calls) == puts_before + 1,
        f"put 次数 {len(oss3.put_calls)}",
    )
    check("跨前缀后 key 仍在 temp/", info3.object_key.startswith("temp/"), info3.object_key)

    # 4) 同前缀内重复内容命中秒传
    await svc3.simple_upload(
        FakeUser(),
        filename="c.docx",
        content_type=None,
        data=b"same-content",
        prefix=settings.temp_upload_prefix,
    )
    check(
        "同前缀内重复内容命中秒传（不再 put）",
        len(oss3.put_calls) == puts_before + 1,
        f"put 次数 {len(oss3.put_calls)}",
    )

    # 5) 秒传查询确实带上 key_prefix
    check(
        "秒传查询带 key_prefix=temp",
        any(pfx == "temp" for _, pfx in repo3.hash_queries),
        str(repo3.hash_queries[:4]),
    )

    # 6) 20MB 上限
    try:
        await svc3.simple_upload(
            FakeUser(),
            filename="big.docx",
            content_type=None,
            data=b"x" * (20 * 1024 * 1024 + 1),
            prefix="temp",
        )
        check("超过 20MB 被拦截", False, "未抛错")
    except ValidationError as exc:
        check("超过 20MB 被拦截", "file_too_large" in str(exc) or "MB" in str(exc), str(exc)[:60])

    # 7) 空文件
    try:
        await svc3.simple_upload(
            FakeUser(), filename="e.docx", content_type=None, data=b"", prefix="temp"
        )
        check("空文件被拦截", False, "未抛错")
    except ValidationError as exc:
        check("空文件被拦截", True, str(exc)[:40])

    # 8) 黑名单扩展名
    try:
        await svc3.simple_upload(
            FakeUser(), filename="x.exe", content_type=None, data=b"MZ", prefix="temp"
        )
        check(".exe 被黑名单拦截", False, "未抛错")
    except ValidationError as exc:
        check(".exe 被黑名单拦截", True, str(exc)[:40])

    # 9) .doc / .docx 放行（本次需求的核心格式）
    for name in ("guide.docx", "guide.doc"):
        try:
            await svc3.simple_upload(
                FakeUser(), filename=name, content_type=None, data=b"doc-bytes", prefix="temp"
            )
            check(f"{name} 放行", True)
        except ValidationError as exc:
            check(f"{name} 放行", False, str(exc)[:60])

    # ---- 输出 ----
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 62}")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if detail:
            line += f"\n         {detail}"
        print(line)
    print("=" * 62)
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
