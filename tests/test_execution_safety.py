import contextvars
import threading
import time

from fastapi.testclient import TestClient

from agent.runtime.loop import run
from agent.runtime.planner import default_executor
from agent.runtime.state import AgentState
from agent.tools.executor import execute_with_timeout, guarded_executor, registry_executor
from agent.tools.registry import Registry, Tool
from security.approval import ApprovalStore
from security.policy import PolicyEngine
from security.tool_effect import CONFLICT, EXECUTE, InMemoryToolEffectRepository, ToolEffectStore


def test_guarded_executor_binds_trusted_tenant_before_policy_and_tool():
    captured = []

    class CapturingRegistry:
        def call(self, tool, args):
            captured.append(dict(args))
            return "ok"

    state = AgentState(task="check")
    user = {"tenant_id": "t2", "role": "admin", "user_id": "u2"}
    result = guarded_executor(
        {"tool": "metrics.get", "args": {}}, state,
        user=user, policy=PolicyEngine(), registry=CapturingRegistry(),
    )
    assert result["outcome"] == "success"
    assert captured == [{"tenant_id": "t2"}]

    approvals = ApprovalStore()
    paused = guarded_executor(
        {"tool": "scm.purchase.create", "args": {"sku": "X"}}, state,
        user=user, policy=PolicyEngine(), approvals=approvals,
    )
    assert paused["outcome"] == "approval_required"
    approval = approvals.get(state.context["approval_id"])
    assert approval["args"]["tenant_id"] == "t2"


def test_loop_bounds_slow_tool_execution(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.05")
    release = threading.Event()
    calls = []

    def planner(state):
        if state.observations:
            return {"action": "finish", "answer": "must not be reached"}
        return {"action": "call_tool", "tool": "metrics.get", "args": {}}

    def executor(plan, state):
        calls.append(plan["tool"])
        release.wait(0.2)
        return {"tool": plan["tool"], "result": "late", "outcome": "success"}

    started = time.monotonic()
    final = run(AgentState(task="slow tool"), planner=planner, executor=executor)
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.18
    assert calls == ["metrics.get"]
    assert final.status == "failed"
    assert final.answer == "tool_timeout"
    assert final.context["failure_code"] == "tool_timeout"
    assert final.context["agent_task"].status == "failed"


def test_timeout_worker_preserves_request_context():
    request_id = contextvars.ContextVar("request_id", default="")

    def read_context():
        return request_id.get()

    token = request_id.set("trace-123")
    try:
        assert execute_with_timeout(read_context, 1) == "trace-123"
    finally:
        request_id.reset(token)


def test_chat_returns_failed_when_first_tool_times_out(monkeypatch):
    import agent.tools.executor as executor_module
    import apps.api.main as main

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")

    def planner(state):
        return {"action": "call_tool", "tool": "metrics.get", "args": {}}

    class SlowRegistry:
        def call(self, tool, args):
            time.sleep(0.15)
            return "late"

    monkeypatch.setattr(main, "build_deepseek_planner", lambda client, model: planner)
    monkeypatch.setattr(executor_module, "_default_registry", SlowRegistry())
    response = TestClient(main.app).post("/chat", json={"message": "slow"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["reply"] == "tool_timeout"
    assert body["trace_id"]


def test_tool_outcomes_are_structured_not_parsed_from_text():
    class FailingRegistry:
        def call(self, tool, args):
            raise RuntimeError("private failure")

    state = AgentState(task="x")
    error = registry_executor({"tool": "metrics.get", "args": {}}, state, registry=FailingRegistry())
    assert error["outcome"] == "error"
    assert error["result"] == "tool error metrics.get"

    unknown = registry_executor({"tool": "missing", "args": {}}, state, registry=Registry())
    assert unknown["outcome"] == "unknown_tool"

    def broken_write(args):
        raise KeyError("response field missing after commit")

    known = Registry()
    known.register(Tool("payment.execute", "broken", broken_write))
    ambiguous = registry_executor(
        {"tool": "payment.execute", "args": {}}, state, registry=known,
    )
    assert ambiguous["outcome"] == "error"

    assert default_executor({"tool": "noop"}, state)["outcome"] == "success"

    denied = guarded_executor(
        {"tool": "scm.purchase.create", "args": {"tenant_id": "t2"}}, state,
        user={"tenant_id": "t1", "role": "viewer", "user_id": "u1"},
        policy=PolicyEngine(),
    )
    assert denied["outcome"] == "denied"

    user = {"tenant_id": "t1", "role": "admin", "user_id": "u1"}
    approvals = ApprovalStore()
    paused = guarded_executor(
        {"tool": "scm.purchase.create", "args": {"sku": "X"}}, state,
        user=user, policy=PolicyEngine(), approvals=approvals,
    )
    assert paused["outcome"] == "approval_required"


def test_loop_uses_structured_approval_outcome():
    def planner(state):
        if state.observations:
            return {"action": "finish", "answer": "must not be reached"}
        return {"action": "call_tool", "tool": "scm.purchase.create", "args": {}}

    final = run(
        AgentState(task="buy"),
        planner=planner,
        executor=lambda plan, state: {
            "tool": plan["tool"], "result": "human review needed", "outcome": "approval_required",
        },
    )
    assert final.status == "paused"
    assert final.context["agent_task"].status == "paused"


def test_ambiguous_failed_effect_blocks_automatic_retry():
    store = ToolEffectStore(InMemoryToolEffectRepository())
    args = {"sku": "X"}
    assert store.begin("t1", "apr-1", "scm.purchase.create", args)["decision"] == EXECUTE
    assert store.fail("t1", "apr-1", "scm.purchase.create", args, maybe_side_effect=True)

    decision = store.begin("t1", "apr-1", "scm.purchase.create", args)
    assert decision["decision"] == CONFLICT
    assert decision["attempt"] == 1
    assert decision["reason"] == "manual reconciliation required"
    assert store.get("t1", "apr-1", "scm.purchase.create", args)["status"] == "failed"


def test_stale_started_effect_requires_reconciliation():
    approvals = ApprovalStore()
    approvals.request({"tenant_id": "t1", "user_id": "u1"}, "scm.purchase.create", {"sku": "X"})
    store = ToolEffectStore(InMemoryToolEffectRepository())
    args = {"sku": "X"}
    assert store.begin("t1", "apr-1", "scm.purchase.create", args)["decision"] == EXECUTE
    store.get("t1", "apr-1", "scm.purchase.create", args)["updated_at"] = time.monotonic() - 10

    assert store.reclaim_stale_started(stale_seconds=1, approvals=approvals) == 1
    decision = store.begin("t1", "apr-1", "scm.purchase.create", args)
    assert decision["decision"] == CONFLICT
    assert decision["reason"] == "manual reconciliation required"
