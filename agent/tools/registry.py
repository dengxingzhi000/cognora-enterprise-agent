"""LangChain Core StructuredTool-backed enterprise tool registry."""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[[dict], object]
    args_schema: type[BaseModel] | None = None
    structured_tool: StructuredTool = field(init=False, repr=False)
    _structured_input: Callable[[dict], dict] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        func: Callable[..., Any]
        if self.args_schema is not None:
            def invoke_schema(**kwargs):
                return self.func(kwargs)

            self._structured_input = lambda args: args
            schema = self.args_schema
            func = invoke_schema
        else:
            def invoke_args(args: dict):
                return self.func(args or {})

            self._structured_input = lambda args: {"args": args or {}}
            schema = ToolInput
            func = invoke_args
        self.structured_tool = StructuredTool.from_function(
            func=func,
            name=self.name,
            description=self.description,
            args_schema=schema,
        )

    def invoke(self, args: dict | None = None):
        return self.structured_tool.invoke(self._structured_input(args or {}))


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def call(self, name: str, args: dict):
        return self._tools[name].invoke(args)
