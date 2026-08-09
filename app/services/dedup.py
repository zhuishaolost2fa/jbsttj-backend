"""全局去重：精确指纹 + SimHash 近似指纹。

DM 主持人手册的重复率高得离谱，实测主要来自四类内容：

  1. **版式噪声** —— 页眉页脚（已由 pdf_extract 剥离）、每章开头的固定提示语；
  2. **规则复述** —— 「搜证阶段每人限搜 3 次」这类规则在总览、分幕流程、
     附录里各写一遍，措辞略有出入；
  3. **角色卡模板** —— N 个角色共用同一套「你的目标是…」骨架，只有专名不同；
  4. **跨版本残留** —— 同一手册的修订版重新上传，绝大部分内容原封不动。

第 1、4 类是**完全一致**的文本，SHA256 精确去重就够；
第 2、3 类改了几个字，精确哈希完全失效，必须靠 SimHash 的汉明距离。

两级串联的顺序不能反：精确哈希是 O(1) 且零误判，先挡掉大头，
剩下的少量文本才值得付出 SimHash 的分词与位运算成本。

**为什么不用 MinHash/LSH？** SimHash 单条指纹只有 8 字节，
配合鸽巢分段索引就能做到近似查询；MinHash 要存上百个哈希值才有同等精度，
对一份产出上万 chunk 的手册来说，存储与网络开销差了两个数量级。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple

logger = logging.getLogger("app.dedup")

# SimHash 位宽。64 位在千万级文档下的碰撞率已足够低，
# 且正好能塞进 PostgreSQL 的 bigint，不需要额外的类型转换。
SIMHASH_BITS = 64
# 鸽巢原理：把 64 位切成 4 段，若两指纹汉明距离 <= 3，
# 则必有至少一段完全相同 —— 这是分段索引能做候选召回的数学保证。
SIMHASH_BANDS = 4
BAND_BITS = SIMHASH_BITS // SIMHASH_BANDS  # 16
BAND_MASK = (1 << BAND_BITS) - 1

_WHITESPACE = re.compile(r"\s+")
# 去掉不参与语义的装饰性符号；保留中文标点以免「不，可以」和「不可以」被判成同一句
_DECORATION = re.compile(r"[…·•▪◦●○■□◆▲★☆※　\-—–_=~`|]+")


# ============================================================
# 归一化与指纹
# ============================================================
def normalize_text(text: str) -> str:
    """归一化文本，用于计算精确指纹。

    NFKC 会把全角字符、罗马数字、上下标等统一成标准形式，
    这样「Ⅲ」和「III」、「１２」和「12」不会被当成两段不同的内容。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _DECORATION.sub("", normalized)
    normalized = _WHITESPACE.sub("", normalized)
    return normalized.strip().lower()


def content_hash(text: str) -> str:
    """归一化文本的 SHA256（十六进制）。"""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _shingles(text: str, size: int = 3) -> Counter:
    """把文本切成字符 n-gram 特征。

    中文没有天然词边界，引入 jieba 这类分词器会带来几十兆的词典依赖，
    而 SimHash 本身对特征粒度不敏感 —— 3-gram 在中文上的表现与分词相当。
    英文与数字部分按整词切，避免 "playstation" 被拆成一堆无意义片段。
    """
    normalized = normalize_text(text)
    if not normalized:
        return Counter()

    features: List[str] = []
    # 先把连续的 ASCII 字母数字整体抽出来当作独立特征
    for word in re.findall(r"[a-z0-9]+", normalized):
        if len(word) >= 2:
            features.append(word)
    # 剩余部分（主要是中文）走字符 n-gram
    cjk_only = re.sub(r"[a-z0-9]+", "", normalized)
    if len(cjk_only) < size:
        if cjk_only:
            features.append(cjk_only)
    else:
        features.extend(cjk_only[i : i + size] for i in range(len(cjk_only) - size + 1))

    return Counter(features)


def _feature_hash(feature: str) -> int:
    """特征 -> 64 位整数。

    用 blake2b 而非内建 hash()：后者在不同进程间带随机盐，
    Celery 的多 worker 会算出完全不同的指纹，去重直接失效。
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(text: str, *, shingle_size: int = 3) -> int:
    """计算 64 位 SimHash 指纹（无符号）。"""
    features = _shingles(text, shingle_size)
    if not features:
        return 0

    vector = [0] * SIMHASH_BITS
    for feature, weight in features.items():
        h = _feature_hash(feature)
        for bit in range(SIMHASH_BITS):
            if h & (1 << bit):
                vector[bit] += weight
            else:
                vector[bit] -= weight

    fingerprint = 0
    for bit in range(SIMHASH_BITS):
        if vector[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """两个指纹的汉明距离。"""
    return bin(a ^ b).count("1")


def to_signed_64(value: int) -> int:
    """无符号 64 位转有符号，用于写入 PostgreSQL 的 bigint 列。"""
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed_64(value: int) -> int:
    """有符号 bigint 转回无符号 64 位。"""
    return value + (1 << 64) if value < 0 else value


def _bands(fingerprint: int) -> List[Tuple[int, int]]:
    """把指纹切成 (段序号, 段值) 列表，用于分段索引。"""
    return [
        (i, (fingerprint >> (i * BAND_BITS)) & BAND_MASK)
        for i in range(SIMHASH_BANDS)
    ]


# ============================================================
# 去重存储后端
# ============================================================
class DedupBackend(Protocol):
    """去重指纹的存储后端。"""

    def seen_exact(self, key: str) -> bool: ...

    def add_exact(self, key: str) -> None: ...

    def candidates(self, fingerprint: int) -> Set[int]: ...

    def add_fingerprint(self, fingerprint: int) -> None: ...

    def clear(self) -> None: ...


class InMemoryDedupBackend:
    """进程内后端。

    单任务内的去重、离线测试用。Celery 多 worker 场景下各进程互不可见，
    必须换成 Redis 后端，否则跨分片的重复内容漏网。
    """

    def __init__(self) -> None:
        self._exact: Set[str] = set()
        self._bands: Dict[Tuple[int, int], Set[int]] = {}

    def seen_exact(self, key: str) -> bool:
        return key in self._exact

    def add_exact(self, key: str) -> None:
        self._exact.add(key)

    def candidates(self, fingerprint: int) -> Set[int]:
        found: Set[int] = set()
        for band in _bands(fingerprint):
            found |= self._bands.get(band, set())
        return found

    def add_fingerprint(self, fingerprint: int) -> None:
        for band in _bands(fingerprint):
            self._bands.setdefault(band, set()).add(fingerprint)

    def clear(self) -> None:
        self._exact.clear()
        self._bands.clear()


class RedisDedupBackend:
    """Redis 后端，供多 worker 共享同一份指纹集合。

    键设计：
      - ``{ns}:exact``          精确指纹集合
      - ``{ns}:band:{i}:{val}`` 第 i 段值为 val 的指纹集合

    所有键统一挂 TTL：一次索引任务跑完指纹就没用了，
    留着只会让 Redis 内存无限膨胀。
    """

    def __init__(self, client, namespace: str, ttl_seconds: int = 24 * 3600) -> None:
        self.client = client
        self.ns = namespace
        self.ttl = ttl_seconds

    def _exact_key(self) -> str:
        return f"{self.ns}:exact"

    def _band_key(self, index: int, value: int) -> str:
        return f"{self.ns}:band:{index}:{value}"

    def seen_exact(self, key: str) -> bool:
        return bool(self.client.sismember(self._exact_key(), key))

    def add_exact(self, key: str) -> None:
        pipe = self.client.pipeline()
        pipe.sadd(self._exact_key(), key)
        pipe.expire(self._exact_key(), self.ttl)
        pipe.execute()

    def candidates(self, fingerprint: int) -> Set[int]:
        pipe = self.client.pipeline()
        for index, value in _bands(fingerprint):
            pipe.smembers(self._band_key(index, value))
        found: Set[int] = set()
        for members in pipe.execute():
            for m in members:
                try:
                    found.add(int(m))
                except (TypeError, ValueError):
                    continue
        return found

    def add_fingerprint(self, fingerprint: int) -> None:
        pipe = self.client.pipeline()
        for index, value in _bands(fingerprint):
            key = self._band_key(index, value)
            pipe.sadd(key, fingerprint)
            pipe.expire(key, self.ttl)
        pipe.execute()

    def clear(self) -> None:
        """按前缀扫描删除。用 scan_iter 而非 keys，避免阻塞 Redis 主线程。"""
        for key in self.client.scan_iter(match=f"{self.ns}:*", count=500):
            self.client.delete(key)


# ============================================================
# 去重器
# ============================================================
@dataclass
class DedupStats:
    total: int = 0
    kept: int = 0
    dropped_exact: int = 0
    dropped_near: int = 0
    dropped_short: int = 0

    @property
    def dropped(self) -> int:
        return self.dropped_exact + self.dropped_near + self.dropped_short

    @property
    def dedup_rate(self) -> float:
        return self.dropped / self.total if self.total else 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "total": self.total,
            "kept": self.kept,
            "dropped_exact": self.dropped_exact,
            "dropped_near": self.dropped_near,
            "dropped_short": self.dropped_short,
            "dedup_rate": round(self.dedup_rate, 4),
        }


@dataclass
class DedupResult:
    """单条文本的判重结论。"""

    is_duplicate: bool
    reason: str  # exact / near / short / new
    content_hash: str
    fingerprint: int
    matched_fingerprint: Optional[int] = None
    distance: Optional[int] = None


class Deduplicator:
    """两级去重器。

    用法::

        dedup = Deduplicator(backend=InMemoryDedupBackend())
        for text in texts:
            r = dedup.check(text)
            if not r.is_duplicate:
                save(text, r.content_hash, r.fingerprint)

    :param threshold: 汉明距离阈值，<= 该值判为近似重复。
        经验值 3：设成 1-2 会漏掉「改了两三个专名」的角色卡模板，
        设成 5 以上开始误杀语义确实不同的短句。
    :param min_chars: 归一化后短于该长度的文本直接丢弃 —— 这类残片
        （孤立的「是」「见下表」）既没有检索价值，又会拉低向量库整体质量。
    """

    def __init__(
        self,
        *,
        backend: Optional[DedupBackend] = None,
        threshold: int = 3,
        min_chars: int = 40,
        shingle_size: int = 3,
    ) -> None:
        self.backend: DedupBackend = backend or InMemoryDedupBackend()
        self.threshold = threshold
        self.min_chars = min_chars
        self.shingle_size = shingle_size
        self.stats = DedupStats()

    def check(self, text: str, *, register: bool = True) -> DedupResult:
        """判断文本是否重复；register=True 时把新指纹登记进后端。"""
        self.stats.total += 1
        normalized = normalize_text(text)

        if len(normalized) < self.min_chars:
            self.stats.dropped_short += 1
            return DedupResult(True, "short", content_hash(text), 0)

        exact = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if self.backend.seen_exact(exact):
            self.stats.dropped_exact += 1
            return DedupResult(True, "exact", exact, 0)

        fingerprint = simhash(text, shingle_size=self.shingle_size)

        # threshold 为 0 时等价于只做精确去重，跳过 SimHash 查询开销
        if self.threshold > 0 and fingerprint:
            for candidate in self.backend.candidates(fingerprint):
                distance = hamming_distance(fingerprint, candidate)
                if distance <= self.threshold:
                    self.stats.dropped_near += 1
                    return DedupResult(
                        True, "near", exact, fingerprint,
                        matched_fingerprint=candidate, distance=distance,
                    )

        if register:
            self.backend.add_exact(exact)
            if fingerprint:
                self.backend.add_fingerprint(fingerprint)

        self.stats.kept += 1
        return DedupResult(False, "new", exact, fingerprint)

    def filter(self, texts: Iterable[str]) -> List[Tuple[int, str, DedupResult]]:
        """批量过滤，返回保留下来的 [(原始下标, 文本, 判重结论)]。"""
        kept: List[Tuple[int, str, DedupResult]] = []
        for index, text in enumerate(texts):
            result = self.check(text)
            if not result.is_duplicate:
                kept.append((index, text, result))
        return kept


def build_backend(redis_url: Optional[str], namespace: str, ttl: int = 24 * 3600) -> DedupBackend:
    """按配置构造后端；Redis 不可用时自动降级为进程内后端。

    降级是有代价的（跨分片重复会漏网），所以要打 warning 让运维看得见，
    但不能因此让整条流水线失败 —— 漏几个重复块远好过整份手册索引不了。
    """
    if not redis_url:
        return InMemoryDedupBackend()
    try:
        import redis  # 延迟导入，未装 redis 包时也能跑离线测试

        client = redis.Redis.from_url(redis_url, decode_responses=True)
        client.ping()
        return RedisDedupBackend(client, namespace=namespace, ttl_seconds=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 去重后端不可用，降级为进程内去重（跨分片重复将漏网）: %s", exc)
        return InMemoryDedupBackend()
