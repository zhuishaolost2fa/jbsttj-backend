"""一键把本地 DM 手册 PDF 灌进 RAG 流水线（开发/验证用）。

流程：
  1. 把本地 PDF 上传到你配置的阿里云 OSS（jbs-store）；
  2. 在 Supabase `scripts` 表建/更新一个剧本行，把 PDF 定位信息写进
     `extra.dmGuide`（这样外键约束满足，且 `maybe_trigger` 逻辑一致）；
  3. 调用 DMGuideService.trigger_ingest，在 CELERY_EAGER 模式下同步跑完
     整条流水线（OCR 兜底 → 分块去重 → LLM 生成问答 → 向量化入库）。

用法：
  python scripts/drive_dm_ingest.py                # 默认用桌面那份 PDF
  python scripts/drive_dm_ingest.py --pdf 路径.pdf # 指定文件
  python scripts/drive_dm_ingest.py --force       # 强制重跑（覆盖已有索引）

注意：脚本会真实写入你的 OSS / Supabase / 调用 SiliconFlow 与阿里云 OCR，
属于真实操作。.env 里 CELERY_EAGER 应为 true（本地无 broker 时）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.schemas.dm_guide import IngestRequest
from app.services.dm_service import DMGuideService
from app.services.dm_store import DMStore
from app.services.oss import OSSService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("drive_dm_ingest")

DEFAULT_PDF = r"C:\Users\Administrator\Desktop\剧本杀文档\病娇男孩的精分日记..pdf"
CODE = "dm-bingjiao-nanhai"
TITLE = "病娇男孩的精分日记（主持人手册 DM 测试）"


async def upload_pdf(s, pdf_path: str) -> tuple[str, str, int]:
    key = f"dm-guides/{CODE}.pdf"
    with open(pdf_path, "rb") as f:
        data = f.read()
    size = len(data)
    logger.info("上传 PDF 到 OSS：%s（%.2f MB）", key, size / 1024 / 1024)
    meta = await OSSService(s).put_object(key, data, "application/pdf")
    logger.info("OSS 上传完成 etag=%s", meta.etag)
    return key, os.path.basename(pdf_path), size


def ensure_script_row(store: DMStore, key: str, file_name: str, size: int) -> str:
    dm_guide = {"objectKey": key, "fileName": file_name, "fileSize": size}
    existing = store.client.get(
        "/scripts", params={"code": f"eq.{CODE}", "select": "id,extra"}
    ).json()
    if existing:
        sid = existing[0]["id"]
        extra = existing[0].get("extra") or {}
        extra["dmGuide"] = dm_guide
        store.client.patch(
            "/scripts",
            params={"id": f"eq.{sid}"},
            json={"title": TITLE, "extra": extra},
        )
        logger.info("复用已有剧本行 id=%s code=%s", sid, CODE)
    else:
        resp = store.client.post(
            "/scripts",
            json={
                "code": CODE,
                "title": TITLE,
                "status": "published",
                "extra": {"dmGuide": dm_guide},
            },
            headers={"Prefer": "return=representation"},
        )
        sid = resp.json()[0]["id"]
        logger.info("新建剧本行 id=%s code=%s", sid, CODE)
    return sid


async def main(pdf_path: str, force: bool) -> None:
    s = get_settings()
    if not s.dm_rag_enabled:
        raise SystemExit("dm_rag_enabled=False，请检查 .env 配置")
    logger.info("celery_eager=%s（必须为 true 才能本地同步跑）", s.celery_eager)

    key, file_name, size = await upload_pdf(s, pdf_path)
    store = DMStore(s)
    sid = ensure_script_row(store, key, file_name, size)
    store.close()

    script = SimpleNamespace(id=sid, title=TITLE, extra={"dmGuide": {
        "objectKey": key, "fileName": file_name, "fileSize": size}})

    logger.info("触发解析流水线 job（eager 模式，将同步跑完）...")
    svc = DMGuideService(s)
    result = await svc.trigger_ingest(
        script, payload=IngestRequest(force=force), user_id=str(uuid.uuid4())
    )
    logger.info("触发结果：%s", result.model_dump())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--force", action="store_true", help="强制重跑，覆盖已有索引")
    args = ap.parse_args()
    if not os.path.exists(args.pdf):
        raise SystemExit(f"PDF 不存在：{args.pdf}")
    asyncio.run(main(args.pdf, args.force))
