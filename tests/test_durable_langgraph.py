import pytest


def test_explicit_pg_failure_does_not_silently_downgrade():
    from workflow.checkpoint import CheckpointUnavailable, get_checkpointer

    with pytest.raises(CheckpointUnavailable):
        get_checkpointer(dsn="postgresql://invalid:1@127.0.0.1:1/nope")


def test_strict_mode_rejects_unavailable_env_checkpointer(monkeypatch):
    from workflow.checkpoint import CheckpointUnavailable, get_checkpointer

    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "true")
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://invalid:1@127.0.0.1:1/nope")
    with pytest.raises(CheckpointUnavailable):
        get_checkpointer()


def test_no_dsn_uses_native_memory_saver():
    from langgraph.checkpoint.memory import MemorySaver

    from workflow.checkpoint import get_checkpointer

    assert isinstance(get_checkpointer(dsn=None), MemorySaver)


def test_expense_pause_and_resume_same_thread():
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph
    cp = get_checkpointer(dsn=None)
    g = build_expense_graph(policy_threshold=5000, checkpointer=cp)
    first = g.invoke({"expense": {"id": "E9", "amount": 6230}}, config={"configurable": {"thread_id": "task-9"}})
    assert first["decision"] in ("paused_human_review", "human_review")
    pending = [item for item in g.approvals.list_items()
               if item["args"].get("thread_id") == "task-9"]
    g.approvals.approve(pending[0]["id"], "manager")
    g2 = build_expense_graph(policy_threshold=5000, checkpointer=cp,
                             approval_store=g.approvals)
    done = g2.invoke(None, config={"configurable": {"thread_id": "task-9"}})
    assert done["decision"] == "human_review"
    assert "judge_rule" in done["trace"]


def test_graph_node_retries_with_framework_backoff(monkeypatch):
    import time

    import workflow.nodes.expense as n
    from workflow.graph_lang import build_expense_graph

    calls = {"n": 0}
    original = n.fetch_expense

    def flaky(state):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("tool timeout")
        return original(state)

    monkeypatch.setattr(n, "fetch_expense", flaky)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = build_expense_graph().invoke({"expense": {"id": "E1", "amount": 100}})

    assert result["decision"] == "auto_approve"
    assert calls["n"] == 3


def test_parity_still_holds_with_durable_graph():
    from workflow.graph import run_expense_workflow
    from workflow.graph_lang import build_expense_graph
    g = build_expense_graph(policy_threshold=5000)
    for expense, expected in [({"id": "E1", "amount": 1000}, "auto_approve")]:
        classic = run_expense_workflow(expense, policy_threshold=5000)["decision"]
        via = g.invoke({"expense": expense}, config={"configurable": {"thread_id": "parity-1"}})["decision"]
        assert classic == expected == via


def test_saver_adapter_delegates_to_native():
    from workflow.checkpoint import _SaverAdapter

    class FakeNative:
        def __init__(self):
            self.put_calls = []
            self.get_calls = []
            self._data = {}

        def put(self, config, checkpoint, metadata=None, new_versions=None):
            self.put_calls.append((config, checkpoint))
            tid = config["configurable"]["thread_id"]
            self._data[tid] = dict(checkpoint)

        def get_tuple(self, config):
            self.get_calls.append(config)
            tid = config["configurable"]["thread_id"]
            v = self._data.get(tid)
            return dict(v) if v is not None else None

    fake = FakeNative()
    adapter = _SaverAdapter(fake)
    adapter.put("t-native-1", {"node": "judge"})
    assert fake.put_calls, "native put must be called on delegation"
    assert fake.put_calls[0][0]["configurable"]["thread_id"] == "t-native-1"
    out = adapter.get("t-native-1")
    assert fake.get_calls, "native get_tuple must be called on delegation"
    assert out["node"] == "judge"


def test_pause_creates_approval_pending_entry_and_resume():
    from security.approval import ApprovalStore
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph
    approvals = ApprovalStore()
    cp = get_checkpointer(dsn=None)
    g = build_expense_graph(policy_threshold=5000, checkpointer=cp, approval_store=approvals)
    first = g.invoke({"expense": {"id": "E9b", "amount": 6230}},
                     config={"configurable": {"thread_id": "task-apr-1"}})
    assert first["decision"] in ("paused_human_review", "human_review")
    pendings = [v for v in approvals._items.values() if v["status"] == "pending"]
    assert len(pendings) >= 1, "pause must dual-write ApprovalStore pending entry"
    approvals.approve(pendings[0]["id"], "manager")
    done = g.invoke(None, config={"configurable": {"thread_id": "task-apr-1"}})
    assert done["decision"] == "human_review"


def test_fault_chain_timeout_retry_fallback_human_trace_order():
    import workflow.nodes.expense as n
    from security.approval import ApprovalStore
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph

    orig = n.check_amount

    def always_timeout(state):
        raise TimeoutError("tool timeout")

    n.check_amount = always_timeout
    try:
        approvals = ApprovalStore()
        cp = get_checkpointer(dsn=None)
        g = build_expense_graph(policy_threshold=5000, checkpointer=cp, approval_store=approvals)
        import time as _time
        orig_sleep = _time.sleep
        sleeps: list = []
        try:
            _time.sleep = lambda s: sleeps.append(s)  # type: ignore
            out = g.invoke({"expense": {"id": "E-fault-1", "amount": 100}},
                           config={"configurable": {"thread_id": "fault-1"}})
        finally:
            _time.sleep = orig_sleep  # type: ignore
        trace = out.get("trace", [])
        idx = {}
        for k in ("timeout", "retry", "fallback", "human"):
            assert k in trace, f"fault-chain trace missing {k}: {trace}"
            idx[k] = trace.index(k)
        assert idx["timeout"] < idx["retry"] < idx["fallback"] < idx["human"], trace
        assert out["decision"] in ("paused_human_review", "human_review")
        pendings = [v for v in approvals._items.values() if v["status"] == "pending"]
        assert len(pendings) >= 1, "retry exhaustion must create ApprovalStore pending (human)"
    finally:
        n.check_amount = orig


def test_graph_checkpoint_prevents_completed_node_replay():
    import workflow.nodes.expense as n
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph

    calls = {"n": 0}
    orig_fetch = n.fetch_expense

    def counting_fetch(state):
        calls["n"] += 1
        return orig_fetch(state)

    n.fetch_expense = counting_fetch
    try:
        cp = get_checkpointer(dsn=None)
        g = build_expense_graph(policy_threshold=5000, checkpointer=cp)
        tid = "idem-graph-1"
        exp = {"id": "E-idem-1", "amount": 10000}
        first = g.invoke({"expense": dict(exp)}, config={"configurable": {"thread_id": tid}})
        assert first["decision"] == "paused_human_review"
        pending = [item for item in g.approvals.list_items()
                   if item["args"].get("thread_id") == tid]
        g.approvals.approve(pending[0]["id"], "manager")
        done = g.invoke(None, config={"configurable": {"thread_id": tid}})
        assert done["decision"] == "human_review"
        assert calls["n"] == 1, f"resume replayed completed fetch: {calls['n']}"
        assert done["trace"].count("fetch") == 1
    finally:
        n.fetch_expense = orig_fetch
