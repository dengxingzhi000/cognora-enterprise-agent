"""并发回归：consume 单次生效 / approve+reject 冲突 / 挂起任务 upsert 不重复。

仅在 ``AGENT_TEST_PG_DSN`` 存在时运行。CI 用一次性 Postgres service（trust 认证）
提供该 DSN，并由 "Concurrency cases must run, not skip" 步骤保证这些用例真的执行。
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

DSN = os.environ.get("AGENT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="AGENT_TEST_PG_DSN not set; real Postgres concurrency test skipped"
)

TENANT_PREFIX = "conc"
TOOL = "scm.purchase.create"


@pytest.fixture(scope="module")
def connector():
    from infrastructure.pg.connector import PGConnector
    from infrastructure.pg.schema import ensure_schema

    c = PGConnector(DSN, pool_size=4)
    assert c.healthcheck()
    ensure_schema(c)
    return c


@pytest.fixture
def store(connector):
    from security.approval import ApprovalStore, PostgresApprovalRepository

    return ApprovalStore(PostgresApprovalRepository(connector))


def _tenant() -> str:
    return f"{TENANT_PREFIX}-{uuid.uuid4().hex[:8]}"


def _cleanup(connector, *, approval_id=None, tenant=None, task_id=None) -> None:
    if approval_id:
        connector.execute("DELETE FROM agent_approval WHERE id = ?", (approval_id,))
    if tenant and task_id:
        connector.execute(
            "DELETE FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
            (tenant, task_id),
        )


def test_concurrent_consume_only_one_wins(store, connector):
    """同一 approval 被 4 个线程同时 consume：恰好 1 个成功。"""
    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    store.approve(aid, "mgr-1")
    barrier = threading.Barrier(4)

    def worker(_):
        barrier.wait()
        try:
            store.consume(aid)
            return "ok"
        except ValueError:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        assert results.count("ok") == 1, results
        assert results.count("conflict") == 3, results
        assert store.get(aid)["consumed"] is True
    finally:
        _cleanup(connector, approval_id=aid)


def test_concurrent_approve_and_reject_one_wins(store, connector):
    """并发 approve + reject：一个生效，另一个拿到 ApprovalConflict。"""
    from security.approval import ApprovalConflict

    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    barrier = threading.Barrier(2)

    def decide(decision: str, approver: str) -> str:
        barrier.wait()
        try:
            if decision == "approve":
                return store.approve(aid, approver)["status"]
            return store.reject(aid, approver)["status"]
        except ApprovalConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            approve_f = pool.submit(decide, "approve", "mgr-a")
            reject_f = pool.submit(decide, "reject", "mgr-b")
            results = [approve_f.result(), reject_f.result()]

        assert results.count("conflict") == 1, results
        winner = results[0] if results[0] != "conflict" else results[1]
        assert winner in ("approved", "rejected")
        assert store.get(aid)["status"] == winner
    finally:
        _cleanup(connector, approval_id=aid)


def test_concurrent_paused_task_upsert_keeps_single_row(connector):
    """8 个线程对同一 (tenant_id, task_id) upsert：表里只有 1 行。"""
    from agent.runtime.paused_tasks import PausedTaskStore, PostgresPausedTaskRepository
    from agent.runtime.state import AgentState

    paused = PausedTaskStore(PostgresPausedTaskRepository(connector))
    tenant = _tenant()
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    barrier = threading.Barrier(8)

    def save(i: int) -> None:
        state = AgentState(task=f"concurrent pause {i}", task_id=task_id)
        state.context["tenant_id"] = tenant
        state.status = "paused"
        barrier.wait()
        paused.save(tenant, task_id, state)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(save, range(8)))

        rows = connector.fetch_all(
            "SELECT count(*) AS n FROM agent_paused_task "
            "WHERE tenant_id = ? AND task_id = ?", (tenant, task_id))
        assert rows[0]["n"] == 1, rows
        restored = paused.get(tenant, task_id)
        assert restored is not None and restored.status == "paused"
    finally:
        _cleanup(connector, tenant=tenant, task_id=task_id)
