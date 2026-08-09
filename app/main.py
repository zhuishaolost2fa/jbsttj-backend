"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import system
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, setup_logging
from app.services.supabase import supabase

settings = get_settings()
logger = logging.getLogger("app")

DESCRIPTION = """
基于 **FastAPI + 阿里云 OSS + Supabase** 的文件上传服务。

### 分片上传流程

1. `POST /api/v1/uploads/init` —— 提交文件名、大小、内容指纹，拿到 `task_id` 与 `chunk_size`
2. `POST /api/v1/uploads/{task_id}/presign` —— 批量换取分片的预签名直传地址
3. 前端并发 `PUT` 分片到 OSS（**Content-Type 必须为 `application/octet-stream`**）
4. `POST /api/v1/uploads/{task_id}/parts/callback` —— 批量回报 ETag
5. `POST /api/v1/uploads/{task_id}/complete` —— 合并分片，落库返回文件信息

断网后重新调用 `init`（携带同样的 `file_hash`）即可断点续传；
文件内容此前已上传过时直接命中秒传，`instant=true`。

### 鉴权

所有业务接口都需要 `Authorization: Bearer <supabase_access_token>`。
"""

tags_metadata = [
    {"name": "鉴权", "description": "登录、注册、身份查询"},
    {"name": "分片上传", "description": "大文件分片上传全流程"},
    {"name": "文件管理", "description": "文件列表、下载地址、删除、小文件直传"},
    {
        "name": "剧本杀选项",
        "description": "玩法 / 题材 / 发行方式 / 难度 / 人数 / 时长等筛选维度字典，公开只读",
    },
    {
        "name": "DM 主持人手册",
        "description": (
            "主持人手册 PDF 的解析入库与语义检索。\n\n"
            "手册经「结构化提取 → 语义分块 → 问答对生成 → 向量化」写入 pgvector，"
            "主持人带本时可以用自然语言即时查规则，命中结果带章节路径与原文页码。\n\n"
            "解析是异步的，触发后用进度接口轮询。"
        ),
    },
    {"name": "系统", "description": "健康检查与配置自检"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.debug)
    logger.info("启动 %s [%s]", settings.app_name, settings.app_env)

    missing = settings.missing_required()
    if missing:
        logger.warning("以下关键配置缺失，相关功能不可用: %s", ", ".join(missing))

    await supabase.startup()
    yield
    await supabase.shutdown()
    logger.info("服务已关闭")


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version=system.VERSION,
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id", "Content-Range"],
    max_age=3600,
)
app.add_middleware(RequestContextMiddleware)

register_exception_handlers(app)

app.include_router(system.router)
app.include_router(api_router, prefix=settings.api_prefix)

# 挂载前端演示页：启动后访问 http://127.0.0.1:8000/ 即可试用分片上传
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.is_dir():
    app.mount("/demo", StaticFiles(directory=str(_frontend_dir), html=True), name="demo")

    @app.get("/", include_in_schema=False)
    async def _index() -> RedirectResponse:
        return RedirectResponse(url="/demo/")

else:

    @app.get("/", include_in_schema=False)
    async def _root() -> RedirectResponse:
        return RedirectResponse(url="/docs")
