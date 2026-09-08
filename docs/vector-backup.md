# pgvector 备份与恢复

> 适用：自建 pgvector（容器 `jbs-pgvector`，库 `jbsvector`）。
> 背景：`script_dm_chunks` / `script_dm_qa` 已从 Supabase 下沉到本地 pgvector，
> 其余表仍在 Supabase（由 Supabase 自己托管备份）。**本地 pgvector 没有副本**，
> 需要自己兜底。

## 一、备份什么

| 对象 | 位置 | 备份方式 |
|---|---|---|
| `script_dm_chunks`、`script_dm_qa`、`dm_documents` | 自建 pgvector | **本方案**（`scripts/backup_pgvector.sh`） |
| documents / jobs / stories / highlights / questions / scripts | Supabase | Supabase 托管，无需自建 |

## 二、自动备份

脚本：`scripts/backup_pgvector.sh`（部署到服务器 `/opt/jbs/scripts/`）。

每天凌晨 3:30 执行：

```cron
30 3 * * * /opt/jbs/scripts/backup_pgvector.sh >> /var/log/jbs-backup.log 2>&1
```

装定时任务（服务器上）：

```bash
(crontab -l 2>/dev/null; echo '30 3 * * * /opt/jbs/scripts/backup_pgvector.sh >> /var/log/jbs-backup.log 2>&1') | crontab -
crontab -l
```

行为：

- `pg_dump -Fc` 自定义格式（**已压缩**，80MB 库 → 约 4.3MB），在事务内取一致性快照，
  **流水线正在写入时备份也不会拿到半截数据**；
- 先写 `.part` 再 rename，且校验文件头为 `PGDMP`，不会把失败产物当成有效备份；
- 若 API 容器配了 `OSS_*`，额外传一份到 `oss://<bucket>/backups/pgvector/`（走内网 endpoint，免流量费）；
- 本地保留 **14 天**（`KEEP_DAYS` 可覆盖）。OSS 侧建议在控制台配生命周期规则：30 天后转低频、90 天删除。

手动跑一次：

```bash
/opt/jbs/scripts/backup_pgvector.sh
SKIP_OSS=1 BACKUP_DIR=/tmp/bk /opt/jbs/scripts/backup_pgvector.sh   # 只本地、改目录试跑
```

## 三、恢复

`-Fc` 格式必须用 `pg_restore`，不能用 `psql` 直接灌。

```bash
# 1) 整库恢复到现有库（会追加/覆盖同名对象，先确认库里没有要保留的新数据）
docker exec -i jbs-pgvector pg_restore -U postgres -d jbsvector --clean --if-exists \
  < /opt/jbs/backups/jbsvector-20260908-232458.dump

# 2) 只恢复某一张表（最常用：误删了某本剧本的 chunks）
docker exec -i jbs-pgvector pg_restore -U postgres -d jbsvector \
  --table=script_dm_chunks --clean --if-exists \
  < /opt/jbs/backups/jbsvector-20260908-232458.dump

# 3) 恢复到临时库，人工比对后再搬（推荐用于不确定场景）
docker exec -i jbs-pgvector pg_restore -U postgres -d postgres -C \
  < /opt/jbs/backups/jbsvector-20260908-232458.dump   # 会建出 jbsvector_restore 之类
```

从 OSS 拉回备份（服务器硬盘也挂了的极端场景）：

```bash
docker exec jbs-api python - <<'PY'
import asyncio, sys
sys.path.insert(0, "/app")
from app.services.oss import OSSService

async def main():
    data = await OSSService().get_object("backups/pgvector/jbsvector-YYYYmmdd-HHMMSS.dump")
    open("/tmp/restore.dump", "wb").write(data)

asyncio.run(main())
PY
docker cp jbs-api:/tmp/restore.dump /opt/jbs/backups/
```

## 四、验证备份有效性

备份没验证过等于没备份。建议**每月**做一次：

```bash
# 建一个临时库，把最新备份灌进去，看能不能查到数据
docker exec jbs-pgvector psql -U postgres -c 'create database bkcheck'
docker exec -i jbs-pgvector pg_restore -U postgres -d bkcheck < /opt/jbs/backups/$(ls -t /opt/jbs/backups | head -1)
docker exec jbs-pgvector psql -U postgres -d bkcheck -c 'select count(*) from script_dm_chunks'
docker exec jbs-pgvector psql -U postgres -c 'drop database bkcheck'
```

## 五、已知约束

- 备份只覆盖 pgvector，**不含 Supabase 侧的表**，也不含 OSS 里的原始手册文件（OSS 本身有多副本）。
- `.env` 里若改了 `VECTOR_DB_DSN` 指向别处，脚本的 `VECTOR_DB` / `VECTOR_CONTAINER` 要同步改。
- 服务器可用内存约 1.1G，`pg_dump` 本身不占内存；但**不要同时跑大 PDF 的 OCR 和整库恢复**。
