"""Read-only system, workflow, and trace analysis primitives."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from observability.redaction import redact
from observability.tracing import Tracer
from workflow.graph import (
    WORKFLOW_NODE_TIMEOUT_SECONDS,
    WORKFLOW_RETRY_ATTEMPTS,
    WORKFLOW_RETRY_BACKOFF_SECONDS,
    build_expense_graph,
)

WORKFLOW_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    "expense": {
        "summary": "Fetch an expense, validate its amount, retrieve policy, and route exceptions to human review.",
        "nodes": {
            "fetch": "Load the expense input.",
            "check": "Compare the amount with the configured threshold.",
            "policy": "Build the applicable policy explanation.",
            "judge": "Evaluate invoice and amount rules; optionally interrupt for approval.",
        },
    }
}

def _redact(value, max_chars: int = 2000):
    return redact(value, max_chars=max_chars)


def _workflow(name: str):
    if name != "expense":
        return None
    return build_expense_graph()


def list_workflows() -> list[dict]:
    workflows = []
    for name, meta in WORKFLOW_DESCRIPTIONS.items():
        drawable = _workflow(name).compiled.get_graph()
        workflows.append({
            "name": name,
            "summary": meta["summary"],
            "engine": "langgraph",
            "nodes": [node for node in drawable.nodes if not node.startswith("__")],
        })
    return workflows


def get_workflow(name: str) -> dict:
    workflow = _workflow(name)
    if workflow is None:
        return {"error": "workflow_not_found", "available": list(WORKFLOW_DESCRIPTIONS)}
    drawable = workflow.compiled.get_graph()
    meta = WORKFLOW_DESCRIPTIONS[name]
    nodes = [node for node in drawable.nodes if not node.startswith("__")]
    edges = [
        {"source": edge.source, "target": edge.target}
        for edge in drawable.edges
        if not edge.target.startswith("__")
    ]
    return {
        "name": name,
        "summary": meta["summary"],
        "engine": "langgraph",
        "nodes": nodes,
        "node_details": {
            node: meta["nodes"].get(node, "Framework node; no domain description registered.")
            for node in nodes
        },
        "edges": edges,
        "retry": {
            "attempts": WORKFLOW_RETRY_ATTEMPTS,
            "backoff_seconds": WORKFLOW_RETRY_BACKOFF_SECONDS,
            "timeout_seconds": WORKFLOW_NODE_TIMEOUT_SECONDS,
            "timeout_retry": False,
        },
        "approval": {
            "enabled": workflow.durable,
            "required_for": (["amount_above_threshold", "missing_invoice"]
                             if workflow.durable else []),
        },
        "checkpoint": {
            "type": type(workflow.checkpointer).__name__,
            "durable": workflow.durable,
        },
    }


def _has_cycle(nodes: list[str], edges: list[dict]) -> bool:
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        outgoing[edge["source"]].append(edge["target"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for target in outgoing[node]:
            if visit(target):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in nodes)


def analyze_workflow(name: str) -> dict:
    workflow = get_workflow(name)
    if "error" in workflow:
        return workflow
    nodes = workflow["nodes"]
    approval_finding = (
        {
            "code": "human_approval_gate",
            "severity": "medium",
            "summary": "High-value or incomplete expenses interrupt for a bound human approval.",
            "evidence": ["judge", "ApprovalStore"],
        }
        if workflow["approval"]["enabled"] else
        {
            "code": "human_approval_disabled",
            "severity": "high",
            "summary": "The default non-durable graph returns a human-review decision without interrupt or a persistent approval record.",
            "evidence": ["judge", "durable=false"],
        }
    )
    findings = [
        approval_finding,
        {
            "code": "decision_logic_inside_node",
            "severity": "medium",
            "summary": "Approval routing is embedded in the judge node rather than represented as a conditional graph edge.",
            "evidence": ["judge"],
        },
        {
            "code": "durability_requires_explicit_checkpointer",
            "severity": "high" if name == "expense" else "medium",
            "summary": "The default in-memory checkpoint survives neither process restart nor multi-instance scheduling.",
            "evidence": ["MemorySaver", "PostgresSaver"],
        },
        {
            "code": "timeout_not_retried",
            "severity": "info",
            "summary": "Ambiguous tool timeouts fall back to human review instead of overlapping automatic retries.",
            "evidence": ["ToolTimeout", "Tenacity"],
        },
    ]
    if _has_cycle(nodes, workflow["edges"]):
        findings.append({
            "code": "cycle_detected", "severity": "high",
            "summary": "The workflow graph contains a cycle and needs an explicit loop guard.",
            "evidence": workflow["edges"],
        })
    else:
        findings.append({
            "code": "acyclic_linear_flow", "severity": "info",
            "summary": "The compiled graph is acyclic; retries are node-local and checkpointed resume does not replay completed nodes.",
            "evidence": workflow["edges"],
        })
    return {
        "name": name,
        "summary": f"Analyzed {name}: {len(nodes)} nodes, {len(workflow['edges'])} edges.",
        "findings": findings,
        "recommendations": [
            "Configure PostgresSaver and a durable ApprovalStore for production pause/resume.",
            "Expose the judge decision as a conditional edge or decision event for clearer topology metrics.",
            "Alert on approval age, checkpoint failures, and repeated timeout fallbacks.",
        ],
        "evidence": {"nodes": nodes, "edges": workflow["edges"]},
    }


def explain_trace(trace_id: str, tenant_id: str, tracer: Tracer | None = None) -> dict:
    source = tracer or Tracer._shared
    trace = None
    persisted = False
    if source is not None:
        try:
            trace = source.get_trace(trace_id)
        except KeyError:
            pass
    if trace is None and Tracer.store is not None:
        trace = Tracer.store.get(trace_id, tenant_id)
        persisted = trace is not None
    if trace is None:
        return {"error": "trace_not_found"}
    if trace.get("tenant_id") != tenant_id:
        return {"error": "trace_not_found"}
    all_events = trace.get("events", [])
    visible_events = all_events[-50:]
    events = [
        {
            "stage": event["stage"],
            "data": _redact(event.get("data") or {}),
            "offset_ms": round((event["ts"] - trace["start"]) * 1000, 3),
        }
        for event in visible_events
    ]
    failed = [
        event["stage"] for event in events
        if event["data"].get("outcome") in ("error", "denied", "unknown_tool")
        or "fail" in event["stage"].lower()
    ]
    return {
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "storage_scope": "postgres" if persisted else "process-local",
        "task": _redact(trace.get("task", ""), max_chars=500),
        "status": trace.get("status"),
        "latency_ms": trace.get("latency_ms", 0.0),
        "event_count": len(all_events),
        "returned_event_count": len(events),
        "truncated": len(all_events) > len(events),
        "stages": [event["stage"] for event in events],
        "failed_stages": failed,
        "events": events,
    }


def describe_system(registry=None, tenant_id: str = "default", include_schemas: bool = True) -> dict:
    from agent.runtime.agent_task import AgentTask
    from agent.runtime.loop import build_runtime_graph
    from agent.runtime.state import AgentState
    from agent.tools.executor import _registry
    from infrastructure.pg.connector import agent_dsn
    from observability.metrics import metrics_available

    tools_registry = registry or _registry()
    runtime = build_runtime_graph(AgentTask.from_state(AgentState(task="capability catalog")))
    tools = []
    for name in tools_registry.list_tools():
        tool = tools_registry.get(name)
        item: dict[str, Any] = {"name": name, "description": tool.description}
        if include_schemas:
            item["input_schema"] = tool.structured_tool.tool_call_schema.model_json_schema()
        tools.append(item)
    return {
        "tenant_id": tenant_id,
        "runtime": {
            "engine": "langgraph",
            "nodes": list(runtime.get_graph().nodes),
            "max_iterations": 8,
            "planner_contract": ["finish", "call_tool"],
        },
        "tools": tools,
        "workflows": list_workflows(),
        "persersistence": {
            "agent_postgres": bool(agent_dsn()),
            "approvals": "postgres" if agent_dsn() else "memory",
            "workflow_checkpoint": "MemorySaver by default; PostgresSaver when explicitly configured",
        },
        "observability": {
            "tracer": "in-memory plus optional OpenTelemetry",
            "trace_analysis_scope": "postgres" if Tracer.store is not None else "process-local",
            "prometheus": metrics_available(),
        },
    }
