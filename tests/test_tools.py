"""Stage2 RED: Tool Registry + 运维四件套。"""
from agent.runtime.loop import run
from agent.runtime.state import AgentState
from agent.tools.builtin import build_default_registry
from agent.tools.executor import registry_executor
from agent.tools.registry import Registry, Tool


def test_registry_register_and_call():
    reg = Registry()
    reg.register(Tool(name="echo.hello", description="hi", func=lambda args: "hello"))
    assert "echo.hello" in reg.list_tools()
    assert reg.call("echo.hello", {}) == "hello"


def test_tool_is_backed_by_langchain_structured_tool():
    from langchain_core.tools import StructuredTool

    reg = Registry()
    tool = Tool(name="echo.hello", description="hi", func=lambda args: "hello")
    reg.register(tool)

    assert isinstance(tool.structured_tool, StructuredTool)
    assert tool.structured_tool.args == {
        "args": {"additionalProperties": True, "title": "Args", "type": "object"}
    }
    assert reg.call("echo.hello", {"name": "world"}) == "hello"


def test_builtin_tools_expose_real_argument_schemas():
    reg = build_default_registry()
    metrics_schema = reg.get("metrics.get").structured_tool.args
    knowledge_schema = reg.get("knowledge.search").structured_tool.args

    assert "service" in metrics_schema
    assert "args" not in metrics_schema
    assert "query" in knowledge_schema

def test_tool_schema_validation_happens_before_business_call():
    import pytest
    from pydantic import ValidationError

    reg = build_default_registry()
    with pytest.raises(ValidationError):
        reg.call("knowledge.search", {})



def test_ops_tools_never_return_fabricated_data_by_default(monkeypatch):
    import pytest
    monkeypatch.delenv("AGENT_ENABLE_MOCK_TOOLS", raising=False)
    reg = build_default_registry()
    for name in ["db.query", "metrics.get", "logs.search", "git.diff"]:
        with pytest.raises(RuntimeError, match="UNSUPPORTED_TOOL"):
            reg.call(name, {})


def test_ops_mock_data_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLE_MOCK_TOOLS", "1")
    reg = build_default_registry()
    assert "5xx" in str(reg.call("metrics.get", {"service": "order"}))
    assert "ERROR" in str(reg.call("logs.search", {"keyword": "5xx"}))


def test_loop_uses_registry_executor_end_to_end():
    reg = Registry()
    reg.register(Tool("metrics.get", "test metrics", lambda args: "5xx rate 1%"))

    def planner(state: AgentState):
        if state.iteration == 0:
            return {"action": "call_tool", "tool": "metrics.get", "args": {"service": "order"}}
        return {"action": "finish", "answer": f"根因分析: {state.observations[0]['result']}"}

    def executor(plan, state: AgentState):
        return registry_executor(plan, state, registry=reg)

    final = run(AgentState(task="订单5xx升高"), planner=planner, executor=executor)
    assert final.status == "done"
    assert "5xx" in final.answer
