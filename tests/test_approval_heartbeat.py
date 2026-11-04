"""执行期心跳/claim token/阈值余量/工具超时/审批指标。

全部离线执行（InMemory），CI 与本地都会真实运行（不会 skip）。真实 Postgres
上的续期/回收/旧 token 用例在 ``tests/test_approval_concurrency.py``，真进程
演练在 ``tests/test_kill_restart_drill.py``。
"""
import threading
import time
import uuid

import pytest
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


def _approved_claim(store: ApprovalStore, lease_seconds: int) -> tuple[str, str]:
    aid = store.request({"tenant_id": "hb", "user_id": "u1"}, TOOL, {"sku": "X"})
    store.approve(aid, "mgr-1")
    claimed = store.claim(aid, lease_seconds=lease_seconds)
    return aid, claimed["claim_token"]


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
    aid, token = _approved_claim(store, lease_seconds=1)
    heartbeat = LeaseHeartbeat(store, aid, token, lease_seconds=1, interval=0.2).start()
    try:
        time.sleep(1.5)  # 超过一个完整租约
        assert heartbeat.lost is False
        assert heartbeat.failures == 0
        assert store.reclaim_expired_claims() == 0
        assert store.renew(aid, token, 1) is True
    finally:
        heartbeat.stop()

    assert store.complete(aid, token)["status"] == "done"
    assert store.get(aid)["consumed"] is True


def test_heartbeat_stops_when_renew_returns_zero_rows():
    """租约过期被回收后，续期 UPDATE 影响 0 行 → 立即判定丢失并停止。"""
    store = _store()
    aid, token = _approved_claim(store, lease_seconds=1)
    time.sleep(1.3)
    assert store.reclaim_expired_claims() == 1
    assert store.get(aid)["status"] == "approved"
    assert store.renew(aid, token, 5) is False

    heartbeat = LeaseHeartbeat(store, aid, token, lease_seconds=1, interval=0.1).start()
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

    def renew(self, approval_id, claim_token, lease_seconds=None):
        self.calls += 1
        raise RuntimeError("db down")


def test_heartbeat_counts_transient_failures_then_gives_up():
    broken = _BrokenStore()
    heartbeat = LeaseHeartbeat(broken, "apr-broken", "tok-broken", lease_seconds=1,
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


# --- 阈值余量：max_failures * interval < lease，不满足则拒绝启动 ----------

def test_validate_heartbeat_settings_enforces_margin(monkeypatch):
    from security.approval import HeartbeatConfigError, validate_heartbeat_settings

    monkeypatch.delenv("AGENT_APPROVAL_HEARTBEAT_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", raising=False)

    # 默认 2 * 100s < 300s，通过。
    validate_heartbeat_settings(lease_seconds=300, interval=100.0, max_failures=2)

    with pytest.raises(HeartbeatConfigError):
        validate_heartbeat_settings(lease_seconds=3, interval=2.0, max_failures=2)
    with pytest.raises(HeartbeatConfigError):
        validate_heartbeat_settings(lease_seconds=3, interval=1.0, max_failures=3)

    # 默认推导 interval=lease/3 时总是满足。
    monkeypatch.setenv("AGENT_APPROVAL_LEASE_SECONDS", "3")
    validate_heartbeat_settings()

    # 环境变量组合违反约束时，无参调用直接拒绝。
    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", "2")
    with pytest.raises(HeartbeatConfigError):
        validate_heartbeat_settings()

    # 心跳关闭时不校验（演练「心跳缺席」仍允许启动）。
    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_ENABLED", "false")
    validate_heartbeat_settings(lease_seconds=3, interval=2.0, max_failures=2)


def test_startup_refuses_invalid_margin(monkeypatch):
    """启动阶段 fail-fast：配置错误不静默降级。"""
    import apps.api.main as main
    from security.approval import HeartbeatConfigError

    monkeypatch.delenv("AGENT_APPROVAL_HEARTBEAT_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_APPROVAL_LEASE_SECONDS", "4")
    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", "3")

    with pytest.raises(HeartbeatConfigError):
        main._startup()


# --- 工具级超时 -------------------------------------------------------------

def test_tool_timeout_config_default_and_env(monkeypatch):
    from agent.tools.executor import DEFAULT_TOOL_TIMEOUT_SECONDS, tool_timeout_seconds

    monkeypatch.delenv("AGENT_TOOL_TIMEOUT_SECONDS", raising=False)
    assert tool_timeout_seconds() == float(DEFAULT_TOOL_TIMEOUT_SECONDS) == 120.0

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "5")
    assert tool_timeout_seconds() == 5.0

    for bad in ("bogus", "0", "-1"):
        monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", bad)
        assert tool_timeout_seconds() == 120.0


def test_execute_with_timeout_abandons_stuck_tool():
    from agent.tools.executor import ToolTimeout, execute_with_timeout

    release = threading.Event()
    try:
        with pytest.raises(ToolTimeout):
            execute_with_timeout(lambda: release.wait(5), timeout_seconds=0.2)
    finally:
        release.set()


def test_api_tool_timeout_marks_failed_and_stops_heartbeat(monkeypatch):
    """卡死的工具不会靠心跳续命永远停在 in_progress。"""
    import agent.tools.executor as executor_module
    import apps.api.main as main
    from infrastructure.pg import connector as connector_module
    from infrastructure.pg import schema as schema_module
    from agent.runtime.paused_tasks import PausedTaskStore

    monkeypatch.setattr(connector_module, "get_connector", lambda: None)
    monkeypatch.setattr(schema_module, "ensure_schema", lambda c: None)
    # 租约 1s、间隔 0.2s（2*0.2 < 1 满足余量约束），工具超时 0.5s。
    monkeypatch.setenv("AGENT_APPROVAL_LEASE_SECONDS", "1")
    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS", "0.2")
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.delenv("AGENT_APPROVAL_HEARTBEAT_ENABLED", raising=False)

    store = _store()
    monkeypatch.setattr(main, "_APPROVALS", store)
    monkeypatch.setattr(main, "_PAUSED_TASKS", PausedTaskStore())
    from security.tool_effect import InMemoryToolEffectRepository, ToolEffectStore
    effects = ToolEffectStore(InMemoryToolEffectRepository())
    monkeypatch.setattr(main, "_TOOL_EFFECTS", effects)

    def planner(state):
        if state.observations:
            return {"action": "finish", "answer": "done"}
        return {"action": "call_tool", "tool": TOOL,
                "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}

    release = threading.Event()

    class Registry:
        def call(self, tool, args):
            release.wait(10)
            return "ok"

    monkeypatch.setattr(main, "build_deepseek_planner", lambda client, model: planner)
    monkeypatch.setattr(executor_module, "_default_registry", Registry())

    try:
        client = TestClient(main.app)
        tenant = f"timeout-{uuid.uuid4().hex[:8]}"
        body = client.post("/chat", json={"message": "buy X", "tenant_id": tenant,
                                         "user_id": "u1"}).json()
        assert body["status"] == "paused", body
        aid = body["approval_id"]
        assert client.post(f"/approval/{aid}/decide",
                           json={"decision": "approve", "approver": "mgr-1",
                                 "tenant_id": tenant}).status_code == 200

        resumed = client.post(f"/chat/{body['task_id']}/resume",
                              json={"tenant_id": tenant})
        assert resumed.status_code == 504, resumed.text
        assert resumed.json()["detail"] == "approved action timed out"

        row = store.get(aid)
        assert row["status"] == "failed", row
        assert row["status"] != "in_progress"
        assert row.get("consumed") is False
        assert row["last_error"] == "tool timeout", row

        # effect 被标为 failed，且带「可能已产生副作用」标志（线程无法强杀）。
        effect_rows = effects.list_items()
        assert len(effect_rows) == 1, effect_rows
        assert effect_rows[0]["status"] == "failed", effect_rows
        assert effect_rows[0]["maybe_side_effect"] is True, effect_rows
        assert effect_rows[0]["attempt"] == 1, effect_rows

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not [t for t in threading.enumerate()
                    if t.name.startswith("approval-heartbeat-")]:
                break
            time.sleep(0.05)
        assert [t.name for t in threading.enumerate()
                if t.name.startswith("approval-heartbeat-")] == []
    finally:
        release.set()


# --- A 心跳冻结 → B 回收/重 claim → A 终态 409 且不改行 -------------------

def test_frozen_heartbeat_stale_complete_is_409_and_row_unchanged(monkeypatch):
    """A 的心跳全程缺席；租约过期后 B 回收并重新 claim，A 的 complete 被
    条件更新挡下 → HTTP 409，且行的 status/consumed/claim_token 不被 A 改写。
    """
    import agent.tools.executor as executor_module
    import apps.api.main as main
    from infrastructure.pg import connector as connector_module
    from infrastructure.pg import schema as schema_module
    from agent.runtime.paused_tasks import PausedTaskStore

    monkeypatch.setattr(connector_module, "get_connector", lambda: None)
    monkeypatch.setattr(schema_module, "ensure_schema", lambda c: None)
    monkeypatch.setenv("AGENT_APPROVAL_LEASE_SECONDS", "30")
    monkeypatch.setenv("AGENT_APPROVAL_HEARTBEAT_ENABLED", "false")  # A 的心跳被冻结
    monkeypatch.delenv("AGENT_TOOL_TIMEOUT_SECONDS", raising=False)

    store = _store()
    monkeypatch.setattr(main, "_APPROVALS", store)
    monkeypatch.setattr(main, "_PAUSED_TASKS", PausedTaskStore())

    def planner(state):
        if state.observations:
            return {"action": "finish", "answer": "done"}
        return {"action": "call_tool", "tool": TOOL,
                "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}

    in_tool = threading.Event()
    b_done = threading.Event()

    class Registry:
        def call(self, tool, args):
            in_tool.set()
            assert b_done.wait(10)
            return "ok"

    monkeypatch.setattr(main, "build_deepseek_planner", lambda client, model: planner)
    monkeypatch.setattr(executor_module, "_default_registry", Registry())

    client = TestClient(main.app)
    tenant = f"frozen-{uuid.uuid4().hex[:8]}"
    body = client.post("/chat", json={"message": "buy X", "tenant_id": tenant,
                                     "user_id": "u1"}).json()
    assert body["status"] == "paused", body
    aid = body["approval_id"]
    assert client.post(f"/approval/{aid}/decide",
                       json={"decision": "approve", "approver": "mgr-1",
                             "tenant_id": tenant}).status_code == 200

    result: dict = {}

    def _resume():
        r = client.post(f"/chat/{body['task_id']}/resume", json={"tenant_id": tenant})
        result["status"] = r.status_code
        result["body"] = r.json()

    worker = threading.Thread(target=_resume, daemon=True)
    worker.start()
    assert in_tool.wait(5), "A never entered the tool"

    row = store.get(aid)
    token_a = row["claim_token"]
    assert row["status"] == "in_progress" and token_a

    # 直接快进租约到过期（等价于心跳线程被冻结/暂停）。
    store._repo._items[aid]["lease_until"] = time.monotonic() - 1
    assert store.reclaim_expired_claims() == 1
    token_b = store.claim(aid, lease_seconds=30)["claim_token"]
    assert token_b != token_a
    before = dict(store.get(aid))
    assert before["status"] == "in_progress" and before.get("consumed") is False
    assert before["claim_token"] == token_b

    b_done.set()
    worker.join(timeout=10)
    assert result["status"] == 409, result
    assert result["body"]["detail"] == "approval lease lost during execution"

    after = dict(store.get(aid))
    assert after["status"] == "in_progress", after
    assert after.get("consumed") is False, after
    assert after["claim_token"] == token_b, after
    assert after["claim_token"] != token_a, after

    # B 仍然可以正常完成（A 的失效写入没有破坏 B 的 claim）。
    assert store.complete(aid, token_b)["status"] == "done"


# --- Prometheus 计数器 ------------------------------------------------------

def test_approval_metric_counters_increment():
    from observability import metrics

    backend = metrics.configure_metrics(memory=True)
    try:
        store = _store()
        aid, token = _approved_claim(store, lease_seconds=1)

        # 快进 + 回收：approval_reclaimed_total = 1
        store._repo._items[aid]["lease_until"] = time.monotonic() - 1
        assert store.reclaim_expired_claims() == 1
        assert backend.value("approval_reclaimed_total") == 1
        assert backend.value("approval_lease_lost_total") == 0
        assert backend.value("approval_heartbeat_failed_total") == 0

        # 已被回收的行续期 0 行 → 心跳判定丢失：approval_lease_lost_total = 1
        heartbeat = LeaseHeartbeat(store, aid, token, lease_seconds=1,
                                   interval=0.05).start()
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline and not heartbeat.lost:
                time.sleep(0.02)
            assert heartbeat.lost is True
        finally:
            heartbeat.stop()
        assert backend.value("approval_lease_lost_total") == 1

        # 数据库瞬时失败达到阈值：heartbeat_failed +1，lease_lost 再 +1
        broken = _BrokenStore()
        heartbeat = LeaseHeartbeat(broken, "apr-metrics", "tok-metrics",
                                   lease_seconds=1, interval=0.05,
                                   max_failures=1).start()
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline and not heartbeat.lost:
                time.sleep(0.02)
            assert heartbeat.lost is True
        finally:
            heartbeat.stop()
        assert backend.value("approval_heartbeat_failed_total") == 1
        assert backend.value("approval_lease_lost_total") == 2

        payload, _ = metrics.render_metrics()
        text = payload.decode()
        assert "approval_reclaimed_total 1" in text
        assert "approval_lease_lost_total 2" in text
        assert "approval_heartbeat_failed_total 1" in text
    finally:
        metrics.reset_metrics()
