"""Celery 应用装配。

**broker 为什么用 RabbitMQ，backend 为什么用 Redis？**

  - *broker = RabbitMQ*：四个阶段的资源画像截然不同 —— T1 吃 CPU（PDF 解析）、
    T3/T4 吃网络与外部配额（LLM 调用）。RabbitMQ 的多队列 + 独立 worker 池
    能让「加 4 个 embedding worker」这种操作不影响解析池，
    还有 publisher confirm 保证任务不会在 broker 重启时凭空消失。
  - *backend = Redis*：chord 的汇聚计数器需要 backend 支持原子自增，
    RPC backend（amqp）做不到，官方也明确不推荐 chord 配 amqp backend。

**队列划分**

===================  ==========================================
队列                  典型瓶颈与扩缩容建议
===================  ==========================================
``dm.extract``       CPU 密集，worker 数 ≈ 物理核数
``dm.chunk``         单任务、内存密集（要汇总全书文本），并发 1~2 即可
``dm.qa``            外部 API 延迟，可开高并发，受 LLM 配额限制
``dm.embed``         同上，但请求更轻，可比 qa 再高一些
===================  ==========================================

分开队列还有个隐性好处：**避免长任务饿死短任务**。若全挤在 default 队列，
一个 20 分钟的 chunk 汇总任务会把 prefetch 到的 embed 任务一起堵住。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import get_settings

logger = logging.getLogger("app.celery")

# Celery 是可选依赖：没装时 FastAPI 主进程依然能启动，
# 只是派发流水线的接口会返回「异步能力未启用」。
try:  # pragma: no cover - 取决于运行环境
    from celery import Celery
    from celery.signals import worker_process_init, worker_process_shutdown

    _HAS_CELERY = True
except ImportError:  # pragma: no cover
    Celery = None  # type: ignore[assignment,misc]
    worker_process_init = None  # type: ignore[assignment]
    worker_process_shutdown = None  # type: ignore[assignment]
    _HAS_CELERY = False


QUEUE_EXTRACT = "dm.extract"
QUEUE_CHUNK = "dm.chunk"
QUEUE_QA = "dm.qa"
QUEUE_EMBED = "dm.embed"
QUEUE_DEFAULT = "default"

ALL_QUEUES = (QUEUE_EXTRACT, QUEUE_CHUNK, QUEUE_QA, QUEUE_EMBED, QUEUE_DEFAULT)


def _build_app() -> Any:
    if not _HAS_CELERY:
        return None

    settings = get_settings()
    app = Celery(
        "jbsttj",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.tasks.dm_ingest"],
    )

    app.conf.update(
        # ---------- 序列化 ----------
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # pickle 能传对象但也能反序列化出任意代码，broker 一旦被入侵就是 RCE。
        # 所以全链路只走 JSON，任务间传的都是 dict —— 这也是各数据类都实现
        # to_dict/from_dict 的原因。
        timezone="Asia/Shanghai",
        enable_utc=True,

        # ---------- 路由 ----------
        task_default_queue=QUEUE_DEFAULT,
        task_routes={
            "dm.extract_shard": {"queue": QUEUE_EXTRACT},
            "dm.prepare_document": {"queue": QUEUE_EXTRACT},
            "dm.chunk_and_dedup": {"queue": QUEUE_CHUNK},
            "dm.generate_qa": {"queue": QUEUE_QA},
            "dm.embed_and_store": {"queue": QUEUE_EMBED},
            # finalize / on_pipeline_error 是流水线的「收尾 / 失败落库」控制任务。
            # 必须用「确实有 worker 在消费」的队列，否则任务进 orphan 队列永远不执行：
            # 成功时 job 卡在 embedding 不翻 completed，失败时 job 永远不翻 failed。
            # 早期版本路由到 default，但 4 个 worker 都各自独占一条队列、无人消费 default，
            # 导致所有任务卡在 embedding。这里改挂到 dm.chunk 控制队列（worker_chunk 常驻）。
            "dm.finalize": {"queue": QUEUE_CHUNK},
            "dm.on_pipeline_error": {"queue": QUEUE_CHUNK},
        },

        # ---------- 可靠性 ----------
        # late ack：任务执行完才确认。worker 被 kill 时任务会重回队列，
        # 代价是任务必须幂等 —— 我们靠 content_hash 唯一约束保证了这点。
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=settings.celery_worker_prefetch,
        task_soft_time_limit=settings.celery_task_soft_time_limit,
        task_time_limit=settings.celery_task_time_limit,

        # ---------- 结果 ----------
        result_expires=86400,
        # chord 汇聚时不把子任务结果全塞进内存，避免 400 页的文本把 Redis 撑爆
        result_chord_join_timeout=3600.0,
        result_chord_retry_interval=2.0,

        # ---------- broker ----------
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            # 可见性超时要大于最长任务耗时，否则任务会被重复投递
            "visibility_timeout": settings.celery_task_time_limit + 300,
            "confirm_publish": True,
        },
        # worker 处理 N 个任务后自杀重建，兜住 PyMuPDF 长跑可能的内存碎片
        worker_max_tasks_per_child=64,
        worker_send_task_events=True,
        task_send_sent_event=True,

        # ---------- 测试 ----------
        task_always_eager=settings.celery_eager,
        task_eager_propagates=settings.celery_eager,
    )

    return app


celery_app = _build_app()


if _HAS_CELERY and celery_app is not None:  # pragma: no cover - 仅在 worker 内生效

    @worker_process_init.connect
    def _init_worker_process(**_kwargs: Any) -> None:
        """prefork 子进程启动时重置各类连接池。

        父进程在 fork 前可能已经建好了 httpx 连接。子进程继承到的是同一个
        socket fd，多个进程往同一条连接上读写会串包，症状是随机的
        「响应对不上请求」。这里主动丢弃，让每个子进程各自新建。
        """
        from app.services.dm_store import reset_dm_store
        from app.services.llm import reset_llm_client

        reset_llm_client()
        reset_dm_store()
        logger.debug("worker 子进程连接池已重置")

    @worker_process_shutdown.connect
    def _shutdown_worker_process(**_kwargs: Any) -> None:
        from app.services.dm_store import reset_dm_store
        from app.services.llm import reset_llm_client

        reset_llm_client()
        reset_dm_store()


def get_celery_app() -> Optional[Any]:
    """获取 Celery 应用，未安装 celery 时返回 None。"""
    return celery_app


def celery_available() -> bool:
    return _HAS_CELERY and celery_app is not None
