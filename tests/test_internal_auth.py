"""Trusted BFF -> Agent HMAC-SHA256-V2 contract and identity override tests."""
import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from agent.runtime.agent_task import AgentTask
from agent.runtime.state import AgentState
from agent.tools.registry import Registry, Tool
from security.approval import ApprovalStore, InMemoryApprovalRepository

SECRET = "test-internal-signature-secret-0123456789"
TENANT = "tenant-signed"
USER = "user-signed"
ROLE = "finance_auditor"


def _headers(method, path, body=b"", *, query="", nonce=None, timestamp=None,
             app_id="platform-agent-bff", tenant_id=TENANT, user_id=USER, role=ROLE,
             platform_token="", device_id="", idempotency_key="", secret=SECRET):
    """Independent signer: keep the test contract out of the implementation."""
    timestamp = str(int(time.time() * 1000) if timestamp is None else timestamp)
    nonce = nonce or uuid.uuid4().hex
    canonical_query = "&".join(sorted(query.split("&"))) if query else ""
    token_hash = hashlib.sha256(platform_token.encode("utf-8")).hexdigest() if platform_token else ""
    fields = [
        method.upper(),
        path,
        canonical_query,
        hashlib.sha256(body).hexdigest(),
        timestamp,
        nonce,
        app_id,
        tenant_id,
        user_id,
        role,
        token_hash,
        device_id,
        idempotency_key,
    ]
    payload = ("\n".join(fields) + "\n").encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    headers = {
        "X-App-Id": app_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id,
        "X-User-Role": role,
    }
    if platform_token:
        headers["X-Platform-Token"] = platform_token
    if device_id:
        headers["X-Device-ID"] = device_id
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _json_headers(method, path, payload, **kwargs):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = _headers(method, path, body, **kwargs)
    headers["Content-Type"] = "application/json"
    return headers, body


@pytest.fixture
def protected_app(monkeypatch):
    import apps.api.main as main
    from agent.runtime.paused_tasks import PausedTaskStore
    from security.tool_effect import InMemoryToolEffectRepository, ToolEffectStore

    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_SECRET", SECRET)
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "true")
    monkeypatch.delenv("API_SECRET_INTERNAL_SERVICE", raising=False)
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.setattr(main, "_APPROVALS", ApprovalStore(InMemoryApprovalRepository()))
    monkeypatch.setattr(main, "_PAUSED_TASKS", PausedTaskStore())
    monkeypatch.setattr(main, "_TOOL_EFFECTS", ToolEffectStore(InMemoryToolEffectRepository()))
    return main, TestClient(main.app)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/chat", {"message": "hi"}),
        ("POST", "/writing-assistant", {"scene": "PRODUCT_DESCRIPTION", "action": "GENERATE", "context": {"title": "背包"}}),
        ("POST", "/chat/task-1/resume", {"tenant_id": "t1"}),
        ("POST", "/approval/apr-missing/decide", {"decision": "approve"}),
        ("GET", "/capabilities", None),
        ("GET", "/workflows", None),
        ("POST", "/workflows/it_ops/runs", {"question": "diagnose"}),
        ("POST", "/knowledge/documents", {"document_id": "d1"}),
        ("POST", "/assistance/predict", {"session_id": "s1", "signals": {}}),
        ("POST", "/assistance/outcomes", {"session_id": "s1"}),
        ("GET", "/assistance/model/status", None),
        ("GET", "/assistance/dashboard/data", None),
    ],
)
def test_missing_signature_is_unauthorized(protected_app, method, path, payload):
    _, client = protected_app
    kwargs = {"json": payload} if payload is not None else {}
    response = client.request(method, path, **kwargs)
    assert response.status_code == 401


def test_malformed_signature_is_unauthorized(protected_app):
    _, client = protected_app
    headers = _headers("POST", "/chat", b'{"message":"hi"}')
    headers["X-Signature"] = "not-a-signature"
    response = client.post("/chat", content=b'{"message":"hi"}', headers=headers)
    assert response.status_code == 401


def test_body_tampering_is_forbidden(protected_app):
    _, client = protected_app
    signed_body = b'{"message":"original"}'
    headers = _headers("POST", "/chat", signed_body)
    response = client.post("/chat", content=b'{"message":"tampered"}', headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "header",
    ["X-App-Id", "X-Tenant-Id", "X-User-Id", "X-User-Role",
     "X-Platform-Token", "X-Device-ID", "Idempotency-Key"],
)
def test_signed_identity_tampering_is_forbidden(protected_app, header):
    _, client = protected_app
    body = b'{"message":"hi"}'
    headers = _headers("POST", "/chat", body, platform_token="user-jwt",
                       device_id="device-1", idempotency_key="idem-1")
    headers[header] = "attacker-controlled"
    response = client.post("/chat", content=body, headers=headers)
    assert response.status_code == 403


def test_stale_timestamp_is_unauthorized(protected_app):
    _, client = protected_app
    body = b'{"message":"hi"}'
    stale = int(time.time() * 1000) - 300_001
    headers = _headers("POST", "/chat", body, timestamp=stale)
    response = client.post("/chat", content=body, headers=headers)
    assert response.status_code == 401


def test_query_segments_are_signed_in_sorted_order(protected_app):
    _, client = protected_app
    body = b'{"message":"hi"}'
    headers = _headers("POST", "/chat", body, query="z=2&a=1&a=0")
    headers["Content-Type"] = "application/json"
    response = client.post("/chat?z=2&a=1&a=0", content=body, headers=headers)
    assert response.status_code == 200


def test_nonce_replay_conflicts(protected_app):
    _, client = protected_app
    headers = _headers("GET", "/capabilities", nonce="replay-nonce")
    first = client.get("/capabilities", headers=headers)
    second = client.get("/capabilities", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 409


def test_public_operational_endpoints_remain_public(protected_app):
    _, client = protected_app
    for path in ("/health", "/metrics", "/docs", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)


def test_signed_chat_identity_overrides_body(protected_app, monkeypatch):
    main, client = protected_app
    captured = {}

    def fake_run(state, **kwargs):
        captured.update(state.context)
        state.status = "done"
        state.answer = "ok"
        return state

    monkeypatch.setattr(main._runtime_loop, "run", fake_run)
    payload = {"message": "hi", "tenant_id": "body-tenant", "user_id": "body-user"}
    headers, body = _json_headers("POST", "/chat", payload)
    response = client.post("/chat", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["user_id"] == USER
    assert captured["tenant_id"] == TENANT
    assert captured["user"] == {"tenant_id": TENANT, "user_id": USER, "role": ROLE}


def test_signed_approver_and_tenant_override_body(protected_app):
    main, client = protected_app
    aid = main._APPROVALS.request(
        {"tenant_id": TENANT, "user_id": "requester", "role": "employee"},
        "scm.purchase.create",
        {"sku": "X", "tenant_id": TENANT},
    )
    payload = {"decision": "approve", "approver": "body-attacker", "tenant_id": "body-tenant"}
    headers, body = _json_headers("POST", f"/approval/{aid}/decide", payload, role="finance")
    response = client.post(f"/approval/{aid}/decide", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["approver"] == USER
    assert main._APPROVALS.get(aid)["approver"] == USER


def test_signed_self_approval_is_rejected(protected_app):
    main, client = protected_app
    aid = main._APPROVALS.request(
        {"tenant_id": TENANT, "user_id": USER, "role": "employee"},
        "scm.purchase.create", {"sku": "X", "tenant_id": TENANT},
    )
    headers, body = _json_headers(
        "POST", f"/approval/{aid}/decide",
        {"decision": "approve"}, role="manager",
    )
    response = client.post(f"/approval/{aid}/decide", content=body, headers=headers)
    assert response.status_code == 403
    assert main._APPROVALS.get(aid)["status"] == "pending"


def test_internal_auth_rejects_oversized_body_before_buffering(protected_app, monkeypatch):
    _, client = protected_app
    monkeypatch.setenv("AGENT_INTERNAL_MAX_BODY_BYTES", "4")
    response = client.post("/chat", content=b'{"message":"too large"}')
    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


def test_signed_async_chat_status_and_history(protected_app):
    import time

    _, client = protected_app
    payload = {"message": "async hello", "session_id": "session-async"}
    headers, body = _json_headers("POST", "/chat/async", payload, role="admin")
    response = client.post("/chat/async", content=body, headers=headers)
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]

    deadline = time.time() + 3
    while time.time() < deadline:
        status_headers = _headers("GET", f"/tasks/{task_id}", role="admin")
        status = client.get(f"/tasks/{task_id}", headers=status_headers)
        if status.status_code == 200 and status.json()["status"] == "done":
            break
        time.sleep(0.02)
    assert status.json()["status"] == "done", status.text
    assert "async hello" in status.json()["result"]["reply"]

    history_headers = _headers("GET", "/chat/sessions/session-async", role="admin")
    history = client.get("/chat/sessions/session-async", headers=history_headers)
    assert history.status_code == 200
    assert [item["role"] for item in history.json()["messages"]] == ["user", "assistant"]


def test_signed_workflow_execute_approve_resume(protected_app):
    _, client = protected_app
    execute_payload = {"expense": {"id": "E-api", "amount": 10000},
                       "thread_id": "caller-controlled"}
    headers, body = _json_headers("POST", "/workflow/expense/execute", execute_payload,
                                  role="finance")
    executed = client.post("/workflow/expense/execute", content=body, headers=headers)
    assert executed.status_code == 200, executed.text
    assert executed.json()["decision"] == "paused_human_review"
    aid = executed.json()["approval_id"]
    thread_id = executed.json()["thread_id"]
    assert thread_id != "caller-controlled"

    decide_headers, decide_body = _json_headers(
        "POST", f"/approval/{aid}/decide", {"decision": "approve"}, role="manager",
        user_id="manager-signed",
    )
    decided = client.post(f"/approval/{aid}/decide", content=decide_body, headers=decide_headers)
    assert decided.status_code == 200, decided.text

    resume_payload = {"approval_id": aid}
    resume_headers, resume_body = _json_headers(
        "POST", f"/workflow/expense/{thread_id}/resume", resume_payload, role="finance",
    )
    resumed = client.post(f"/workflow/expense/{thread_id}/resume",
                          content=resume_body, headers=resume_headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["decision"] == "human_review"


def test_signed_resume_uses_trusted_tenant(protected_app):
    main, client = protected_app
    task_id = "signed-resume-task"
    aid = main._APPROVALS.request(
        {"tenant_id": TENANT, "user_id": USER, "role": ROLE},
        "scm.purchase.create",
        {"sku": "X", "tenant_id": TENANT},
    )
    main._APPROVALS.reject(aid, "manager")
    state = AgentState(task="buy", task_id=task_id)
    state.status = "paused"
    state.context.update({
        "tenant_id": TENANT,
        "user": {"tenant_id": TENANT, "user_id": USER, "role": ROLE},
        "pause_reason": "need_approval",
        "approval_id": aid,
        "approval_tool": "scm.purchase.create",
        "approval_args": {"sku": "X", "tenant_id": TENANT},
    })
    task = AgentTask.from_state(state, tenant_id=TENANT)
    task.transition("running", "loop_start")
    task.transition("paused", "need_approval")
    state.context["agent_task"] = task
    main._PAUSED_TASKS.save(TENANT, task_id, state)

    headers, body = _json_headers(
        "POST", f"/chat/{task_id}/resume", {"tenant_id": "body-tenant"}
    )
    response = client.post(f"/chat/{task_id}/resume", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert main._PAUSED_TASKS.get(TENANT, task_id) is None


def test_signed_resume_rejects_a_different_user(protected_app):
    main, client = protected_app
    task_id = "signed-cross-user-task"
    aid = main._APPROVALS.request(
        {"tenant_id": TENANT, "user_id": USER, "role": ROLE},
        "scm.purchase.create",
        {"sku": "X", "tenant_id": TENANT},
    )
    main._APPROVALS.reject(aid, "manager")
    state = AgentState(task="buy", task_id=task_id)
    state.status = "paused"
    state.context.update({
        "tenant_id": TENANT,
        "user": {"tenant_id": TENANT, "user_id": USER, "role": ROLE},
        "pause_reason": "need_approval",
        "approval_id": aid,
        "approval_tool": "scm.purchase.create",
        "approval_args": {"sku": "X", "tenant_id": TENANT},
    })
    task = AgentTask.from_state(state, tenant_id=TENANT)
    task.transition("running", "loop_start")
    task.transition("paused", "need_approval")
    state.context["agent_task"] = task
    main._PAUSED_TASKS.save(TENANT, task_id, state)

    headers, body = _json_headers(
        "POST", f"/chat/{task_id}/resume", {"tenant_id": "body-tenant"},
        user_id="another-user",
    )
    response = client.post(f"/chat/{task_id}/resume", content=body, headers=headers)
    assert response.status_code == 403
    assert main._PAUSED_TASKS.get(TENANT, task_id) is not None


def test_chat_binds_but_never_persists_platform_token(protected_app, monkeypatch):
    main, client = protected_app
    from agent.runtime.paused_tasks import _state_to_payload
    from integrations.scm.context import current_scm_context

    captured = []

    def fake_run(state, **kwargs):
        captured.append(current_scm_context())
        state.status = "paused"
        state.answer = "paused"
        state.context.update({
            "pause_reason": "need_approval",
            "approval_id": "apr-token-test",
            "approval_tool": "scm.purchase.create",
            "approval_args": {"sku": "X", "tenant_id": TENANT},
        })
        task = AgentTask.from_state(state, tenant_id=TENANT)
        task.transition("running", "loop_start")
        task.transition("paused", "need_approval")
        state.context["agent_task"] = task
        return state

    monkeypatch.setattr(main._runtime_loop, "run", fake_run)
    headers, body = _json_headers(
        "POST", "/chat", {"message": "buy", "tenant_id": "body-tenant"},
        platform_token="user-jwt", device_id="device-1", idempotency_key="idem-1",
    )
    response = client.post("/chat", content=body, headers=headers)
    assert response.status_code == 200
    assert captured[0].platform_token == "user-jwt"
    saved = main._PAUSED_TASKS.get(TENANT, response.json()["task_id"])
    serialized = json.dumps(_state_to_payload(saved), default=str)
    assert "user-jwt" not in serialized
    assert "platform_token" not in saved.context
    assert current_scm_context() is None


def test_capabilities_are_stable_without_optional_mcp(protected_app, monkeypatch):
    import agent.tools.executor as executor_module

    _, client = protected_app
    registry = Registry()
    registry.register(Tool("z.tool", "z", lambda args: "z"))
    registry.register(Tool("a.tool", "a", lambda args: "a"))
    monkeypatch.setattr(executor_module, "_default_registry", registry)

    response = client.get("/capabilities", headers=_headers("GET", "/capabilities"))
    assert response.status_code == 200
    assert response.json() == {
        "tools": ["a.tool", "z.tool"],
        "knowledge": True,
        "memory": True,
        "workflows": True,
        "mcp": True,
        "mcpInstalled": False,
        "writingAssistant": False,
    }


def test_standalone_mode_keeps_body_identity_and_admin_default(monkeypatch):
    import apps.api.main as main

    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "false")
    monkeypatch.delenv("AGENT_INTERNAL_SIGNATURE_SECRET", raising=False)
    captured = []

    def fake_run(state, **kwargs):
        captured.append(dict(state.context["user"]))
        state.status = "done"
        state.answer = "ok"
        return state

    monkeypatch.setattr(main._runtime_loop, "run", fake_run)
    client = TestClient(main.app)
    response = client.post("/chat", json={"message": "hi", "tenant_id": "body-tenant",
                                          "user_id": "body-user"})
    assert response.status_code == 200
    assert captured == [{"tenant_id": "body-tenant", "user_id": "body-user", "role": "admin"}]


def test_legacy_shared_secret_name_is_accepted(protected_app, monkeypatch):
    _, client = protected_app
    monkeypatch.delenv("AGENT_INTERNAL_SIGNATURE_SECRET")
    monkeypatch.setenv("API_SECRET_INTERNAL_SERVICE", SECRET)
    response = client.get("/capabilities", headers=_headers("GET", "/capabilities"))
    assert response.status_code == 200


def test_canonical_payload_has_exact_v2_field_order_and_trailing_newline():
    from security.internal_auth import canonical_payload

    body = b'{"message":"hi"}'
    payload = canonical_payload(
        "post", b"/chat", b"b=2&a=1", body,
        "1700000000000", "nonce-123", "platform-agent", "tenant-1", "user-1", "admin",
        platform_token="user-jwt", device_id="device-1", idempotency_key="idem-1",
    )
    expected = (
        "POST\n/chat\na=1&b=2\n" + hashlib.sha256(body).hexdigest()
        + "\n1700000000000\nnonce-123\nplatform-agent\ntenant-1\nuser-1\nadmin\n"
        + hashlib.sha256(b"user-jwt").hexdigest() + "\ndevice-1\nidem-1\n"
    ).encode("utf-8")
    assert payload == expected
    signature = hmac.new(
        b"0123456789abcdef0123456789abcdef", payload, hashlib.sha256
    ).hexdigest()
    assert signature == "39aabe757f716509b1406f13cf6a361825e8392e4bea0494ad26b1fe7c88d708"


def test_v3_empty_body_and_optional_fields_fixed_vector():
    from security.internal_auth import canonical_payload, verify_signature

    secret = "0123456789abcdef0123456789abcdef"
    payload = canonical_payload(
        "GET", "/chat", "b=2&&a=1&", b"",
        "1700000000000", "nonce-123", "platform-agent", "tenant-1", "user-1", "admin",
    )
    expected = (
        b"GET\n/chat\na=1&b=2\n"
        b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        b"1700000000000\nnonce-123\nplatform-agent\ntenant-1\nuser-1\nadmin\n\n\n\n"
    )
    assert payload == expected
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert signature == "a1bc05fc3df040b1e1a864c7b4472485a9c95711a822d25689aa42a47bf13c4e"

    headers = {
        "X-App-Id": "platform-agent", "X-Timestamp": "1700000000000",
        "X-Nonce": "nonce-123", "X-Signature": signature,
        "X-Tenant-Id": "tenant-1", "X-User-Id": "user-1", "X-User-Role": "admin",
    }
    identity = verify_signature(
        "GET", "/chat", "b=2&&a=1&", b"", headers,
        secret=secret, now_ms=1700000000000,
    )
    assert identity.platform_token == ""
    assert identity.device_id == ""
    assert identity.idempotency_key == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "GET\n"), ("raw_path", "/chat\r"), ("raw_query", "a=1\n"),
        ("timestamp", "1700000000000\n"), ("nonce", "nonce\r"),
        ("app_id", "app\n"), ("tenant_id", "tenant\r"), ("user_id", "user\n"),
        ("role", "admin\r"), ("platform_token", "jwt\n"),
        ("device_id", "device\r"), ("idempotency_key", "idem\n"),
    ],
)
def test_canonical_payload_rejects_newlines_in_signed_fields(field, value):
    from security.internal_auth import canonical_payload

    kwargs = {
        "method": "GET", "raw_path": "/chat", "raw_query": "", "body": b"",
        "timestamp": "1700000000000", "nonce": "nonce", "app_id": "app",
        "tenant_id": "tenant", "user_id": "user", "role": "admin",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="newline"):
        canonical_payload(**kwargs)


@pytest.mark.parametrize(("field", "value"), [("method", "GET\n"),
                                               ("raw_path", "/chat\r"),
                                               ("raw_query", "a=1\n")])
def test_verify_signature_rejects_newlines_in_request_target(field, value):
    from security.internal_auth import InternalAuthError, verify_signature

    headers = _headers("GET", "/chat", timestamp=1700000000000, nonce="nonce-newline")
    request = {"method": "GET", "raw_path": "/chat", "raw_query": "", "body": b""}
    request[field] = value
    with pytest.raises(InternalAuthError) as exc:
        verify_signature(**request, headers=headers, secret=SECRET, now_ms=1700000000000)
    assert exc.value.status_code == 401


def test_v2_optional_headers_default_to_empty_signed_fields(protected_app):
    _, client = protected_app
    response = client.get("/capabilities", headers=_headers("GET", "/capabilities"))
    assert response.status_code == 200


def test_verified_identity_carries_delegation_without_token_in_repr():
    from security.internal_auth import TrustedIdentity, verify_signature

    body = b'{"message":"hi"}'
    headers = _headers("POST", "/chat", body, query="b=2&a=1", timestamp=1700000000000,
                       nonce="nonce-123", app_id="platform-agent", tenant_id="tenant-1",
                       user_id="user-1", role="admin", platform_token="user-jwt",
                       device_id="device-1", idempotency_key="idem-1",
                       secret="0123456789abcdef0123456789abcdef")
    identity = verify_signature("POST", "/chat", "b=2&a=1", body, headers,
                                secret="0123456789abcdef0123456789abcdef",
                                now_ms=1700000000000)
    assert isinstance(identity, TrustedIdentity)
    assert identity.platform_token == "user-jwt"
    assert identity.device_id == "device-1"
    assert identity.idempotency_key == "idem-1"
    assert "user-jwt" not in repr(identity)


@pytest.mark.parametrize("path", ["/workflows", "/workflows/it_ops/runs", "/knowledge/documents",
                                  "/knowledge/search"])
def test_new_api_paths_are_signature_protected(path):
    from security.internal_auth import is_protected_path

    assert is_protected_path(path) is True


def test_startup_rejects_short_configured_secret(monkeypatch):
    from security.internal_auth import validate_internal_signature_config

    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "true")
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_SECRET", "too-short")
    monkeypatch.delenv("API_SECRET_INTERNAL_SERVICE", raising=False)
    with pytest.raises(RuntimeError, match="32"):
        validate_internal_signature_config()


def test_inmemory_nonce_cache_is_atomic_and_expires(monkeypatch):
    import security.internal_auth as internal_auth

    store = internal_auth.InMemoryNonceStore(ttl_seconds=10)
    assert store.consume("nonce-1", now=100.0) is True
    assert store.consume("nonce-1", now=109.0) is False
    assert store.consume("nonce-1", now=111.0) is True


@pytest.mark.skipif(
    not os.environ.get("AGENT_TEST_PG_DSN"),
    reason="AGENT_TEST_PG_DSN not set; real Postgres nonce test skipped",
)
def test_postgres_nonce_consumption_and_expired_cleanup():
    from infrastructure.pg.connector import PGConnector
    from infrastructure.pg.schema import ensure_schema
    from security.internal_auth import PostgresNonceStore

    connector = PGConnector(os.environ["AGENT_TEST_PG_DSN"], pool_size=2)
    ensure_schema(connector)
    store = PostgresNonceStore(connector)
    nonce = f"nonce-{uuid.uuid4().hex}"
    expired = f"expired-{uuid.uuid4().hex}"
    try:
        connector.execute(
            "INSERT INTO agent_internal_nonce (nonce, expires_at) "
            "VALUES (?, now() - INTERVAL '1 second')",
            (expired,),
        )
        assert store.consume(nonce) is True
        assert store.consume(nonce) is False
        assert connector.fetch_one(
            "SELECT nonce FROM agent_internal_nonce WHERE nonce = ?", (expired,)
        ) is None
    finally:
        connector.execute("DELETE FROM agent_internal_nonce WHERE nonce IN (?, ?)",
                          (nonce, expired))
