"""并发回归：claim 单次生效 / approve+reject 冲突 / 租约回收 / 挂起任务 upsert 不重复。

仅在 ``AGENT_TEST_PG_DSN`` 存在时运行。CI 用一次性 Postgres service（trust 认证）
提供该 DSN，并由 "Concurrency and kill/restart drills must run, not skip" 步骤
保证这些用例真的执行。
"""
import os
import threading
import time
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
        connector.execute("DELETE FROM agent_tool_effect WHERE approval_id = ?", (approval_id,))
        connector.execute("DELETE FROM agent_approval WHERE id = ?", (approval_id,))
    if tenant and task_id:
        connector.execute(
            "DELETE FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
            (tenant, task_id),
        )


def test_concurrent_claim_only_one_wins(store, connector):
    """同一 approval 被 4 个线程同时 claim：恰好 1 个成功，其余 409 语义。"""
    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    store.approve(aid, "mgr-1")
    barrier = threading.Barrier(4)

    def worker(_):
        barrier.wait()
        try:
            return store.claim(aid)["claim_token"]
        except ValueError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        tokens = [r for r in results if r]
        assert len(tokens) == 1, results
        assert results.count(None) == 3, results
        assert store.get(aid)["status"] == "in_progress"
        assert store.get(aid)["consumed"] is False
        assert store.complete(aid, tokens[0])["consumed"] is True
        assert store.get(aid)["status"] == "done"
    finally:
        _cleanup(connector, approval_id=aid)


def test_expired_lease_is_reclaimed(store, connector):
    """租约 1 秒：过期后 reclaim_expired_claims 退回 approved，可再次 claim。"""
    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    store.approve(aid, "mgr-1")
    try:
        store.claim(aid, lease_seconds=1)
        assert store.get(aid)["status"] == "in_progress"

        time.sleep(1.5)
        assert store.reclaim_expired_claims() >= 1
        assert store.get(aid)["status"] == "approved"

        token = store.claim(aid, lease_seconds=60)["claim_token"]
        assert store.get(aid)["status"] == "in_progress"
        assert store.complete(aid, token)["status"] == "done"
    finally:
        _cleanup(connector, approval_id=aid)


def test_renew_extends_live_lease(store, connector):
    """心跳持续续期：即使超过原始租约，行仍存活且回收拿到 0 条。"""
    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    store.approve(aid, "mgr-1")
    try:
        token = store.claim(aid, lease_seconds=1)["claim_token"]
        for _ in range(8):
            assert store.renew(aid, token, 1) is True
            time.sleep(0.2)  # 累计 1.6s > 1s 原始租约
        assert store.get(aid)["status"] == "in_progress"
        live = connector.fetch_one(
            "SELECT (lease_until > now()) AS live FROM agent_approval WHERE id = ?",
            (aid,))
        assert live["live"] is True, live
        store.complete(aid, token)
    finally:
        _cleanup(connector, approval_id=aid)


def test_reclaimed_row_is_not_renewed(store, connector):
    """A 的租约过期被回收后，A 的续期 UPDATE 必须 0 行，心跳立即停止。"""
    from security.approval import LeaseHeartbeat

    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    store.approve(aid, "mgr-1")
    try:
        token_a = store.claim(aid, lease_seconds=1)["claim_token"]
        time.sleep(1.4)
        assert store.reclaim_expired_claims() >= 1
        assert store.get(aid)["status"] == "approved"

        # 续期条件更新含 status='in_progress' / claim_token / lease_until > now()，故 0 行。
        assert store.renew(aid, token_a, 30) is False

        heartbeat = LeaseHeartbeat(store, aid, token_a, lease_seconds=1, interval=0.1).start()
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline and not heartbeat.lost:
                time.sleep(0.05)
            assert heartbeat.lost is True
        finally:
            heartbeat.stop()

        # 回收后可以重新 claim 执行
        token_b = store.claim(aid, lease_seconds=30)["claim_token"]
        assert store.get(aid)["status"] == "in_progress"
        assert store.complete(aid, token_b)["status"] == "done"
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


def test_reclaim_respects_instance_ownership(store, connector):
    """claim 写入 claimed_by；本实例未过期租约不回收，他实例过期租约可回收。"""
    tenant = _tenant()
    own = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    other = store.request({"tenant_id": tenant, "user_id": "u1"},
                          TOOL, {"sku": "Y", "tenant_id": tenant})
    store.approve(own, "mgr-1")
    store.approve(other, "mgr-1")
    try:
        claimed = store.claim(own, lease_seconds=120, claimed_by="inst-self")
        assert claimed["claimed_by"] == "inst-self"
        assert store.reclaim_expired_claims(claimed_by="inst-self") == 0
        assert store.get(own)["status"] == "in_progress"

        store.claim(other, lease_seconds=1, claimed_by="inst-other")
        time.sleep(1.4)
        assert store.reclaim_expired_claims(claimed_by="inst-self") >= 1
        o = store.get(other)
        assert o["status"] == "approved", o
        assert o["claimed_by"] is None, o

        # 本实例自己的过期租约仍然可回收（只保护未过期的）。
        connector.execute(
            "UPDATE agent_approval SET lease_until = now() - INTERVAL '1 second' "
            "WHERE id = ?", (own,))
        assert store.reclaim_expired_claims(claimed_by="inst-self") >= 1
        assert store.get(own)["status"] == "approved"
    finally:
        _cleanup(connector, approval_id=own)
        _cleanup(connector, approval_id=other)


def test_tool_effect_pg_is_idempotent(connector):
    """PG 占位：succeeded 回放不执行；failed 只允许一次重试（attempt=2）。"""
    from security.tool_effect import (
        CACHED, CONFLICT, EXECUTE, build_tool_effect_store,
    )

    effects = build_tool_effect_store(connector)
    tenant = _tenant()
    ok_id = f"apr-eff-{uuid.uuid4().hex[:10]}"
    retry_id = f"apr-eff-{uuid.uuid4().hex[:10]}"
    try:
        d1 = effects.begin(tenant, ok_id, TOOL, {"sku": "A"})
        assert d1["decision"] == EXECUTE and d1["attempt"] == 1
        assert effects.succeed(tenant, ok_id, TOOL, {"sku": "A"}, "sha256:ref1") is True
        d2 = effects.begin(tenant, ok_id, TOOL, {"sku": "A"})
        assert d2["decision"] == CACHED, d2
        assert d2["result_ref"] == "sha256:ref1"

        r1 = effects.begin(tenant, retry_id, TOOL, {"sku": "B"})
        assert r1["decision"] == EXECUTE and r1["attempt"] == 1
        assert effects.fail(tenant, retry_id, TOOL, {"sku": "B"},
                            maybe_side_effect=False) is True
        r2 = effects.begin(tenant, retry_id, TOOL, {"sku": "B"})
        assert r2["decision"] == EXECUTE and r2["attempt"] == 2, r2
        assert effects.fail(tenant, retry_id, TOOL, {"sku": "B"},
                            maybe_side_effect=True) is True
        r3 = effects.begin(tenant, retry_id, TOOL, {"sku": "B"})
        assert r3["decision"] == CONFLICT, r3
        assert r3["reason"] == "retry budget exhausted", r3
        row = effects.get(tenant, retry_id, TOOL, {"sku": "B"})
        assert row["status"] == "failed" and row["attempt"] == 2, row
        assert bool(row["maybe_side_effect"]) is True, row
    finally:
        _cleanup(connector, approval_id=ok_id)
        _cleanup(connector, approval_id=retry_id)


def test_stale_claim_token_cannot_write_terminal_state(store, connector):
    """A 的租约被回收、B 重新 claim 后，A 用旧 token 写终态必须 0 行。

    行的 status / consumed / claim_token 全部保持 B 的值，A 不能覆盖。
    """
    tenant = _tenant()
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        TOOL, {"sku": "X", "tenant_id": tenant})
    store.approve(aid, "mgr-1")
    try:
        token_a = store.claim(aid, lease_seconds=1)["claim_token"]
        time.sleep(1.4)
        assert store.reclaim_expired_claims() >= 1
        token_b = store.claim(aid, lease_seconds=60)["claim_token"]
        assert token_b != token_a

        before = store.get(aid)
        assert before["status"] == "in_progress"
        assert before["consumed"] is False
        assert before["claim_token"] == token_b

        with pytest.raises(ValueError):
            store.complete(aid, token_a)
        with pytest.raises(ValueError):
            store.fail(aid, token_a, "stale token")

        after = store.get(aid)
        assert after["status"] == "in_progress", after
        assert after["consumed"] is False, after
        assert after["claim_token"] == token_b, after
        assert after["last_error"] is None, after

        # B 仍可正常完成。
        assert store.complete(aid, token_b)["status"] == "done"
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
