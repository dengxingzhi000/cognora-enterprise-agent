"""Human-in-the-loop: 高风险操作先建审批单，批了才能执行。

存储抽象：``ApprovalRepository`` 有 InMemory 与 Postgres 两种实现。
``AGENT_PG_DSN`` 是唯一开关；未配置时回落 InMemory，本地开发与 CI 不依赖数据库。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
import warnings
from itertools import count

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 300
# 2 次心跳失败 * 默认间隔（租约/3）= 2/3 租约 < 租约，满足下方的余量约束。
DEFAULT_HEARTBEAT_MAX_FAILURES = 2


class HeartbeatConfigError(ValueError):
    """心跳配置不满足余量约束；启动阶段直接失败，避免租约被误回收。"""


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


def instance_id() -> str:
    """本实例标识：``AGENT_INSTANCE_ID`` 优先，否则 ``hostname:pid``。"""
    raw = os.environ.get("AGENT_INSTANCE_ID")
    if raw and raw.strip():
        return raw.strip()
    return f"{socket.gethostname()}:{os.getpid()}"


def approval_heartbeat_enabled() -> bool:
    """执行期租约心跳开关（默认开）。关闭只用于演练「心跳缺席仍会过期」。"""
    raw = os.environ.get("AGENT_APPROVAL_HEARTBEAT_ENABLED", "true")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def heartbeat_interval_seconds(lease_seconds: int) -> float:
    """续期间隔。默认取租约的 1/3；显式配置优先，非法值回落推导值。"""
    fallback = max(0.05, float(lease_seconds) / 3.0)
    raw = os.environ.get("AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS")
    if not raw:
        return fallback
    try:
        return max(0.01, float(raw))
    except (TypeError, ValueError):
        return fallback


def validate_heartbeat_settings(lease_seconds: int | None = None,
                                interval: float | None = None,
                                max_failures: int | None = None) -> None:
    """启动校验：``max_failures * interval`` 必须严格小于租约，否则拒绝启动。

    心跳缺席时租约最多在 ``max_failures * interval`` 内被判定丢失；若该时长
    >= 租约，判定「丢失」时行可能已经过期而被他人回收，阈值就失去意义。
    选择 fail-fast 而不是自动缩小 interval：静默降级会让配置错误不可见，
    而这段窗口直接决定租约正确性。
    """
    if not approval_heartbeat_enabled():
        return
    lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
    interval = (heartbeat_interval_seconds(lease)
                if interval is None else max(0.01, float(interval)))
    failures = DEFAULT_HEARTBEAT_MAX_FAILURES if max_failures is None else max(1, int(max_failures))
    if failures * interval >= lease:
        raise HeartbeatConfigError(
            "invalid approval heartbeat config: "
            f"max_failures({failures}) * interval({interval}s) = {failures * interval}s "
            f"must be < lease({lease}s); lower AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS "
            "or raise AGENT_APPROVAL_LEASE_SECONDS (or disable the heartbeat)")


class ApprovalConflict(ValueError):
    """并发下审批状态已被其他请求改写；调用方应按 409 处理。"""


class ApprovalRepository:
    def create(self, user: dict, tool: str, args: dict) -> str: ...
    def get(self, approval_id: str) -> dict: ...
    def approve(self, approval_id: str, approver: str) -> dict: ...
    def reject(self, approval_id: str, approver: str) -> dict: ...
    def claim(self, approval_id: str, lease_seconds: int | None = None,
              claimed_by: str | None = None) -> dict: ...
    def renew(self, approval_id: str, claim_token: str,
              lease_seconds: int | None = None,
              claimed_by: str | None = None) -> bool: ...
    def complete(self, approval_id: str, claim_token: str) -> dict: ...
    def fail(self, approval_id: str, claim_token: str, last_error: str) -> dict: ...
    def release(self, approval_id: str, claim_token: str) -> bool: ...
    def reclaim_expired_claims(self, claimed_by: str | None = None) -> int: ...
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
                                "args": args or {}, "status": "pending",
                                "consumed": False, "claim_token": None,
                                "claimed_by": None, "last_error": None}
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

    def claim(self, approval_id: str, lease_seconds: int | None = None,
              claimed_by: str | None = None) -> dict:
        """原子认领：approved + 未消费 → in_progress；并发下只有 1 个赢家。"""
        lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
        token = str(uuid.uuid4())
        owner = claimed_by or instance_id()
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "approved" or item.get("consumed"):
                raise ApprovalConflict(
                    f"approval {approval_id} is {item['status']}, not claimable")
            item["status"] = "in_progress"
            item["claim_token"] = token
            item["claimed_by"] = owner
            item["claimed_at"] = time.monotonic()
            item["lease_until"] = item["claimed_at"] + lease
            return dict(item)

    def renew(self, approval_id: str, claim_token: str,
              lease_seconds: int | None = None,
              claimed_by: str | None = None) -> bool:
        """心跳续期：仅当行仍由同一 claim_token/实例持有且未过期时生效。"""
        lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
        owner = claimed_by or instance_id()
        now = time.monotonic()
        with self._lock:
            item = self._items.get(approval_id)
            if item is None or item["status"] != "in_progress":
                return False
            if item.get("claim_token") != claim_token:
                return False
            if item.get("claimed_by") != owner:
                return False
            until = item.get("lease_until")
            if until is None or until <= now:
                return False
            item["lease_until"] = now + lease
            return True

    def complete(self, approval_id: str, claim_token: str) -> dict:
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "in_progress" or item.get("claim_token") != claim_token:
                raise ValueError(
                    f"approval {approval_id} is {item['status']}, not held by this claim")
            item["status"] = "done"
            item["consumed"] = True
            item["consumed_at"] = time.monotonic()
            return dict(item)

    def fail(self, approval_id: str, claim_token: str, last_error: str) -> dict:
        with self._lock:
            item = self._items[approval_id]
            if item["status"] != "in_progress" or item.get("claim_token") != claim_token:
                raise ValueError(
                    f"approval {approval_id} is {item['status']}, not held by this claim")
            item["status"] = "failed"
            item["last_error"] = (last_error or "")[:200]
            return dict(item)

    def release(self, approval_id: str, claim_token: str) -> bool:
        """把刚 claim 的行退回 approved（尚未执行任何副作用时使用）。"""
        with self._lock:
            item = self._items.get(approval_id)
            if (item is None or item["status"] != "in_progress"
                    or item.get("claim_token") != claim_token):
                return False
            item["status"] = "approved"
            item["claimed_at"] = None
            item["lease_until"] = None
            item["claim_token"] = None
            item["claimed_by"] = None
            return True

    def reclaim_expired_claims(self, claimed_by: str | None = None) -> int:
        """租约过期的 in_progress 退回 approved；返回回收条数。"""
        now = time.monotonic()
        owner = claimed_by or instance_id()
        reclaimed = 0
        with self._lock:
            for item in self._items.values():
                if (item["status"] == "in_progress"
                        and item.get("lease_until") is not None
                        and item["lease_until"] < now
                        and not (item.get("claimed_by") == owner
                                 and item["lease_until"] > now)):
                    item["status"] = "approved"
                    item["claimed_at"] = None
                    item["lease_until"] = None
                    item["claim_token"] = None
                    item["claimed_by"] = None
                    reclaimed += 1
        if reclaimed:
            from observability import metrics as _metrics
            _metrics.inc_approval_reclaimed(reclaimed)
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
            "claim_token": str(row["claim_token"]) if row.get("claim_token") else None,
            "claimed_by": row.get("claimed_by"),
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
            "SELECT id, user_json, tool, args, status, approver, consumed, last_error, "
            "claim_token, claimed_by "
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

    def claim(self, approval_id: str, lease_seconds: int | None = None,
              claimed_by: str | None = None) -> dict:
        """原子认领：approved + 未消费 → in_progress；0 行 = 已被他人认领或不可执行。"""
        lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
        owner = claimed_by or instance_id()
        token = str(uuid.uuid4())
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'in_progress', claim_token = ?, "
            "claimed_by = ?, claimed_at = now(), "
            "lease_until = now() + (CAST(? AS INTEGER) * INTERVAL '1 second'), "
            "updated_at = now() "
            "WHERE id = ? AND status = 'approved' AND consumed = FALSE",
            (token, owner, lease, approval_id),
        )
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)  # 不存在 → KeyError
        raise ApprovalConflict(
            f"approval {approval_id} is {item['status']}, not claimable")

    def renew(self, approval_id: str, claim_token: str,
              lease_seconds: int | None = None,
              claimed_by: str | None = None) -> bool:
        """心跳续期：条件更新，0 行 = 租约已被回收/已过期/已换主，必须停止续期。"""
        lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
        owner = claimed_by or instance_id()
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval "
            "SET lease_until = now() + (CAST(? AS INTEGER) * INTERVAL '1 second'), "
            "updated_at = now() "
            "WHERE id = ? AND status = 'in_progress' AND claim_token = ? "
            "AND claimed_by = ? "
            "AND lease_until IS NOT NULL AND lease_until > now()",
            (lease, approval_id, claim_token, owner),
        )
        return bool(updated)

    def complete(self, approval_id: str, claim_token: str) -> dict:
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET consumed = TRUE, consumed_at = now(), "
            "status = 'done', updated_at = now() "
            "WHERE id = ? AND status = 'in_progress' AND consumed = FALSE "
            "AND claim_token = ?",
            (approval_id, claim_token),
        )
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)
        raise ValueError(
            f"approval {approval_id} is {item['status']}, not held by this claim")

    def fail(self, approval_id: str, claim_token: str, last_error: str) -> dict:
        summary = (last_error or "")[:200]
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'failed', last_error = ?, "
            "updated_at = now() WHERE id = ? AND status = 'in_progress' "
            "AND claim_token = ?",
            (summary, approval_id, claim_token),
        )
        if updated:
            return self.get(approval_id)
        item = self.get(approval_id)
        raise ValueError(
            f"approval {approval_id} is {item['status']}, not held by this claim")

    def release(self, approval_id: str, claim_token: str) -> bool:
        """把刚 claim 的行退回 approved（尚未执行任何副作用时使用）。"""
        updated = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'approved', claimed_at = NULL, "
            "lease_until = NULL, claim_token = NULL, claimed_by = NULL, updated_at = now() "
            "WHERE id = ? AND status = 'in_progress' AND claim_token = ?",
            (approval_id, claim_token),
        )
        return bool(updated)

    def reclaim_expired_claims(self, claimed_by: str | None = None) -> int:
        """租约过期的 in_progress 退回 approved；返回回收条数（供观测）。

        显式排除「本实例持有且租约仍在有效期内」的行，避免自己回收自己。
        """
        owner = claimed_by or instance_id()
        reclaimed = self._c.execute_rowcount(
            "UPDATE agent_approval SET status = 'approved', claimed_at = NULL, "
            "lease_until = NULL, claim_token = NULL, claimed_by = NULL, updated_at = now() "
            "WHERE status = 'in_progress' AND lease_until IS NOT NULL "
            "AND lease_until < now() "
            "AND NOT (claimed_by = ? AND lease_until > now())",
            (owner,),
        )
        if reclaimed:
            from observability import metrics as _metrics
            _metrics.inc_approval_reclaimed(reclaimed)
        return reclaimed

    def list_items(self) -> list[dict]:
        rows = self._c.fetch_all(
            "SELECT id, user_json, tool, args, status, approver, consumed, last_error, "
            "claim_token, claimed_by "
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

    def claim(self, approval_id: str, lease_seconds: int | None = None,
              claimed_by: str | None = None) -> dict:
        return self._repo.claim(approval_id, lease_seconds, claimed_by)

    def renew(self, approval_id: str, claim_token: str,
              lease_seconds: int | None = None,
              claimed_by: str | None = None) -> bool:
        return self._repo.renew(approval_id, claim_token, lease_seconds, claimed_by)

    def complete(self, approval_id: str, claim_token: str) -> dict:
        return self._repo.complete(approval_id, claim_token)

    def fail(self, approval_id: str, claim_token: str, last_error: str) -> dict:
        return self._repo.fail(approval_id, claim_token, last_error)

    def release(self, approval_id: str, claim_token: str) -> bool:
        return self._repo.release(approval_id, claim_token)

    def reclaim_expired_claims(self, claimed_by: str | None = None) -> int:
        return self._repo.reclaim_expired_claims(claimed_by)

    def list_items(self) -> list[dict]:
        return self._repo.list_items()


class LeaseHeartbeat:
    """执行期租约心跳：每隔 ``lease/3`` 续一次，租约不再需要大于工具时长。

    续期条件更新同时带 ``claim_token``、``status='in_progress'`` 与
    ``lease_until > now()``，所以被他人回收/重新认领的行不会被续命；0 行受影响
    即判定租约丢失（``lost=True``），由调用方决定如何处置。数据库瞬时不可用只记
    warning 并计数，超过阈值同样判定丢失。
    """

    def __init__(self, store, approval_id: str, claim_token: str, lease_seconds: int,
                 interval: float | None = None,
                 max_failures: int = DEFAULT_HEARTBEAT_MAX_FAILURES):
        self._store = store
        self._id = approval_id
        self._token = claim_token
        self._lease = max(1, int(lease_seconds))
        self.interval = (heartbeat_interval_seconds(self._lease)
                         if interval is None else max(0.01, float(interval)))
        self.max_failures = max(1, int(max_failures))
        self.failures = 0
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run, name=f"approval-heartbeat-{self._id}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止续期；工具返回/抛异常后必须调用（finally），避免线程泄漏。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self.interval * 2))
        self._thread = None

    def _run(self) -> None:
        from observability import metrics as _metrics

        while not self._stop.wait(self.interval):
            try:
                renewed = self._store.renew(self._id, self._token, self._lease)
            except Exception as e:  # noqa: BLE001 - 瞬时故障不断执行，只计数
                self.failures += 1
                _metrics.inc_approval_heartbeat_failed()
                logger.warning("approval heartbeat renew failed (%d/%d): %s",
                               self.failures, self.max_failures, e)
                if self.failures >= self.max_failures:
                    self.lost = True
                    _metrics.inc_approval_lease_lost()
                    logger.warning("approval %s lease lost after %d renew failures",
                                   self._id, self.failures)
                    return
                continue
            self.failures = 0
            if not renewed:
                self.lost = True
                _metrics.inc_approval_lease_lost()
                logger.warning("approval %s lease was reclaimed; heartbeat stopped",
                               self._id)
                return


def start_lease_heartbeat(store, approval_id: str, claim_token: str,
                          lease_seconds: int | None = None) -> LeaseHeartbeat | None:
    """claim 成功后启动心跳；配置关闭时返回 None（演练「心跳缺席」可复现）。"""
    if not approval_heartbeat_enabled():
        return None
    lease = approval_lease_seconds() if lease_seconds is None else max(1, int(lease_seconds))
    return LeaseHeartbeat(store, approval_id, claim_token, lease).start()


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
