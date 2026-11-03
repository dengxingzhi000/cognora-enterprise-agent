"""可选 pgvector 后端（默认关闭）。

开关：``RAG_PGVECTOR_ENABLED=1`` 且存在 DSN。
任何建库/扩展失败都回落 ``KnowledgeStore``，不影响离线路径。
"""
from __future__ import annotations

import json
import warnings

from .document import Chunk
from .embedding import get_embedding_provider
from .store import KnowledgeStore, _score
from .vector_index import rerank

TABLE = "agent_knowledge_chunk"


class PgVectorUnavailable(RuntimeError):
    pass


class PGVectorStore:
    def __init__(self, dsn: str | None = None, connector=None, dim: int = 256,
                 table: str = TABLE):
        from infrastructure.pg.connector import PGConnector

        self.dim = dim
        self.table = table
        self.embedding = get_embedding_provider(dim=dim)
        self._c = connector or PGConnector(dsn)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            self._c.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self._c.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table} ("
                "id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, department TEXT, "
                "document_id TEXT, chunk_id INT, permission TEXT NOT NULL, "
                "version TEXT, text TEXT NOT NULL, "
                f"embedding vector({self.dim}), "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            self._c.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.table}_tenant "
                f"ON {self.table}(tenant_id, permission)"
            )
        except Exception as e:  # noqa: BLE001
            raise PgVectorUnavailable(f"pgvector schema unavailable: {e}") from e

    def add_many(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            vec = json.dumps(self.embedding.embed(c.text))
            self._c.execute(
                f"INSERT INTO {self.table} "
                "(tenant_id, department, document_id, chunk_id, permission, version, text, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS vector))",
                (c.tenant_id, c.department, c.document_id, c.chunk_id,
                 c.permission, c.version, c.text, vec),
            )

    def _row_to_chunk(self, row: dict) -> Chunk:
        return Chunk(
            text=row.get("text", ""), tenant_id=row.get("tenant_id", ""),
            department=row.get("department") or "", document_id=row.get("document_id") or "",
            version=row.get("version") or "", permission=row.get("permission", ""),
            created_at=str(row.get("created_at") or ""),
            chunk_id=int(row.get("chunk_id") or 0),
        )

    def search(self, query: str, tenant_id: str = "t1",
               allowed_permissions: list[str] | None = None, top_k: int = 3) -> list[Chunk]:
        allowed = list(allowed_permissions or ["employee"])
        if not query or not query.strip() or not allowed:
            return []
        qvec = json.dumps(self.embedding.embed(query))
        placeholders = ", ".join("?" for _ in allowed)
        candidate_k = max(top_k * 3, top_k)
        sql = (
            f"SELECT tenant_id, department, document_id, chunk_id, permission, version, "
            f"text, created_at, 1 - (embedding <=> CAST(? AS vector)) AS score "
            f"FROM {self.table} "
            f"WHERE tenant_id = ? AND permission IN ({placeholders}) "
            f"ORDER BY embedding <=> CAST(? AS vector) LIMIT ?"
        )
        params = [qvec, tenant_id, *allowed, qvec, candidate_k]
        try:
            rows = self._c.fetch_all(sql, tuple(params))
        except Exception as e:  # noqa: BLE001
            warnings.warn(f"pgvector search failed ({e}); use knowledge fallback")
            return KnowledgeStore().search(query, tenant_id, allowed, top_k)

        items = []
        for row in rows:
            chunk = self._row_to_chunk(row)
            try:
                vec = max(0.0, float(row.get("score") or 0.0))
            except (TypeError, ValueError):
                vec = 0.0
            items.append({"chunk": chunk, "lexical": _score(query, chunk.text), "vector": vec})
        if not items:
            return []
        ranked = rerank(query, items, lexical_weight=0.6, vector_weight=0.4)
        return [i["chunk"] for i in ranked][:top_k]


def pgvector_enabled() -> bool:
    import os
    return os.environ.get("RAG_PGVECTOR_ENABLED") == "1"


def build_knowledge_store(dsn: str | None = None, enabled: bool | None = None,
                          connector=None):
    """开关开启且有 DSN 时尝试 pgvector；否则返回内存 KnowledgeStore。"""
    import os
    if enabled is None:
        enabled = pgvector_enabled()
    if not enabled:
        return KnowledgeStore()
    dsn = dsn or os.environ.get("RAG_PGVECTOR_DSN") or os.environ.get("AGENT_PG_DSN") \
        or os.environ.get("PG_DSN") or os.environ.get("POSTGRES_DSN")
    if connector is None and not dsn:
        warnings.warn("RAG_PGVECTOR_ENABLED=1 but no DSN; using in-memory KnowledgeStore")
        return KnowledgeStore()
    try:
        return PGVectorStore(dsn=dsn, connector=connector)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"pgvector unavailable ({e}); using in-memory KnowledgeStore")
        return KnowledgeStore()
