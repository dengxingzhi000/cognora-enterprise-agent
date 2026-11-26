"""RED-first contracts for delegated SCM, workflow API and knowledge API."""
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from security.internal_auth import TrustedIdentity

SECRET = "0123456789abcdef0123456789abcdef"
TENANT = "tenant-signed"
USER = "user-signed"


def _headers(method, path, body=b"", *, query="", nonce=None, timestamp=None,
             tenant_id=TENANT, user_id=USER, role="admin", platform_token="user-jwt",
             device_id="device-1", idempotency_key="idem-1"):
    timestamp = str(int(time.time() * 1000) if timestamp is None else timestamp)
    nonce = nonce or uuid.uuid4().hex
    token_hash = hashlib.sha256(platform_token.encode()).hexdigest() if platform_token else ""
    fields = [
        method.upper(), path, "&".join(sorted(query.split("&"))) if query else "",
        hashlib.sha256(body).hexdigest(), timestamp, nonce, "platform-agent",
        tenant_id, user_id, role, token_hash, device_id, idempotency_key,
    ]
    signature = hmac.new(SECRET.encode(), ("\n".join(fields) + "\n").encode(),
                         hashlib.sha256).hexdigest()
    headers = {
        "X-App-Id": "platform-agent", "X-Timestamp": timestamp, "X-Nonce": nonce,
        "X-Signature": signature, "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id, "X-User-Role": role,
    }
    if platform_token:
        headers["X-Platform-Token"] = platform_token
    if device_id:
        headers["X-Device-ID"] = device_id
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _json_headers(method, path, payload, **kwargs):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers = _headers(method, path, body, **kwargs)
    headers["Content-Type"] = "application/json"
    return headers, body


@pytest.fixture
def signed_app(monkeypatch):
    import apps.api.main as main
    from agent.runtime.paused_tasks import PausedTaskStore
    from security.approval import ApprovalStore, InMemoryApprovalRepository
    from security.tool_effect import InMemoryToolEffectRepository, ToolEffectStore

    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_SECRET", SECRET)
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "true")
    monkeypatch.delenv("API_SECRET_INTERNAL_SERVICE", raising=False)
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.setattr(main, "_APPROVALS", ApprovalStore(InMemoryApprovalRepository()))
    monkeypatch.setattr(main, "_PAUSED_TASKS", PausedTaskStore())
    monkeypatch.setattr(main, "_TOOL_EFFECTS", ToolEffectStore(InMemoryToolEffectRepository()))
    return main, TestClient(main.app)


def _identity(tenant_id=TENANT, user_id=USER, role="admin"):
    return TrustedIdentity(
        app_id="platform-agent", tenant_id=tenant_id, user_id=user_id, role=role,
        platform_token="user-jwt", device_id="device-1", idempotency_key="idem-1",
    )


def test_request_context_is_copied_into_timeout_worker_and_cleaned_up():
    from agent.tools.executor import execute_with_timeout
    from integrations.scm.context import current_scm_context, scm_identity_context

    with scm_identity_context(_identity()):
        assert current_scm_context().platform_token == "user-jwt"
        copied = execute_with_timeout(current_scm_context, timeout_seconds=1)
        assert copied.tenant_id == TENANT
        assert copied.user_id == USER
        assert copied.device_id == "device-1"
    assert current_scm_context() is None


def test_resume_executes_with_current_signed_delegation(signed_app, monkeypatch):
    main, client = signed_app
    from agent.runtime.agent_task import AgentTask
    from agent.runtime.state import AgentState
    from integrations.scm.context import current_scm_context

    task_id = "resume-context-task"
    args = {"sku": "X", "tenant_id": TENANT}
    aid = main._APPROVALS.request(
        {"tenant_id": TENANT, "user_id": USER, "role": "employee"},
        "scm.purchase.create", args,
    )
    main._APPROVALS.approve(aid, "manager")
    state = AgentState(task="buy", task_id=task_id)
    state.status = "paused"
    state.context.update({
        "tenant_id": TENANT,
        "user": {"tenant_id": TENANT, "user_id": USER, "role": "employee"},
        "pause_reason": "need_approval", "approval_id": aid,
        "approval_tool": "scm.purchase.create", "approval_args": args,
    })
    task = AgentTask.from_state(state, tenant_id=TENANT)
    task.transition("running", "loop_start")
    task.transition("paused", "need_approval")
    state.context["agent_task"] = task
    main._PAUSED_TASKS.save(TENANT, task_id, state)
    captured = []

    def fake_execute(plan, state, **kwargs):
        captured.append((current_scm_context(), kwargs["user"]))
        return {"tool": plan["tool"], "result": "ok", "outcome": "success"}

    monkeypatch.setattr(main, "guarded_executor", fake_execute)
    headers, body = _json_headers("POST", f"/chat/{task_id}/resume", {})
    response = client.post(f"/chat/{task_id}/resume", content=body, headers=headers)
    assert response.status_code == 200
    context, user = captured[0]
    assert context.platform_token == "user-jwt"
    assert context.device_id == "device-1"
    assert context.user_id == USER
    assert user == {"tenant_id": TENANT, "user_id": USER, "role": "admin"}


def test_delegated_scm_uses_user_token_and_never_logs_in_on_401(monkeypatch):
    from integrations.scm.client import ScmClient
    from integrations.scm.context import scm_identity_context

    captured = []

    class Response:
        status_code = 401
        text = '{"code":401}'

        def json(self):
            return {"code": 401, "message": "expired"}

    def request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return Response()

    def unexpected_login(*args, **kwargs):
        raise AssertionError("delegated authentication must not use the shared account")

    monkeypatch.setattr("integrations.scm.client.httpx.request", request)
    monkeypatch.setattr("integrations.scm.client.httpx.post", unexpected_login)
    monkeypatch.setenv("SCM_TENANT_MAP", json.dumps({TENANT: "must-not-be-used"}))
    client = ScmClient(gateway_url="http://gateway", auth_url="http://auth",
                       username="shared", password="shared", backoff_seconds=0)
    with scm_identity_context(_identity()):
        code, body = client.get("/scm-order/api/v1/orders/query")
    assert code == 401
    assert body["code"] == "SCM_AUTHENTICATION_FAILED"
    assert len(captured) == 1
    headers = captured[0][2]["headers"]
    assert headers["Authorization"] == "Bearer user-jwt"
    assert headers["X-Tenant-Id"] == TENANT
    assert headers["X-Device-ID"] == "device-1"
    assert headers["Idempotency-Key"] == "idem-1"


def test_missing_delegated_token_does_not_fall_back_to_shared_account(monkeypatch):
    from integrations.scm.client import ScmClient
    from integrations.scm.context import scm_identity_context

    def unexpected(*args, **kwargs):
        raise AssertionError("no HTTP request is allowed without a delegated token")

    monkeypatch.setattr("integrations.scm.client.httpx.request", unexpected)
    monkeypatch.setattr("integrations.scm.client.httpx.post", unexpected)
    identity = TrustedIdentity("platform-agent", TENANT, USER, "admin")
    client = ScmClient(gateway_url="http://gateway", auth_url="http://auth",
                       username="shared", password="shared")
    with scm_identity_context(identity):
        code, body = client.get("/scm-order/api/v1/orders/query")
    assert code == 401
    assert body["code"] == "SCM_AUTHENTICATION_FAILED"


def test_scm_read_tools_match_real_gateway_controller_contracts():
    from agent.tools.registry import Registry
    from integrations.scm.tools import register_scm_tools

    class FakeClient:
        def __init__(self):
            self.calls = []

        def get(self, path, params=None, headers=None):
            self.calls.append(("GET", path, params, headers))
            return 200, {"data": {"ok": True}}

        def post(self, path, json=None, params=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            return 200, {"data": {"records": []}}

    client = FakeClient()
    registry = Registry()
    register_scm_tools(registry, client=client)
    registry.call("scm.order.get", {"order_no": "O-123", "tenant_id": TENANT})
    registry.call("scm.inventory.query", {"sku": "SKU-1", "tenant_id": TENANT})
    registry.call("scm.sales.report", {"range": "7d", "tenant_id": TENANT})
    registry.call("scm.supplier.get", {"supplier_id": "SP-001", "tenant_id": TENANT})

    headers = {"X-Tenant-Id": TENANT}
    assert client.calls == [
        ("POST", "/scm-order/api/v1/orders/query",
         {"orderNo": "O-123", "pageNum": 1, "pageSize": 1}, headers),
        ("POST", "/platform-inventory/api/v1/inventory/query",
         {"skuIds": ["SKU-1"], "page": 1, "size": 20}, headers),
        ("GET", "/scm-order/api/v1/orders/dashboard/summary",
         {"range": "LAST_7_DAYS", "compare": "NONE"}, headers),
        ("GET", "/scm-supplier/api/v1/suppliers/SP-001", None, headers),
    ]


def test_scm_auth_failure_is_structured_error_not_success():
    from agent.tools.registry import Registry
    from integrations.scm.tools import ToolBusinessError, register_scm_tools

    class FakeClient:
        def get(self, path, params=None, headers=None):
            return 401, {"code": "SCM_AUTHENTICATION_FAILED"}

        def post(self, path, json=None, params=None, headers=None):
            return 401, {"code": "SCM_AUTHENTICATION_FAILED"}

    registry = Registry()
    register_scm_tools(registry, client=FakeClient())
    with pytest.raises(ToolBusinessError) as caught:
        registry.call("scm.order.get", {"order_no": "O-123", "tenant_id": TENANT})
    assert caught.value.code == "SCM_AUTHENTICATION_FAILED"
    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("scm.purchase.create", {"sku": "SKU-1", "quantity": 1, "tenant_id": TENANT}),
        ("scm.order.cancel", {"order_no": "O-123", "tenant_id": TENANT}),
        ("scm.stock.adjust", {"sku": "SKU-1", "quantity": 1, "tenant_id": TENANT}),
    ],
)
def test_unsafe_scm_write_tools_remain_explicitly_unsupported(tool, args):
    from agent.tools.registry import Registry
    from integrations.scm.tools import ToolBusinessError, register_scm_tools

    class FakeClient:
        def get(self, *args, **kwargs):
            raise AssertionError("unsupported writes must not call SCM")

        def post(self, *args, **kwargs):
            raise AssertionError("unsupported writes must not call SCM")

    registry = Registry()
    register_scm_tools(registry, client=FakeClient())
    with pytest.raises(ToolBusinessError) as caught:
        registry.call(tool, args)
    assert caught.value.code == "UNSUPPORTED_TOOL"


def test_workflow_catalog_and_read_only_runs_are_signed(signed_app):
    main, client = signed_app
    from workflow.registry import WorkflowRegistry

    registry = WorkflowRegistry()
    main._WORKFLOWS = registry
    assert client.get("/workflows").status_code == 401
    response = client.get("/workflows", headers=_headers("GET", "/workflows"))
    assert response.status_code == 200
    entries = {item["name"]: item for item in response.json()["workflows"]}
    assert entries["it_ops"]["runnable"] is True
    assert entries["contract"]["runnable"] is True
    assert entries["sales"]["runnable"] is True
    assert entries["expense"]["runnable"] is False


def test_workflow_runs_use_langgraph_and_trusted_context(signed_app, monkeypatch):
    main, client = signed_app
    from langgraph.graph.state import CompiledStateGraph

    from agent.tools.registry import Registry
    from integrations.scm.context import current_scm_context
    from workflow.registry import WorkflowRegistry

    registry = WorkflowRegistry()
    main._WORKFLOWS = registry
    assert isinstance(registry.graph_for("it_ops"), CompiledStateGraph)
    captured = []
    original = Registry.call

    def recording_call(self, name, args):
        captured.append((name, current_scm_context()))
        return original(self, name, args)

    monkeypatch.setattr(Registry, "call", recording_call)
    headers, body = _json_headers(
        "POST", "/workflows/it_ops/runs",
        {"question": "服务异常", "tenant_id": "body-tenant", "user_id": "body-user"},
    )
    response = client.post("/workflows/it_ops/runs", content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"run_id", "workflow", "status", "output"}
    assert payload["workflow"] == "it_ops"
    assert payload["status"] == "succeeded"
    assert payload["run_id"]
    assert captured
    assert all(ctx.tenant_id == TENANT and ctx.user_id == USER for _, ctx in captured)
    assert all(ctx.platform_token == "user-jwt" for _, ctx in captured)
    assert registry.get(TENANT, payload["run_id"])["tenant_id"] == TENANT
    assert registry.get("other-tenant", payload["run_id"]) is None


def test_contract_workflow_runs_with_validated_input(signed_app):
    main, client = signed_app
    from workflow.registry import WorkflowRegistry

    main._WORKFLOWS = WorkflowRegistry()
    headers, body = _json_headers(
        "POST", "/workflows/contract/runs",
        {"amount": 20000, "clauses": ["无限赔偿"], "supplier_id": "SP-001"},
    )
    response = client.post("/workflows/contract/runs", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["output"]["decision"] == "human_review"


@pytest.mark.parametrize(
    "raw_input",
    [
        b'{"amount":NaN,"clauses":[]}',
        b'{"amount":Infinity,"clauses":[]}',
        b'{"amount":true,"clauses":[]}',
        b'{"amount":100,"clauses":[],"data_path":"/etc/passwd"}',
        b'{"amount":100,"clauses":[],"path":"C:\\Windows\\win.ini"}',
        json.dumps({"amount": 100, "clauses": ["x" * 5000]}).encode(),
    ],
)
def test_workflow_rejects_nonfinite_types_paths_and_oversized_input(signed_app, raw_input):
    main, client = signed_app
    from workflow.registry import WorkflowRegistry

    main._WORKFLOWS = WorkflowRegistry()
    headers = _headers("POST", "/workflows/contract/runs", raw_input)
    headers["Content-Type"] = "application/json"
    response = client.post("/workflows/contract/runs", content=raw_input, headers=headers)
    assert response.status_code == 422


def test_workflow_unknown_and_nonrunnable_entries_fail_explicitly(signed_app):
    main, client = signed_app
    from workflow.registry import WorkflowRegistry

    main._WORKFLOWS = WorkflowRegistry()
    headers, body = _json_headers("POST", "/workflows/missing/runs", {})
    assert client.post("/workflows/missing/runs", content=body, headers=headers).status_code == 404
    headers, body = _json_headers("POST", "/workflows/expense/runs", {"amount": 100})
    response = client.post("/workflows/expense/runs", content=body, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKFLOW_NOT_RUNNABLE"


def test_sales_workflow_uses_delegated_scm_when_configured(signed_app, monkeypatch):
    main, client = signed_app
    from agent.tools.registry import Registry
    from integrations.scm.context import current_scm_context
    from workflow.registry import WorkflowRegistry

    main._WORKFLOWS = WorkflowRegistry()
    monkeypatch.setenv("SCM_GATEWAY_URL", "http://gateway")
    captured = []
    original = Registry.call

    def recording_call(self, name, args):
        if name == "scm.sales.report":
            captured.append(current_scm_context())
            return {"data": {"currentRevenue": 123, "currentOrders": 2}}
        return original(self, name, args)

    monkeypatch.setattr(Registry, "call", recording_call)
    headers, body = _json_headers(
        "POST", "/workflows/sales/runs", {"question": "销售趋势", "range": "7d"},
    )
    response = client.post("/workflows/sales/runs", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert captured[0].tenant_id == TENANT
    assert captured[0].user_id == USER
    assert captured[0].platform_token == "user-jwt"
    assert "scm.sales.report" in response.json()["output"]["trace"]


def _document_payload(**overrides):
    payload = {
        "document_id": "finance-secret", "version": "v1", "department": "finance",
        "permission": "finance", "text": "报销超过5000元需要财务审批。",
    }
    payload.update(overrides)
    return payload


def test_knowledge_ingest_list_search_and_tenant_isolation(signed_app):
    main, client = signed_app
    from knowledge.service import KnowledgeService
    from knowledge.store import KnowledgeStore

    main._KNOWLEDGE = KnowledgeService(store=KnowledgeStore(), seed=False)
    assert client.get("/knowledge/documents").status_code == 401
    headers, body = _json_headers(
        "POST", "/knowledge/documents",
        {**_document_payload(), "tenant_id": "body-tenant", "user_id": "body-user",
         "allowed_permissions": ["admin"]},
    )
    response = client.post("/knowledge/documents", content=body, headers=headers)
    assert response.status_code == 201
    metadata = response.json()
    assert metadata["tenant_id"] == TENANT
    assert "text" not in metadata

    response = client.get("/knowledge/documents", headers=_headers("GET", "/knowledge/documents"))
    assert response.status_code == 200
    assert len(response.json()["documents"]) == 1
    assert "text" not in response.json()["documents"][0]

    response = client.get("/knowledge/search", params={"query": "报销5000"},
                          headers=_headers("GET", "/knowledge/search", query="query=%E6%8A%A5%E9%94%805000"))
    assert response.status_code == 200
    assert "5000" in response.json()["results"][0]["text"]

    other_headers = _headers("GET", "/knowledge/documents", tenant_id="tenant-other")
    assert client.get("/knowledge/documents", headers=other_headers).json()["documents"] == []
    other_headers = _headers("GET", "/knowledge/search", query="query=5000", tenant_id="tenant-other")
    assert client.get("/knowledge/search?query=5000", headers=other_headers).json()["results"] == []


def test_knowledge_permissions_are_role_derived_not_body_controlled(signed_app):
    main, client = signed_app
    from knowledge.service import KnowledgeService
    from knowledge.store import KnowledgeStore

    main._KNOWLEDGE = KnowledgeService(store=KnowledgeStore(), seed=False)
    headers, body = _json_headers("POST", "/knowledge/documents", _document_payload())
    assert client.post("/knowledge/documents", content=body, headers=headers).status_code == 201

    employee_headers = _headers("GET", "/knowledge/search", query="query=5000", role="employee")
    response = client.get("/knowledge/search?query=5000", headers=employee_headers)
    assert response.json()["results"] == []

    employee_headers, body = _json_headers(
        "POST", "/knowledge/documents", _document_payload(document_id="denied"), role="employee"
    )
    assert client.post("/knowledge/documents", content=body, headers=employee_headers).status_code == 403

    finance_headers = _headers("GET", "/knowledge/search", query="query=5000", role="finance")
    assert client.get("/knowledge/search?query=5000", headers=finance_headers).json()["results"]


@pytest.mark.parametrize(
    "override",
    [
        {"document_id": ""}, {"document_id": "../escape"}, {"version": ""},
        {"version": "../v1"}, {"department": ""}, {"permission": "root"}, {"text": "   "},
    ],
)
def test_knowledge_metadata_validation(signed_app, override):
    main, client = signed_app
    from knowledge.service import KnowledgeService
    from knowledge.store import KnowledgeStore

    main._KNOWLEDGE = KnowledgeService(store=KnowledgeStore(), seed=False)
    headers, body = _json_headers("POST", "/knowledge/documents", _document_payload(**override))
    assert client.post("/knowledge/documents", content=body, headers=headers).status_code == 422


def test_agent_knowledge_tool_and_api_share_one_builder_and_store(monkeypatch):
    from agent.tools.builtin import build_default_registry
    from integrations.scm.context import scm_identity_context
    from knowledge import service as service_module
    from knowledge.store import KnowledgeStore

    store = KnowledgeStore()
    builds = []

    def builder():
        builds.append(1)
        return store

    monkeypatch.setattr(service_module, "_service", None)
    monkeypatch.setattr(service_module, "build_knowledge_store", builder)
    service = service_module.get_knowledge_service()
    service.add_document(
        document_id="shared-doc", version="v1", department="finance", permission="finance",
        text="API与Agent必须共享同一个知识库。", tenant_id=TENANT, role="admin",
    )
    with scm_identity_context(_identity(role="finance")):
        result = build_default_registry().call(
            "knowledge.search", {"query": "共享知识库", "tenant_id": "body-tenant"}
        )
    assert "shared-doc" in result
    assert len(builds) == 1
    assert service_module.get_knowledge_service() is service


def test_pgvector_configuration_fails_closed_instead_of_empty_memory_fallback(monkeypatch):
    from knowledge.pgvector_store import PgVectorUnavailable, build_knowledge_store

    monkeypatch.setenv("RAG_PGVECTOR_ENABLED", "1")
    monkeypatch.delenv("RAG_PGVECTOR_DSN", raising=False)
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    with pytest.raises(PgVectorUnavailable):
        build_knowledge_store()
