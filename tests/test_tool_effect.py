"""写工具幂等占位（agent_tool_effect）的离线用例，全部真实执行、不会 skip。

真实 Postgres 的占位/重试用例在 ``tests/test_approval_concurrency.py``。
"""
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from security.approval import ApprovalStore, InMemoryApprovalRepository
from security.tool_effect import (
    CACHED,
    CONFLICT,
    EXECUTE,
    InMemoryToolEffectRepository,
    ToolEffectStore,
    args_hash,
    result_ref,
)

TOOL = "scm.purchase.create"


def _effects() -> ToolEffectStore:
    return ToolEffectStore(InMemoryToolEffectRepository())


def _planner(state):
    if state.observations:
        return {"action": "finish", "answer": "done"}
    return {"action": "call_tool", "tool": TOOL,
            "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}


class _Registry:
    def __init__(self):
        self.calls = 0
        self.release = threading.Event()

    def call(self, tool, args):
        self.calls += 1
        return "ok"


class _CrashBeforeComplete:
    """包装 ApprovalStore：第一次 complete 抛错，模拟「工具已成功但进程崩溃」。"""

    def __init__(self, inner: ApprovalStore):
        self.inner = inner
        self.fail_next = True

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, approval_id, claim_token):
        if self.fail_next:
            self.fail_next = False
            raise ValueError("simulated crash before complete")
        return self.inner.complete(approval_id, claim_token)


def _prepare(monkeypatch):
    """把 main 的全局仓储换成全新的 InMemory 实现。"""
    import agent.tools.executor as executor_module
    import apps.api.main as main
    from infrastructure.pg import connector as connector_module
    from infrastructure.pg import schema as schema_module
    from agent.runtime.paused_tasks import PausedTaskStore

    monkeypatch.setattr(connector_module, "get_connector", lambda: None)
    monkeypatch.setattr(schema_module, "ensure_schema", lambda c: None)
    monkeypatch.setenv("AGENT_APPROVAL_LEASE_SECONDS", "30")
    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_ENABLED", "false")
    monkeypatch.delenv("AGENT_TOOL_TIMEOUT_SECONDS", raising=False)

    approvals = ApprovalStore(InMemoryApprovalRepository())
    effects = _effects()
    monkeypatch.setattr(main, "_APPROVALS", approvals)
    monkeypatch.setattr(main, "_TOOL_EFFECTS", effects)
    monkeypatch.setattr(main, "_PAUSED_TASKS", PausedTaskStore())
    registry = _Registry()
    monkeypatch.setattr(executor_module, "_default_registry", registry)
    monkeypatch.setattr(main, "build_deepseek_planner", lambda client, model: _planner)
    return TestClient(main.app), approvals, effects, registry


def _pause_and_approve(client: TestClient) -> tuple[str, str]:
    tenant = f"effect-{uuid.uuid4().hex[:8]}"
    body = client.post("/chat", json={"message": "buy X", "tenant_id": tenant,
                                     "user_id": "u1"}).json()
    assert body["status"] == "paused", body
    aid = body["approval_id"]
    decided = client.post(f"/approval/{aid}/decide",
                          json={"decision": "approve", "approver": "mgr-1",
                                "tenant_id": tenant})
    assert decided.status_code == 200, decided.text
    return tenant, body["task_id"]


# --- case 1: same approval + same tool + same args → tool runs once ---------

def test_retry_after_crash_replays_cached_effect_without_tool(monkeypatch):
    client, approvals, effects, registry = _prepare(monkeypatch)
    approver = _CrashBeforeComplete(approvals)
    import apps.api.main as main
    monkeypatch.setattr(main, "_APPROVALS", approver)

    tenant, task_id = _pause_and_approve(client)
    aid = _last_aid(approvals, tenant)
    approval = approvals.get(aid)

    first = client.post(f"/chat/{task_id}/resume", json={"tenant_id": tenant})
    assert first.status_code == 409, first.text
    assert registry.calls == 1

    effect = effects.get(tenant, aid, TOOL, approval["args"])
    assert effect["status"] == "succeeded", effect
    assert effect["maybe_side_effect"] is False

    # 模拟租约到期后重新可认领（相当于崩溃恢复）。
    approvals._repo._items[aid]["lease_until"] = -1
    assert approvals.reclaim_expired_claims() == 1

    second = client.post(f"/chat/{task_id}/resume", json={"tenant_id": tenant})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "done", body
    assert body["cached"] is True, body
    assert body["executed"] is False, body
    assert body["execution_result"].startswith("sha256:"), body
    assert registry.calls == 1, "cached effect must not invoke the tool again"
    assert approvals.get(aid)["status"] == "done"


def _last_aid(approvals: ApprovalStore, tenant: str) -> str:
    items = [i for i in approvals.list_items() if i["user"].get("tenant_id") == tenant]
    assert items, "approval was not created"
    return items[-1]["id"]


# --- case 2: placeholder status=started → explicit conflict, no parallel run -

def test_started_placeholder_blocks_a_second_execution(monkeypatch):
    client, approvals, effects, registry = _prepare(monkeypatch)
    tenant, task_id = _pause_and_approve(client)
    aid = _last_aid(approvals, tenant)
    args = approvals.get(aid)["args"]

    # 另一个执行（或已崩溃的进程）留下了 started 占位。
    decision = effects.begin(tenant, aid, TOOL, args)
    assert decision["decision"] == EXECUTE
    assert decision["status"] == "started"

    resumed = client.post(f"/chat/{task_id}/resume", json={"tenant_id": tenant})
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"] == "tool execution already started"
    assert registry.calls == 0
    # 冲突发生在执行前，claim 已退回 approved，不白等一个租约。
    row = approvals.get(aid)
    assert row["status"] == "approved", row
    assert row["claim_token"] is None, row


# --- case 3: failed effect allows exactly one retry, third attempt refused ---

def test_failed_effect_allows_one_retry_then_refuses():
    store = _effects()
    tenant, aid, args = "t1", "apr-retry", {"sku": "X"}

    first = store.begin(tenant, aid, TOOL, args)
    assert first["decision"] == EXECUTE and first["attempt"] == 1
    assert store.fail(tenant, aid, TOOL, args, maybe_side_effect=False) is True

    second = store.begin(tenant, aid, TOOL, args)
    assert second["decision"] == EXECUTE, second
    assert second["attempt"] == 2, second
    assert store.fail(tenant, aid, TOOL, args, maybe_side_effect=True) is True

    third = store.begin(tenant, aid, TOOL, args)
    assert third["decision"] == CONFLICT, third
    assert third["reason"] == "retry budget exhausted", third
    row = store.get(tenant, aid, TOOL, args)
    assert row["status"] == "failed" and row["attempt"] == 2, row
    assert row["maybe_side_effect"] is True, row


# --- case 4: args hash is stable; different args are separate effects --------

def test_args_hash_ignores_volatile_fields_and_key_order():
    a = args_hash({"b": 1, "a": {"y": 2, "x": 3}, "tenant_id": "t1",
                   "created_at": "2026-01-01"})
    b = args_hash({"a": {"x": 3, "y": 2}, "b": 1, "tenant_id": "t2",
                   "created_at": "2026-02-02"})
    assert a == b
    assert args_hash({"b": 1, "a": {"x": 3, "y": 9}}) != a


def test_different_args_create_distinct_effects():
    store = _effects()
    d1 = store.begin("t1", "apr-args", TOOL, {"sku": "A"})
    d2 = store.begin("t1", "apr-args", TOOL, {"sku": "B"})
    assert d1["decision"] == EXECUTE
    assert d2["decision"] == EXECUTE
    assert len(store.list_items()) == 2


def test_result_ref_is_a_digest_not_plaintext():
    ref = result_ref("customer-secret-payload")
    assert ref.startswith("sha256:")
    assert "secret" not in ref
    assert len(ref) == len("sha256:") + 32


def test_cached_decision_does_not_execute_and_returns_ref():
    store = _effects()
    assert store.begin("t1", "apr-c", TOOL, {"sku": "X"})["decision"] == EXECUTE
    assert store.succeed("t1", "apr-c", TOOL, {"sku": "X"}, "sha256:abc") is True
    cached = store.begin("t1", "apr-c", TOOL, {"sku": "X"})
    assert cached["decision"] == CACHED
    assert cached["status"] == "succeeded"
    assert cached["result_ref"] == "sha256:abc"
