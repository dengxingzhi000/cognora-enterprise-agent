"""Batch 3: MCP Server stub — 描述映射、调用形状、缺依赖降级。"""
import pytest


def test_tool_specs_match_registry_descriptions():
    from agent.tools.builtin import build_default_registry
    from agent.mcp.server import build_tool_specs

    reg = build_default_registry()
    specs = build_tool_specs(reg)
    assert [s.name for s in specs] == ["metrics.get", "logs.search", "git.diff"]
    for spec in specs:
        assert spec.description == reg.get(spec.name).description
        assert spec.input_schema["type"] == "object"


def test_call_tool_returns_mcp_content_shape():
    from agent.mcp.server import call_tool

    result = call_tool("metrics.get", {"service": "order"})
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert "5xx rate" in result["content"][0]["text"]

    result = call_tool("logs.search", {"keyword": "5xx"})
    assert "ERROR" in result["content"][0]["text"]

    unknown = call_tool("deploy.restart", {})
    assert unknown["isError"] is True
    assert "unknown MCP tool" in unknown["content"][0]["text"]


def test_call_tool_wraps_registry_errors_as_mcp_error():
    from agent.mcp.server import call_tool
    from agent.tools.registry import Registry, Tool

    reg = Registry()
    reg.register(Tool("metrics.get", "desc", lambda args: (_ for _ in ()).throw(RuntimeError("boom"))))
    result = call_tool("metrics.get", {}, registry=reg)
    assert result["isError"] is True
    assert "tool error metrics.get" in result["content"][0]["text"]


def test_build_server_degrades_when_mcp_missing(monkeypatch):
    import agent.mcp.server as server_mod
    monkeypatch.setattr(server_mod, "_load_server_class", lambda: None)
    assert server_mod.mcp_available() is False
    with pytest.raises(server_mod.MCPUnavailable):
        server_mod.build_server()


def test_build_server_registers_tools_with_fake_transport():
    from agent.mcp.server import build_server, build_tool_specs
    from agent.tools.builtin import build_default_registry

    class FakeFastMCP:
        def __init__(self, name):
            self.name = name
            self.tools = {}

        def tool(self, name=None, description=None):
            def deco(fn):
                self.tools[name] = {"description": description, "fn": fn}
                return fn
            return deco

    reg = build_default_registry()
    server = build_server(registry=reg, fastmcp=FakeFastMCP)
    specs = build_tool_specs(reg)
    assert set(server.tools) == {s.name for s in specs}
    for spec in specs:
        assert server.tools[spec.name]["description"] == spec.description
    text = server.tools["metrics.get"]["fn"](service="order")
    assert "5xx rate" in text


def test_mcp_available_probe_is_boolean():
    from agent.mcp.server import mcp_available
    assert isinstance(mcp_available(), bool)


def test_real_server_if_mcp_installed():
    pytest.importorskip("mcp")
    from agent.mcp.server import build_server, mcp_available
    if not mcp_available():
        pytest.skip("mcp is importable but exposes neither FastMCP nor MCPServer")
    server = build_server()
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == {"metrics.get", "logs.search", "git.diff"}
    tool = next(t for t in server._tool_manager.list_tools() if t.name == "metrics.get")
    assert "service" in tool.parameters["properties"]
    assert "5xx rate" in tool.fn(service="order")
