"""内存向量索引 + 加权 rerank；租户/权限过滤在进入索引前完成。"""
from __future__ import annotations

import math

from .embedding import EmbeddingProvider, get_embedding_provider
from .ingestion import Chunk


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


class InMemoryVectorIndex:
    def __init__(self, provider: EmbeddingProvider | None = None):
        self.provider = provider or get_embedding_provider()
        self._vectors: dict[int, list[float]] = {}

    def add_many(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._vectors[id(chunk)] = self.provider.embed(chunk.text)

    def score(self, query: str, chunks: list[Chunk]) -> dict[int, float]:
        qv = self.provider.embed(query or "")
        return {id(c): cosine(qv, self._vectors.get(id(c), [])) for c in chunks}


def rerank(query: str, items: list[dict], lexical_weight: float = 0.65,
           vector_weight: float = 0.35) -> list[dict]:
    """输入含 ``chunk`` / ``lexical`` / ``vector`` 的候选，返回降序副本。

    词面分数按本批最大值归一化，向量分数天然在 [-1, 1]；权重让配置可调，
    默认仍以精确词面命中为主（中文无分词时更稳）。
    """
    if not items:
        return []
    max_lex = max((float(i.get("lexical", 0.0)) for i in items), default=0.0) or 1.0
    ranked: list[tuple[float, dict]] = []
    for item in items:
        lex = max(0.0, float(item.get("lexical", 0.0))) / max_lex
        vec = max(0.0, float(item.get("vector", 0.0)))
        score = lexical_weight * lex + vector_weight * vec
        enriched = dict(item)
        enriched["rerank_score"] = score
        ranked.append((score, enriched))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["chunk"].chunk_id))
    return [item for _, item in ranked]
