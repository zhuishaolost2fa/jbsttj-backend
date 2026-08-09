"""Celery 异步任务包。

导出 celery_app 供 `celery -A app.tasks worker` 直接引用。
"""

from app.tasks.celery_app import celery_app

__all__ = ["celery_app"]
