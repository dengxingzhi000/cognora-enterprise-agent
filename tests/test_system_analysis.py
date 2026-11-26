from agent.runtime.state import AgentState
from agent.tools.builtin import build_default_registry
from agent.tools.executor import registry_executor
from observability.tracing import Tracer
from security.policy import PolicyEngine


def test_analysis_tools_are_registered_typed_and_read_only():
    registry = build_default_registry()
    expected = {
        "system.describe", "system.analyze",
        "workflow.list", "workflow.get", "workflow.analyze",
        "trace.explain",
    }
    assert expected <= set(registry.list_tools())
    for name in expected:
        assert "args" not in registry.get(name).structured_tool.args
        decision = PolicyEngine().check(
            {"tenant_id": "t1", "role": "employee", "user_id": "u1"},
            name, {"tenant_id": "t1"},
        )
        assert decision["decision"] == "allow"


def test_workflow_catalog_reads_real_compiled_graph():
    from analysis.catalog import get_workflow, list_workflows

    workflows = list_workflows()
    assert [item["name"] for item in workflows] == ["expense"]
    expense = get_workflow("expense")
    assert {"fetch", "check", "policy", "judge"} <= set(expense["nodes"])
    assert {tuple(edge.values()) for edge in expense["edges"]} >= {
        ("fetch", "check"), ("check", "policy"), ("policy", "judge")
    }
    assert expense["engine"] == "langgraph"


def test_workflow_catalog_does_not_hide_new_compiled_nodes(monkeypatch):
    from types import SimpleNamespace

    from langgraph.checkpoint.memory import MemorySaver

    import analysis.catalog as catalog

    drawable = SimpleNamespace(
        nodes=("fetch", "check", "policy", "judge", "audit"),
        edges=(SimpleNamespace(source="judge", target="audit"),),
    )
    workflow = SimpleNamespace(
        compiled=SimpleNamespace(get_graph=lambda: drawable),
        checkpointer=MemorySaver(),
        durable=True,
    )
    monkeypatch.setattr(catalog, "_workflow", lambda name: workflow)

    report = catalog.get_workflow("expense")
    assert report["nodes"][-1] == "audit"
    assert report["edges"][-1] == {"source": "judge", "target": "audit"}
    assert report["checkpoint"] == {"type": "InMemorySaver", "durable": True}


def test_analysis_tool_result_has_size_bound():
    from agent.tools.analysis import _json

    result = _json({"payload": "x" * 100_000})
    assert len(result) < 1000
    assert "analysis_result_too_large" in result


def test_default_workflow_analysis_does_not_claim_enabled_approval():
    from analysis.catalog import analyze_workflow, get_workflow

    workflow = get_workflow("expense")
    assert workflow["approval"]["enabled"] is False
    report = analyze_workflow("expense")
    codes = {finding["code"] for finding in report["findings"]}
    assert "human_approval_disabled" in codes
    assert "human_approval_gate" not in codes


def test_durable_workflow_analysis_reports_bound_approval(monkeypatch):
    from types import SimpleNamespace

    from langgraph.checkpoint.memory import MemorySaver

    import analysis.catalog as catalog

    drawable = SimpleNamespace(
        nodes=("fetch", "check", "policy", "judge"),
        edges=(
            SimpleNamespace(source="fetch", target="check"),
            SimpleNamespace(source="check", target="policy"),
            SimpleNamespace(source="policy", target="judge"),
        ),
    )
    workflow = SimpleNamespace(
        compiled=SimpleNamespace(get_graph=lambda: drawable),
        checkpointer=MemorySaver(),
        durable=True,
    )
    monkeypatch.setattr(catalog, "_workflow", lambda name: workflow)

    report = catalog.analyze_workflow("expense")
    codes = {finding["code"] for finding in report["findings"]}
    assert "human_approval_gate" in codes
    assert "human_approval_disabled" not in codes


def test_production_image_includes_analysis_package():
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    assert "COPY analysis ./analysis" in dockerfile.read_text(encoding="utf-8")


def test_workflow_analysis_reports_actionable_findings():
    from analysis.catalog import analyze_workflow

    report = analyze_workflow("expense")
    codes = {finding["code"] for finding in report["findings"]}
    assert "human_approval_disabled" in codes
    assert "decision_logic_inside_node" in codes
    assert "durability_requires_explicit_checkpointer" in codes
    assert report["evidence"]["nodes"] == ["fetch", "check", "policy", "judge"]


def test_trace_explanation_survives_new_tracer_instance():
    from analysis.catalog import explain_trace

    first = Tracer()
    trace_id = first.start_trace("cross request", tenant_id="t1")
    first.end_trace(trace_id, "done")
    second = Tracer()

    report = explain_trace(trace_id, tenant_id="t1", tracer=second)
    assert report["status"] == "done"
    assert report["trace_id"] == trace_id


def test_trace_explanation_redacts_and_bounds_payload():
    from analysis.catalog import explain_trace

    tracer = Tracer()
    trace_id = tracer.start_trace("sensitive " + "x" * 2000, tenant_id="t1")
    tracer.log_event(trace_id, "tool", {
        "password": "do-not-leak",
        "payload": "y" * 100_000,
    })
    tracer.end_trace(trace_id, "done")

    report = explain_trace(trace_id, tenant_id="t1", tracer=tracer)
    assert len(report["task"]) <= 500
    assert report["events"][0]["data"]["password"] == "[REDACTED]"
    assert len(report["events"][0]["data"]["payload"]) <= 2000


def test_unknown_workflow_analysis_is_not_completed():
    from analysis.graph import run_system_analysis

    report = run_system_analysis("analyse", tenant_id="t1", workflow="does-not-exist")
    assert report["status"] == "failed"
    assert report["reason"] == "workflow_not_found"


def test_analysis_never_invokes_workflow(monkeypatch):
    from analysis.graph import run_system_analysis
    from workflow.graph import _ExpenseWorkflow

    def unexpected_invoke(*args, **kwargs):
        raise AssertionError("analysis must not execute workflows")

    monkeypatch.setattr(_ExpenseWorkflow, "invoke", unexpected_invoke)
    report = run_system_analysis("analyse", tenant_id="t1", workflow="expense")
    assert report["status"] == "completed"


def test_trace_explanation_reads_cross_replica_postgres_store():
    from analysis.catalog import explain_trace
    from observability.tracing import configure_trace_store

    class Store:
        def get(self, trace_id, tenant_id):
            if trace_id != "remote-trace" or tenant_id != "t1":
                return None
            return {
                "id": trace_id, "tenant_id": tenant_id, "task": "remote",
                "status": "done", "latency_ms": 12.0, "start": 0.0,
                "events": [{"stage": "planner", "data": {}, "ts": 0.01}],
            }

    configure_trace_store(Store())
    try:
        report = explain_trace("remote-trace", tenant_id="t1", tracer=Tracer())
        assert report["status"] == "done"
        assert report["storage_scope"] == "postgres"
        assert explain_trace("remote-trace", tenant_id="t2", tracer=Tracer()) == {
            "error": "trace_not_found"
        }
    finally:
        configure_trace_store(None)


def test_trace_redaction_masks_credentials_and_pii():
    from analysis.catalog import explain_trace

    tracer = Tracer()
    trace_id = tracer.start_trace("contact user@example.com 13800138000", tenant_id="t1")
    tracer.log_event(trace_id, "tool", {
        "authorization": "Bearer private-token",
        "text": "sk-abcdefghijklmnopqrstuvwxyz",
    })
    tracer.end_trace(trace_id, "done")
    report = explain_trace(trace_id, "t1", tracer=tracer)
    assert "user@example.com" not in report["task"]
    assert "13800138000" not in report["task"]
    assert report["events"][0]["data"]["authorization"] == "[REDACTED]"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in report["events"][0]["data"]["text"]


def test_trace_explanation_is_tenant_scoped():
    from analysis.catalog import explain_trace

    tracer = Tracer()
    trace_id = tracer.start_trace("analyse order", tenant_id="t1")
    tracer.log_event(trace_id, "planner", {"action": "call_tool"})
    tracer.log_event(trace_id, "tool", {"tool": "metrics.get", "outcome": "success"})
    tracer.end_trace(trace_id, "done")

    report = explain_trace(trace_id, tenant_id="t1", tracer=tracer)
    assert report["status"] == "done"
    assert report["event_count"] == 2
    assert report["stages"] == ["planner", "tool"]
    assert explain_trace(trace_id, tenant_id="t2", tracer=tracer) == {
        "error": "trace_not_found"
    }


def test_system_analysis_uses_langgraph_subgraph():
    from analysis.graph import build_analysis_graph, run_system_analysis

    graph = build_analysis_graph()
    assert {"collect", "inspect_workflows", "explain_trace", "synthesize"} <= set(
        graph.get_graph().nodes
    )
    report = run_system_analysis(
        " Analyse the expense workflow and recent failure ",
        tenant_id="t1",
        workflow="expense",
    )
    assert report["status"] == "completed"
    assert "expense" in report["summary"]
    assert report["evidence"]["workflow"] == "expense"
    assert report["findings"]
    assert report["recommendations"]


def test_agent_runtime_can_select_analysis_tool_and_finish():
    from agent.runtime.loop import run
    from agent.tools.executor import guarded_executor
    from security.approval import ApprovalStore

    registry = build_default_registry()

    def planner(state):
        if state.observations:
            return {"action": "finish", "answer": state.observations[0]["result"]}
        return {"action": "call_tool", "tool": "system.analyze", "args": {
            "question": "Analyze the expense workflow",
            "workflow": "expense",
        }}

    user = {"tenant_id": "t1", "role": "employee", "user_id": "u1"}
    final = run(
        AgentState(task="analyse"),
        planner=planner,
        executor=lambda plan, state: guarded_executor(
            plan, state, registry=registry, user=user,
            policy=PolicyEngine(), approvals=ApprovalStore(),
        ),
    )
    assert final.status == "done"
    assert final.observations[0]["outcome"] == "success"
    assert "human_approval_disabled" in final.answer
    assert final.observations[0]["tool"] == "system.analyze"


def test_system_analyze_tool_returns_grounded_report():
    registry = build_default_registry()
    state = AgentState(task="analyse")
    result = registry_executor(
        {"tool": "system.analyze", "args": {
            "question": "What can fail in the expense workflow?",
            "tenant_id": "t1",
            "workflow": "expense",
        }},
        state,
        registry=registry,
    )
    assert result["outcome"] == "success"
    assert '"status":"completed"' in result["result"]
    assert "human_approval_disabled" in result["result"]
