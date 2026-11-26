"""LangGraph subgraph that produces evidence-backed system analysis."""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from .catalog import analyze_workflow, describe_system, explain_trace, list_workflows


class AnalysisState(TypedDict):
    question: str
    tenant_id: str
    workflow: str | None
    trace_id: str | None
    system: dict
    workflows: list[dict]
    trace: dict
    report: dict


def build_analysis_graph(registry=None, tracer=None):
    def collect(state: AnalysisState):
        return {"system": describe_system(
            registry=registry, tenant_id=state["tenant_id"], include_schemas=False,
        )}

    def inspect_workflows(state: AnalysisState):
        if state["workflow"]:
            reports = [analyze_workflow(state["workflow"])]
        else:
            reports = [analyze_workflow(item["name"]) for item in list_workflows()]
        return {"workflows": reports}

    def explain_trace_node(state: AnalysisState):
        if not state["trace_id"]:
            return {"trace": {"status": "not_requested"}}
        return {"trace": explain_trace(
            state["trace_id"], state["tenant_id"], tracer=tracer,
        )}

    def synthesize(state: AnalysisState):
        workflows = state["workflows"]
        errors = [report["error"] for report in workflows if report.get("error")]
        if errors:
            return {"report": {
                "status": "failed",
                "reason": errors[0],
                "question": state["question"].strip(),
                "summary": f"Analysis failed: {errors[0]}.",
                "system": state["system"],
                "workflows": workflows,
                "trace": state["trace"],
                "findings": [],
                "recommendations": [],
                "evidence": {"workflow": state["workflow"], "trace_id": state["trace_id"]},
            }}
        findings = [finding for report in workflows for finding in report.get("findings", [])]
        recommendations = []
        for report in workflows:
            for recommendation in report.get("recommendations", []):
                if recommendation not in recommendations:
                    recommendations.append(recommendation)
        trace = state["trace"]
        if trace.get("error") == "trace_not_found":
            findings.append({
                "code": "trace_unavailable", "severity": "info",
                "summary": "The requested trace is missing or outside the caller's tenant.",
                "evidence": [state["trace_id"]],
            })
        workflow_name = state["workflow"] or ", ".join(
            report["name"] for report in workflows
        )
        report = {
            "status": "completed",
            "question": state["question"].strip(),
            "summary": (
                f"Analyzed {workflow_name}: {len(state['system']['tools'])} tools, "
                f"{len(workflows)} workflow(s), {len(findings)} finding(s)."
            ),
            "system": state["system"],
            "workflows": workflows,
            "trace": trace,
            "findings": findings,
            "recommendations": recommendations,
            "evidence": {
                "tools": [tool["name"] for tool in state["system"]["tools"]],
                "workflow": workflow_name,
                "trace_id": state["trace_id"],
            },
        }
        return {"report": report}

    graph = StateGraph(AnalysisState)
    graph.add_node("collect", collect)
    graph.add_node("inspect_workflows", inspect_workflows)
    graph.add_node("explain_trace", explain_trace_node)
    graph.add_node("synthesize", synthesize)
    graph.set_entry_point("collect")
    graph.add_edge("collect", "inspect_workflows")
    graph.add_edge("inspect_workflows", "explain_trace")
    graph.add_edge("explain_trace", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


def run_system_analysis(question: str, tenant_id: str = "default",
                        workflow: str | None = None, trace_id: str | None = None,
                        registry=None, tracer=None) -> dict:
    graph = build_analysis_graph(registry=registry, tracer=tracer)
    result = graph.invoke({
        "question": question,
        "tenant_id": tenant_id,
        "workflow": workflow,
        "trace_id": trace_id,
        "system": {},
        "workflows": [],
        "trace": {},
        "report": {},
    })
    return result["report"]
