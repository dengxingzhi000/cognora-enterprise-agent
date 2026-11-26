"""Unified, tenant-bound registry for read-only LangGraph workflows."""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.tools.executor import ToolTimeout, execute_with_timeout
from integrations.scm.context import scm_identity_context
from security.internal_auth import TrustedIdentity

MAX_INPUT_BYTES = 64 * 1024
MAX_STRING_CHARS = 2000
MAX_CLAUSES = 100
MAX_AMOUNT = 999_999_999_999
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WorkflowError(ValueError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    description: str
    runnable: bool
    input_fields: dict[str, str]
    reason: str | None = None
    read_only: bool = True

    def catalog_entry(self) -> dict:
        entry = {
            "name": self.name,
            "description": self.description,
            "runnable": self.runnable,
            "readOnly": self.read_only,
            "input": self.input_fields,
        }
        if self.reason:
            entry["reason"] = self.reason
        return entry


def _reject_nonfinite(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input is nested too deeply")
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow numbers must be finite")
    if isinstance(value, dict):
        if len(value) > 50:
            raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input has too many fields")
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input keys must be strings")
            _reject_nonfinite(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_CLAUSES:
            raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input has too many items")
        for item in value:
            _reject_nonfinite(item, depth=depth + 1)


def _required_string(data: dict, field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", f"{field} must be a non-blank string")
    if len(value) > MAX_STRING_CHARS:
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", f"{field} is too long")
    return value.strip()


def _only_fields(data: dict, allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input contains unsupported fields")


def _validate_it_ops(data: dict) -> dict:
    _only_fields(data, {"question"})
    return {"question": _required_string(data, "question")}


def _validate_contract(data: dict) -> dict:
    _only_fields(data, {"amount", "clauses", "supplier_id"})
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "amount must be a number")
    if not math.isfinite(amount) or amount < 0 or amount > MAX_AMOUNT:
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "amount is outside the allowed range")
    clauses = data.get("clauses")
    if not isinstance(clauses, list) or len(clauses) > MAX_CLAUSES:
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "clauses must be a bounded list")
    if any(not isinstance(clause, str) or len(clause) > MAX_STRING_CHARS for clause in clauses):
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "each clause must be a bounded string")
    validated: dict[str, Any] = {"amount": amount, "clauses": list(clauses)}
    if "supplier_id" in data:
        supplier_id = data["supplier_id"]
        if not isinstance(supplier_id, str) or not _SAFE_ID.fullmatch(supplier_id):
            raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "supplier_id is invalid")
        validated["supplier_id"] = supplier_id
    return validated


def _validate_sales(data: dict) -> dict:
    _only_fields(data, {"question", "range"})
    selected_range = data.get("range", "7d")
    if selected_range not in ("today", "7d", "30d", "TODAY", "LAST_7_DAYS", "LAST_30_DAYS"):
        raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "unsupported sales range")
    return {"question": _required_string(data, "question"), "range": selected_range}


_VALIDATORS: dict[str, Callable[[dict], dict]] = {
    "it_ops": _validate_it_ops,
    "contract": _validate_contract,
    "sales": _validate_sales,
}


class WorkflowState(TypedDict, total=False):
    run_id: str
    input: dict[str, Any]
    tenant_id: str
    user_id: str
    role: str
    output: dict


class WorkflowRegistry:
    """Catalog, validation, LangGraph execution and tenant-scoped run metadata."""

    def __init__(self):
        self._definitions = OrderedDict([
            ("it_ops", WorkflowDefinition(
                "it_ops", "Read-only IT operations diagnosis", True,
                {"question": "string, required"},
            )),
            ("contract", WorkflowDefinition(
                "contract", "Read-only contract compliance review", True,
                {"amount": "finite number, required", "clauses": "string list, required",
                 "supplier_id": "safe identifier, optional"},
            )),
            ("sales", WorkflowDefinition(
                "sales", "Read-only sales analysis", True,
                {"question": "string, required", "range": "today|7d|30d, optional"},
            )),
            ("expense", WorkflowDefinition(
                "expense", "Expense approval workflow", False,
                {"expense": "object"},
                reason="durable pause/resume is not exposed by the unified workflow API",
            )),
        ])
        self._graphs: dict[str, CompiledStateGraph] = {}
        self._runs: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._lock = threading.RLock()
        for name in _VALIDATORS:
            self._graphs[name] = self._build_graph(name)

    @staticmethod
    def _build_graph(name: str) -> CompiledStateGraph:
        def execute(state: WorkflowState) -> dict:
            from workflow import scenarios

            handlers = {
                "it_ops": lambda: scenarios.run_it_ops(
                    state["input"]["question"], tenant_id=state["tenant_id"],
                ),
                "contract": lambda: scenarios.review_contract(
                    state["input"], tenant_id=state["tenant_id"],
                ),
                "sales": lambda: scenarios.analyze_sales(
                    state["input"]["question"], tenant_id=state["tenant_id"],
                    range=state["input"]["range"],
                ),
            }
            try:
                output = execute_with_timeout(handlers[name])
            except ToolTimeout:
                return {"output": {"status": "error", "error": "tool_timeout"}}
            return {"output": output}

        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node("execute", execute)
        graph.set_entry_point("execute")
        graph.add_edge("execute", END)
        return graph.compile()

    def catalog(self) -> list[dict]:
        return [definition.catalog_entry() for definition in self._definitions.values()]

    def graph_for(self, name: str) -> CompiledStateGraph:
        if name not in self._definitions:
            raise WorkflowError(404, "WORKFLOW_NOT_FOUND", "workflow not found")
        if name not in self._graphs:
            raise WorkflowError(409, "WORKFLOW_NOT_RUNNABLE",
                                self._definitions[name].reason or "workflow is not runnable")
        return self._graphs[name]

    @staticmethod
    def validate_input(name: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input must be an object")
        raw = dict(payload)
        if "input" in raw:
            raw = raw["input"]
            if not isinstance(raw, dict):
                raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "input must be an object")
        # Identity and run identifiers are server-owned even when sent at the top level.
        for field in ("tenant_id", "user_id", "role", "run_id", "platform_token"):
            raw.pop(field, None)
        _reject_nonfinite(raw)
        try:
            encoded_size = len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input is not valid JSON") from None
        if encoded_size > MAX_INPUT_BYTES:
            raise WorkflowError(422, "INVALID_WORKFLOW_INPUT", "workflow input is too large")
        return _VALIDATORS[name](raw)

    def run(self, name: str, payload: dict, identity: TrustedIdentity) -> dict:
        self.graph_for(name)  # unknown/non-runnable fails before allocating a run id
        validated = self.validate_input(name, payload)
        run_id = f"run-{uuid.uuid4().hex}"
        state = {
            "run_id": run_id,
            "input": validated,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "role": identity.role,
        }
        with scm_identity_context(identity):
            try:
                result = self._graphs[name].invoke(
                    state, config={"configurable": {"thread_id": run_id}},
                )
                output = result.get("output", {})
                status = "succeeded"
            except Exception:  # noqa: BLE001 - do not expose internal workflow errors
                output = {"status": "error", "error": "workflow execution failed"}
                status = "failed"
        response = {"run_id": run_id, "workflow": name, "status": status, "output": output}
        with self._lock:
            self._runs[(identity.tenant_id, run_id)] = {
                **copy.deepcopy(response), "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
            }
            while len(self._runs) > 1000:
                self._runs.popitem(last=False)
        return copy.deepcopy(response)

    def get(self, tenant_id: str, run_id: str) -> dict | None:
        with self._lock:
            value = self._runs.get((tenant_id, run_id))
            return copy.deepcopy(value) if value is not None else None
