"""日志与请求追踪。"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(request_id)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # 降低第三方库噪音
    for noisy in ("httpx", "httpcore", "alibabacloud_oss_v2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 request-id 并记录耗时。"""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            cost = (time.perf_counter() - started) * 1000
            logging.getLogger("app.access").info(
                "%s %s - %.1fms", request.method, request.url.path, cost
            )
            request_id_ctx.reset(token)
        response.headers["X-Request-Id"] = rid
        return response
