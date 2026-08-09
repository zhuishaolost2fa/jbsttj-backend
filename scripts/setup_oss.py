"""一键配置 OSS Bucket：跨域规则 + 分片碎片清理规则。

用法（在项目根目录执行，需先配好 .env）：
    python scripts/setup_oss.py            # 应用配置
    python scripts/setup_oss.py --show     # 只查看当前配置

前端直传 OSS 时浏览器会先发 OPTIONS 预检，Bucket 没配 CORS 就会直接失败，
而且必须把 ETag 放进 expose-headers，否则 JS 读不到分片的 ETag。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import oss2  # noqa: E402
from oss2.models import (  # noqa: E402
    AbortMultipartUpload,
    BucketCors,
    BucketLifecycle,
    CorsRule,
    LifecycleRule,
)

from app.core.config import get_settings  # noqa: E402

CORS_MAX_AGE = 3600
LIFECYCLE_RULE_ID = "abort-incomplete-multipart"
STALE_PART_DAYS = 7


def build_bucket() -> oss2.Bucket:
    s = get_settings()
    missing = [
        name
        for name, value in {
            "OSS_ACCESS_KEY_ID": s.oss_access_key_id,
            "OSS_ACCESS_KEY_SECRET": s.oss_access_key_secret,
            "OSS_ENDPOINT": s.oss_endpoint,
            "OSS_BUCKET": s.oss_bucket,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"缺少配置: {', '.join(missing)}，请先填写 .env")

    if s.oss_signature_version.lower() == "v4":
        if not s.oss_region:
            raise SystemExit("使用 V4 签名必须配置 OSS_REGION")
        auth = oss2.AuthV4(s.oss_access_key_id, s.oss_access_key_secret)
        return oss2.Bucket(auth, s.oss_endpoint, s.oss_bucket, region=s.oss_region)

    auth = oss2.Auth(s.oss_access_key_id, s.oss_access_key_secret)
    return oss2.Bucket(auth, s.oss_endpoint, s.oss_bucket)


def apply_cors(bucket: oss2.Bucket) -> None:
    origins = get_settings().cors_origin_list
    rule = CorsRule(
        allowed_origins=origins,
        allowed_methods=["GET", "PUT", "POST", "DELETE", "HEAD"],
        allowed_headers=["*"],
        # ETag 必须暴露，前端分片上传要读它；x-oss-request-id 便于排查问题
        expose_headers=["ETag", "x-oss-request-id", "Content-Length"],
        max_age_seconds=CORS_MAX_AGE,
    )
    bucket.put_bucket_cors(BucketCors([rule]))
    print(f"[OK] CORS 规则已应用，允许来源: {', '.join(origins)}")
    if "*" in origins:
        print("[!]  当前允许任意来源，生产环境请在 .env 的 CORS_ORIGINS 里收紧")


def apply_lifecycle(bucket: oss2.Bucket) -> None:
    """自动清理超过 N 天仍未合并的分片，避免碎片长期占用存储费用。"""
    prefix = get_settings().upload_prefix.strip("/") + "/"
    rule = LifecycleRule(
        LIFECYCLE_RULE_ID,
        prefix,
        status=LifecycleRule.ENABLED,
        abort_multipart_upload=AbortMultipartUpload(days=STALE_PART_DAYS),
    )

    existing = []
    try:
        existing = [r for r in bucket.get_bucket_lifecycle().rules if r.id != LIFECYCLE_RULE_ID]
    except oss2.exceptions.NoSuchLifecycle:
        pass

    bucket.put_bucket_lifecycle(BucketLifecycle(existing + [rule]))
    print(f"[OK] 生命周期规则已应用：{prefix} 下未完成的分片 {STALE_PART_DAYS} 天后自动清理")


def show(bucket: oss2.Bucket) -> None:
    print(f"Bucket: {bucket.bucket_name} @ {bucket.endpoint}")
    try:
        for rule in bucket.get_bucket_cors().rules:
            print("  CORS:")
            print(f"    origins  = {rule.allowed_origins}")
            print(f"    methods  = {rule.allowed_methods}")
            print(f"    expose   = {rule.expose_headers}")
    except oss2.exceptions.OssError as exc:
        print(f"  CORS: 未配置 ({exc.code})")

    try:
        for rule in bucket.get_bucket_lifecycle().rules:
            print(f"  Lifecycle: id={rule.id} prefix={rule.prefix} status={rule.status}")
    except oss2.exceptions.OssError as exc:
        print(f"  Lifecycle: 未配置 ({exc.code})")


def main() -> None:
    bucket = build_bucket()
    if "--show" in sys.argv:
        show(bucket)
        return
    apply_cors(bucket)
    apply_lifecycle(bucket)
    print("\n完成。可再执行 `python scripts/setup_oss.py --show` 复核。")


if __name__ == "__main__":
    main()
