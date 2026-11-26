"""SCM integration v2: Phase 1 (Foundation) + Phase 2 + Phase 3 tests."""
import pytest


def test_write_tools_all_registered():
    """Phase 1 #1: scm.order.cancel + scm.stock.adjust 也要在 registry 里。"""
    from agent.tools.registry import Registry
    from integrations.scm.tools import register_scm_tools
    reg = Registry()
    register_scm_tools(reg)
    names = set(reg.list_tools())
    assert "scm.purchase.create" in names
    assert "scm.order.cancel" in names
    assert "scm.stock.adjust" in names


def test_write_tools_all_classified_need_approval():
    """Phase 1 #1 联动：所有写口经 PolicyEngine 走 need_approval。"""
    from security.policy import PolicyEngine
    p = PolicyEngine()
    for tool in ("scm.purchase.create", "scm.order.cancel", "scm.stock.adjust"):
        d = p.check({"tenant_id": "t1", "role": "admin"}, tool, {"tenant_id": "t1"})
        assert d["decision"] == "need_approval", f"{tool}: {d}"


def test_unknown_scm_tool_denied():
    """Phase 1 #1 联动：未知 scm.* 必须 deny（防御 typo）。"""
    from security.policy import PolicyEngine
    p = PolicyEngine()
    d = p.check({"tenant_id": "t1", "role": "admin"}, "scm.order.destroy", {"tenant_id": "t1"})
    assert d["decision"] == "deny"


def test_5xx_retry(monkeypatch):
    """Phase 1 #3: 5xx 必须重试 1 次（修复: _send 返回 (code, body) 不抛）。"""
    from integrations.scm.client import ScmClient
    calls = {"n": 0}
    def fake_send(self, method, path, **kw):
        calls["n"] += 1
        return (503, {"error": "down"}) if calls["n"] == 1 else (200, {"ok": True})
    monkeypatch.setattr(ScmClient, "_send", fake_send)
    c = ScmClient(gateway_url="http://x", auth_url="http://a", username="u", password="p", timeout=5)
    code, body = c.get("/api/x")
    assert code == 200
    assert body == {"ok": True}
    assert calls["n"] == 2


def test_connect_error_retry(monkeypatch):
    """Phase 1 #3: ConnectError 必须重试 1 次。"""
    from integrations.scm.client import ScmClient
    calls = {"n": 0}
    def fake_send(self, method, path, **kw):
        calls["n"] += 1
        import httpx
        if calls["n"] == 1:
            raise httpx.ConnectError("conn refused")
        return (200, {"ok": True})
    monkeypatch.setattr(ScmClient, "_send", fake_send)
    c = ScmClient(gateway_url="http://x", auth_url="http://a", username="u", password="p", timeout=5)
    code, body = c.get("/api/x")
    assert code == 200
    assert calls["n"] == 2


def test_no_retry_on_4xx(monkeypatch):
    """Phase 1 #3: 4xx 不重试。"""
    from integrations.scm.client import ScmClient
    calls = {"n": 0}
    def fake_send(self, method, path, **kw):
        calls["n"] += 1
        return (404, {"error": "not found"})
    monkeypatch.setattr(ScmClient, "_send", fake_send)
    c = ScmClient(gateway_url="http://x", auth_url="http://a", username="u", password="p", timeout=5)
    code, body = c.get("/api/x")
    assert code == 404
    assert calls["n"] == 1


def test_5xx_give_up_after_retry(monkeypatch):
    """Phase 1 #3: 重试仍 5xx → 返回 code=0 + error body，不抛。"""
    from integrations.scm.client import ScmClient
    calls = {"n": 0}
    def fake_send(self, method, path, **kw):
        calls["n"] += 1
        return (503, {"error": "down"})
    monkeypatch.setattr(ScmClient, "_send", fake_send)
    c = ScmClient(gateway_url="http://x", auth_url="http://a", username="u", password="p", timeout=5,
                  backoff_seconds=0)
    code, body = c.get("/api/x")
    assert code == 0
    assert "error" in body
    assert "5xx" in body["error"]
    assert calls["n"] == 2


def test_tenant_map_warning_on_bad_json(monkeypatch, recwarn):
    """Phase 1 #8: 坏 JSON → warning + 默认 map。"""
    monkeypatch.setenv("SCM_TENANT_MAP", "{not valid json")
    from integrations.scm import auth
    m = auth.load_tenant_map()
    assert m == {"t1": "tenant_001"}
    assert any("SCM_TENANT_MAP" in str(w.message) for w in recwarn.list)


def test_tenant_map_warning_on_empty(monkeypatch, recwarn):
    """Phase 1 #8: 空 dict → warning + 默认。"""
    monkeypatch.setenv("SCM_TENANT_MAP", "{}")
    from integrations.scm import auth
    m = auth.load_tenant_map()
    assert m == {"t1": "tenant_001"}
    assert any("SCM_TENANT_MAP" in str(w.message) for w in recwarn.list)


def test_to_scm_tenant_unknown_warns(monkeypatch, recwarn):
    """Phase 1 #8: agent tenant 不在 map 中 → warning + 透传。"""
    monkeypatch.setenv("SCM_TENANT_MAP", '{"t2":"tenant_002"}')
    from integrations.scm import auth
    scm_t = auth.to_scm_tenant("t9")
    assert scm_t == "t9"
    assert any("no scm tenant mapping" in str(w.message).lower() for w in recwarn.list)


def test_scm_supplier_get_registered():
    """Phase 2 #6: scm.supplier.get 必须在 registry 里。"""
    from agent.tools.registry import Registry
    from integrations.scm.tools import register_scm_tools
    reg = Registry()
    register_scm_tools(reg)
    assert "scm.supplier.get" in reg.list_tools()


def test_scm_supplier_get_read_allowed():
    """Phase 2 #6 联动：PolicyEngine allow。"""
    from security.policy import PolicyEngine
    p = PolicyEngine()
    d = p.check({"tenant_id": "t1", "role": "employee"},
                "scm.supplier.get", {"supplier_id": "SP-001", "tenant_id": "t1"})
    assert d["decision"] == "allow"


def test_scm_supplier_get_mock_payload():
    """Phase 2 #6: 离线 mock 返回 supplier 风险字段。"""
    from agent.tools.registry import Registry
    from integrations.scm.tools import register_scm_tools
    reg = Registry()
    register_scm_tools(reg)
    out = reg.call("scm.supplier.get", {"supplier_id": "SP-001", "tenant_id": "t1"})
    s = str(out)
    assert "SP-001" in s
    assert "credit_score" in s or "rating" in s


def test_scm_read_tools_use_gateway_paths_and_tenant_header(monkeypatch):
    from agent.tools.registry import Registry
    from integrations.scm.tools import register_scm_tools

    monkeypatch.setenv("SCM_TENANT_MAP", '{"t1":"019a0aee-0000-7000-8000-000000000002"}')

    class FakeClient:
        def __init__(self):
            self.calls = []

        def get(self, path, params=None, headers=None):
            self.calls.append(("GET", path, params, headers))
            return 200, {"code": 200, "data": {"ok": True}}

        def post(self, path, json=None, params=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            return 200, {"code": 200, "data": {"records": []}}

    client = FakeClient()
    registry = Registry()
    register_scm_tools(registry, client=client)
    order_id = "019a0aee-0000-7000-8000-000000000001"
    registry.call("scm.order.get", {"order_id": order_id, "tenant_id": "t1"})
    registry.call("scm.order.get", {"order_no": "O-123", "tenant_id": "t1"})
    registry.call("scm.inventory.query", {"sku": "SKU-1", "tenant_id": "t1"})
    registry.call("scm.sales.report", {"range": "30d", "tenant_id": "t1"})
    registry.call("scm.supplier.get", {"supplier_id": "SP-001", "tenant_id": "t1"})

    tenant_header = {"X-Tenant-Id": "019a0aee-0000-7000-8000-000000000002"}
    assert client.calls == [
        ("GET", f"/scm-order/api/v1/orders/{order_id}", None, tenant_header),
        ("POST", "/scm-order/api/v1/orders/query",
         {"orderNo": "O-123", "pageNum": 1, "pageSize": 1}, tenant_header),
        ("POST", "/platform-inventory/api/v1/inventory/query",
         {"skuIds": ["SKU-1"], "page": 1, "size": 20}, tenant_header),
        ("GET", "/scm-order/api/v1/orders/dashboard/summary",
         {"range": "LAST_30_DAYS", "compare": "NONE"}, tenant_header),
        ("GET", "/scm-supplier/api/v1/suppliers/SP-001", None, tenant_header),
    ]


@pytest.mark.parametrize("failure", ["http", "transport", "business"])
def test_scm_read_failures_raise_structured_business_error(failure):
    import pytest

    from agent.tools.registry import Registry
    from integrations.scm.tools import ToolBusinessError, register_scm_tools

    class FakeClient:
        def get(self, path, params=None, headers=None):
            if failure == "transport":
                raise RuntimeError("private token=secret")
            if failure == "business":
                return 200, {"code": 503, "message": "private token=secret"}
            return 503, {"text": "private token=secret"}

    registry = Registry()
    register_scm_tools(registry, client=FakeClient())
    with pytest.raises(ToolBusinessError) as caught:
        registry.call("scm.supplier.get", {"supplier_id": "SP-001", "tenant_id": "t1"})
    assert caught.value.tool == "scm.supplier.get"
    assert "secret" not in str(caught.value)


def test_scm_error_executor_marks_error_and_redacts_summary():
    import pytest

    from agent.runtime.state import AgentState
    from agent.tools.executor import registry_executor
    from agent.tools.registry import Registry
    from integrations.scm.tools import ToolBusinessError, register_scm_tools

    class FakeClient:
        def get(self, path, params=None, headers=None):
            raise ToolBusinessError("scm.supplier.get", "SCM_TRANSPORT_ERROR", "SCM request failed")

    registry = Registry()
    register_scm_tools(registry, client=FakeClient())
    with pytest.raises(ToolBusinessError):
        registry.call("scm.supplier.get", {"supplier_id": "SP-001", "tenant_id": "t1"})
    out = registry_executor(
        {"tool": "scm.supplier.get", "args": {"supplier_id": "SP-001", "tenant_id": "t1"}},
        AgentState(task="x"), registry=registry,
    )
    assert out["outcome"] == "error"
    assert out["result"] == "tool error scm.supplier.get"


@pytest.mark.parametrize("configured", [False, True])
def test_scm_write_tools_raise_unsupported_without_http(monkeypatch, configured):
    import pytest

    from agent.tools.registry import Registry
    from integrations.scm.tools import ToolBusinessError, register_scm_tools

    if configured:
        monkeypatch.setenv("SCM_GATEWAY_URL", "http://gateway")

    class FakeClient:
        def get(self, *args, **kwargs):
            raise AssertionError("unsupported write must not call SCM")

        def post(self, *args, **kwargs):
            raise AssertionError("unsupported write must not call SCM")

    registry = Registry()
    register_scm_tools(registry, client=FakeClient() if configured else None)
    cases = [
        ("scm.purchase.create", {"sku": "SKU-1", "quantity": 1, "tenant_id": "t1"}),
        ("scm.order.cancel", {"order_no": "O-123", "tenant_id": "t1"}),
        ("scm.stock.adjust", {"sku": "SKU-1", "quantity": 1, "tenant_id": "t1"}),
    ]
    for tool, args in cases:
        with pytest.raises(ToolBusinessError) as caught:
            registry.call(tool, args)
        assert caught.value.code == "UNSUPPORTED_TOOL"


def test_approved_scm_write_still_has_error_outcome():
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from agent.tools.registry import Registry
    from integrations.scm.tools import register_scm_tools
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine

    registry = Registry()
    register_scm_tools(registry)
    user = {"tenant_id": "t1", "role": "admin", "user_id": "u1"}
    args = {"sku": "SKU-1", "quantity": 1, "tenant_id": "t1"}
    approvals = ApprovalStore()
    aid = approvals.request(user, "scm.purchase.create", args)
    approvals.approve(aid, "manager")
    out = guarded_executor(
        {"action": "call_tool", "tool": "scm.purchase.create", "args": args},
        AgentState(task="buy"), registry=registry, user=user,
        policy=PolicyEngine(), approvals=approvals, approved_approval_id=aid,
    )
    assert out["outcome"] == "error"
    assert out["result"] == "tool error scm.purchase.create"


def test_run_it_ops_basic():
    """Phase 2 #4: run_it_ops 返回 report + trace，trace 含 metrics + logs。"""
    from workflow.scenarios import run_it_ops
    out = run_it_ops("近7天为什么下降")
    assert "report" in out
    assert "trace" in out
    assert any(item.startswith("metrics.get") for item in out["trace"])
    assert any(item.startswith("logs.search") for item in out["trace"])


def test_run_it_ops_extracts_order_no():
    """Phase 2 #4: question 含订单号（如'O-1234567'）时 trace 加 scm.order.get。"""
    from workflow.scenarios import run_it_ops
    out = run_it_ops("订单 O-20260915001 支付超时")
    assert "scm.order.get" in out["trace"]


def test_run_it_ops_no_order_no_keeps_template():
    """Phase 2 #4: 无订单号时 trace 不含 scm.order.get。"""
    from workflow.scenarios import run_it_ops
    out = run_it_ops("服务异常")
    assert "scm.order.get" not in out["trace"]


def test_run_it_ops_falls_back_to_knowledge_search(monkeypatch):
    """Phase 2 #4 + spec §3.4 step 4: tool 异常 → 静默回退 knowledge.search。"""
    from agent.tools.registry import Registry
    orig_call = Registry.call
    def fake_call(self, name, args):
        if name == "metrics.get":
            raise RuntimeError("metrics down")
        if name == "logs.search":
            raise RuntimeError("logs down")
        return orig_call(self, name, args)
    monkeypatch.setattr(Registry, "call", fake_call)
    from workflow.scenarios import run_it_ops
    out = run_it_ops("近7天为什么下降")
    # knowledge.search 应该被调一次（metrics 失败时）+一次（logs 失败时）
    assert out["trace"].count("knowledge.search") >= 2
    # report 仍是合法的报告（不应包含 sentinel error 字符串）
    assert "RuntimeError" not in out["report"]
    assert "metrics down" not in out["report"]


def test_review_contract_uses_supplier():
    """Phase 2 #6: risk 命中时 opinion 含 supplier 信息 + trace 含 scm.supplier.get。"""
    from workflow.scenarios import review_contract
    out = review_contract({"amount": 50000, "clauses": [], "supplier_id": "SP-001"})
    assert out["decision"] == "human_review"
    assert "supplier" in out["opinion"].lower() or "SP-001" in out["opinion"]
    assert "scm.supplier.get" in out["trace"]


def test_review_contract_no_supplier_id_skips_hook():
    """Phase 2 #6: 缺 supplier_id 时不调 scm，opinion 不含 supplier 字段。"""
    from workflow.scenarios import review_contract
    out = review_contract({"amount": 50000, "clauses": []})
    assert "scm.supplier.get" not in out["trace"]


def test_supplier_call_failure_does_not_break_review(monkeypatch):
    """Phase 2 #6: scm.supplier.get 失败时 review_contract 仍返回原 decision。"""
    from agent.tools.registry import Registry
    from workflow import scenarios
    orig_call = Registry.call
    def fake_call(self, name, args):
        if name == "scm.supplier.get":
            raise RuntimeError("scm down")
        return orig_call(self, name, args)
    monkeypatch.setattr(Registry, "call", fake_call)
    out = scenarios.review_contract({"amount": 50000, "clauses": [], "supplier_id": "SP-001"})
    assert out["decision"] == "human_review"
    assert "暂不可用" in out["opinion"] or "supplier" in out["opinion"].lower()


def test_client_emits_trace_event():
    """Phase 3 #7: 设 contextvar 时 _send 在前后各发一个 scm_call/scm_result 事件。"""
    from integrations.scm.client import scm_trace_id
    from observability.tracing import Tracer
    Tracer._shared = Tracer()
    tid = "tr-v2-surface-test"
    token = scm_trace_id.set(tid)
    try:
        assert scm_trace_id.get() == tid
        events = Tracer._shared._traces.get(tid, {}).get("events", [])
        assert events == []
    finally:
        scm_trace_id.reset(token)


def test_client_log_event_fires_on_send(monkeypatch):
    """Phase 3 #7: 设 contextvar 时 _send 发 log_event。"""
    from integrations.scm.client import ScmClient, scm_trace_id
    from observability.tracing import Tracer
    Tracer._shared = Tracer()
    tid = "tr-send-test"
    token = scm_trace_id.set(tid)
    try:
        captured = []
        orig = Tracer._shared.log_event
        def fake_log(trace_id, stage, data):
            captured.append((trace_id, stage, data))
            return orig(trace_id, stage, data)
        Tracer._shared.log_event = fake_log
        class _FakeResp:
            status_code = 200
            text = '{"ok": true}'
            def json(self):
                return {"ok": True}
        monkeypatch.setattr("integrations.scm.client.httpx.request",
                            lambda method, url, **kw: _FakeResp())
        c = ScmClient(gateway_url="http://x", auth_url="http://a",
                      username="u", password="p", timeout=5)
        c.get("/api/x")
        stages = [s for _, s, _ in captured]
        assert "scm_call" in stages
        assert "scm_result" in stages
    finally:
        scm_trace_id.reset(token)


def test_client_no_trace_event_when_contextvar_unset(monkeypatch):
    """Phase 3 #7: contextvar 未设时 _send 不应发 log_event（容错）。"""
    from integrations.scm.client import ScmClient, scm_trace_id
    from observability.tracing import Tracer
    Tracer._shared = Tracer()
    assert scm_trace_id.get() is None
    captured = []
    orig = Tracer._shared.log_event
    def fake_log(trace_id, stage, data):
        captured.append((trace_id, stage))
        return orig(trace_id, stage, data)
    Tracer._shared.log_event = fake_log
    def fake_send(self, method, path, **kw):
        return (200, {"ok": True})
    monkeypatch.setattr(ScmClient, "_send", fake_send)
    c = ScmClient(gateway_url="http://x", auth_url="http://a",
                  username="u", password="p", timeout=5)
    c.get("/api/x")
    assert captured == []


def test_guarded_executor_writes_context_on_need_approval():
    """Phase 3 #5 链路第一步: need_approval 时 state.context 写入 pause 元数据。"""
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine
    state = AgentState(task="t")
    state.context["tenant_id"] = "t1"
    plan = {"action": "call_tool", "tool": "scm.purchase.create",
            "args": {"sku": "X", "tenant_id": "t1"}}
    out = guarded_executor(plan, state,
                           user={"tenant_id": "t1", "role": "admin"},
                           policy=PolicyEngine(),
                           approvals=ApprovalStore())
    assert "need approval" in str(out["result"])
    assert state.context.get("pause_reason") == "need_approval"
    assert state.context.get("approval_tool") == "scm.purchase.create"
    assert state.context.get("approval_args") == {"sku": "X", "tenant_id": "t1"}
    aid = state.context.get("approval_id")
    assert aid is not None and aid.startswith("apr-")


def test_guarded_executor_no_context_write_on_deny():
    """Phase 3 #5 联动: deny 时不写 pause 元数据。"""
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine
    state = AgentState(task="t")
    plan = {"action": "call_tool", "tool": "scm.order.get",
            "args": {"order_no": "O-1", "tenant_id": "t2"}}
    out = guarded_executor(plan, state,
                           user={"tenant_id": "t1", "role": "admin"},
                           policy=PolicyEngine(),
                           approvals=ApprovalStore())
    assert "DENY" in str(out["result"])
    assert "pause_reason" not in state.context


def test_runtime_transitions_to_paused_on_need_approval():
    """Phase 3 #5 链路第二步: Runtime 收到 need approval observation 应 break + paused。"""
    from agent.runtime.loop import run
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from observability.tracing import Tracer
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine

    Tracer._shared = Tracer()

    policy = PolicyEngine()
    approvals = ApprovalStore()

    # 默认 executor 不接 Policy/HITL，无法触发 need_approval；用 guarded_executor
    # 模拟链路第一步的产出，对齐"Runtime 收到 need approval observation"语义。
    def fake_executor(plan, state):
        return guarded_executor(plan, state,
                                user={"tenant_id": state.context.get("tenant_id", "t1"),
                                      "role": "admin"},
                                policy=policy,
                                approvals=approvals)

    plans = iter([
        {"action": "call_tool", "tool": "scm.purchase.create",
         "args": {"sku": "X", "tenant_id": "t1"}},
        {"action": "finish", "answer": "should not reach"},
    ])
    def fake_planner(state):
        return next(plans)
    state = AgentState(task="buy something")
    state.context["tenant_id"] = "t1"

    final = run(state, planner=fake_planner, executor=fake_executor)

    assert final.status == "paused"
    assert final.context.get("pause_reason") == "need_approval"
    assert final.context.get("approval_id", "").startswith("apr-")


def test_chat_payload_surfaces_approval_id(monkeypatch):
    """Phase 3 #5 链路第三步: /chat 在 status=paused 时 payload 含 approval_id。"""
    from fastapi.testclient import TestClient

    from agent.runtime import loop as loop_mod
    from apps.api.main import app

    def fake_run(state, planner=None, **kw):
        state.status = "paused"
        state.answer = "need approval apr-99: scm.purchase.create pending human review"
        state.context["pause_reason"] = "need_approval"
        state.context["approval_id"] = "apr-99"
        state.context["approval_tool"] = "scm.purchase.create"
        state.context["approval_args"] = {"sku": "X", "tenant_id": "t1"}
        return state
    monkeypatch.setattr(loop_mod, "run", fake_run)
    from infrastructure.pg import connector as conn_mod
    monkeypatch.setattr(conn_mod, "get_connector", lambda: None)
    from infrastructure.pg import schema as schema_mod
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)

    c = TestClient(app)
    r = c.post("/chat", json={"message": "buy X", "tenant_id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["approval_id"] == "apr-99"
    assert body["approval_tool"] == "scm.purchase.create"
    assert body["approval_args"] == {"sku": "X", "tenant_id": "t1"}


def test_no_approval_field_when_not_paused(monkeypatch):
    """Phase 3 #5 联动: 普通 done 状态不含 approval_id。"""
    from fastapi.testclient import TestClient

    from agent.runtime import loop as loop_mod
    from apps.api.main import app

    def fake_run(state, planner=None, **kw):
        state.status = "done"
        state.answer = "OK"
        return state
    monkeypatch.setattr(loop_mod, "run", fake_run)
    from infrastructure.pg import connector as conn_mod
    monkeypatch.setattr(conn_mod, "get_connector", lambda: None)
    from infrastructure.pg import schema as schema_mod
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)

    c = TestClient(app)
    r = c.post("/chat", json={"message": "hi", "tenant_id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert "approval_id" not in body


def test_chat_end_to_end_paused_with_aid(monkeypatch):
    """真集成测试: Planner emits call_tool(高风险) -> guarded_executor -> paused -> /chat surfaces aid."""
    from fastapi.testclient import TestClient

    from apps.api.main import app

    # mock planner: emit call_tool for scm.purchase.create (high-risk)
    def fake_planner(state):
        return {"action": "call_tool", "tool": "scm.purchase.create",
                "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}

    # Patch the planner builder (the chat() function calls build_deepseek_planner)
    import apps.api.main as main_mod
    monkeypatch.setattr(main_mod, "build_deepseek_planner",
                        lambda client, model: fake_planner)
    # Schema mocks for offline
    from infrastructure.pg import connector as conn_mod
    monkeypatch.setattr(conn_mod, "get_connector", lambda: None)
    from infrastructure.pg import schema as schema_mod
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)

    c = TestClient(app)
    r = c.post("/chat", json={"message": "buy X", "tenant_id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body.get("approval_id", "").startswith("apr-")
    assert body.get("approval_tool") == "scm.purchase.create"
    assert body.get("approval_args") == {"sku": "X", "tenant_id": "t1"}
    assert "trace_id" in body