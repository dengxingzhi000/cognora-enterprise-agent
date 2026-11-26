from knowledge.document import Document
from knowledge.embedding import OpenAICompatibleEmbeddingProvider
from knowledge.ingestion import ingest


def test_openai_compatible_embedding_uses_configured_endpoint(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("httpx.post", post)
    provider = OpenAICompatibleEmbeddingProvider(
        model="embed-model", base_url="https://embeddings.example", api_key="secret", dim=2,
    )
    assert provider.embed("hello") == [0.1, 0.2]
    assert captured["url"] == "https://embeddings.example/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_model_rerank_uses_configured_provider(monkeypatch):
    from knowledge.document import Chunk
    from knowledge.vector_index import rerank

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1},
            ]}

    def post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv("RAG_RERANK_PROVIDER", "cohere")
    monkeypatch.setenv("COHERE_API_KEY", "secret")
    monkeypatch.setattr("httpx.post", post)
    items = [
        {"chunk": Chunk("low", "t1", "", "d", "v1", "employee", "", 0),
         "lexical": 10, "vector": 0.9},
        {"chunk": Chunk("high", "t1", "", "d", "v1", "employee", "", 1),
         "lexical": 1, "vector": 0.1},
    ]
    ranked = rerank("query", items)
    assert [item["chunk"].text for item in ranked] == ["high", "low"]
    assert ranked[0]["rerank_provider"] == "cohere"
    assert captured["json"]["documents"] == ["low", "high"]


def test_pgvector_document_replacement_uses_one_transaction():
    from knowledge.pgvector_store import PGVectorStore

    class Connector:
        def __init__(self):
            self.operations = None

        def execute(self, sql, params=()):
            return None

        def execute_transaction(self, operations):
            self.operations = operations

    connector = Connector()
    store = PGVectorStore(connector=connector, dim=2)
    document = Document(
        text="policy", tenant_id="t1", department="finance",
        document_id="policy", version="v1", permission="finance",
    )
    store.replace_document("t1", "policy", "v1", ingest(document))
    assert connector.operations is not None
    assert connector.operations[0][0].startswith("DELETE FROM")
    assert any("ON CONFLICT" in sql for sql, _ in connector.operations)
    assert connector.operations[-1][0].endswith("chunk_id >= ?")


def test_paragraph_chunking_preserves_metadata_and_boundaries():
    document = Document(
        text="First policy paragraph.\n\nSecond policy paragraph.",
        tenant_id="t1", department="finance", document_id="policy",
        version="v2", permission="finance",
    )
    chunks = ingest(document, max_chars=25, overlap=5, strategy="paragraph")
    assert [chunk.text for chunk in chunks] == [
        "First policy paragraph.", "Second policy paragraph.",
    ]
    assert all(chunk.tenant_id == "t1" and chunk.version == "v2" for chunk in chunks)
