# jbsttj-backend 项目记忆

## 环境配置要点
- `.env` 是共享基线，`.env.local` 是本地联调覆盖（gitignore）。pydantic-settings 按 `(.env, .env.local)` 顺序读取，后者优先级更高。
- `.env.local` 当前配置：`CELERY_EAGER=false`，broker 走单 Redis（`redis://127.0.0.1:6379/2`），result backend 走 `redis://127.0.0.1:6379/0`，去重指纹走 `redis://127.0.0.1:6379/1`。本地免 RabbitMQ。
- Redis 安装在 `C:\Program Files\Redis\redis-server.exe`，需手动启动（PowerShell: `Start-Process "C:\Program Files\Redis\redis-server.exe" -ArgumentList "--port","6379"`）。
- Worker 启动：`cd jbsttj-backend && .venv/Scripts/python.exe run_worker.py`（threads 池，concurrency=6，消费全部 5 条队列）。
- 若不想装 Redis，可注释掉 `.env.local` 中的 CELERY_* 三行、改回 `CELERY_EAGER=true`（注意 chord 在 eager 模式下可能仍有问题）。

## DM RAG 流水线
- Celery 任务链路：`dm.prepare_document → dm.extract_shard(×N) → dm.chunk_and_dedup → dm.generate_qa → dm.embed_and_store → dm.finalize`
- 表名前缀 `script_dm_`：`script_dm_jobs`、`script_dm_documents`、`script_dm_chunks`、`script_dm_qa`、`script_dm_questions`
- Redis/worker 未运行时，prepare_document 会把任务派发到 Redis 队列但无人消费，job 卡在 `extracting` 状态。
- Redis 重启后内存数据丢失，队列中的任务不会自动恢复，需手动重新派发（force=true）。
- 2026-08-27 新增故事还原设计（sql/dm_story.sql，待在 Supabase 执行）：`script_dm_stories`（LLM 采集还原条目，story_type 六分类，content_hash 去重，1024 维 HNSW）+ `script_dm_highlights`（用户划线评论，Web Annotation 式锚点 quote/offset/prefix/suffix，story_id on delete set null，orphaned 状态 + reanchor_dm_highlights 重锚；user_id 沿用自建鉴权无外键；visibility private/public）。jobs/documents 补 total_stories/embedded_stories，bump_dm_job_progress 已扩展（带默认参数，旧调用兼容）。流水线待改造点：T3 prompt 同时产出 story items，T4 落库+向量化，finalize/purge 后调 reanchor。
