"""一键把本地 DM 手册 Word(.docx) 灌进 RAG 流水线（开发/验证用）。

与 drive_dm_ingest.py（PDF 版）流程一致：
  1. 上传 docx 到阿里云 OSS（jbs-store）；
  2. 在 Supabase `scripts` 表建/更新剧本行，把文件定位写进 `extra.dmGuide`；
  3. 调用 DMGuideService.trigger_ingest，CELERY_EAGER=true 时同步跑完整条
     流水线（docx 直接解析文字层，无 OCR → 分块去重 → LLM 生成问答 → 向量化入库）。

用法：
  python scripts/drive_dm_ingest_docx.py                 # 默认桌面《豪门46山鬼母》
  python scripts/drive_dm_ingest_docx.py --docx 路径.docx
  python scripts/drive_dm_ingest_docx.py --force         # 强制重跑（清空旧索引）

注意：真实写入 OSS / Supabase 并调用 SiliconFlow，属于真实操作。
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
logger = logging.getLogger("drive_dm_ingest_docx")

DEFAULT_DOCX = r"C:\Users\Administrator\Desktop\剧本杀文档\豪门46山鬼母.docx"
CODE = "dm-haomen46-shanguimu"
TITLE = "豪门46山鬼母（主持人手册 DM 测试）"


async def upload_docx(s, docx_path: str) -> tuple[str, str, int]:
    key = f"dm-guides/{CODE}.docx"
    with open(docx_path, "rb") as f:
        data = f.read()
    size = len(data)
    logger.info("上传 DOCX 到 OSS：%s（%.2f MB）", key, size / 1024 / 1024)
    meta = await OSSService(s).put_object(
        key, data,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    logger.info("OSS 上传完成 etag=%s", meta.etag)
    return key, os.path.basename(docx_path), size


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


async def main(docx_path: str, force: bool) -> None:
    s = get_settings()
    if not s.dm_rag_enabled:
        raise SystemExit("dm_rag_enabled=False，请检查 .env 配置")
    logger.info("celery_eager=%s（必须为 true 才能本地同步跑）", s.celery_eager)

    key, file_name, size = await upload_docx(s, docx_path)
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

    # 汇总实际入库量，直接验证 script_dm_qa 是否有数据
    store = DMStore(s)
    try:
        doc = store.get_active_document(sid)
        if doc:
            doc_id = str(doc["id"])
            logger.info(
                "最终核验 doc=%s: total_chunks=%s total_qa=%s | 实际行数 chunks=%s qa=%s",
                doc_id,
                doc.get("total_chunks"), doc.get("total_qa"),
                store.count_chunks(doc_id), store.count_qa(doc_id),
            )
        else:
            logger.warning("未找到 active 文档，流水线可能未跑完")
    finally:
        store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--docx", default=DEFAULT_DOCX)
    ap.add_argument("--force", action="store_true", help="强制重跑，覆盖已有索引")
    args = ap.parse_args()
    if not os.path.exists(args.docx):
        raise SystemExit(f"DOCX 不存在：{args.docx}")
    asyncio.run(main(args.docx, args.force))
