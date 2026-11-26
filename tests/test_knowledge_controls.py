"""P0/P1 knowledge ACL, strict pgvector and signed management API contracts."""
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from knowledge.document import Document
from knowledge.ingestion import ingest
from knowledge.store import KnowledgeStore

SECRET = "0123456789abcdef0123456789abcdef"
TENANT = "tenant-knowledge"


def _document(text="报销超过5000元需要财务审批。", *, document_id="finance-policy",
              version="v1", permission="finance", tenant_id=TENANT):
    return Document(text=text, tenant_id=tenant_id, department="finance",
                    document_id=document_id, version=version, permission=permission)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("employee", ["employee"]),
        ("manager", ["employee", "manager"]),
        ("finance", ["employee", "finance"]),
        ("admin", ["employee", "finance", "manager", "admin"]),
        ("viewer", []),
    ],
)
def test_knowledge_acl_is_derived_from_role(role, expected):
    from knowledge.service import permissions_for_role

    assert permissions_for_role(role) == expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("employee", ["employee"]),
        ("manager", ["employee", "manager"]),
        ("finance", ["employee", "finance"]),
        ("admin", ["employee", "finance", "manager", "admin"]),
    ],
)
def test_guarded_executor_overwrites_model_permissions_before_policy_and_schema(role, expected):
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor

    policy_args = []
    tool_args = []

    class Policy:
        def check(self, user, tool, args):
            policy_args.append(dict(args))
            return {"decision": "allow"}

    class Registry:
        def call(self, tool, args):
            tool_args.append(dict(args))
            return "ok"

    result = guarded_executor(
        {"tool": "knowledge.search", "args": {
            "query": "报销", "tenant_id": TENANT, "allowed_permissions": ["admin"],
        }},
        AgentState(task="knowledge"),
        user={"tenant_id": TENANT, "role": role, "user_id": "u1"},
        policy=Policy(), registry=Registry(),
    )

    assert result["outcome"] == "success"
    assert policy_args[0]["allowed_permissions"] == expected
    assert tool_args[0]["allowed_permissions"] == expected


def test_knowledge_tool_does_not_default_to_all_permissions():
    from agent.tools.builtin import build_default_registry
    from knowledge import service as service_module

    store = KnowledgeStore()
    store.add_many(ingest(_document()))
    service_module._service = service_module.KnowledgeService(store=store, seed=False)
    try:
        registry = build_default_registry()
        result = registry.call(
            "knowledge.search", {"query": "报销5000", "tenant_id": TENANT}
        )
        assert result == "no hits"
        from agent.runtime.state import AgentState
        from agent.tools.executor import guarded_executor
        from security.policy import PolicyEngine
        guarded = guarded_executor(
            {"tool": "knowledge.search", "args": {
                "query": "报销5000", "tenant_id": TENANT,
                "allowed_permissions": ["admin"],
            }},
            AgentState(task="knowledge"), registry=registry,
            user={"tenant_id": TENANT, "role": "employee", "user_id": "u1"},
            policy=PolicyEngine(),
        )
        assert guarded["result"] == "no hits"
    finally:
        service_module._service = None


def test_strict_pgvector_requires_dsn_and_propagates_schema_failure(monkeypatch):
    from knowledge.pgvector_store import PgVectorUnavailable, build_knowledge_store

    monkeypatch.setenv("RAG_PGVECTOR_ENABLED", "1")
    for name in ("RAG_PGVECTOR_DSN", "AGENT_PG_DSN", "PG_DSN", "POSTGRES_DSN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PgVectorUnavailable, match="DSN"):
        build_knowledge_store()

    class BrokenConnector:
        def execute(self, sql, params=()):
            raise RuntimeError("connection refused")

    with pytest.raises(PgVectorUnavailable, match="schema unavailable"):
        build_knowledge_store(connector=BrokenConnector(), enabled=True)

    class SearchFailConnector:
        def execute(self, sql, params=()):
            return None

        def fetch_all(self, sql, params=()):
            raise RuntimeError("connection lost")

    from knowledge.pgvector_store import PGVectorStore
    store = PGVectorStore(connector=SearchFailConnector(), dim=8)
    with pytest.raises(PgVectorUnavailable, match="search failed"):
        store.search("5000", TENANT, ["finance"])


def test_pgvector_factory_is_disabled_by_default_and_wires_enabled_backend(monkeypatch):
    import knowledge.pgvector_store as module

    monkeypatch.delenv("RAG_PGVECTOR_ENABLED", raising=False)
    assert isinstance(module.build_knowledge_store(), KnowledgeStore)

    calls = []

    class FakeStore:
        def __init__(self, dsn=None, connector=None):
            calls.append((dsn, connector))

    monkeypatch.setattr(module, "PGVectorStore", FakeStore)
    result = module.build_knowledge_store(dsn="postgresql://knowledge", enabled=True)
    assert isinstance(result, FakeStore)
    assert calls[-1][0] == "postgresql://knowledge"


def test_default_store_uses_shared_factory_once_and_seeds_idempotently(monkeypatch):
    from knowledge import seed as seed_module
    from knowledge import service as service_module

    store = KnowledgeStore()
    builds = []

    def builder():
        builds.append(1)
        return store

    monkeypatch.setattr(service_module, "_service", None)
    monkeypatch.setattr(service_module, "build_knowledge_store", builder)
    monkeypatch.setenv("AGENT_KNOWLEDGE_SEED_TENANT_ID", "t1")
    first = seed_module.get_default_store()
    second = seed_module.get_default_store()

    assert first is store
    assert second is store
    assert builds == [1]
    all_permissions = ["employee", "finance", "manager", "admin"]
    documents = first.list_documents("t1", all_permissions)
    assert len(documents) == 3
    service_module.get_knowledge_service()
    assert len(first.list_documents("t1", all_permissions)) == 3


def test_replace_cannot_overwrite_or_downgrade_hidden_document():
    from knowledge.service import KnowledgeService

    store = KnowledgeStore()
    store.add_many(ingest(_document()))
    service = KnowledgeService(store=store, seed=False)
    import pytest
    with pytest.raises(PermissionError):
        service.add_document(
            document_id="finance-policy", version="v1", department="finance",
            permission="employee", text="malicious replacement", tenant_id=TENANT,
            role="manager",
        )
    assert store.search("报销", TENANT, ["finance"])[0].permission == "finance"


def test_manager_cannot_delete_hidden_finance_document():
    from knowledge.service import KnowledgeService

    store = KnowledgeStore()
    store.add_many(ingest(_document()))
    service = KnowledgeService(store=store, seed=False)
    import pytest
    with pytest.raises(PermissionError):
        service.delete_document("finance-policy", "v1", TENANT, "manager")
    assert store.search("报销", TENANT, ["finance"])


def test_pgvector_acl_guard_runs_inside_document_transaction():
    from knowledge.pgvector_store import PGVectorStore

    class DenyingConnector(RecordingPgConnector):
        def execute_transaction(self, operations):
            self.statements.extend(operations)
            raise RuntimeError("knowledge_acl_denied")

    connector = DenyingConnector()
    store = PGVectorStore(connector=connector, dim=8)
    import pytest
    with pytest.raises(PermissionError):
        store.replace_document(
            TENANT, "finance-policy", "v1",
            ingest(_document(permission="employee")), allowed_permissions=["employee"],
        )
    sql = [statement for statement, _ in connector.statements]
    assert any("pg_advisory_xact_lock" in statement for statement in sql)
    assert any("knowledge_acl_denied" in statement for statement in sql)


def test_memory_store_replaces_and_deletes_exact_document_version():
    store = KnowledgeStore()
    store.add_many(ingest(_document("旧版本5000规则", version="v1")))
    store.add_many(ingest(_document("另一版本5000规则", version="v2")))
    store.add_many(ingest(_document(tenant_id="other-tenant")))

    replacement = ingest(_document("新版本5000规则", version="v1"))
    assert store.replace_document(TENANT, "finance-policy", "v1", replacement) == 1
    hits = store.search("5000", TENANT, ["finance"], top_k=10)
    assert len(hits) == 2
    assert {h.version for h in hits} == {"v1", "v2"}
    assert "新版本" in next(h.text for h in hits if h.version == "v1")

    assert store.delete_document(TENANT, "finance-policy", "v1") == 1
    assert store.delete_document(TENANT, "finance-policy", "v1") == 0
    assert [h.version for h in store.search("5000", TENANT, ["finance"])] == ["v2"]
    assert len(store.search("5000", "other-tenant", ["finance"])) == 1


class RecordingPgConnector:
    def __init__(self):
        self.statements = []
        self.rows = {}

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql.lstrip().upper().startswith("DELETE"):
            key = tuple(params[:3])
            self.rows = {k: v for k, v in self.rows.items() if k[:3] != key}
        elif "INSERT INTO" in sql and "ON CONFLICT" in sql:
            # tenant_id, department, document_id, chunk_id, permission, version, ...
            key = (params[0], params[2], params[5], params[3])
            self.rows[key] = tuple(params)

    def execute_rowcount(self, sql, params=()):
        before = len(self.rows)
        self.execute(sql, params)
        return before - len(self.rows)

    def fetch_all(self, sql, params=()):
        return []


def test_explicit_service_seed_also_requires_tenant(monkeypatch):
    from knowledge.service import KnowledgeService

    monkeypatch.delenv("AGENT_KNOWLEDGE_SEED_TENANT_ID", raising=False)
    with pytest.raises(ValueError, match="tenant_id is required"):
        KnowledgeService(store=KnowledgeStore(), seed=True)


def test_seed_requires_explicit_tenant():
    import pytest

    from knowledge.seed import seed_default_store

    with pytest.raises(ValueError, match="tenant_id is required"):
        seed_default_store(KnowledgeStore())


def test_seed_uses_requested_tenant_and_never_overwrites_edits():
    from knowledge.seed import seed_default_store

    store = KnowledgeStore()
    store.add_many(ingest(_document("管理员修订内容", tenant_id="tenant-a",
                                    document_id="expense-policy")))
    seed_default_store(store, "tenant-a")
    seed_default_store(store, "tenant-a")
    documents = store.list_documents("tenant-a", ["employee", "finance", "manager", "admin"])
    assert len(documents) == 3
    edited = store.search("管理员修订", "tenant-a", ["finance"])
    assert edited and "差旅报销制度" not in edited[0].text
    assert store.list_documents("t1", ["employee", "finance", "manager", "admin"]) == []


def test_pgvector_seed_replacement_is_idempotent_and_has_unique_key():
    from knowledge.pgvector_store import PGVectorStore
    from knowledge.seed import seed_default_store

    connector = RecordingPgConnector()
    store = PGVectorStore(connector=connector, dim=8)
    seed_default_store(store, "t1")
    seed_default_store(store, "t1")

    assert len(connector.rows) == 3
    assert len({(key[0], key[1], key[2]) for key in connector.rows}) == 3
    assert any("CREATE UNIQUE INDEX" in sql and "chunk_id" in sql
               for sql, _ in connector.statements)
    assert sum("ON CONFLICT" in sql for sql, _ in connector.statements) == 6
    assert store.delete_document("t1", "expense-policy", "v1") == 1
    assert len(connector.rows) == 2


def _headers(method, path, body=b"", *, query="", role="admin", tenant_id=TENANT):
    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex
    fields = [
        method.upper(), path, "&".join(sorted(query.split("&"))) if query else "",
        hashlib.sha256(body).hexdigest(), timestamp, nonce, "platform-agent",
        tenant_id, "user-knowledge", role, "", "", "",
    ]
    signature = hmac.new(SECRET.encode(), ("\n".join(fields) + "\n").encode(),
                         hashlib.sha256).hexdigest()
    return {
        "X-App-Id": "platform-agent", "X-Timestamp": timestamp, "X-Nonce": nonce,
        "X-Signature": signature, "X-Tenant-Id": tenant_id,
        "X-User-Id": "user-knowledge", "X-User-Role": role,
    }


def _json_headers(method, path, payload, **kwargs):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers = _headers(method, path, body, **kwargs)
    headers["Content-Type"] = "application/json"
    return headers, body


@pytest.fixture
def knowledge_client(monkeypatch):
    import apps.api.main as main
    from knowledge.service import KnowledgeService

    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_SECRET", SECRET)
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "true")
    monkeypatch.delenv("API_SECRET_INTERNAL_SERVICE", raising=False)
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.setattr(main, "_KNOWLEDGE", KnowledgeService(store=KnowledgeStore(), seed=False))
    return main, TestClient(main.app)


def test_signed_knowledge_document_api_list_ingest_search_delete(knowledge_client):
    _, client = knowledge_client
    path = "/knowledge/documents"
    payload = {
        "document_id": "finance-policy", "version": "v1", "department": "finance",
        "permission": "finance", "text": "报销超过5000元需要财务审批。",
        "tenant_id": "attacker-tenant", "allowed_permissions": ["admin"],
    }
    assert client.post(path, json=payload).status_code == 401
    headers, body = _json_headers("POST", path, payload)
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["tenant_id"] == TENANT
    assert "text" not in response.json()

    response = client.get(path, headers=_headers("GET", path))
    assert response.status_code == 200
    assert len(response.json()["documents"]) == 1
    assert "text" not in response.json()["documents"][0]
    other = _headers("GET", path, tenant_id="other-tenant")
    assert client.get(path, headers=other).json()["documents"] == []

    search_path = "/knowledge/search"
    query = "query=%E6%8A%A5%E9%94%805000"
    employee = _headers("GET", search_path, query=query, role="employee")
    assert client.get(search_path, params={"query": "报销5000"}, headers=employee).json()["results"] == []
    finance = _headers("GET", search_path, query=query, role="finance")
    assert client.get(search_path, params={"query": "报销5000"}, headers=finance).json()["results"]

    denied_headers, denied_body = _json_headers("POST", path, payload, role="employee")
    assert client.post(path, content=denied_body, headers=denied_headers).status_code == 403
    delete_path = path + "/finance-policy/v1"
    denied = _headers("DELETE", delete_path, role="employee")
    assert client.delete(delete_path, headers=denied).status_code == 403
    assert client.delete(delete_path, headers=_headers("DELETE", delete_path)).status_code == 200
    assert client.get(path, headers=_headers("GET", path)).json()["documents"] == []
