#!/usr/bin/env bash
# 一次性脚本：把 /opt/jbs 从「scp 手工目录」换成 git 仓库。
# 目录名必须保持 jbs —— compose 项目名由目录名决定，改名会让 volume 名变化、向量数据「消失」。
# 注意：/opt 属 root，目录的建/删/改名用 sudo；git clone 必须用 ubuntu 身份（deploy key 在 ubuntu 的 ~/.ssh）。

set -euo pipefail

REPO="git@github.com:zhuishaolost2fa/jbsttj-backend.git"
BRANCH="main"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="/opt/jbs.bak.$TS"

echo "== 0. 校验 GitHub 可达 =="
if ! timeout 25 git ls-remote "$REPO" > /dev/null 2>&1; then
    echo "::error:: 连不上 GitHub —— 确认 Deploy Key 已加到仓库（Settings → Deploy keys）"
    exit 1
fi
echo "GitHub 可达 ✓"

echo "== 1. 停容器（数据卷保留）=="
cd /opt/jbs
docker compose -f docker-compose.prod.yml --env-file .env down || true

echo "== 2. 旧目录归档（sudo）=="
sudo mv /opt/jbs "$BACKUP"
echo "旧目录已归档: $BACKUP"

echo "== 3. 建目录并 clone（ubuntu 身份）=="
sudo mkdir -p /opt/jbs
sudo chown "$(whoami):$(id -gn)" /opt/jbs
git clone -b "$BRANCH" "$REPO" /opt/jbs
echo "clone 完成，HEAD=$(cd /opt/jbs && git log --oneline -1)"

echo "== 4. 恢复不在 git 里的文件 =="
cp ~/jbs-backup/.env /opt/jbs/.env
cp -r ~/jbs-backup/frontend /opt/jbs/frontend
chmod +x /opt/jbs/scripts/*.sh
echo "  .env $(wc -l < /opt/jbs/.env) 行，frontend $(find /opt/jbs/frontend -type f | wc -l) 个文件"

echo "== 5. 起容器 =="
cd /opt/jbs
if ! docker compose -f docker-compose.prod.yml --env-file .env up -d; then
    echo "::error:: 启动失败，回滚目录"
    docker compose -f docker-compose.prod.yml --env-file .env down || true
    cd /opt
    sudo rm -rf /opt/jbs
    sudo mv "$BACKUP" /opt/jbs
    cd /opt/jbs && docker compose -f docker-compose.prod.yml --env-file .env up -d
    exit 1
fi

echo "== 6. 健康检查 =="
OK=0
for i in $(seq 1 45); do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/ready > /dev/null 2>&1; then
        echo "健康检查通过（${i}/45）"
        OK=1
        break
    fi
    sleep 2
done

if [ "$OK" != "1" ]; then
    echo "::error:: 健康检查失败，回滚"
    docker compose -f docker-compose.prod.yml logs --tail=40 api || true
    docker compose -f docker-compose.prod.yml --env-file .env down || true
    cd /opt
    sudo rm -rf /opt/jbs
    sudo mv "$BACKUP" /opt/jbs
    cd /opt/jbs && docker compose -f docker-compose.prod.yml --env-file .env up -d
    exit 1
fi

echo "== 7. 校验向量数据完好 =="
docker exec jbs-pgvector psql -U postgres -d jbsvector -t -c \
    "select 'chunks=' || (select count(*) from public.script_dm_chunks) || ' qa=' || (select count(*) from public.script_dm_qa);"

echo "== 8. 容器状态 =="
docker ps --format "{{.Names}}\t{{.Status}}"
echo
echo "完成。旧目录保留在 $BACKUP，确认无误后可删：sudo rm -rf $BACKUP"
