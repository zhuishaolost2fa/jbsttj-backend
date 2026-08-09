"""端到端真实联通测试。

与 `smoke_test.py`（用内存假实现替换 IO 边界，只验证业务编排）不同，本脚本会真正打通
**阿里云 OSS** 与 **Supabase**，走的是货真价实的 HTTP 接口链路：

  初始化分片 -> 预签名直传地址 -> 浏览器直传 OSS -> 回报 ETag -> 合并 -> 列表 -> 下载 -> 删除

鉴权采用「服务间调用通道」`X-API-Key + X-User-Id`（见 app/core/security.py），
这样无需去 Supabase 申请一张真实签发的 JWT 也能跑通受保护接口。

运行前准备：
  1. 复制 .env.example 为 .env 并填好 Supabase / 阿里云 OSS 配置；
  2. 在 .env 里设置一个 SERVICE_API_KEY（任意字符串即可），用于本测试的鉴权；
  3. 在 Supabase SQL Editor 执行 sql/schema.sql 建表；
  4. 执行 python scripts/setup_oss.py 配置 Bucket CORS（直传必须 expose ETag）。

然后运行：
  python scripts/e2e_test.py

任何一步失败都会立即中断并给出可读的错误信息，退出码非 0。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from typing import Any, Dict, List, Optional

# 让脚本在仓库任意位置都能 import 到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402

# 分片直传统一 Content-Type 说明：V2 OSS SDK 的预签名对 Content-Type 是 unsigned 的，
# 因此直传时**不能**额外带 Content-Type 头（否则 OSS 服务端会把 content-type 纳入 V4
# 签名校验，与预签名时不一致 → 403）。直传时由 OSS 使用默认 Content-Type 即可。
# 这与 scripts/verify_oss_live.py 的成功行为一致。


def _mask(secret: str) -> str:
    if not secret:
        return "<空>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "*" * (len(secret) - 8) + secret[-4:]


def check_config() -> int:
    settings = get_settings()
    print("== 配置自检 ==")
    print(f"  SUPABASE_URL          : {settings.supabase_url or '<空>'}")
    print(f"  SUPABASE_SERVICE_ROLE : {_mask(settings.supabase_service_role_key)}")
    print(f"  SUPABASE_JWT_SECRET   : {_mask(settings.supabase_jwt_secret)}")
    print(f"  SUPABASE_JWKS_URL     : {settings.jwks_url or '<空>'}")
    print(f"  OSS_BUCKET            : {settings.oss_bucket or '<空>'}")
    print(f"  OSS_ENDPOINT          : {settings.oss_endpoint or '<空>'}")
    print(f"  OSS_REGION            : {settings.oss_region or '<空>'}")
    print(f"  OSS_SIGN_VERSION      : {settings.oss_signature_version}")
    print(f"  SERVICE_API_KEY       : {_mask(settings.service_api_key)}")

    missing = settings.missing_required()
    if missing:
        print("\n[配置不完整] 缺少以下关键项，无法联通真实服务：")
        for m in missing:
            print(f"  - {m}")
        print("\n请复制 .env.example 为 .env 并补全后再运行。")
        return 2

    if not settings.service_api_key:
        print("\n[配置不完整] 未设置 SERVICE_API_KEY。")
        print("  端到端测试通过服务间通道鉴权，请在 .env 里设置一个任意字符串的 SERVICE_API_KEY。")
        return 2

    print("  配置完整。\n")
    return 0


def _chunk(data: bytes, chunk_size: int) -> List[bytes]:
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def _put_part(client: Any, url: str, part: bytes) -> str:
    """直传单个分片到 OSS 预签名地址，返回 ETag（已去引号）。

    注意：V2 OSS SDK 的预签名对 Content-Type 是 unsigned 的，因此此处**不能**
    额外带 Content-Type 头，否则 OSS 服务端会把 content-type 纳入 V4 签名校验、
    与预签名时不一致而返回 403。与 verify_oss_live.py 行为一致。
    """
    resp = client.put(url, content=part)
    resp.raise_for_status()
    etag = resp.headers.get("ETag", "").strip().strip('"')
    if not etag:
        raise RuntimeError("OSS 未返回 ETag，直传可能失败")
    return etag


def run(args: argparse.Namespace) -> int:
    import httpx
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.oss import get_oss_service
    from app.services.supabase import supabase

    settings = get_settings()
    # 建表后 user_id 仍是 uuid 类型（仅去掉了对 auth.users 的外键，类型保留以兼容 RLS 策略），
    # 这里用一段合法 UUID 格式的业务标识，避免插入时类型报错；FK 已去除，无需它是真实 Supabase 用户。
    user_id = "e2e00000-0000-0000-0000-000000000000"
    headers = {"X-API-Key": settings.service_api_key, "X-User-Id": user_id}

    # 文件规模：约 3 个分片，单分片 200KB，避免测试产生过大流量
    chunk_size_req = 200 * 1024
    file_size = 450 * 1024
    content = os.urandom(file_size)
    file_hash = hashlib.sha256(content).hexdigest()

    created_file_ids: List[str] = []
    passed = 0
    failed = 0

    def step(name: str, ok: bool, detail: str = "") -> bool:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
        return ok

    with TestClient(app) as client:
        base = settings.api_prefix

        # ---- 0. 鉴权拒绝 ----
        print("\n== 0. 鉴权校验 ==")
        r = client.get(f"{base}/files")
        step("无凭证访问被拒(401)", r.status_code == 401, f"status={r.status_code}")
        bad = client.get(f"{base}/files", headers={"X-API-Key": "wrong", "X-User-Id": user_id})
        step("错误 API Key 被拒(401)", bad.status_code == 401, f"status={bad.status_code}")
        no_user = client.get(f"{base}/files", headers={"X-API-Key": settings.service_api_key})
        step("缺少 X-User-Id 被拒(401)", no_user.status_code == 401, f"status={no_user.status_code}")

        # ---- 1. 连通性 ----
        print("\n== 1. 连通性探测 ==")
        # 注：run() 为同步函数，OSS/Supabase 的 ping() 是协程不便直接 await；
        # 此处仅做「配置层」快速标记，真实联通性由后续「分片上传完整链路」步骤
        # （init→预签名直传→合并）实际打通并验证。
        oss_ok = bool(settings.oss_bucket and (settings.oss_endpoint or settings.oss_public_endpoint))
        step("OSS 配置就绪", oss_ok)
        db_ok = supabase.available
        step("Supabase 配置就绪", db_ok)
        if not (oss_ok and db_ok):
            print("\n配置不完整，终止后续测试。请检查 .env 中的 OSS/Supabase 配置。")
            return 3

        # ---- 2. 分片上传完整链路 ----
        print("\n== 2. 分片上传完整链路 ==")
        r = client.post(
            f"{base}/uploads/init",
            headers=headers,
            json={
                "filename": "e2e-big.bin",
                "file_size": file_size,
                "content_type": "application/octet-stream",
                "file_hash": file_hash,
                "chunk_size": chunk_size_req,
            },
        )
        if not step("init 成功(201)", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}"):
            return 4
        init = r.json()
        task_id = init["task_id"]
        step("非秒传/非续传(全新任务)", not init["instant"] and not init["resumed"])
        print(f"         task_id={task_id} chunk_size={init['chunk_size']} total_parts={init['total_parts']}")

        real_chunk = init["chunk_size"]
        total_parts = init["total_parts"]
        parts_data = _chunk(content, real_chunk)
        if not step("分片数正确", len(parts_data) == total_parts, f"{len(parts_data)} vs {total_parts}"):
            return 4

        # 预签名
        r = client.post(
            f"{base}/uploads/{task_id}/presign",
            headers=headers,
            json={"part_numbers": list(range(1, total_parts + 1))},
        )
        if not step("presign 成功", r.status_code == 200, f"status={r.status_code}"):
            return 4
        presigned = {p["part_number"]: p["url"] for p in r.json()["parts"]}

        # 直传 OSS
        uploaded: List[Dict[str, Any]] = []
        with httpx.Client(timeout=30.0) as oss_client:
            for idx, part in enumerate(parts_data, start=1):
                try:
                    etag = _put_part(oss_client, presigned[idx], part)
                    uploaded.append({"part_number": idx, "etag": etag, "size": len(part)})
                except Exception as exc:  # noqa: BLE001
                    step(f"分片 {idx} 直传 OSS", False, str(exc))
                    return 4
        step("全部分片直传 OSS 成功", len(uploaded) == total_parts, f"{len(uploaded)}/{total_parts}")

        # 回报 ETag
        r = client.post(
            f"{base}/uploads/{task_id}/parts/callback",
            headers=headers,
            json={"parts": uploaded},
        )
        step("批量回报 ETag 成功", r.status_code == 200, f"status={r.status_code}")

        # 合并
        r = client.post(f"{base}/uploads/{task_id}/complete", headers=headers, json={})
        if not step("complete 成功", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}"):
            return 4
        file_info = r.json()["file"]
        file_id = file_info["id"]
        created_file_ids.append(file_id)
        step("返回文件信息", bool(file_id))

        # 列表
        r = client.get(f"{base}/files", headers=headers, params={"limit": 50})
        step("文件列表可查", r.status_code == 200 and any(f["id"] == file_id for f in r.json()["items"]))

        # 下载校验
        r = client.get(f"{base}/files/{file_id}/download-url", headers=headers)
        if step("获取下载地址成功", r.status_code == 200):
            dl_url = r.json()["url"]
            with httpx.Client(timeout=30.0) as dl:
                dlr = dl.get(dl_url)
            ok_dl = dlr.status_code == 200 and len(dlr.content) == file_size
            step("下载内容大小一致", ok_dl, f"got={len(dlr.content) if dlr.status_code == 200 else dlr.status_code} expect={file_size}")

        # ---- 3. 秒传 ----
        print("\n== 3. 秒传 ==")
        r = client.post(
            f"{base}/uploads/init",
            headers=headers,
            json={
                "filename": "e2e-big-copy.bin",
                "file_size": file_size,
                "content_type": "application/octet-stream",
                "file_hash": file_hash,
            },
        )
        if not step("相同内容再次 init", r.status_code == 201):
            return 4
        second = r.json()
        step("命中秒传(instant=true)", second.get("instant") is True, f"instant={second.get('instant')}")
        if second.get("instant") and second.get("file"):
            created_file_ids.append(second["file"]["id"])

        # ---- 4. 断点续传 ----
        print("\n== 4. 断点续传 ==")
        resume_size = 300 * 1024
        resume_content = os.urandom(resume_size)
        resume_hash = hashlib.sha256(resume_content).hexdigest()
        r1 = client.post(
            f"{base}/uploads/init",
            headers=headers,
            json={
                "filename": "e2e-resume.bin",
                "file_size": resume_size,
                "content_type": "application/octet-stream",
                "file_hash": resume_hash,
                "chunk_size": chunk_size_req,
            },
        )
        if not step("续传-首次 init", r1.status_code == 201):
            return 4
        rinit = r1.json()
        rtask = rinit["task_id"]
        rchunk = rinit["chunk_size"]
        rparts = _chunk(resume_content, rchunk)
        rp = client.post(
            f"{base}/uploads/{rtask}/presign",
            headers=headers,
            json={"part_numbers": [1]},
        )
        if not step("续传-预签名第1片", rp.status_code == 200):
            return 4
        with httpx.Client(timeout=30.0) as oss_client:
            etag1 = _put_part(oss_client, rp.json()["parts"][0]["url"], rparts[0])
        cb = client.post(
            f"{base}/uploads/{rtask}/parts/callback",
            headers=headers,
            json={"parts": [{"part_number": 1, "etag": etag1, "size": len(rparts[0])}]},
        )
        step("续传-回报第1片", cb.status_code == 200)

        r2 = client.post(
            f"{base}/uploads/init",
            headers=headers,
            json={
                "filename": "e2e-resume.bin",
                "file_size": resume_size,
                "content_type": "application/octet-stream",
                "file_hash": resume_hash,
                "chunk_size": chunk_size_req,
            },
        )
        if not step("续传-二次 init", r2.status_code == 201):
            return 4
        resumed = r2.json()
        step("命中断点续传(resumed=true)", resumed.get("resumed") is True, f"resumed={resumed.get('resumed')}")
        step(
            "已上传分片被正确识别",
            any(p["part_number"] == 1 for p in resumed.get("uploaded_parts", [])),
        )
        # 清理续传产生的 OSS 碎片（不落库文件，直接取消任务）
        ab = client.delete(f"{base}/uploads/{rtask}", headers=headers)
        step("取消续传任务(清理碎片)", ab.status_code in (204, 200), f"status={ab.status_code}")

        # ---- 5. 清理 ----
        print("\n== 5. 清理测试文件 ==")
        for fid in created_file_ids:
            d = client.delete(f"{base}/files/{fid}", headers=headers)
            step(f"删除文件 {fid[:8]}", d.status_code in (204, 200), f"status={d.status_code}")

    print(f"\n== 结果: 通过 {passed} 项, 失败 {failed} 项 ==")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端真实联通测试")
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="只做配置自检，不启动应用、不联通云服务",
    )
    args = parser.parse_args()

    rc = check_config()
    if rc != 0:
        return rc
    if args.config_only:
        print("配置自检通过（--config-only）。")
        return 0

    return run(args)


if __name__ == "__main__":
    t0 = time.time()
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n被用户中断")
        code = 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n[未捕获异常] {type(exc).__name__}: {exc}")
        code = 99
    print(f"耗时 {time.time() - t0:.1f}s")
    sys.exit(code)
