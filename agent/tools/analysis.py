"""Typed tools for read-only system and workflow analysis."""
import json

from pydantic import BaseModel

from agent.tools.registry import Tool


class TenantArgs(BaseModel):
    tenant_id: str = "t1"


class WorkflowArgs(TenantArgs):
    name: str


class TraceArgs(TenantArgs):
    trace_id: str


class SystemAnalyzeArgs(TenantArgs):
    question: str
    workflow: str | None = None
    trace_id: str | None = None


def _json(value) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= 50_000:
        return text
    return json.dumps({
        "error": "analysis_result_too_large",
        "size": len(text),
        "summary": "Narrow the workflow or trace scope and retry.",
    }, separators=(",", ":"))


def _describe(args: dict) -> str:
    from agent.tools.executor import _registry
    from analysis.catalog import describe_system
    return _json(describe_system(_registry(), args["tenant_id"]))


def _analyze_system(args: dict) -> str:
    from agent.tools.executor import _registry
    from analysis.graph import run_system_analysis
    return _json(run_system_analysis(
        args["question"], args["tenant_id"], args.get("workflow"), args.get("trace_id"),
        registry=_registry(),
    ))


def _list_workflows(args: dict) -> str:
    from analysis.catalog import list_workflows
    return _json({"tenant_id": args["tenant_id"], "workflows": list_workflows()})


def _get_workflow(args: dict) -> str:
    from analysis.catalog import get_workflow
    return _json(get_workflow(args["name"]))


def _analyze_workflow(args: dict) -> str:
    from analysis.catalog import analyze_workflow
    return _json(analyze_workflow(args["name"]))


def _explain_trace(args: dict) -> str:
    from analysis.catalog import explain_trace
    return _json(explain_trace(args["trace_id"], args["tenant_id"]))


def register_analysis_tools(registry) -> None:
    registry.register(Tool(
        "system.describe", "查看运行时、工具目录、工作流、持久化与可观测能力", _describe, TenantArgs))
    registry.register(Tool(
        "system.analyze", "对系统/工作流/trace 做只读分析并返回证据、风险与建议",
        _analyze_system, SystemAnalyzeArgs))
    registry.register(Tool(
        "workflow.list", "列出可用工作流及节点", _list_workflows, TenantArgs))
    registry.register(Tool(
        "workflow.get", "查看指定工作流的真实图结构、重试、审批与 checkpoint 配置",
        _get_workflow, WorkflowArgs))
    registry.register(Tool(
        "workflow.analyze", "分析指定工作流的根因风险、可观测性与持久化缺口",
        _analyze_workflow, WorkflowArgs))
    registry.register(Tool(
        "trace.explain", "按租户解释指定 trace 的阶段、耗时与失败点", _explain_trace, TraceArgs))
