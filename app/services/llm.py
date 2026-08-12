"""硅基流动（SiliconFlow）客户端：Chat 补全 + 文本向量化。

硅基流动的接口是 OpenAI 兼容格式，所以这里不引入 `openai` SDK ——
只用两个端点（`/chat/completions`、`/embeddings`），httpx 直调更轻，
也免得 SDK 版本升级把重试语义改掉。

**为什么同时提供同步和异步两套？**

流水线里两种调用场景的执行模型完全不同：

  - **Celery worker**（T3 生成问答对、T4 向量化）是同步进程，
    在里面跑 asyncio event loop 属于自找麻烦（信号处理、优雅退出都会出问题）；
  - **FastAPI 检索接口**是异步的，同步调用会阻塞整个 event loop。

两者共用鉴权、重试、错误映射逻辑，只有传输层分开。

**bge-large-zh-v1.5 的检索指令前缀**

BGE 中文模型在训练时对 query 侧加了指令前缀
「为这个句子生成表示以用于检索相关文章：」。v1.5 之后不加也能用，
但**短 query 场景加上仍有明显收益**（DM 手册的检索 query 往往就七八个字）。
关键是**文档侧绝对不能加** —— 两侧都加反而会让向量空间错位。
所以这里把 query / document 拆成两个方法，从 API 层面杜绝用错。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError, LLMError

logger = logging.getLogger("app.llm")

# BGE 中文模型的检索指令前缀，只加在 query 侧
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# 可重试的 HTTP 状态：限流 + 网关类错误
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# 从模型回复里抠 JSON：优先 ```json 围栏，其次裸的 [] / {}
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


@dataclass
class QAPair:
    """一条问答对。"""

    question: str
    answer: str
    category: str = "general"
    # 来源 chunk 在本批次里的序号，用于回写外键
    source_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "source_index": self.source_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QAPair":
        return cls(
            question=str(data.get("question", "")).strip(),
            answer=str(data.get("answer", "")).strip(),
            category=str(data.get("category", "general")).strip() or "general",
            source_index=int(data.get("source_index", 0)),
        )


# ============================================================
# 提示词
# ============================================================
_QA_SYSTEM_PROMPT = """你是一名资深的剧本杀主持人（DM）培训师，深谙新手主持人在带本现场会卡壳的每一个环节。

你的任务：把《主持人手册》的片段，改写成主持人在**实际带本过程中会真正提问、并需要手册给出确定答案**的问答对。关键在于「预判」——站在第一次带这本本的 DM 视角，问他/她此刻最想知道什么。

生成原则：
1. 只能使用所给片段回答，严禁引入片段外的任何信息，严禁编造规则、数字、人名、道具、页码。
2. 问题要用主持人的口吻，针对「带本现场」发问，例如：
   -「搜证阶段每位玩家最多搜几次？」（规则流程）
   -「33 号线索卡什么时候该亮给玩家看？」（线索道具）
   -「星期一这个角色本轮话术为什么和其他人不一样？」（角色人设）
   -「这轮演绎 DM 要重点表现什么情绪、避免什么翻车？」（主持技巧）
   不要写「本段讲了什么」这类没有检索价值的元问题。
3. 问题中不要出现「本文」「该片段」「上述」「前面」等指代词——
   这些问答对会脱离上下文单独被检索命中，指代词会失效。
4. 答案要完整、可直接照着执行，原样保留手册里的关键数字、时间、人名、道具名、页码。
5. 尽可能多地生成：只要片段信息密度允许，就**从多个角度各出一条**——
   规则流程 / 线索物证 / 角色人设 / 时间线 / 主持技巧与翻车处理 / 常见玩家疑问。
   内容单薄（如纯目录、纯页眉、无实质信息）的片段返回空数组 []。
6. category 从以下枚举选最贴切的一个：
   rule（规则流程）/ clue（线索物证）/ character（角色人设）/
   timeline（时间线剧情）/ host_tip（主持技巧与翻车处理）/ general（其他）。
7. 不同片段若覆盖同一规则，避免逐字重复提问；换角度或换措辞，让每条问答都有独立检索价值。

只输出 JSON 数组，不要任何解释文字、不要 markdown 围栏。
格式：[{"index": 片段序号, "question": "...", "answer": "...", "category": "..."}]"""


def build_qa_user_prompt(
    chunks: Sequence[Dict[str, Any]],
    *,
    script_title: str = "",
    qa_per_chunk: int = 3,
) -> str:
    """拼装批量生成问答对的用户提示词。

    一次喂多个片段而不是逐个调用，是因为 DM 手册的 chunk 平均只有几百字，
    单独调一次 LLM 的话，system prompt 的 token 开销比正文还大。
    批量还能让模型看到相邻片段，减少重复提问。
    """
    lines: List[str] = []
    if script_title:
        lines.append(f"剧本名称：《{script_title}》")
    lines.append(
        f"请为下面 {len(chunks)} 个片段分别生成问答对，"
        f"每个片段**尽可能多**地生成：信息丰富的片段最多可到 {qa_per_chunk} 条；"
        f"信息单薄的片段可以少生成或不生成。相邻片段已合并展示，请避免跨片段重复提问同一内容。"
    )
    lines.append("")

    for i, chunk in enumerate(chunks):
        section = " > ".join(chunk.get("section_path") or [])
        header = f"【片段 {i}】"
        if section:
            header += f" 章节：{section}"
        page_start = chunk.get("page_start")
        page_end = chunk.get("page_end")
        if page_start:
            header += f"（P{page_start}" + (f"-{page_end}" if page_end and page_end != page_start else "") + "）"
        lines.append(header)
        lines.append(str(chunk.get("text", "")).strip())
        lines.append("")

    lines.append('务必用片段序号填写 index 字段。只输出 JSON 数组。')
    return "\n".join(lines)


def parse_qa_response(content: str, *, max_index: int) -> List[QAPair]:
    """解析模型返回的 JSON 数组，容忍围栏、前后废话、单引号等常见脏输出。

    LLM 输出格式不稳定是常态，这里宁可多兜几层也不要让整批 chunk 白跑一趟 ——
    T3 失败会连带 T4 空转，一批的重试成本远高于几行解析代码。
    """
    if not content or not content.strip():
        return []

    raw = content.strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 截取第一个 [ 到最后一个 ] 之间的内容再试
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("问答对 JSON 解析失败，原始输出前 200 字: %s", raw[:200])
                return []
        else:
            logger.warning("问答对响应中未找到 JSON 数组: %s", raw[:200])
            return []

    # 有的模型会包一层 {"data": [...]} 或 {"qa_pairs": [...]}
    if isinstance(data, dict):
        for key in ("data", "qa_pairs", "result", "items", "list"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []

    pairs: List[QAPair] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if len(question) < 4 or len(answer) < 2:
            continue
        try:
            idx = int(item.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        # 模型偶尔会把 index 写飞，钳制到合法范围，避免外键挂到不存在的 chunk 上
        idx = max(0, min(idx, max_index))
        pairs.append(
            QAPair(
                question=question,
                answer=answer,
                category=str(item.get("category") or "general").strip().lower() or "general",
                source_index=idx,
            )
        )
    return pairs


# ============================================================
# 客户端
# ============================================================
class SiliconFlowClient:
    """硅基流动 OpenAI 兼容接口的薄封装（同步 + 异步）。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._sync: Optional[httpx.Client] = None
        self._async: Optional[httpx.AsyncClient] = None

    # ---------------- 基础设施 ----------------
    @property
    def settings(self) -> Settings:
        return self._settings

    def _headers(self) -> Dict[str, str]:
        key = self._settings.siliconflow_api_key
        if not key:
            raise ConfigError("未配置 SILICONFLOW_API_KEY，无法调用大模型服务")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _timeout(self) -> httpx.Timeout:
        # 生成任务的首字节延迟可能很长，read 给足；连接超时保持短，快速失败快速重试
        return httpx.Timeout(180.0, connect=10.0, write=30.0)

    def sync_client(self) -> httpx.Client:
        if self._sync is None:
            self._sync = httpx.Client(
                base_url=self._settings.siliconflow_base_url.rstrip("/"),
                headers=self._headers(),
                timeout=self._timeout(),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._sync

    def async_client(self) -> httpx.AsyncClient:
        if self._async is None:
            self._async = httpx.AsyncClient(
                base_url=self._settings.siliconflow_base_url.rstrip("/"),
                headers=self._headers(),
                timeout=self._timeout(),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._async

    def close(self) -> None:
        if self._sync is not None:
            self._sync.close()
            self._sync = None

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None

    # ---------------- 重试策略 ----------------
    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[float]) -> float:
        """退避时长：优先尊重服务端的 Retry-After，否则指数退避 + 抖动。

        抖动很关键：T4 有多个 worker 并发打同一个 embedding 接口，
        整齐划一的退避会让它们在同一时刻重新涌上去，把限流窗口再撞爆一次。
        """
        if retry_after and retry_after > 0:
            return min(retry_after, 60.0)
        base = min(2.0 ** attempt, 30.0)
        return base + random.uniform(0, base * 0.3)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        value = resp.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _raise_for_response(self, resp: httpx.Response, endpoint: str) -> None:
        detail: Any
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = resp.text[:500]
        logger.error("硅基流动 %s -> %s %s", endpoint, resp.status_code, detail)
        if resp.status_code in (401, 403):
            raise LLMError("大模型服务鉴权失败，请检查 SILICONFLOW_API_KEY", details=detail)
        if resp.status_code == 429:
            raise LLMError(
                "大模型服务触发限流",
                code="llm_rate_limited",
                details={"retry_after": self._retry_after(resp), "body": detail},
            )
        raise LLMError(f"大模型服务返回 {resp.status_code}", details=detail)

    def _post_sync(self, endpoint: str, payload: Dict[str, Any], *, max_retries: int) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.sync_client().post(endpoint, json=payload)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
                delay = self._backoff(attempt, None)
                logger.warning("硅基流动 %s 网络异常(%s)，%.1fs 后重试", endpoint, exc, delay)
                time.sleep(delay)
                continue

            if resp.status_code < 400:
                return resp.json()

            if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                delay = self._backoff(attempt, self._retry_after(resp))
                logger.warning(
                    "硅基流动 %s 返回 %s，%.1fs 后重试(%s/%s)",
                    endpoint, resp.status_code, delay, attempt + 1, max_retries,
                )
                time.sleep(delay)
                continue

            self._raise_for_response(resp, endpoint)

        raise LLMError(f"大模型服务请求失败: {last_exc}") from last_exc

    async def _post_async(self, endpoint: str, payload: Dict[str, Any], *, max_retries: int) -> Dict[str, Any]:
        import asyncio

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self.async_client().post(endpoint, json=payload)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
                await asyncio.sleep(self._backoff(attempt, None))
                continue

            if resp.status_code < 400:
                return resp.json()

            if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                await asyncio.sleep(self._backoff(attempt, self._retry_after(resp)))
                continue

            self._raise_for_response(resp, endpoint)

        raise LLMError(f"大模型服务请求失败: {last_exc}") from last_exc

    # ---------------- Embedding ----------------
    def _embed_payload(self, texts: Sequence[str]) -> Dict[str, Any]:
        # 硅基流动 bge 系列对超长输入直接 400，需在客户端截断到安全长度。
        # 截断保留头部（章节面包屑 + 正文开头最富含语义），丢弃尾部。
        max_chars = self._settings.embedding_max_chars
        truncated = [t[:max_chars] if max_chars and len(t) > max_chars else t for t in texts]
        return {
            "model": self._settings.siliconflow_embed_model,
            "input": truncated,
            "encoding_format": "float",
        }

    @staticmethod
    def _parse_embeddings(data: Dict[str, Any], expect: int, dim: int) -> List[List[float]]:
        items = data.get("data") or []
        if len(items) != expect:
            raise LLMError(
                f"向量化返回数量不匹配：期望 {expect}，实际 {len(items)}",
                details={"usage": data.get("usage")},
            )
        # 接口不保证顺序，按 index 排序后再取，否则向量会和文本错位——
        # 这种错位不会报错，只会让检索结果莫名其妙地不相关，极难排查
        try:
            items = sorted(items, key=lambda x: int(x.get("index", 0)))
        except (TypeError, ValueError):
            pass

        vectors: List[List[float]] = []
        for item in items:
            vec = item.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise LLMError("向量化返回结果缺少 embedding 字段")
            if dim and len(vec) != dim:
                raise LLMError(
                    f"向量维度不匹配：模型返回 {len(vec)} 维，配置为 {dim} 维。"
                    f"请确认 EMBEDDING_DIM 与 SILICONFLOW_EMBED_MODEL 是否对应"
                )
            vectors.append([float(v) for v in vec])
        return vectors

    def embed_documents(self, texts: Sequence[str], *, max_retries: int = 3) -> List[List[float]]:
        """文档侧向量化（**不加**指令前缀），自动按 batch_size 分批。"""
        return self._embed_batched(texts, max_retries=max_retries)

    def embed_query(self, text: str, *, max_retries: int = 3) -> List[float]:
        """查询侧向量化（加 BGE 指令前缀）。"""
        prefixed = self._apply_query_instruction(text)
        vectors = self._embed_batched([prefixed], max_retries=max_retries)
        return vectors[0]

    async def aembed_query(self, text: str, *, max_retries: int = 3) -> List[float]:
        prefixed = self._apply_query_instruction(text)
        data = await self._post_async(
            "/embeddings", self._embed_payload([prefixed]), max_retries=max_retries
        )
        return self._parse_embeddings(data, 1, self._settings.embedding_dim)[0]

    def _apply_query_instruction(self, text: str) -> str:
        model = (self._settings.siliconflow_embed_model or "").lower()
        # 只对 BGE 中文系列加前缀；换成 m3 / gte 之类的模型时前缀是纯噪声
        if "bge" in model and "zh" in model:
            return f"{BGE_QUERY_INSTRUCTION}{text}"
        return text

    def _embed_batched(self, texts: Sequence[str], *, max_retries: int) -> List[List[float]]:
        if not texts:
            return []
        batch_size = max(1, self._settings.embedding_batch_size)
        dim = self._settings.embedding_dim
        out: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            data = self._post_sync("/embeddings", self._embed_payload(batch), max_retries=max_retries)
            out.extend(self._parse_embeddings(data, len(batch), dim))
        return out

    # ---------------- Chat ----------------
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        max_retries: int = 3,
        response_format_json: bool = False,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": self._settings.siliconflow_chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}

        data = self._post_sync("/chat/completions", payload, max_retries=max_retries)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("大模型返回空结果", details=data.get("usage"))
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def generate_qa(
        self,
        chunks: Sequence[Dict[str, Any]],
        *,
        script_title: str = "",
        qa_per_chunk: Optional[int] = None,
        max_retries: int = 2,
    ) -> List[QAPair]:
        """为一批 chunk 生成问答对。

        解析失败不抛异常而是返回空列表：单批问答对生成失败属于**可降级**故障，
        正文 chunk 的向量照样能入库检索，没必要让整条流水线红掉。
        """
        if not chunks:
            return []
        per_chunk = qa_per_chunk or self._settings.dm_qa_per_chunk
        user_prompt = build_qa_user_prompt(
            chunks, script_title=script_title, qa_per_chunk=per_chunk
        )
        try:
            content = self.chat(
                [
                    {"role": "system", "content": _QA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=8192,
                max_retries=max_retries,
            )
        except LLMError as exc:
            logger.warning("问答对生成失败（跳过本批 %s 个片段）: %s", len(chunks), exc)
            return []

        pairs = parse_qa_response(content, max_index=len(chunks) - 1)
        logger.info("问答对生成: %s 个片段 -> %s 条", len(chunks), len(pairs))
        return pairs


# ============================================================
# LangChain 适配器
# ============================================================
class SiliconFlowEmbeddings:
    """把 :class:`SiliconFlowClient` 适配成 LangChain 的 Embeddings 接口。

    SemanticChunker 只依赖 ``embed_documents`` / ``embed_query`` 两个方法，
    用鸭子类型即可，不必继承 langchain_core 的基类 —— 这样 LangChain
    没装的时候本模块照样能 import。
    """

    def __init__(self, client: Optional[SiliconFlowClient] = None) -> None:
        self._client = client or SiliconFlowClient()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._client.embed_query(text)


# ============================================================
# 单例
# ============================================================
_client: Optional[SiliconFlowClient] = None


def get_llm_client() -> SiliconFlowClient:
    """进程内单例。

    Celery 用 prefork 模型，每个 worker 子进程会各自持有一份 —— 这正是想要的：
    httpx 的连接池不能跨 fork 共享，跨进程复用会拿到已被对端关闭的死连接。
    """
    global _client
    if _client is None:
        _client = SiliconFlowClient()
    return _client


def reset_llm_client() -> None:
    """重置单例（测试用，或 worker fork 后主动丢弃父进程的连接池）。"""
    global _client
    if _client is not None:
        _client.close()
    _client = None
