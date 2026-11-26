"""Strict opt-in pgvector backend for enterprise knowledge.

``RAG_PGVECTOR_ENABLED=1`` makes configuration, connection and schema failures
fatal. The in-memory backend is used only when the feature is disabled.
"""
from __future__ import annotations

import json
import os

from .document import Chunk
from .embedding import get_embedding_provider
from .store import KnowledgeStore, _score
from .vector_index import rerank

TABLE = "agent_knowledge_chunk"


class PgVectorUnavailable(RuntimeError):
    pass


class PGVectorStore:
    def __init__(self, dsn: str | None = None, connector=None, dim: int | None = None,
                 table: str = TABLE):
        import os

        from infrastructure.pg.connector import PGConnector

        self.dim = dim or int(os.getenv("RAG_EMBEDDING_DIM", "256"))
        self.table = table
        self.embedding = get_embedding_provider(dim=self.dim)
        if dsn is None and connector is None:
            raise PgVectorUnavailable("pgvector DSN is required")
        try:
            if connector is not None:
                self._c = connector
            else:
                assert dsn is not None
                self._c = PGConnector(dsn)
            self._ensure_schema()
        except PgVectorUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - strict backend must fail closed
            raise PgVectorUnavailable(f"pgvector connection unavailable: {e}") from e

    def _ensure_schema(self) -> None:
        import os

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
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{self.table}_document_version "
                f"ON {self.table}(tenant_id, document_id, version, chunk_id)"
            )
            self._c.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.table}_tenant "
                f"ON {self.table}(tenant_id, permission)"
            )
            self._c.execute(
                "CREATE OR REPLACE FUNCTION agent_knowledge_assert_acl("
                "p_tenant TEXT, p_document TEXT, p_version TEXT, p_allowed JSONB) "
                "RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN "
                "PERFORM pg_advisory_xact_lock(hashtext("
                "p_tenant || ':' || p_document || ':' || p_version)); "
                "PERFORM 1 FROM agent_knowledge_chunk WHERE tenant_id = p_tenant "
                "AND document_id = p_document AND version = p_version FOR UPDATE; "
                "IF EXISTS (SELECT 1 FROM agent_knowledge_chunk WHERE tenant_id = p_tenant "
                "AND document_id = p_document AND version = p_version "
                "AND NOT (permission = ANY (SELECT jsonb_array_elements_text(p_allowed)))) "
                "THEN RAISE EXCEPTION 'knowledge_acl_denied'; END IF; END $$"
            )
            if os.getenv("RAG_PGVECTOR_HNSW") == "1":
                self._c.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{self.table}_embedding_hnsw "
                    f"ON {self.table} USING hnsw (embedding vector_cosine_ops)"
                )
        except Exception as e:  # noqa: BLE001
            raise PgVectorUnavailable(f"pgvector schema unavailable: {e}") from e

    def replace_document(self, tenant_id: str, document_id: str, version: str,
                         chunks: list[Chunk], allowed_permissions: list[str] | None = None) -> int:
        if any((c.tenant_id, c.document_id, c.version) != (tenant_id, document_id, version)
               for c in chunks):
            raise ValueError("chunk metadata does not match document key")
        rows = []
        for chunk in chunks:
            rows.append((
                chunk.tenant_id, chunk.department, chunk.document_id, chunk.chunk_id,
                chunk.permission, chunk.version, chunk.text,
                json.dumps(self.embedding.embed(chunk.text)),
            ))
        insert_sql = (
            f"INSERT INTO {self.table} "
            "(tenant_id, department, document_id, chunk_id, permission, version, text, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS vector)) "
            "ON CONFLICT (tenant_id, document_id, version, chunk_id) DO UPDATE SET "
            "department = EXCLUDED.department, permission = EXCLUDED.permission, "
            "text = EXCLUDED.text, embedding = EXCLUDED.embedding"
        )
        if hasattr(self._c, "execute_transaction"):
            operations: list[tuple[str, tuple]] = []
            if allowed_permissions is not None:
                operations.append((
                    "SELECT agent_knowledge_assert_acl(?, ?, ?, CAST(? AS JSONB))",
                    (tenant_id, document_id, version,
                     json.dumps(allowed_permissions, ensure_ascii=False)),
                ))
            delete_sql = (
                f"DELETE FROM {self.table} WHERE tenant_id = ? AND document_id = ? AND version = ?"
            )
            delete_params: tuple = (tenant_id, document_id, version)
            if allowed_permissions is not None:
                placeholders = ", ".join("?" for _ in allowed_permissions)
                delete_sql += f" AND permission IN ({placeholders})"
                delete_params = (*delete_params, *allowed_permissions)
            operations.append((delete_sql, delete_params))
            operations.extend((insert_sql, row) for row in rows)
            operations.append((
                f"DELETE FROM {self.table} WHERE tenant_id = ? AND document_id = ? "
                "AND version = ? AND chunk_id >= ?",
                (tenant_id, document_id, version, len(rows)),
            ))
            try:
                self._c.execute_transaction(operations)
            except Exception as exc:
                if "knowledge_acl_denied" in str(exc):
                    raise PermissionError("role cannot replace this knowledge permission") from exc
                raise PgVectorUnavailable("pgvector document replace failed") from exc
            return len(rows)
        if allowed_permissions is not None:
            raise PgVectorUnavailable("authorized pgvector replacement requires transactions")
        removed = self.delete_document(tenant_id, document_id, version)
        self.add_many(chunks)
        return removed

    def delete_document(self, tenant_id: str, document_id: str, version: str,
                        allowed_permissions: list[str] | None = None) -> int:
        sql = (
            f"DELETE FROM {self.table} "
            "WHERE tenant_id = ? AND document_id = ? AND version = ?"
        )
        params: tuple = (tenant_id, document_id, version)
        if allowed_permissions is not None:
            placeholders = ", ".join("?" for _ in allowed_permissions)
            sql += f" AND permission IN ({placeholders})"
            params = (*params, *allowed_permissions)
        try:
            return int(self._c.execute_rowcount(sql, params))
        except Exception as e:  # noqa: BLE001
            raise PgVectorUnavailable(f"pgvector document delete failed: {e}") from e

    def document_exists(self, tenant_id: str, document_id: str, version: str) -> bool:
        try:
            return self._c.fetch_one(
                f"SELECT 1 AS present FROM {self.table} "
                "WHERE tenant_id = ? AND document_id = ? AND version = ? LIMIT 1",
                (tenant_id, document_id, version),
            ) is not None
        except Exception as e:  # noqa: BLE001
            raise PgVectorUnavailable(f"pgvector document lookup failed: {e}") from e

    def list_documents(self, tenant_id: str,
                       allowed_permissions: list[str] | None = None) -> list[dict]:
        allowed = list(["employee"] if allowed_permissions is None else allowed_permissions)
        if not allowed:
            return []
        placeholders = ", ".join("?" for _ in allowed)
        sql = (
            f"SELECT tenant_id, document_id, version, department, permission, "
            f"COUNT(*) AS chunk_count, MIN(created_at) AS created_at "
            f"FROM {self.table} "
            f"WHERE tenant_id = ? AND permission IN ({placeholders}) "
            f"GROUP BY tenant_id, document_id, version, department, permission "
            f"ORDER BY document_id, version"
        )
        try:
            return self._c.fetch_all(sql, tuple([tenant_id, *allowed]))
        except Exception as e:  # noqa: BLE001
            raise PgVectorUnavailable(f"pgvector document list failed: {e}") from e

    def add_many(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            vec = json.dumps(self.embedding.embed(c.text))
            try:
                self._c.execute(
                    f"INSERT INTO {self.table} "
                    "(tenant_id, department, document_id, chunk_id, permission, version, text, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS vector)) "
                    "ON CONFLICT (tenant_id, document_id, version, chunk_id) DO UPDATE SET "
                    "department = EXCLUDED.department, permission = EXCLUDED.permission, "
                    "text = EXCLUDED.text, embedding = EXCLUDED.embedding",
                    (c.tenant_id, c.department, c.document_id, c.chunk_id,
                     c.permission, c.version, c.text, vec),
                )
            except Exception as e:  # noqa: BLE001
                raise PgVectorUnavailable(f"pgvector document ingest failed: {e}") from e

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
        allowed = list(["employee"] if allowed_permissions is None else allowed_permissions)
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
            raise PgVectorUnavailable(f"pgvector search failed: {e}") from e

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
    return os.environ.get("RAG_PGVECTOR_ENABLED") == "1"


def build_knowledge_store(dsn: str | None = None, enabled: bool | None = None,
                          connector=None):
    """Build the configured backend; enabled pgvector never falls back to memory."""
    if enabled is None:
        enabled = pgvector_enabled()
    if not enabled:
        return KnowledgeStore()
    dsn = dsn or os.environ.get("RAG_PGVECTOR_DSN") or os.environ.get("AGENT_PG_DSN") \
        or os.environ.get("PG_DSN") or os.environ.get("POSTGRES_DSN")
    if connector is None and not dsn:
        raise PgVectorUnavailable("RAG_PGVECTOR_ENABLED=1 requires a pgvector DSN")
    return PGVectorStore(dsn=dsn, connector=connector)
