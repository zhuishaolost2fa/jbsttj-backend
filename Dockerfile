# 后端镜像：FastAPI API + 4 个 Celery worker，单容器由 supervisord 编排。
# 适用：Railway / Render / Fly.io / 任意支持 Docker 的平台。
# RabbitMQ 与 Redis 由外部托管（CloudAMQP / Upstash），通过环境变量注入，
# 因此镜像内不打包 .env，避免把本地 127.0.0.1 配置带到生产。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pip 源可换（国内服务器直连 pypi 经常超时）。构建时传：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# Debian 官方源 deb.debian.org 在国内经常超时卡住构建。
# 国内服务器构建时传 --build-arg APT_MIRROR=mirrors.cloud.tencent.com（或 tuna），
# 留空则保持官方源（海外 CI 用）。compose 里的默认值已是腾讯云源。
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi

# PyMuPDF 自带 MuPDF 引擎，但图像旋转/解码偶尔会用到系统图形库，装一份无副作用
#
# Check-Valid-Until=false 不是随手加的：国内镜像站的 debian-security 仓库同步经常滞后，
# 实测腾讯云镜像站 2026-09-08 时 trixie-security 的 InRelease 还是 08-31 的，已过期 5 小时，
# apt 直接 exit 100 让整个构建失败。装的只是 libgl1/libglib2.0-0 这类基础库，
# 元数据晚几天不影响正确性，不值得为此把构建卡死。
RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3 update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 单容器内多进程管理（web + 4 个队列 worker）
RUN pip install supervisor

COPY . .

# 兜底：即便 .dockerignore 哪天被改漏，也绝不让本地密钥留在镜像层里
RUN rm -f .env .env.local .env.* && find /app -name '*.pyc' -delete 2>/dev/null || true

EXPOSE 8000

CMD ["supervisord", "-c", "/app/supervisord.conf"]
