"""Batch 4: embedding / 向量检索 / rerank / pgvector 回落。"""
import math
import os

import pytest

from knowledge.document import Document
from knowledge.embedding import HashingEmbeddingProvider
from knowledge.ingestion import ingest
from knowledge.store import KnowledgeStore
from knowledge.vector_index import rerank


def _chunks(text, doc_id="d1", tenant="t1", permission="employee"):
    return ingest(Document(text=text, tenant_id=tenant, department="general",
                           document_id=doc_id, version="v1", permission=permission))


def test_hashing_embedding_deterministic_and_normalized():
    p = HashingEmbeddingProvider(dim=64)
    a = p.embed("订单服务 5xx 超时排查")
    b = p.embed("订单服务 5xx 超时排查")
    assert a == b
    assert len(a) == 64
    assert abs(math.sqrt(sum(x * x for x in a)) - 1.0) < 1e-9


def test_vector_retrieval_hits_paraphrase_and_ranks_first():
    store = KnowledgeStore()
    store.add_many(_chunks("订单服务出现 5xx 错误时先检查网关，再看支付服务超时。",
                           doc_id="ops"))
    store.add_many(_chunks("普通员工午餐补贴每天 20 元。", doc_id="lunch"))
    store.add_many(_chunks("合同预付款条款需要财务审批。", doc_id="contract"))
    hits = store.search("网关 502 报错怎么排查", tenant_id="t1",
                        allowed_permissions=["employee"], top_k=2)
    assert hits
    assert "网关" in hits[0].text


def test_rag_tenant_isolation_even_with_same_text():
    store = KnowledgeStore()
    store.add_many(_chunks("订单 5xx 排查手册", doc_id="d-t1", tenant="t1"))
    store.add_many(_chunks("订单 5xx 排查手册", doc_id="d-t2", tenant="t2"))
    hits = store.search("订单 5xx", tenant_id="t1", allowed_permissions=["employee"])
    assert len(hits) == 1
    assert hits[0].tenant_id == "t1"
    assert store.search("订单 5xx", tenant_id="t3", allowed_permissions=["employee"]) == []


def test_rag_permission_filter_cannot_be_bypassed_by_vector_recall():
    store = KnowledgeStore()
    store.add_many(_chunks("报销超过 5000 元需财务审批", doc_id="finance",
                           permission="finance"))
    store.add_many(_chunks("午餐补贴 20 元", doc_id="employee",
                           permission="employee"))
    hits = store.search("报销 5000 审批", tenant_id="t1",
                        allowed_permissions=["employee"])
    assert all(h.permission == "employee" for h in hits)
    assert all(h.document_id != "finance" for h in hits)


def test_rerank_can_weight_vector_over_lexical():
    a = ingest(Document(text="精确词面命中", tenant_id="t1", document_id="a"))
    b = ingest(Document(text="语义相关但词面不同", tenant_id="t1", document_id="b"))
    items = [
        {"chunk": a[0], "lexical": 10.0, "vector": 0.0},
        {"chunk": b[0], "lexical": 0.0, "vector": 1.0},
    ]
    lexical_first = rerank("精确", items, lexical_weight=0.65, vector_weight=0.35)
    assert lexical_first[0]["chunk"].document_id == "a"
    vector_first = rerank("精确", items, lexical_weight=0.2, vector_weight=0.8)
    assert vector_first[0]["chunk"].document_id == "b"
    assert vector_first[0]["rerank_score"] > vector_first[1]["rerank_score"]


def test_build_knowledge_store_defaults_off_and_falls_back(monkeypatch):
    monkeypatch.delenv("RAG_PGVECTOR_ENABLED", raising=False)
    monkeypatch.delenv("RAG_PGVECTOR_DSN", raising=False)
    from knowledge.pgvector_store import build_knowledge_store, pgvector_enabled
    assert pgvector_enabled() is False
    store = build_knowledge_store()
    assert isinstance(store, KnowledgeStore)
    assert not hasattr(store, "_c")


@pytest.mark.skipif(not os.environ.get("AGENT_TEST_PG_DSN"),
                    reason="AGENT_TEST_PG_DSN not set; pgvector integration skipped")
def test_pgvector_roundtrip_and_tenant_isolation():
    from knowledge.pgvector_store import PGVectorStore, PgVectorUnavailable

    tenant = "test-rag-tenant"
    try:
        store = PGVectorStore(dsn=os.environ["AGENT_TEST_PG_DSN"])
    except PgVectorUnavailable as e:
        pytest.skip(f"pgvector extension unavailable on test database: {e}")

    try:
        store._c.execute("DELETE FROM agent_knowledge_chunk WHERE tenant_id = ?", (tenant,))
        store.add_many(_chunks("订单服务 5xx 先查网关", doc_id="pg-ops", tenant=tenant))
        store.add_many(_chunks("午餐补贴 20 元", doc_id="pg-lunch", tenant=tenant,
                               permission="employee"))
        hits = store.search("网关 5xx 排查", tenant_id=tenant,
                            allowed_permissions=["employee"], top_k=2)
        assert hits
        assert hits[0].document_id in ("pg-ops", "pg-lunch")
        assert all(h.tenant_id == tenant for h in hits)
        assert store.search("网关 5xx", tenant_id="other", allowed_permissions=["employee"]) == []
    finally:
        store._c.execute("DELETE FROM agent_knowledge_chunk WHERE tenant_id = ?", (tenant,))
