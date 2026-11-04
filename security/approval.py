"""Human-in-the-loop: 高风险操作先建审批单，批了才能执行。

存储抽象：``ApprovalRepository`` 有 InMemory 与 Postgres 两种实现。
``AGENT_PG_DSN`` 是唯一开关；未配置时回落 InMemory，本地开发与 CI 不依赖数据库。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
import warnings
from itertools import count

DEFAULT_LEASE_SECONDS = 300


def approval_lease_seconds() -> int:
    """审批执行租约（秒）。默认 5 分钟；解析失败/非法值回落默认。"""
    raw = os.environ.get("AGENT_APPROVAL_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_LEASE_SECONDS


def reclaim_on_resume_enabled() -> bool:
    """resume 入口是否惰性回收过期租约（默认开）。"""
    raw = os.environ.get("AGENT_APPROVAL_RECLAIM_ON_RESUME", "true")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


class ApprovalConflict(ValueError):
    """并发下审批状态已被其他请求改写；调用方应按 409 处理。"""


class ApprovalRepository:
    def create(self, user: dict, tool: str, args: dict) -> str: ...
    def get(self, approval_id: str) -> dict: ...
    def approve(self, approval_id: str, approver: str) -> dict: ...
    def reject(self, approval_id: str, approver: str) -> dict: ...
    def claim(self, approval_id: str, lease_seconds: int | None = None) -> dict: ...
    def complete(self, approval_id: str) -> dict: ...
    def fail(self, approval_id: str, last_error: str) -> dict: ...
    def reclaim_expired_claims(self) -> int: ...
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

    def claim(self, approval_id: str, lease_seconds: int | None = None) -> dict:
        """原子认领：approved + 未消费 → in_progress；并发下只有 1 个赢家。"""
        lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "approved" or item.get("consumed"):
                raise ApprovalConflict(
                    f"approval {approval_id} is {item['status']}, not claimable")
            item["status"] = "in_progress"
            item["claimed_at"] = time.monotonic()
            item["lease_until"] = item["claimed_at"] + lease
            return item

    def complete(self, approval_id: str) -> dict:
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "in_progress":
                raise ValueError(
                    f"approval {approval_id} is {item['status']}, not in_progress")
            item["status"] = "done"
            item["consumed"] = True
            item["consumed_at"] = time.monotonic()
            return item

    def fail(self, approval_id: str, last_error: str) -> dict:
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "in_progress":
                raise ValueError(
                    f"approval {approval_id} is {item['status']}, not in_progress")
            item["status"] = "failed"
            item["last_error"] = (last_error or "")[:200]
            return item

    def reclaim_expired_claims(self) -> int:
        """租约过期的 in_progress 退回 approved；返回回收条数。"""
        now = time.monotonic()
        reclaimed = 0
        with self._lock:
            for item in self._items.values():
                if (item["status"] == "in_progress"
                        and item.get("lease_until") is not None
                        and item["lease_until"] < now):
                    item["status"] = "approved"
                    item["claimed_at"] = None
                    item["lease_until"] = None
                    reclaimed += 1
        return reclaimed

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
            "last_error": row.get("last_error"),
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
            "SELECT id, user_json, tool, args, status, approver, consumed, last_error "
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

    def claim(self, approval_id: str, lease_seconds: int | None = None) -> dict:
        """原子认领：approved + 未消费 → in_progress；0 行 = 已被他人认领或不可执行。"""
        lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'in_progress', claimed_at = now(), "
            "lease_until = now() + (CAST(? AS INTEGER) * INTERVAL '1 second'), "
            "updated_at = now() "
            "WHERE id = ? AND status = 'approved' AND consumed = FALSE",
            (lease, approval_id),
        )
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)  # 不存在 → KeyError
        raise ApprovalConflict(
            f"approval {approval_id} is {item['status']}, not claimable")

    def complete(self, approval_id: str) -> dict:
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET consumed = TRUE, consumed_at = now(), "
            "status = 'done', updated_at = now() "
            "WHERE id = ? AND status = 'in_progress' AND consumed = FALSE",
            (approval_id,),
        )
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)
        raise ValueError(f"approval {approval_id} is {item['status']}, not in_progress")

    def fail(self, approval_id: str, last_error: str) -> dict:
        summary = (last_error or "")[:200]
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'failed', last_error = ?, "
            "updated_at = now() WHERE id = ? AND status = 'in_progress'",
            (summary, approval_id),
        )
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)
        raise ValueError(f"approval {approval_id} is {item['status']}, not in_progress")

    def reclaim_expired_claims(self) -> int:
        """租约过期的 in_progress 退回 approved；返回回收条数（供观测）。"""
        return self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'approved', claimed_at = NULL, "
            "lease_until = NULL, updated_at = now() "
            "WHERE status = 'in_progress' AND lease_until IS NOT NULL "
            "AND lease_until < now()"
        )

    def list_items(self) -> list[dict]:
        rows = self._c.fetch_all(
            "SELECT id, user_json, tool, args, status, approver, consumed, last_error "
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

    def claim(self, approval_id: str, lease_seconds: int | None = None) -> dict:
        return self._repo.claim(approval_id, lease_seconds)

    def complete(self, approval_id: str) -> dict:
        return self._repo.complete(approval_id)

    def fail(self, approval_id: str, last_error: str) -> dict:
        return self._repo.fail(approval_id, last_error)

    def reclaim_expired_claims(self) -> int:
        return self._repo.reclaim_expired_claims()

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
