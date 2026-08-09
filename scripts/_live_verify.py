"""现场验证：直接打运行中 uvicorn(:8000) 的真实接口，走完整上传链路。

与服务通道鉴权（X-API-Key + X-User-Id），分片直传严格不带 Content-Type。
"""
import asyncio
import hashlib
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings

BASE = "http://127.0.0.1:8000/api/v1"
PASS, FAIL = 0, 0


def step(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def parse_field(resp_json: dict, field: str):
    return resp_json.get(field)


async def main():
    s = get_settings()
    key = s.service_api_key
    user_id = "11111111-1111-1111-1111-111111111111"
    headers = {"X-API-Key": key, "X-User-Id": user_id, "Accept": "application/json"}

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as api, httpx.AsyncClient(timeout=60) as oss_client:
        # 1) health（挂在根路径，非 /api/v1 下）
        r = await api.get("/health", headers=headers)
        step("GET /health", r.status_code == 200, str(r.json()))

        # 2) ready
        r = await api.get("/ready", headers=headers)
        step("GET /ready", r.status_code == 200, str(r.json().get("checks")))

        # 3) init
        data = b"".join(bytes([i % 256]) * 1000 for i in range(200))  # 200000 字节
        file_hash = hashlib.sha256(data[:1024] + str(len(data)).encode()).hexdigest()
        r = await api.post(
            "/uploads/init",
            headers={**headers, "Content-Type": "application/json"},
            json={"filename": "manual-test.bin", "file_size": len(data),
                  "content_type": "application/octet-stream", "file_hash": file_hash},
        )
        step("POST /uploads/init", r.status_code == 201, f"status={r.status_code}")
        init = r.json()
        task_id = init["task_id"]
        chunk_size = init["chunk_size"]
        total = init["total_parts"]
        print(f"        task_id={task_id} chunk_size={chunk_size} total_parts={total} instant={init.get('instant')}")

        # 4) presign
        r = await api.post(
            f"/uploads/{task_id}/presign",
            headers={**headers, "Content-Type": "application/json"},
            json={"part_numbers": list(range(1, total + 1))},
        )
        step("POST /uploads/{id}/presign", r.status_code == 200, f"status={r.status_code}")
        urls = {p["part_number"]: p["url"] for p in r.json()["parts"]}

        # 5) 分片直传 OSS（绝不带 Content-Type）
        for n in range(1, total + 1):
            start = (n - 1) * chunk_size
            chunk = data[start:start + chunk_size]
            rr = await oss_client.put(urls[n], content=chunk)
            step(f"OSS PUT part {n}", rr.status_code == 200,
                 f"status={rr.status_code} etag={(rr.headers.get('ETag') or '')[:16]}")

        # 6) 批量回报 ETag
        parts = []
        for n in range(1, total + 1):
            start = (n - 1) * chunk_size
            chunk = data[start:start + chunk_size]
            etag = (await oss_client.put(urls[n], content=chunk)).headers.get("ETag", "").strip('"')
            parts.append({"part_number": n, "etag": etag, "size": len(chunk)})
        r = await api.post(
            f"/uploads/{task_id}/parts/callback",
            headers={**headers, "Content-Type": "application/json"},
            json={"parts": parts},
        )
        step("POST /uploads/{id}/parts/callback", r.status_code == 200, str(r.json()))

        # 7) complete
        r = await api.post(
            f"/uploads/{task_id}/complete",
            headers={**headers, "Content-Type": "application/json"},
            json={"parts": parts},
        )
        step("POST /uploads/{id}/complete", r.status_code == 200, f"status={r.status_code}")
        file_id = (r.json().get("file") or {}).get("id")
        print(f"        file_id={file_id} object_key={(r.json().get('file') or {}).get('object_key')}")

        # 8) 下载地址 + 下载比对
        r = await api.get(f"/files/{file_id}/download-url", headers=headers)
        step("GET /files/{id}/download-url", r.status_code == 200)
        dl = (r.json() or {}).get("url")
        if dl:
            dlr = await oss_client.get(dl)
            step("下载内容大小一致", dlr.status_code == 200 and len(dlr.content) == len(data),
                 f"len={len(dlr.content)} expected={len(data)}")

        # 9) 删除
        r = await api.delete(f"/files/{file_id}", headers=headers)
        step("DELETE /files/{id}", r.status_code in (200, 204), f"status={r.status_code}")

    print(f"\n现场验证结果：{PASS} 通过 / {FAIL} 失败")


if __name__ == "__main__":
    asyncio.run(main())
