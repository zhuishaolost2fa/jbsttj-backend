"""本地联调 Celery worker 启动脚本: python run_worker.py

与线上一致：**单 worker（threads 池）消费全部队列**（supervisord.conf 的
[program:worker] 同样 --pool=threads 消费 dm.extract,dm.chunk,dm.qa,dm.embed,default），
本地调试时日志集中在一处、内存占用也贴合免费档（约 450MB）。

Windows 注意事项：
  - Celery 默认的 prefork 池在 Windows 上不可用，必须用 threads / solo；
  - 本脚本默认 ``--pool=threads --concurrency=6``，IO 密集的 qa/embed 阶段够用
    （LLM 客户端内部有 llm_max_concurrency=4 的信号量闸门，不会把配额打爆）；
  - 若调试 PDF 解析（CPU 密集 + PyMuPDF 在子线程不稳定），可改用 solo：
        set LOCAL_WORKER_POOL=solo && python run_worker.py

可用环境变量覆盖默认值：
  LOCAL_WORKER_QUEUES       消费的队列，默认全部 5 条
  LOCAL_WORKER_POOL         执行池，默认 threads
  LOCAL_WORKER_CONCURRENCY  并发数，默认 6
"""

import os
import sys

from app.tasks.celery_app import ALL_QUEUES, celery_app

if __name__ == "__main__":
    if celery_app is None:
        print("celery 未安装，无法启动 worker（pip install -r requirements.txt）")
        sys.exit(1)

    queues = os.environ.get("LOCAL_WORKER_QUEUES", ",".join(ALL_QUEUES))
    pool = os.environ.get("LOCAL_WORKER_POOL", "threads")
    concurrency = os.environ.get("LOCAL_WORKER_CONCURRENCY", "6")

    argv = [
        "worker",
        # 注意：短选项不能用等号写法（-Q=xxx 会把 "=" 吞进队列名，
        # 导致 worker 绑到名为 "=dm.extract" 的假队列、真实任务永远无人消费），
        # 一律用「选项与值分两个 token」的形式传参。
        "-Q", queues,
        "--pool", pool,
        "--concurrency", concurrency,
        "--loglevel=info",
        "-n", "local@%h",
        # 与线上 supervisord 一致：长任务必须 prefetch=1，配置里已设
        "--without-gossip",
        "--without-mingle",
    ]
    # 允许命令行追加覆盖，如 python run_worker.py -Q dm.qa
    argv.extend(sys.argv[1:])
    celery_app.worker_main(argv)
