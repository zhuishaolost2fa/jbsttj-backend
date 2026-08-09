#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实云上 OSS + STS 链路验证（不依赖 Supabase）。

用途：在已配置好阿里云 OSS 与 STS 授权后，用真实 STS 临时凭证走一遍
真实的分片上传往返（init -> 预签名直传 -> 列举分片 -> 合并 -> 下载校验 -> 删除），
确认后端 `OSSService` 与 STS 接入在真实云上可用。

运行：
    python scripts/verify_oss_live.py
退出码：
    0 = 全部通过；1 = 某步失败；2 = 配置缺失。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys

# 让脚本可在项目任意位置运行：把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx


def _config_missing() -> list[str]:
    from app.core.config import Settings

    s = Settings(_env_file=None) if False else Settings()
    missing = []
    if not s.oss_bucket:
        missing.append("OSS_BUCKET")
    if not s.oss_region:
        missing.append("OSS_REGION")
    if not s.oss_public_endpoint and not s.oss_endpoint:
        missing.append("OSS_ENDPOINT")
    if s.oss_use_sts:
        if not s.oss_sts_role_arn:
            missing.append("OSS_STS_ROLE_ARN")
        if not (s.oss_sts_access_key_id or s.oss_access_key_id):
            missing.append("OSS_STS_ACCESS_KEY_ID")
        if not (s.oss_sts_access_key_secret or s.oss_access_key_secret):
            missing.append("OSS_STS_ACCESS_KEY_SECRET")
    else:
        if not s.oss_access_key_id or not s.oss_access_key_secret:
            missing.append("OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET")
    return missing


async def main() -> int:
    from app.core.config import Settings
    from app.services.oss import OSSService, RemotePart

    missing = _config_missing()
    if missing:
        print(f"[CONFIG] 缺少必要配置：{', '.join(missing)}")
        print("请检查 .env 中的 OSS / STS 配置项。")
        return 2

    s = Settings()
    # 本地 / CI 验证：始终走公网 endpoint（内网 endpoint 仅同地域 ECS 可达）。
    # 应用部署到 ECS 时由 .env 的 OSS_INTERNAL_ENDPOINT 控制走内网。
    s.oss_internal_endpoint = ""
    svc = OSSService(settings=s)
    key = f"verify/sts-live-test-{__import__('time').time_ns()}.bin"

    # 构造 3 个分片，总内容已知
    part_size = 256 * 1024  # 256KB
    payload = b""
    chunks: list[bytes] = []
    for i in range(3):
        chunk = bytes((i * 37 + j) % 256 for j in range(part_size))
        chunks.append(chunk)
        payload += chunk
    expected_md5 = hashlib.md5(payload).hexdigest()
    print(f"[INFO] 测试对象 key={key} 大小={len(payload)} 字节，分片数={len(chunks)}")

    async with httpx.AsyncClient(timeout=60) as client:
        # 1) 初始化分片上传
        upload_id = await svc.init_multipart(key, content_type="application/octet-stream")
        print(f"[1/6] init_multipart OK upload_id={upload_id}")

        # 2) 预签名 + 直传每个分片（模拟浏览器行为）
        etags: list[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            url = await svc.presign_part(key, upload_id, idx, 3600)
            resp = await client.put(url, content=chunk)
            if resp.status_code != 200:
                print(f"[2/6] 直传分片 {idx} 失败 HTTP {resp.status_code}: {resp.text[:200]}")
                await svc.abort_multipart(key, upload_id)
                return 1
            etag = resp.headers.get("ETag", "").strip('"')
            etags.append(etag)
            print(f"[2/6] 直传分片 {idx} OK ETag={etag[:16]}...")
        print(f"[2/6] 全部 {len(chunks)} 个分片直传完成")

        # 3) 列举 OSS 端已落盘分片（进度可信来源）
        remote = await svc.list_parts(key, upload_id)
        if [p.part_number for p in remote] != list(range(1, len(chunks) + 1)):
            print(f"[3/6] list_parts 不一致：{[p.part_number for p in remote]}")
            await svc.abort_multipart(key, upload_id)
            return 1
        remote_etags = {p.part_number: p.etag for p in remote}
        for idx, e in enumerate(etags, start=1):
            if remote_etags.get(idx) != e:
                print(f"[3/6] 分片 {idx} ETag 与服务端不符")
                await svc.abort_multipart(key, upload_id)
                return 1
        print(f"[3/6] list_parts OK 共 {len(remote)} 片，ETag 校验一致")

        # 4) 合并分片
        parts = [
            RemotePart(part_number=p.part_number, etag=p.etag, size=p.size) for p in remote
        ]
        meta = await svc.complete_multipart(key, upload_id, parts)
        print(f"[4/6] complete_multipart OK etag={meta.etag[:16]}... size={meta.size}")

        # 5) 下载校验
        meta2 = await svc.head_object(key)
        if meta2 is None or meta2.size != len(payload):
            print(f"[5/6] head_object 校验失败 size={None if meta2 is None else meta2.size}")
            await svc.delete_object(key)
            return 1
        dl_url = await svc.sign_download_url(key, 600, filename="test.bin", inline=False)
        dl = await client.get(dl_url)
        if dl.status_code != 200 or dl.content != payload:
            print(f"[5/6] 下载校验失败 HTTP {dl.status_code} 内容匹配={dl.content == payload}")
            await svc.delete_object(key)
            return 1
        dl_md5 = hashlib.md5(dl.content).hexdigest()
        print(f"[5/6] 下载校验 OK HTTP 200，MD5 一致={dl_md5 == expected_md5}")

        # 6) 删除
        await svc.delete_object(key)
        after = await svc.head_object(key)
        if after is not None:
            print("[6/6] 删除后对象仍存在")
            return 1
        print("[6/6] delete_object OK（对象已清理）")

    print("\n[RESULT] OSS + STS 真实链路全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\n[RESULT] 验证异常失败：{type(exc).__name__}: {exc}")
        code = 1
    sys.exit(code)
