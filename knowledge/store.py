"""内存 RAG：租户/权限硬过滤 → embedding 向量召回 → 词面+向量加权 rerank。"""
from __future__ import annotations

import threading

from .embedding import EmbeddingProvider, get_embedding_provider
from .ingestion import Chunk
from .vector_index import InMemoryVectorIndex, rerank


def _score(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    s = 0.0
    if query in text:
        s += 10.0
    # 2-gram 命中加分，中文无分词时的最小可用排序
    for i in range(len(query) - 1):
        if query[i:i + 2] in text:
            s += 1.0
    # 数字关键词加权（5000这类规则阈值必须排前面）
    for tok in ("5000", "5xx", "审批", "报销"):
        if tok in query and tok in text:
            s += 3.0
    return s


class KnowledgeStore:
    def __init__(self, embedding_provider: EmbeddingProvider | None = None,
                 use_vector: bool = True, rerank_enabled: bool = True,
                 lexical_weight: float = 0.65, vector_weight: float = 0.35,
                 min_vector_score: float = 0.25):
        self._chunks: list[Chunk] = []
        self.embedding = embedding_provider or get_embedding_provider()
        self.use_vector = use_vector
        self.rerank_enabled = rerank_enabled
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight
        self.min_vector_score = min_vector_score
        self._index = InMemoryVectorIndex(self.embedding) if use_vector else None
        self.last_scores: dict[int, dict] = {}
        self._lock = threading.RLock()

    def add_many(self, chunks: list[Chunk]):
        with self._lock:
            self._chunks.extend(chunks)
            if self._index is not None:
                self._index.add_many(chunks)

    def replace_document(self, tenant_id: str, document_id: str, version: str,
                         chunks: list[Chunk], allowed_permissions: list[str] | None = None) -> int:
        """Atomically replace one document version without retaining stale chunks."""
        if any((c.tenant_id, c.document_id, c.version) != (tenant_id, document_id, version)
               for c in chunks):
            raise ValueError("chunk metadata does not match document key")
        key = (tenant_id, document_id, version)
        with self._lock:
            if allowed_permissions is not None:
                existing = {c.permission for c in self._chunks
                            if (c.tenant_id, c.document_id, c.version) == key}
                if existing - set(allowed_permissions):
                    raise PermissionError("role cannot replace this knowledge permission")
            self.delete_document(tenant_id, document_id, version)
            self.add_many(chunks)
            return len(chunks)

    def delete_document(self, tenant_id: str, document_id: str, version: str,
                        allowed_permissions: list[str] | None = None) -> int:
        key = (tenant_id, document_id, version)
        with self._lock:
            existing_permissions = {c.permission for c in self._chunks
                                    if (c.tenant_id, c.document_id, c.version) == key}
            if allowed_permissions is not None and existing_permissions - set(allowed_permissions):
                raise PermissionError("role cannot delete this knowledge permission")
            removed = [c for c in self._chunks
                       if (c.tenant_id, c.document_id, c.version) == key]
            if not removed:
                return 0
            self._chunks = [c for c in self._chunks
                            if (c.tenant_id, c.document_id, c.version) != key]
            if self._index is not None:
                self._index.remove_many(removed)
            self.last_scores = {}
            return len(removed)

    def document_exists(self, tenant_id: str, document_id: str, version: str) -> bool:
        key = (tenant_id, document_id, version)
        with self._lock:
            return any((c.tenant_id, c.document_id, c.version) == key for c in self._chunks)

    def list_documents(self, tenant_id: str,
                       allowed_permissions: list[str] | None = None) -> list[dict]:
        allowed = set(["employee"] if allowed_permissions is None else allowed_permissions)
        with self._lock:
            documents: dict[tuple[str, str], dict] = {}
            for chunk in self._chunks:
                if chunk.tenant_id != tenant_id or chunk.permission not in allowed:
                    continue
                key = (chunk.document_id, chunk.version)
                item = documents.setdefault(key, {
                    "tenant_id": chunk.tenant_id,
                    "document_id": chunk.document_id,
                    "version": chunk.version,
                    "department": chunk.department,
                    "permission": chunk.permission,
                    "chunk_count": 0,
                    "created_at": chunk.created_at,
                })
                item["chunk_count"] += 1
            return [dict(documents[key]) for key in sorted(documents)]

    def search(self, query: str, tenant_id: str = "t1",
               allowed_permissions: list[str] | None = None, top_k: int = 3) -> list[Chunk]:
        allowed = set(["employee"] if allowed_permissions is None else allowed_permissions)
        with self._lock:
            # 1) 租户/权限硬过滤：向量召回绝不能绕过这一步。
            candidates = [c for c in self._chunks
                          if c.tenant_id == tenant_id and c.permission in allowed]
            if not candidates or not query or not query.strip():
                return []

            lexical = {id(c): _score(query, c.text) for c in candidates}
            vectors = self._index.score(query, candidates) if self._index is not None else {}
            items: list[dict] = []
            self.last_scores = {}
            for c in candidates:
                lex = lexical[id(c)]
                vec = float(vectors.get(id(c), 0.0))
                if lex <= 0 and vec < self.min_vector_score:
                    continue
                items.append({"chunk": c, "lexical": lex, "vector": vec})
                self.last_scores[id(c)] = {"lexical": lex, "vector": vec}

            if not items:
                return []
            if self.rerank_enabled:
                ranked = rerank(query, items, lexical_weight=self.lexical_weight,
                                vector_weight=self.vector_weight)
            else:
                ranked = sorted(items, key=lambda i: (-i["lexical"], i["chunk"].chunk_id))
            return [i["chunk"] for i in ranked][:top_k]
