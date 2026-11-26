import time

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph


def test_expense_workflow_is_backed_by_compiled_langgraph():
    from workflow.graph import build_expense_graph

    saver = MemorySaver()
    workflow = build_expense_graph(checkpointer=saver)

    assert isinstance(workflow.compiled, CompiledStateGraph)
    assert workflow.checkpointer is saver
    assert {"fetch", "check", "policy", "judge"} <= set(workflow.compiled.get_graph().nodes)


def test_classic_expense_runner_uses_the_framework_graph(monkeypatch):
    import workflow.graph as graph_module

    calls = []
    original = graph_module.build_expense_graph

    def recording_builder(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(graph_module, "build_expense_graph", recording_builder)
    result = graph_module.run_expense_workflow({"id": "E1", "amount": 1000})

    assert result["decision"] == "auto_approve"
    assert len(calls) == 1


def test_resume_requires_and_consumes_real_approval():
    from security.approval import ApprovalStore
    from workflow.graph import build_expense_graph

    approvals = ApprovalStore()
    workflow = build_expense_graph(checkpointer=MemorySaver(), approval_store=approvals)
    config = {"configurable": {"thread_id": "approval-required"}}
    first = workflow.invoke({"expense": {"id": "E1", "amount": 10000}}, config=config)
    assert first["decision"] == "paused_human_review"
    pending = [item for item in approvals.list_items()
               if item["args"].get("thread_id") == "approval-required"]
    assert len(pending) == 1
    aid = pending[0]["id"]

    blocked = workflow.invoke(None, config=config)
    assert blocked["decision"] == "paused_human_review"
    assert approvals.get(aid)["status"] == "pending"

    approvals.approve(aid, "manager")
    done = workflow.invoke(None, config=config)
    assert done["decision"] == "human_review"
    assert approvals.get(aid)["status"] == "done"
    assert approvals.get(aid)["consumed"] is True


def test_resume_rejects_approval_for_different_expense():
    from security.approval import ApprovalStore
    from workflow.graph import build_expense_graph

    approvals = ApprovalStore()
    workflow = build_expense_graph(checkpointer=MemorySaver(), approval_store=approvals)
    config = {"configurable": {"thread_id": "expense-binding"}}
    workflow.invoke({"expense": {"id": "E1", "amount": 10000}}, config=config)
    approval = [item for item in approvals.list_items()
                if item["args"].get("thread_id") == "expense-binding"][0]
    approval["args"]["expense"] = {"id": "E2", "amount": 999999}
    approvals.approve(approval["id"], "manager")

    result = workflow.invoke(None, config=config)
    assert result["decision"] == "failed"
    assert result["reason"] == "approval_expense_mismatch"
    assert approvals.get(approval["id"])["status"] == "approved"


def test_durable_checkpoint_defaults_to_durable_approval_store(monkeypatch):
    import workflow.graph as graph_module
    from security.approval import ApprovalStore

    sentinel = ApprovalStore()
    monkeypatch.setattr(graph_module, "build_approval_store", lambda: sentinel)
    workflow = graph_module.build_expense_graph(checkpointer=MemorySaver())
    assert workflow.approvals is sentinel


def test_resume_reclaims_expired_approval_claim():
    from security.approval import ApprovalStore
    from workflow.graph import build_expense_graph

    approvals = ApprovalStore()
    workflow = build_expense_graph(checkpointer=MemorySaver(), approval_store=approvals)
    config = {"configurable": {"thread_id": "expired-claim"}}
    workflow.invoke({"expense": {"id": "E1", "amount": 10000}}, config=config)
    approval = [item for item in approvals.list_items()
                if item["args"].get("thread_id") == "expired-claim"][0]
    approvals.approve(approval["id"], "manager")
    approvals.claim(approval["id"], lease_seconds=1)
    approvals._repo._items[approval["id"]]["lease_until"] = time.monotonic() - 1

    result = workflow.invoke(None, config=config)
    assert result["decision"] == "human_review"
    assert approvals.get(approval["id"])["status"] == "done"


def test_default_workflow_uses_memory_saver_without_opening_pg():
    import workflow.graph as graph_module

    workflow = graph_module.build_expense_graph()
    assert isinstance(workflow.checkpointer, MemorySaver)


def test_tool_timeout_is_not_automatically_retried(monkeypatch):
    import workflow.graph as graph_module
    from agent.tools.executor import ToolTimeout

    def timeout(fn, *args, **kwargs):
        raise ToolTimeout("ambiguous")
    monkeypatch.setattr(graph_module, "execute_with_timeout", timeout)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = graph_module.build_expense_graph().invoke({"expense": {"id": "E1", "amount": 100}})

    assert result["decision"] == "human_review"
    assert result["trace"].count("timeout") == 4
    assert "retry" not in result["trace"]


def test_default_checkpointer_is_native_langgraph_memory_saver():
    from workflow.checkpoint import get_checkpointer

    checkpointer = get_checkpointer(dsn=None)
    assert isinstance(checkpointer, MemorySaver)
