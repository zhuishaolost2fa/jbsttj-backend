"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    dm_guides,
    files,
    script_options,
    script_requests,
    scripts,
    sts,
    uploads,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(uploads.router)
api_router.include_router(files.router)
api_router.include_router(sts.router)
api_router.include_router(script_options.router)
# 求解析挂 /scripts/requests 前缀，必须先于 /scripts（含 /{id_or_code}）注册，
# 避免 "requests" 被单段路由误匹配成剧本 ID
api_router.include_router(script_requests.router)
api_router.include_router(scripts.router)
# 同为 /scripts 前缀，但路径都带 /dm-guide 段，与剧本自身的单段路由不冲突
api_router.include_router(dm_guides.router)
# 扁平问答接口：POST /dm-guide/ask，前端只需剧本 code + 询问，无需剧本路径参数
api_router.include_router(dm_guides.ask_router)
