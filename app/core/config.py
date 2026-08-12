"""应用配置：全部通过环境变量 / .env 注入。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 应用 ----------------
    app_name: str = "File Upload Service"
    app_env: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # ---------------- Supabase ----------------
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwks_url: str = ""
    supabase_jwt_audience: str = "authenticated"

    # ---------------- 阿里云 OSS ----------------
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_endpoint: str = ""
    oss_region: str = ""
    oss_bucket: str = ""
    oss_signature_version: str = "v4"
    oss_internal_endpoint: str = ""
    oss_cdn_domain: str = ""
    oss_use_cname: bool = False

    # ---------------- 阿里云 OCR（文字识别，处理扫描件 PDF）----------------
    # 默认复用 OSS 的 AccessKey（同一阿里云账号），无需单独申请密钥。
    # 但必须在阿里云控制台「开通文字识别服务」(产品名：文字识别 / ocr-api) 才能调用，
    # 否则流水线在扫描件上会报「纯扫描件需要 OCR」。
    ocr_access_key_id: str = ""        # 留空 → 复用 oss_access_key_id
    ocr_access_key_secret: str = ""    # 留空 → 复用 oss_access_key_secret
    ocr_endpoint: str = "ocr-api.cn-hangzhou.aliyuncs.com"
    ocr_type: str = "General"          # General=通用印刷体基础版；Advanced=高精版
    ocr_dpi: int = 130                 # 扫描页渲染成 PNG 的 DPI（越大越准但越慢；内部还会按最长边封顶）

    # ---------------- 本地免费 OCR（已取代阿里云 OCR，离线运行）----------------
    # 阿里云 OCR 已禁用：OCR_ENGINE 不再接受 aliyun 取值，默认改为本地引擎。
    # rapid=RapidOCR(ONNX，轻量中文准)；paddle=PaddleOCR(精度最高但依赖 paddlepaddle，体积大)；
    # tesseract=Tesseract(超轻量，中文版式弱)。对应依赖需在 requirements 中自行启用，
    # 未安装时 OCR 会优雅跳过（扫描页拿不到文字层，不再触发任何对外请求）。
    ocr_engine: str = "rapid"
    # LibreOffice 可执行文件路径，用于把旧版二进制 .doc 本地转成 .docx；留空自动探测 soffice/libreoffice
    libreoffice_path: str = ""

    # ---------------- 阿里云 OSS（STS 临时凭证接入）----------------
    # 开启后，服务端用下方「长期 AccessKey」调用 STS AssumeRole 扮演
    # OSS_STS_ROLE_ARN 指定的 RAM 角色，拿到短期临时凭证再去访问 OSS。
    # 长期密钥永不下发到浏览器，安全性优于直配长期密钥。
    oss_use_sts: bool = False
    # 调用 STS 的长期 AccessKey；缺省复用 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET
    oss_sts_access_key_id: str = ""
    oss_sts_access_key_secret: str = ""
    oss_sts_role_arn: str = ""
    oss_sts_session_name: str = "file-upload-service"
    oss_sts_duration_seconds: int = 3600  # AssumeRole 会话时长，最大通常 3600（取决于角色配置）
    oss_sts_endpoint: str = "sts.aliyuncs.com"

    # ---------------- 上传策略 ----------------
    upload_prefix: str = "uploads"
    # 临时文件专用前缀：对象落在此前缀下，便于用 OSS 生命周期规则单独过期清理；
    # 永久文件走 upload_prefix，不被自动清理。
    temp_upload_prefix: str = "temp"
    upload_chunk_size: int = 5 * 1024 * 1024
    max_file_size: int = 5 * 1024 * 1024 * 1024
    max_part_count: int = 10000
    presign_expire_seconds: int = 3600
    download_url_expire_seconds: int = 1800
    max_presign_batch: int = 200
    allowed_extensions: str = ""
    blocked_extensions: str = "exe,bat,cmd,sh,com,scr,msi,dll,jar"

    # ---------------- 服务间调用 ----------------
    service_api_key: str = ""

    # ---------------- Celery / RabbitMQ / Redis ----------------
    # broker 走 RabbitMQ（可靠投递、支持多队列独立扩缩容），
    # result backend 走 Redis（chord 汇聚要靠它做计数器，RPC backend 做不了）。
    celery_broker_url: str = "amqp://guest:guest@127.0.0.1:5672//"
    celery_result_backend: str = "redis://127.0.0.1:6379/0"
    # 去重指纹的全局集合也放 Redis，跨 shard、跨任务共享
    celery_redis_url: str = "redis://127.0.0.1:6379/1"
    celery_task_soft_time_limit: int = 1500  # 单任务软超时（秒），超时抛异常可捕获收尾
    celery_task_time_limit: int = 1800  # 硬超时，直接杀进程
    celery_worker_prefetch: int = 1  # 长任务必须设 1，否则任务会堆在某个 worker 上排队
    celery_eager: bool = False  # 开启后任务同步执行，供测试用

    # ---------------- 硅基流动（SiliconFlow）----------------
    # OpenAI 兼容协议，chat 与 embedding 共用同一个 base_url 与 API Key
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_chat_model: str = "Qwen/Qwen2.5-72B-Instruct"
    siliconflow_embed_model: str = "BAAI/bge-large-zh-v1.5"
    # bge-large-zh-v1.5 固定 1024 维，改模型时必须同步改 SQL 里的 vector(N)
    embedding_dim: int = 1024
    # 硅基流动 embeddings 接口单次批量上限，超过会 400
    embedding_batch_size: int = 32
    # 单条文本送 embedding 前的最大字符数。bge-large-zh-v1.5 的 token 上限约
    # 512，且硅基流动对超长输入直接返回 400（而非服务端截断），所以这里在客户端
    # 按字符预留余量截断。中文约 1 字 1 token，取 480 留足安全边界。
    embedding_max_chars: int = 480
    llm_request_timeout: int = 120
    llm_max_retries: int = 3
    # 单个 worker 进程内对 LLM 的并发上限，防止把配额瞬间打满触发 429
    llm_max_concurrency: int = 4

    # ---------------- DM 指南 RAG 流水线 ----------------
    # 每个提取任务负责多少页：太小则任务调度开销占比高，太大则单任务耗时长、失败重试代价大
    dm_extract_pages_per_shard: int = 20
    # Word(.docx) 提取没有「页」概念，分片以文本单元（段落+表格行）为单位。
    # 每个提取任务负责的单元数；Word 解析是纯文本、无 OCR/渲染，可以比 PDF 的 20 页更大。
    dm_extract_blocks_per_shard: int = 400
    # 把 Word 无页码的文本单元映射成「伪页码」：每 N 个单元算一页，
    # 用于兼容下游基于 page 的跨片续接与检索结果出处展示。
    dm_docx_blocks_per_page: int = 40
    # 结构化粗分的目标块大小（字符数）与重叠
    dm_chunk_size: int = 800
    dm_chunk_overlap: int = 120
    # 超过该长度的粗块才送去做语义细分，控制 embedding 调用成本
    dm_semantic_split_threshold: int = 1600
    # 语义分块的断点分位数，越大切得越少
    dm_semantic_breakpoint_percentile: int = 88
    # 小于该长度的块直接丢弃（多为残留页码、孤立标点）
    dm_min_chunk_chars: int = 40
    # SimHash 汉明距离阈值，<= 该值判为近似重复
    dm_simhash_threshold: int = 3
    # 页眉页脚判定：同一文本在超过该比例的页面重复出现即视为版式噪声
    dm_header_footer_ratio: float = 0.6
    # 每批送给 LLM 生成问答对的块数
    dm_qa_batch_size: int = 6
    # 每个块期望生成的问答对数量上限（提示词已要求「尽可能多」、
    # 多角度生成；此处为安全上限，避免单批 token 爆炸）
    dm_qa_per_chunk: int = 5
    # 检索默认返回条数与相似度下限
    dm_search_top_k: int = 8
    dm_search_min_similarity: float = 0.25
    # hybrid 模式「以 qa 为主召回」：qa 取满 top_k 做主答案来源，
    # chunk 仅作出处佐证、配额收紧到这个上限（默认 3）。
    dm_search_qa_supplement_k: int = 3
    # 合并为扁平 hits 时给 qa 相似度乘的权重，使其稳定排在 chunk 之前。
    dm_search_qa_boost: float = 1.12
    # PDF 本地缓存目录，同机多 worker 复用同一份下载
    dm_cache_dir: str = ""
    dm_max_pdf_bytes: int = 200 * 1024 * 1024

    # OSS 单个分片的硬性下限（最后一片除外）
    min_part_size: int = Field(default=100 * 1024, exclude=True)

    # ---------------- 派生属性 ----------------
    @property
    def cors_origin_list(self) -> List[str]:
        return _split_csv(self.cors_origins) or ["*"]

    @property
    def allowed_extension_set(self) -> set[str]:
        return {e.lower().lstrip(".") for e in _split_csv(self.allowed_extensions)}

    @property
    def blocked_extension_set(self) -> set[str]:
        return {e.lower().lstrip(".") for e in _split_csv(self.blocked_extensions)}

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def supabase_rest_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/rest/v1"

    @property
    def supabase_auth_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return ""

    @property
    def oss_write_endpoint(self) -> str:
        """服务端自身访问 OSS 时使用的 endpoint，优先内网。"""
        return self.oss_internal_endpoint or self.oss_endpoint

    @property
    def oss_public_endpoint(self) -> str:
        """签发给浏览器的 URL 所用 endpoint，必须是公网可达的。"""
        return self.oss_cdn_domain or self.oss_endpoint

    @property
    def dm_cache_path(self) -> str:
        """PDF 下载缓存目录，未配置时落到系统临时目录。"""
        if self.dm_cache_dir:
            return self.dm_cache_dir
        import tempfile

        return str(Path(tempfile.gettempdir()) / "jbs-dm-cache")

    @property
    def dm_rag_enabled(self) -> bool:
        """RAG 流水线是否具备最低运行条件。缺 Key 时接口要明确报错而不是静默失败。"""
        return bool(self.siliconflow_api_key and self.supabase_url and self.supabase_service_role_key)

    def missing_rag_config(self) -> List[str]:
        """返回 RAG 流水线缺失的配置项，供 /ready 与触发接口给出可操作提示。"""
        required = {
            "SILICONFLOW_API_KEY": self.siliconflow_api_key,
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
            "CELERY_BROKER_URL": self.celery_broker_url,
            "CELERY_RESULT_BACKEND": self.celery_result_backend,
        }
        return [k for k, v in required.items() if not v]

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.upload_chunk_size < self.min_part_size:
            raise ValueError(f"UPLOAD_CHUNK_SIZE 不能小于 {self.min_part_size} 字节")
        if self.max_part_count > 10000:
            raise ValueError("MAX_PART_COUNT 不能超过 OSS 上限 10000")
        if self.oss_signature_version.lower() not in {"v1", "v4"}:
            raise ValueError("OSS_SIGNATURE_VERSION 只能是 v1 或 v4")
        if self.oss_use_sts:
            if not self.oss_sts_role_arn:
                raise ValueError("开启 OSS_USE_STS 时必须配置 OSS_STS_ROLE_ARN")
            if not (0 < self.oss_sts_duration_seconds <= 43200):
                raise ValueError("OSS_STS_DURATION_SECONDS 需在 1~43200 之间")

        # ---- RAG 流水线参数自洽性 ----
        if self.embedding_dim <= 0:
            raise ValueError("EMBEDDING_DIM 必须为正整数")
        if self.dm_chunk_overlap >= self.dm_chunk_size:
            raise ValueError("DM_CHUNK_OVERLAP 必须小于 DM_CHUNK_SIZE，否则分块会无限循环")
        if self.dm_semantic_split_threshold < self.dm_chunk_size:
            raise ValueError("DM_SEMANTIC_SPLIT_THRESHOLD 不应小于 DM_CHUNK_SIZE")
        if not (0 <= self.dm_simhash_threshold <= 16):
            raise ValueError("DM_SIMHASH_THRESHOLD 需在 0~16 之间（64 位指纹）")
        if self.ocr_engine and self.ocr_engine.lower() not in {
            "rapid", "paddle", "tesseract",
        }:
            raise ValueError("OCR_ENGINE 已禁用阿里云，仅支持 rapid / paddle / tesseract")
        if not (0 < self.dm_header_footer_ratio <= 1):
            raise ValueError("DM_HEADER_FOOTER_RATIO 需在 (0, 1] 之间")
        if self.dm_extract_pages_per_shard < 1:
            raise ValueError("DM_EXTRACT_PAGES_PER_SHARD 至少为 1")
        if self.embedding_batch_size < 1:
            raise ValueError("EMBEDDING_BATCH_SIZE 至少为 1")
        return self

    def missing_required(self) -> List[str]:
        """返回缺失的关键配置项，用于启动自检与 /ready 探针。"""
        required = {
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
            "OSS_ACCESS_KEY_ID": self.oss_access_key_id,
            "OSS_ACCESS_KEY_SECRET": self.oss_access_key_secret,
            "OSS_ENDPOINT": self.oss_endpoint,
            "OSS_BUCKET": self.oss_bucket,
        }
        missing = [k for k, v in required.items() if not v]
        if not self.supabase_jwt_secret and not self.jwks_url:
            missing.append("SUPABASE_JWT_SECRET 或 SUPABASE_JWKS_URL")
        if self.oss_signature_version.lower() == "v4" and not self.oss_region:
            missing.append("OSS_REGION (V4 签名必填)")
        if self.oss_use_sts:
            if not self.oss_sts_role_arn:
                missing.append("OSS_STS_ROLE_ARN")
            # STS 专用长期密钥缺省复用 OSS_*，故只需检查最终是否拿到
            sts_ak = self.oss_sts_access_key_id or self.oss_access_key_id
            sts_sk = self.oss_sts_access_key_secret or self.oss_access_key_secret
            if not sts_ak:
                missing.append("OSS_STS_ACCESS_KEY_ID（或退化为 OSS_ACCESS_KEY_ID）")
            if not sts_sk:
                missing.append("OSS_STS_ACCESS_KEY_SECRET（或退化为 OSS_ACCESS_KEY_SECRET）")
            if not self.oss_sts_endpoint:
                missing.append("OSS_STS_ENDPOINT")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
