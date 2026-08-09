# 后端镜像：FastAPI API + 4 个 Celery worker，单容器由 supervisord 编排。
# 适用：Railway / Render / Fly.io / 任意支持 Docker 的平台。
# RabbitMQ 与 Redis 由外部托管（CloudAMQP / Upstash），通过环境变量注入，
# 因此镜像内不打包 .env，避免把本地 127.0.0.1 配置带到生产。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# PyMuPDF 自带 MuPDF 引擎，但图像旋转/解码偶尔会用到系统图形库，装一份无副作用
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 单容器内多进程管理（web + 4 个队列 worker）
RUN pip install supervisor

COPY . .

EXPOSE 8000

CMD ["supervisord", "-c", "/app/supervisord.conf"]
