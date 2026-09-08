"""设置 jbs-store bucket 的 CORS 规则（浏览器直传 OSS 分片上传依赖）。

用法（在服务器 api 容器内执行，或本地配好 .env 后执行）：
    python scripts/set_oss_cors.py             # 写入（覆盖）CORS 规则
    python scripts/set_oss_cors.py --show      # 只读当前规则

背景：前端 H5 用预签名 URL 直接 PUT 分片到 OSS（跨域），需要 bucket CORS 放行
前端 Origin + PUT 方法 + 暴露 ETag 响应头（putPart 要读 ETag 计算合并列表）。
三个缺一不可，缺了浏览器就报 CORS 错或「读取不到分片 ETag」。

坑：备案前用 http:// 访问、备案后切 https://，两者 Origin 不同，必须同时配；
纯 IP 访问（http://101.34.58.31）也要单独配。域名/证书变化时记得回来更新本脚本。
"""

from __future__ import annotations

import sys

import alibabacloud_oss_v2 as oss

from app.core.config import get_settings
from app.services.oss import OSSService

# 本地开发 origin（各端口 H5/小程序调试）与生产 origin（http 备案前 + https 备案后 + 纯 IP）
ALLOWED_ORIGINS = [
    # 本地开发
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://localhost:10086",
    # 生产
    "https://www.jbs-ttj.store",
    "http://www.jbs-ttj.store",
    "http://jbs-ttj.store",
    "http://101.34.58.31",
]

ALLOWED_METHODS = ["GET", "PUT", "POST", "DELETE", "HEAD"]
ALLOWED_HEADERS = ["*"]
EXPOSE_HEADERS = ["ETag", "x-oss-request-id", "Content-Length"]
MAX_AGE_SECONDS = 3600


def main() -> None:
    settings = get_settings()
    client = OSSService(settings).client

    if "--show" in sys.argv:
        res = client.get_bucket_cors(oss.GetBucketCorsRequest(bucket=settings.oss_bucket))
        rules = res.cors_configuration.cors_rules or []
        print(f"bucket={settings.oss_bucket} CORS 规则数={len(rules)}")
        for i, r in enumerate(rules):
            print(f"  [{i}] Origin={r.allowed_origins}")
            print(f"      Method={r.allowed_methods}")
            print(f"      Header={r.allowed_headers}")
            print(f"      Expose={r.expose_headers}")
        return

    rule = oss.CORSRule(
        allowed_origins=ALLOWED_ORIGINS,
        allowed_methods=ALLOWED_METHODS,
        allowed_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSE_HEADERS,
        max_age_seconds=MAX_AGE_SECONDS,
    )
    cfg = oss.CORSConfiguration(cors_rules=[rule])
    client.put_bucket_cors(
        oss.PutBucketCorsRequest(bucket=settings.oss_bucket, cors_configuration=cfg)
    )
    print(f"CORS 已写入 bucket={settings.oss_bucket}")
    print(f"  Origin: {ALLOWED_ORIGINS}")
    print(f"  Method: {ALLOWED_METHODS}")
    print(f"  Expose: {EXPOSE_HEADERS}")


if __name__ == "__main__":
    main()
