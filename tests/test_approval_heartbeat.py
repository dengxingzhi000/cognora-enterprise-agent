"""执行期租约心跳：续期、丢租约即停、瞬时故障阈值、线程不泄漏。

这些用例不依赖 Postgres（InMemory repository），因此在 CI 与本地都会真实执行。
真实 Postgres 上的 renew / 回收用例在 ``tests/test_approval_concurrency.py``。
"""
import threading
import time
import uuid

from fastapi.testclient import TestClient

from apps.api.main import app
from security.approval import (
    ApprovalStore,
    InMemoryApprovalRepository,
    LeaseHeartbeat,
    heartbeat_interval_seconds,
)

TOOL = "scm.purchase.create"


def _store() -> ApprovalStore:
    return ApprovalStore(InMemoryApprovalRepository())


def _approved_claim(store: ApprovalStore, lease_seconds: int) -> str:
    aid = store.request({"tenant_id": "hb", "user_id": "u1"}, TOOL, {"sku": "X"})
    store.approve(aid, "mgr-1")
    store.claim(aid, lease_seconds=lease_seconds)
    return aid


def test_heartbeat_interval_defaults_to_third_of_lease(monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", raising=False)
    assert heartbeat_interval_seconds(300) == 100.0
    assert heartbeat_interval_seconds(6) == 2.0

    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", "0.5")
    assert heartbeat_interval_seconds(300) == 0.5

    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", "bogus")
    assert heartbeat_interval_seconds(6) == 2.0


def test_heartbeat_keeps_operation_alive_past_a_full_lease():
    """工具运行 1.5s > 1s 租约：心跳续期期间回收必须拿到 0 条。"""
    store = _store()
    aid = _approved_claim(store, lease_seconds=1)
    heartbeat = LeaseHeartbeat(store, aid, lease_seconds=1, interval=0.2).start()
    try:
        time.sleep(1.5)  # 超过一个完整租约
        assert heartbeat.lost is False
        assert heartbeat.failures == 0
        assert store.reclaim_expired_claims() == 0
        assert store.renew(aid, 1) is True
    finally:
        heartbeat.stop()

    assert store.complete(aid)["status"] == "done"
    assert store.get(aid)["consumed"] is True


def test_heartbeat_stops_when_renew_returns_zero_rows():
    """租约过期被回收后，续期 UPDATE 影响 0 行 → 立即判定丢失并停止。"""
    store = _store()
    aid = _approved_claim(store, lease_seconds=1)
    time.sleep(1.3)
    assert store.reclaim_expired_claims() == 1
    assert store.get(aid)["status"] == "approved"
    assert store.renew(aid, 5) is False

    heartbeat = LeaseHeartbeat(store, aid, lease_seconds=1, interval=0.1).start()
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and not heartbeat.lost:
            time.sleep(0.05)
        assert heartbeat.lost is True, "renew returned 0 rows but heartbeat kept running"
        assert heartbeat.failures == 0
    finally:
        heartbeat.stop()

    # 回收后可以重新认领执行
    assert store.claim(aid, lease_seconds=5)["status"] == "in_progress"


class _BrokenStore:
    """续期始终失败，模拟数据库瞬时不可用。"""

    def __init__(self):
        self.calls = 0

    def renew(self, approval_id, lease_seconds=None):
        self.calls += 1
        raise RuntimeError("db down")


def test_heartbeat_counts_transient_failures_then_gives_up():
    broken = _BrokenStore()
    heartbeat = LeaseHeartbeat(broken, "apr-broken", lease_seconds=1,
                               interval=0.05, max_failures=2).start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and not heartbeat.lost:
            time.sleep(0.05)
        assert heartbeat.lost is True
        assert heartbeat.failures == 2, heartbeat.failures
        assert broken.calls == 2, broken.calls
    finally:
        heartbeat.stop()


def test_heartbeat_threads_do_not_leak_across_twenty_resumes(monkeypatch):
    """10 次成功 + 10 次失败的 resume 后，线程数回到基线。"""
    import agent.tools.executor as executor_module
    import apps.api.main as main
    from infrastructure.pg import connector as connector_module
    from infrastructure.pg import schema as schema_module

    monkeypatch.setattr(connector_module, "get_connector", lambda: None)
    monkeypatch.setattr(schema_module, "ensure_schema", lambda c: None)

    flag = {"fail": False}

    def planner(state):
        if state.observations:
            return {"action": "finish", "answer": "done"}
        return {"action": "call_tool", "tool": TOOL,
                "args": {"sku": "X", "fail": flag["fail"],
                         "tenant_id": state.context.get("tenant_id", "t1")}}

    class Registry:
        def call(self, tool, args):
            if args.get("fail"):
                raise RuntimeError("leak-marker-heartbeat")
            return "ok"

    monkeypatch.setattr(main, "build_deepseek_planner", lambda client, model: planner)
    monkeypatch.setattr(executor_module, "_default_registry", Registry())

    client = TestClient(main.app)

    def run_one(fail: bool) -> int:
        flag["fail"] = fail
        tenant = f"hb-{uuid.uuid4().hex[:8]}"
        body = client.post("/chat", json={"message": "buy X", "tenant_id": tenant,
                                         "user_id": "u1"}).json()
        assert body["status"] == "paused", body
        aid = body["approval_id"]
        decided = client.post(f"/approval/{aid}/decide",
                              json={"decision": "approve", "approver": "mgr-1",
                                    "tenant_id": tenant})
        assert decided.status_code == 200, decided.text
        resumed = client.post(f"/chat/{body['task_id']}/resume",
                              json={"tenant_id": tenant})
        assert resumed.status_code == (502 if fail else 200), resumed.text
        return resumed.status_code

    # 预热一次，排除 TestClient 首次请求创建的线程。
    run_one(False)
    baseline = len(threading.enumerate())

    results = [run_one(i % 2 == 0) for i in range(20)]  # 10 failures, 10 successes
    assert results.count(200) == 10, results
    assert results.count(502) == 10, results

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if len(threading.enumerate()) == baseline:
            break
        time.sleep(0.05)

    remaining = [t.name for t in threading.enumerate()
                 if t.name.startswith("approval-heartbeat-")]
    after = len(threading.enumerate())
    print(f"thread count: baseline={baseline} after={after}")
    assert remaining == [], remaining
    assert after == baseline, (
        f"thread count did not return to baseline: {baseline} -> {after}")
