"""挂起任务持久化：InMemory 与 Postgres 两实现，按 (tenant_id, task_id) 隔离。

状态序列化要保留 resume 路径需要的一切：AgentState 字段 + context（含
user/trace_id/approval 元数据）+ context 里的 ``agent_task``。``agent_task``
本身不可 JSON 化，单独按 ``AgentTask.to_dict()`` 存，读回时重建。
"""
from __future__ import annotations

import json
import warnings


def _state_to_payload(state) -> dict:
    context = dict(getattr(state, "context", {}) or {})
    agent_task = context.pop("agent_task", None)
    payload = {
        "task": getattr(state, "task", ""),
        "task_id": getattr(state, "task_id", None),
        "messages": list(getattr(state, "messages", []) or []),
        "plan": getattr(state, "plan", None),
        "observations": list(getattr(state, "observations", []) or []),
        "tool_calls": list(getattr(state, "tool_calls", []) or []),
        "memory": dict(getattr(state, "memory", {}) or {}),
        "context": context,
        "iteration": getattr(state, "iteration", 0),
        "status": getattr(state, "status", "pending"),
        "answer": getattr(state, "answer", ""),
        "agent_task": agent_task.to_dict() if agent_task is not None else None,
    }
    return payload


def _payload_to_state(payload: dict):
    from agent.runtime.state import AgentState
    from agent.runtime.agent_task import AgentTask

    state = AgentState(
        task=payload.get("task", ""),
        task_id=payload.get("task_id"),
        messages=list(payload.get("messages") or []),
        plan=payload.get("plan"),
        observations=list(payload.get("observations") or []),
        tool_calls=list(payload.get("tool_calls") or []),
        memory=dict(payload.get("memory") or {}),
        context=dict(payload.get("context") or {}),
        iteration=int(payload.get("iteration") or 0),
        status=payload.get("status", "pending"),
        answer=payload.get("answer", ""),
    )
    at_data = payload.get("agent_task")
    if isinstance(at_data, dict):
        at = AgentTask(
            task_id=at_data.get("task_id") or state.task_id or "",
            tenant_id=at_data.get("tenant_id") or state.context.get("tenant_id") or "default",
            state=state,
            status=at_data.get("status", "paused"),
            checkpoint_id=at_data.get("checkpoint_id"),
            token_usage=dict(at_data.get("token_usage") or {"input": 0, "output": 0, "total": 0}),
            cost=float(at_data.get("cost") or 0.0),
            created_at=at_data.get("created_at") or "",
            updated_at=at_data.get("updated_at") or "",
            history=list(at_data.get("history") or []),
        )
        state.context["agent_task"] = at
    return state


def _json_load(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return None


class PausedTaskRepository:
    def save(self, tenant_id: str, task_id: str, payload: dict) -> None: ...
    def get(self, tenant_id: str, task_id: str) -> dict | None: ...
    def delete(self, tenant_id: str, task_id: str) -> None: ...


class InMemoryPausedTaskRepository(PausedTaskRepository):
    def __init__(self):
        self._items: dict[tuple[str, str], dict] = {}

    def save(self, tenant_id: str, task_id: str, payload: dict) -> None:
        self._items[(tenant_id, task_id)] = dict(payload)

    def get(self, tenant_id: str, task_id: str) -> dict | None:
        value = self._items.get((tenant_id, task_id))
        return dict(value) if value is not None else None

    def delete(self, tenant_id: str, task_id: str) -> None:
        self._items.pop((tenant_id, task_id), None)


class PostgresPausedTaskRepository(PausedTaskRepository):
    def __init__(self, connector):
        self._c = connector

    def save(self, tenant_id: str, task_id: str, payload: dict) -> None:
        self._c.execute(
            "INSERT INTO agent_paused_task (tenant_id, task_id, state, updated_at) "
            "VALUES (?, ?, ?, now()) "
            "ON CONFLICT (tenant_id, task_id) DO UPDATE "
            "SET state = EXCLUDED.state, updated_at = now()",
            (tenant_id, task_id, json.dumps(payload, default=str)),
        )

    def get(self, tenant_id: str, task_id: str) -> dict | None:
        row = self._c.fetch_one(
            "SELECT state FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
            (tenant_id, task_id),
        )
        if not row:
            return None
        return _json_load(row.get("state"))

    def delete(self, tenant_id: str, task_id: str) -> None:
        self._c.execute(
            "DELETE FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
            (tenant_id, task_id),
        )


class PausedTaskStore:
    def __init__(self, repository: PausedTaskRepository | None = None):
        self._repo = repository or InMemoryPausedTaskRepository()

    def save(self, tenant_id: str, task_id: str, state) -> None:
        self._repo.save(tenant_id, task_id, _state_to_payload(state))

    def get(self, tenant_id: str, task_id: str):
        payload = self._repo.get(tenant_id, task_id)
        return _payload_to_state(payload) if payload else None

    def delete(self, tenant_id: str, task_id: str) -> None:
        self._repo.delete(tenant_id, task_id)


def build_paused_task_store(dsn: str | None = None, connector=None) -> PausedTaskStore:
    from infrastructure.pg.connector import get_agent_connector
    if connector is None:
        connector = get_agent_connector(dsn)
    if connector is None:
        return PausedTaskStore(InMemoryPausedTaskRepository())
    try:
        return PausedTaskStore(PostgresPausedTaskRepository(connector))
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"paused-task PG repo init failed ({e}); using in-memory")
        return PausedTaskStore(InMemoryPausedTaskRepository())
