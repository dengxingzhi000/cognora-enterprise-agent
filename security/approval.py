"""Human-in-the-loop: 高风险操作先建审批单，批了才能执行。

存储抽象：``ApprovalRepository`` 有 InMemory 与 Postgres 两种实现。
``AGENT_PG_DSN`` 是唯一开关；未配置时回落 InMemory，本地开发与 CI 不依赖数据库。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
import warnings
from itertools import count


class ApprovalConflict(ValueError):
    """并发下审批状态已被其他请求改写；调用方应按 409 处理。"""


class ApprovalRepository:
    def create(self, user: dict, tool: str, args: dict) -> str: ...
    def get(self, approval_id: str) -> dict: ...
    def approve(self, approval_id: str, approver: str) -> dict: ...
    def reject(self, approval_id: str, approver: str) -> dict: ...
    def consume(self, approval_id: str) -> dict: ...
    def list_items(self) -> list[dict]: ...


class InMemoryApprovalRepository(ApprovalRepository):
    def __init__(self):
        self._items: dict[str, dict] = {}
        self._seq = count(1)
        self._lock = threading.Lock()

    def create(self, user: dict, tool: str, args: dict) -> str:
        aid = f"apr-{next(self._seq)}"
        with self._lock:
            self._items[aid] = {"id": aid, "user": user or {}, "tool": tool,
                                "args": args or {}, "status": "pending"}
        return aid

    def get(self, approval_id: str) -> dict:
        return self._items[approval_id]

    def _set_status(self, approval_id: str, status: str, approver: str) -> dict:
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "pending":
                raise ApprovalConflict(
                    f"approval {approval_id} is {item['status']}, not pending")
            item["status"] = status
            item["approver"] = approver
            return item

    def approve(self, approval_id: str, approver: str) -> dict:
        return self._set_status(approval_id, "approved", approver)

    def reject(self, approval_id: str, approver: str) -> dict:
        return self._set_status(approval_id, "rejected", approver)

    def consume(self, approval_id: str) -> dict:
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "approved":
                raise ValueError(f"approval {approval_id} is {item['status']}, not approved")
            if item.get("consumed"):
                raise ValueError(f"approval {approval_id} already consumed")
            item["consumed"] = True
            return item

    def list_items(self) -> list[dict]:
        return list(self._items.values())


def _json_load(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class PostgresApprovalRepository(ApprovalRepository):
    """``agent_approval`` 表实现；id 用 uuid 避免多进程撞号。"""

    def __init__(self, connector):
        self._c = connector

    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "user": _json_load(row.get("user_json"), {}),
            "tool": row["tool"],
            "args": _json_load(row.get("args"), {}),
            "status": row["status"],
            "approver": row.get("approver"),
            "consumed": bool(row.get("consumed")),
        }

    def create(self, user: dict, tool: str, args: dict) -> str:
        aid = f"apr-{uuid.uuid4().hex[:12]}"
        tenant_id = (user or {}).get("tenant_id") or (args or {}).get("tenant_id") or "default"
        self._c.execute(
            "INSERT INTO agent_approval "
            "(id, tenant_id, user_id, user_json, tool, args, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (aid, tenant_id, (user or {}).get("user_id"),
             json.dumps(user or {}), tool, json.dumps(args or {})),
        )
        return aid

    def get(self, approval_id: str) -> dict:
        row = self._c.fetch_one(
            "SELECT id, user_json, tool, args, status, approver, consumed "
            "FROM agent_approval WHERE id = ?", (approval_id,))
        if not row:
            raise KeyError(approval_id)
        return self._row(row)

    def _set_status(self, approval_id: str, status: str, approver: str) -> dict:
        # 单条条件更新：并发 approve/reject 只有一个能把 pending 改走。
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = ?, approver = ?, updated_at = now() "
            "WHERE id = ? AND status = 'pending'", (status, approver, approval_id))
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)  # 不存在 → KeyError
        raise ApprovalConflict(f"approval {approval_id} is {item['status']}, not pending")

    def approve(self, approval_id: str, approver: str) -> dict:
        return self._set_status(approval_id, "approved", approver)

    def reject(self, approval_id: str, approver: str) -> dict:
        return self._set_status(approval_id, "rejected", approver)

    def consume(self, approval_id: str) -> dict:
        # 单条条件更新：并发 consume 只有一个能把 consumed 从 FALSE 改走。
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET consumed = TRUE, consumed_at = now(), "
            "updated_at = now() WHERE id = ? AND consumed = FALSE AND status = 'approved'",
            (approval_id,))
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)
        if item["status"] != "approved":
            raise ValueError(f"approval {approval_id} is {item['status']}, not approved")
        raise ValueError(f"approval {approval_id} already consumed")

    def list_items(self) -> list[dict]:
        rows = self._c.fetch_all(
            "SELECT id, user_json, tool, args, status, approver, consumed "
            "FROM agent_approval ORDER BY created_at")
        return [self._row(r) for r in rows]


class ApprovalStore:
    def __init__(self, repository: ApprovalRepository | None = None):
        self._repo = repository or InMemoryApprovalRepository()

    @property
    def _items(self) -> dict[str, dict]:
        """兼容既有调用方（graph_lang 只读遍历）；PG 实现每次读取全量。"""
        return {item["id"]: item for item in self._repo.list_items()}

    def request(self, user: dict, tool: str, args: dict) -> str:
        return self._repo.create(user or {}, tool, args or {})

    def get(self, approval_id: str) -> dict:
        return self._repo.get(approval_id)

    def approve(self, approval_id: str, approver: str) -> dict:
        return self._repo.approve(approval_id, approver)

    def reject(self, approval_id: str, approver: str) -> dict:
        return self._repo.reject(approval_id, approver)

    def consume(self, approval_id: str) -> dict:
        return self._repo.consume(approval_id)

    def list_items(self) -> list[dict]:
        return self._repo.list_items()


def build_approval_store(dsn: str | None = None, connector=None) -> ApprovalStore:
    """有可用 PG 时用 Postgres 表，否则 InMemory；任何失败都回落。"""
    from infrastructure.pg.connector import get_agent_connector
    if connector is None:
        connector = get_agent_connector(dsn)
    if connector is None:
        return ApprovalStore(InMemoryApprovalRepository())
    try:
        return ApprovalStore(PostgresApprovalRepository(connector))
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"approval PG repo init failed ({e}); using in-memory")
        return ApprovalStore(InMemoryApprovalRepository())
