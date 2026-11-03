"""最小 MCP Server：复用 Registry 的 metrics / logs / git 工具。

``mcp`` 是可选依赖（``pip install -e .[mcp]``）。未安装时本模块仍可导入：
- ``list_tool_specs`` / ``call_tool`` 提供可测的纯函数降级路径；
- ``build_server`` 抛 ``MCPUnavailable``，调用方决定是否继续。

真实 MCP 工具名保持 Registry 原名（``metrics.get`` 等），描述直接取自
Registry 的 ``Tool.description``，避免两处文档漂移。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from agent.tools.builtin import build_default_registry


class MCPUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


TOOL_NAMES = ("metrics.get", "logs.search", "git.diff")

_INPUT_SCHEMAS: dict[str, dict] = {
    "metrics.get": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "服务名，默认 order"},
            "tenant_id": {"type": "string", "description": "租户 id"},
        },
    },
    "logs.search": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "日志关键词，默认 5xx"},
            "query": {"type": "string", "description": "自然语言查询（兼容 Registry 调用）"},
            "tenant_id": {"type": "string", "description": "租户 id"},
        },
    },
    "git.diff": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库路径（可选）"},
        },
    },
}


def _load_server_class():
    """mcp 1.x 用 FastMCP；mcp 2.x 改名为 MCPServer，二者 tool() 形状一致。"""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
        return FastMCP
    except Exception:
        pass
    try:
        from mcp.server.mcpserver import MCPServer  # type: ignore
        return MCPServer
    except Exception:
        return None


def mcp_available() -> bool:
    return _load_server_class() is not None


def build_tool_specs(registry=None) -> list[MCPToolSpec]:
    """按 Registry 实际工具生成 MCP 描述；缺失项跳过而不是伪造。"""
    reg = registry or build_default_registry()
    specs: list[MCPToolSpec] = []
    for name in TOOL_NAMES:
        tool = reg.get(name)
        specs.append(MCPToolSpec(name=name, description=tool.description,
                                 input_schema=_INPUT_SCHEMAS[name]))
    return specs


def call_tool(name: str, args: dict, registry=None) -> dict:
    """返回 MCP CallToolResult 形状；业务异常转 isError，不向 MCP 层抛出。"""
    reg = registry or build_default_registry()
    if name not in TOOL_NAMES:
        return {"content": [{"type": "text", "text": f"unknown MCP tool: {name}"}],
                "isError": True}
    try:
        result = reg.call(name, args or {})
        text = str(result)
        is_error = False
    except Exception as e:  # noqa: BLE001 - MCP 结果必须可序列化
        text = f"tool error {name}: {e}"
        is_error = True
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _metrics_handler(registry=None):
    def handler(service: str = "order", tenant_id: str | None = None):
        return _call_text("metrics.get", {"service": service,
                                          "tenant_id": tenant_id}, registry)
    return handler


def _logs_handler(registry=None):
    def handler(keyword: str = "5xx", query: str | None = None,
                tenant_id: str | None = None):
        return _call_text("logs.search", {"keyword": keyword, "query": query,
                                          "tenant_id": tenant_id}, registry)
    return handler


def _git_handler(registry=None):
    def handler(repo: str | None = None):
        return _call_text("git.diff", {"repo": repo}, registry)
    return handler


_HANDLERS = {
    "metrics.get": _metrics_handler,
    "logs.search": _logs_handler,
    "git.diff": _git_handler,
}


def _call_text(name: str, args: dict, registry=None) -> str:
    clean = {k: v for k, v in (args or {}).items() if v is not None}
    return call_tool(name, clean, registry)["content"][0]["text"]


def build_server(registry=None, name: str = "enterprise-agent-ops", fastmcp=None):
    """构造 MCP server。``fastmcp`` 仅供测试注入，生产支持 mcp 1.x/2.x。"""
    if fastmcp is None:
        fastmcp = _load_server_class()
        if fastmcp is None:
            raise MCPUnavailable(
                "mcp package not installed; run `pip install -e .[mcp]`"
            )
    reg = registry or build_default_registry()
    server = fastmcp(name)
    for spec in build_tool_specs(reg):
        handler = _HANDLERS[spec.name](reg)
        handler.__name__ = spec.name.replace(".", "_")
        handler.__doc__ = f"MCP wrapper for {spec.name}"
        # 直接注册具名 handler，保证 MCP 从函数签名推导出的 schema 有意义。
        server.tool(name=spec.name, description=spec.description)(handler)
    return server


def main():
    server = build_server()
    server.run()


def dump_specs(registry=None) -> str:
    return json.dumps([s.as_dict() for s in build_tool_specs(registry)], ensure_ascii=False)


if __name__ == "__main__":
    main()
