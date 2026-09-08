#!/usr/bin/env bash
# 服务器端部署脚本 —— 手动或由 GitHub Actions 调用，线上线下走同一条路径。
#
#   ./scripts/deploy.sh              拉最新代码 → 构建 → 重启 → 健康检查（失败自动回滚）
#   ./scripts/deploy.sh --no-build   只拉代码 + 重启（改了 .env / nginx.conf 时用）
#   ./scripts/deploy.sh --force      代码没变也强制重建
#
# 前置：/opt/jbs 必须是 git 仓库（git clone 出来的），且 .env 已在 .gitignore 里。

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/jbs}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BRANCH="${BRANCH:-main}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/ready}"

cd "$APP_DIR"

BUILD=1
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --no-build) BUILD=0 ;;
        --force)    FORCE=1 ;;
    esac
done

if [ ! -f .env ]; then
    echo "::error:: $APP_DIR/.env 不存在，部署中止" >&2
    exit 1
fi

PREV_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
echo "当前版本: ${PREV_SHA:0:8}"

# ---------------------------------------------------------------- 1. 拉代码
if [ -n "$PREV_SHA" ]; then
    git fetch --quiet origin "$BRANCH"
    git checkout --quiet "$BRANCH"
    # 服务器上不该有本地改动；有就让部署失败而不是被 merge 覆盖，避免「以为部署了其实没有」
    if ! git merge --ff-only --quiet "origin/$BRANCH"; then
        echo "::error:: 服务器上有未提交改动，无法快进。先处理：" >&2
        git status --short >&2
        exit 1
    fi
fi
NEW_SHA=$(git rev-parse HEAD)
echo "目标版本: ${NEW_SHA:0:8}"

rollback() {
    echo "::error:: 部署失败，回滚到 ${PREV_SHA:0:8}" >&2
    if [ -n "$PREV_SHA" ] && [ "$PREV_SHA" != "$NEW_SHA" ]; then
        git checkout --quiet "$PREV_SHA"
        docker compose -f "$COMPOSE_FILE" --env-file .env build api worker
        docker compose -f "$COMPOSE_FILE" --env-file .env up -d -t 300 --remove-orphans
    fi
    exit 1
}

# ---------------------------------------------------------------- 2. 构建
# 层缓存命中时这一步只要几十秒；只有 requirements.txt / Dockerfile 变了才会全量重装依赖。
if [ "$BUILD" = "1" ]; then
    if [ "$FORCE" != "1" ] && [ "$PREV_SHA" = "$NEW_SHA" ]; then
        echo "代码无变化，跳过构建（要强制重建加 --force）"
    else
        echo "== 构建镜像 =="
        docker compose -f "$COMPOSE_FILE" --env-file .env build api worker || rollback
    fi
else
    echo "== --no-build：跳过构建 =="
fi

# ---------------------------------------------------------------- 3. 重启
# -t 300 与 worker 的 stop_grace_period 对齐，给 Celery 优雅退出的时间。
docker compose -f "$COMPOSE_FILE" --env-file .env up -d -t 300 --remove-orphans || rollback

# ---------------------------------------------------------------- 4. 健康检查
OK=0
for i in $(seq 1 45); do
    if curl -fsS --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        echo "健康检查通过（${i}/45）"
        OK=1
        break
    fi
    sleep 2
done

if [ "$OK" != "1" ]; then
    docker compose -f "$COMPOSE_FILE" logs --tail=60 api >&2 || true
    rollback
fi

# ---------------------------------------------------------------- 5. 收尾
echo "$NEW_SHA" > .last_good_sha
docker compose -f "$COMPOSE_FILE" ps
docker image prune -f > /dev/null 2>&1 || true
echo "部署完成: ${NEW_SHA:0:8}"
