"""内存向量索引 + 加权 rerank；租户/权限过滤在进入索引前完成。"""
from __future__ import annotations

import math

from .embedding import EmbeddingProvider, get_embedding_provider
from .ingestion import Chunk


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
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

    def remove_many(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._vectors.pop(id(chunk), None)

    def score(self, query: str, chunks: list[Chunk]) -> dict[int, float]:
        qv = self.provider.embed(query or "")
        return {id(c): cosine(qv, self._vectors.get(id(c), [])) for c in chunks}


def _model_rerank(query: str, items: list[dict]) -> list[dict] | None:
    import os
    import warnings

    if os.getenv("RAG_RERANK_PROVIDER", "weighted") != "cohere":
        return None
    api_key = os.getenv("COHERE_API_KEY", "")
    if not api_key:
        warnings.warn("COHERE_API_KEY missing; using weighted rerank", stacklevel=2)
        return None
    import httpx

    try:
        response = httpx.post(
            "https://api.cohere.com/v1/rerank",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": os.getenv("RAG_RERANK_MODEL", "rerank-v3.5"),
                "query": query,
                "documents": [item["chunk"].text for item in items],
                "top_n": len(items),
            },
            timeout=30,
        )
        response.raise_for_status()
        results = response.json()["results"]
        ranked = []
        for result in results:
            item = dict(items[int(result["index"])])
            item["rerank_score"] = float(result["relevance_score"])
            item["rerank_provider"] = "cohere"
            ranked.append(item)
        return ranked
    except Exception as exc:  # noqa: BLE001 - optional model rerank must not break recall
        if os.getenv("RAG_RERANK_STRICT") == "1":
            raise
        warnings.warn(f"model rerank unavailable ({type(exc).__name__}); using weighted rerank", stacklevel=2)
        return None


def rerank(query: str, items: list[dict], lexical_weight: float = 0.65,
           vector_weight: float = 0.35) -> list[dict]:
    if not items:
        return []
    model_ranked = _model_rerank(query, items)
    if model_ranked is not None:
        return model_ranked
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
