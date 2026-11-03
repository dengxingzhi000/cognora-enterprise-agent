"""离线可测的确定性 embedding：feature hashing，无外部模型/网络依赖。"""
from __future__ import annotations

import hashlib
import math
import re

_WORD_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider:
    dim: int

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class HashingEmbeddingProvider(EmbeddingProvider):
    """把 ASCII 词与字符 2-gram 哈希到固定维度，L2 归一化。

    相同输入在任何进程/机器上得到相同向量，适合离线测试与无向量库环境。
    """

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("dim must be > 0")
        self.dim = dim

    @staticmethod
    def _features(text: str) -> list[tuple[str, float]]:
        low = (text or "").lower()
        feats: list[tuple[str, float]] = []
        for word in _WORD_RE.findall(low):
            feats.append((f"w:{word}", 2.0))
        clean = "".join(ch for ch in low if not ch.isspace())
        for i in range(max(0, len(clean) - 1)):
            feats.append((f"g:{clean[i:i + 2]}", 1.0))
        return feats

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature, weight in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


def get_embedding_provider(name: str | None = None, dim: int = 256) -> EmbeddingProvider:
    import os
    import warnings

    name = name or os.environ.get("RAG_EMBEDDING_PROVIDER", "hashing")
    if name in ("hashing", "local", "default"):
        return HashingEmbeddingProvider(dim=dim)
    warnings.warn(f"unknown RAG_EMBEDDING_PROVIDER={name}; falling back to hashing")
    return HashingEmbeddingProvider(dim=dim)
