"""统一异常体系与全局异常处理器。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.error")


class AppError(Exception):
    """业务异常基类，所有对外错误都会被序列化成统一结构。"""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "请求错误"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Any = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)

    def to_dict(self) -> dict:
        body: dict = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return {"error": body}


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "身份认证失败"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "没有权限访问该资源"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "资源不存在"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "资源状态冲突"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"
    message = "参数校验失败"


class StorageError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "storage_error"
    message = "对象存储操作失败"


class DatabaseError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "database_error"
    message = "数据库操作失败"


class ConfigError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "服务配置不完整"


class LLMError(AppError):
    """大模型 / 向量化服务调用失败。

    与 StorageError 一样归到 502：责任方是上游供应商，不是调用方的参数问题，
    客户端可以原样重试。限流（429）也归到这里，但会在 details 里带上 retry_after，
    让 Celery 任务据此决定退避时长。
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "llm_error"
    message = "大模型服务调用失败"


def _json_safe(obj: Any) -> Any:
    """把 pydantic 的错误结构转成可 JSON 序列化。

    pydantic v2 的 `ValidationError.errors()` 会在 `ctx` 里塞入原始的异常实例
    （如 model_validator 里 `raise ValueError(...)` 的对象），直接 `json.dumps`
    会抛 `TypeError`。递归地把这些非基础类型降级成字符串，既避免 500，又保留
    了「player_min 与 player_max 必须同时提供」这类可读信息。
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("AppError %s: %s", exc.code, exc.message, exc_info=exc)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "参数校验失败",
                    "details": _json_safe(exc.errors()),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": f"http_{exc.status_code}", "message": str(exc.detail)}},
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "internal_error", "message": "服务器内部错误"}},
        )
