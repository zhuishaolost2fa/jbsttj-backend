#!/usr/bin/env bash
# ============================================================
# jbsvector（自建 pgvector）备份脚本
#
# 背景：chunks / qa 两张表已从 Supabase 下沉到自建 pgvector，
#       容器卷之外没有任何副本，硬盘故障 = 全部解析产物丢失。
#
# 用法（服务器上）：
#   /opt/jbs/scripts/backup_pgvector.sh
#   BACKUP_DIR=/tmp/bk KEEP_DAYS=1 /opt/jbs/scripts/backup_pgvector.sh   # 试跑
#
# 产物：<BACKUP_DIR>/jbsvector-<YYYYmmdd-HHMMSS>.dump
#       格式 pg_dump -Fc（自定义格式，已压缩，可 pg_restore 单表恢复）
#
# 行为：
#   1. 在事务内做一致性快照（pg_dump -Fc 默认 --single-transaction 语义），
#      流水线正在写入时备份也不会拿到半截数据；
#   2. 写 .part 临时文件，成功才 rename，避免把损坏文件当成有效备份；
#   3. 若 API 容器配了 OSS_*，额外传一份到 OSS（走内网 endpoint，免流量费）；
#   4. 清理本地超过 KEEP_DAYS 天的旧备份。
#
# 还原：见 docs/vector-backup.md
# ============================================================
set -euo pipefail

CONTAINER="${VECTOR_CONTAINER:-jbs-pgvector}"
DB="${VECTOR_DB:-jbsvector}"
DB_USER="${VECTOR_USER:-postgres}"
API_CONTAINER="${API_CONTAINER:-jbs-api}"
BACKUP_DIR="${BACKUP_DIR:-/opt/jbs/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
OSS_PREFIX="${OSS_PREFIX:-backups/pgvector}"
SKIP_OSS="${SKIP_OSS:-0}"

log() { echo "[$(date '+%F %T')] $*"; }

ts="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/jbsvector-$ts.dump"
part="$out.part"

# ---------- 1. dump ----------
log "开始备份 $DB (容器 $CONTAINER) -> $out"
if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB" > "$part"; then
  rm -f "$part"
  log "!! pg_dump 失败，未产生备份"
  exit 1
fi

if [ ! -s "$part" ]; then
  rm -f "$part"
  log "!! dump 文件为空，判定失败"
  exit 1
fi

# 自定义格式以 'PGDMP' 开头，校验一下防止拿到 HTML 错误页之类的东西
if [ "$(head -c 5 "$part")" != "PGDMP" ]; then
  rm -f "$part"
  log "!! 文件头不是 PGDMP，不是有效的 pg_dump 产物"
  exit 1
fi

mv "$part" "$out"
log "备份完成: $out ($(du -h "$out" | cut -f1))"

# ---------- 2. 上传 OSS（可选） ----------
if [ "$SKIP_OSS" = "1" ]; then
  log "SKIP_OSS=1，跳过 OSS 上传"
else
  if docker exec "$API_CONTAINER" printenv OSS_BUCKET > /dev/null 2>&1; then
    oss_key="$OSS_PREFIX/$(basename "$out")"
    log "上传 OSS -> $oss_key"
    if ! docker cp "$out" "$API_CONTAINER":/tmp/jbs-vector-backup.dump > /dev/null 2>&1; then
      log "!! docker cp 失败，跳过 OSS 上传"
    else
      # 注意：docker exec 必须带 -i，否则 heredoc 传不进去，python 读到空脚本
      # 会「成功」退出 0 —— 曾经因此出现「假上传成功」。
      # 上传后立刻 head 校验大小，不一致就退出非 0，不让脚本误报成功。
      if oss_out="$(docker exec -i "$API_CONTAINER" python - "$oss_key" <<'PY' 2>&1
import asyncio
import os
import sys

sys.path.insert(0, "/app")
from app.services.oss import OSSService

KEY = sys.argv[1]
LOCAL = "/tmp/jbs-vector-backup.dump"


async def main() -> int:
    expect = os.path.getsize(LOCAL)
    svc = OSSService()
    with open(LOCAL, "rb") as f:
        data = f.read()
    await svc.put_object(KEY, data, content_type="application/octet-stream")

    # head_object 在对象不存在时返回 None（不抛异常），必须判空
    meta = await svc.head_object(KEY)
    if meta is None:
        print(f"!! 上传后校验失败：OSS 上查不到 {KEY}")
        return 1
    if meta.size != expect:
        print(f"!! 大小不符：本地 {expect} vs OSS {meta.size}")
        return 1
    print(f"uploaded {meta.key} ({meta.size} bytes) 校验一致")
    return 0


sys.exit(asyncio.run(main()))
PY
)"; then
        echo "$oss_out" | sed 's/^/    oss: /'
        log "OSS 上传完成"
      else
        echo "$oss_out" | sed 's/^/    oss: /'
        log "!! OSS 上传失败（本地备份仍在，不影响本次备份有效性）"
      fi
      docker exec "$API_CONTAINER" rm -f /tmp/jbs-vector-backup.dump > /dev/null 2>&1 || true
    fi
  else
    log "API 容器未配置 OSS_BUCKET，跳过上传（仅本地备份）"
  fi
fi

# ---------- 3. 清理旧备份 ----------
deleted="$(find "$BACKUP_DIR" -name 'jbsvector-*.dump' -type f -mtime "+$KEEP_DAYS" -print -delete | wc -l)"
log "清理完成：删除 $deleted 个超过 ${KEEP_DAYS} 天的备份"
log "当前备份清单："
ls -lh "$BACKUP_DIR" | sed 's/^/    /'
