"""写工具的幂等占位：同一 approval + 工具 + 参数最多真正执行一次。

执行前先 ``INSERT ... ON CONFLICT DO NOTHING`` 占位（status=started），占位成功
才调用工具；已存在时按状态处理：

- ``succeeded`` → 返回缓存引用，**不再执行**
- ``started`` → 有执行在进行/已崩溃，返回冲突（绝不并跑）
- ``failed`` → 允许一次重试（attempt=2），第三次拒绝

``args_hash`` 用稳定序列化：递归按 key 排序、去掉租约/时间等易变字段，因此
同一动作的不同请求体顺序不会产生不同 effect。``result_ref`` 只存摘要，不存
工具返回明文。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from infrastructure.pg.connector import get_agent_connector

MAX_TOOL_EFFECT_ATTEMPTS = 2
STALE_STARTED_ERROR = "stale started effect reclaimed"


def tool_effect_stale_seconds() -> int:
    """``started`` 占位的过期窗口。

    默认 = 工具超时（``AGENT_TOOL_TIMEOUT_SECONDS``，默认 120s）+ 一个审批租约
    （``AGENT_APPROVAL_LEASE_SECONDS``，默认 300s）= 420s：超时是工具允许的最长
    执行时间，再加一个租约确保即使崩溃发生在心跳续期前后，审批行也已自然过期；
    真正回收时还会再次校验 approval 不持有活跃租约，所以窗口只是保守门槛。
    """
    raw = os.getenv("AGENT_TOOL_EFFECT_STALE_SECONDS")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    from agent.tools.executor import tool_timeout_seconds
    from security.approval import approval_lease_seconds

    return tool_timeout_seconds() + approval_lease_seconds()


def _approval_active(approvals, approval_id: str) -> bool:
    """approval 是否仍持有活跃执行租约；查询失败按「未知」处理为活跃（不回收）。"""
    if approvals is None:
        return True
    try:
        item = approvals.get(approval_id)
    except Exception:  # noqa: BLE001 - 查不到按活跃处理，绝不误回收
        return True
    if item.get("status") != "in_progress":
        return False
    until = item.get("lease_until")
    return until is not None and until > time.monotonic()


# 参数里不影响「这是什么动作」的易变字段；比较前递归剔除。
VOLATILE_ARG_KEYS = frozenset({
    "tenant_id", "tenantId",
    "request_id", "requestId", "trace_id", "traceId",
    "created_at", "updated_at", "timestamp", "request_time",
    "started_at", "finished_at", "expires_at", "deadline",
})

EXECUTE = "execute"
CACHED = "cached"
CONFLICT = "conflict"


def _strip_volatile(value):
    if isinstance(value, dict):
        return {k: _strip_volatile(value[k]) for k in sorted(value) if k not in VOLATILE_ARG_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def args_hash(args: dict | None) -> str:
    """稳定 args 摘要：排序 key、剔除易变字段、紧凑 JSON、sha256。"""
    payload = json.dumps(_strip_volatile(args or {}), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def result_ref(result) -> str:
    """工具结果的摘要引用（不落明文），用于 succeeded 时回放。"""
    text = str(result)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def effect_key(tenant_id: str, approval_id: str, tool_name: str, args: dict | None):
    return (tenant_id or "default", approval_id, tool_name, args_hash(args))


class InMemoryToolEffectRepository:
    def __init__(self):
        self._items: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    def begin(self, tenant_id, approval_id, tool_name, args):
        key = effect_key(tenant_id, approval_id, tool_name, args)
        now = time.monotonic()
        with self._lock:
            row = self._items.get(key)
            if row is None:
                self._items[key] = {
                    "tenant_id": key[0], "approval_id": key[1], "tool_name": key[2],
                    "args_hash": key[3], "status": "started", "attempt": 1,
                    "result_ref": None, "maybe_side_effect": False,
                    "last_error": None, "created_at": now, "updated_at": now,
                }
                return {"decision": EXECUTE, "attempt": 1, "status": "started",
                        "result_ref": None, "maybe_side_effect": False}
            if row["status"] == "succeeded":
                return {"decision": CACHED, "attempt": row["attempt"], "status": "succeeded",
                        "result_ref": row["result_ref"],
                        "maybe_side_effect": row["maybe_side_effect"]}
            if row["status"] == "started":
                return {"decision": CONFLICT, "attempt": row["attempt"], "status": "started",
                        "result_ref": None, "maybe_side_effect": row["maybe_side_effect"],
                        "reason": "an execution is in progress or crashed"}
            if row["attempt"] >= MAX_TOOL_EFFECT_ATTEMPTS:
                return {"decision": CONFLICT, "attempt": row["attempt"], "status": "failed",
                        "result_ref": row["result_ref"],
                        "maybe_side_effect": row["maybe_side_effect"],
                        "reason": "retry budget exhausted"}
            row["status"] = "started"
            row["attempt"] += 1
            row["result_ref"] = None
            row["maybe_side_effect"] = False
            row["last_error"] = None
            row["updated_at"] = now
            return {"decision": EXECUTE, "attempt": row["attempt"], "status": "started",
                    "result_ref": None, "maybe_side_effect": False}

    def succeed(self, tenant_id, approval_id, tool_name, args, ref):
        key = effect_key(tenant_id, approval_id, tool_name, args)
        with self._lock:
            row = self._items.get(key)
            if row is None or row["status"] != "started":
                return False
            row["status"] = "succeeded"
            row["result_ref"] = ref
            row["maybe_side_effect"] = False
            row["updated_at"] = time.monotonic()
            return True

    def fail(self, tenant_id, approval_id, tool_name, args, *, maybe_side_effect: bool):
        key = effect_key(tenant_id, approval_id, tool_name, args)
        with self._lock:
            row = self._items.get(key)
            if row is None or row["status"] != "started":
                return False
            row["status"] = "failed"
            row["maybe_side_effect"] = bool(maybe_side_effect)
            row["last_error"] = None
            row["updated_at"] = time.monotonic()
            return True

    def reclaim_stale_started(self, stale_seconds: int, approvals) -> int:
        """回收崩溃遗留的 ``started``：仅当过期且 approval 已无活跃租约。"""
        threshold = time.monotonic() - max(1, int(stale_seconds))
        count = 0
        with self._lock:
            for row in self._items.values():
                if row["status"] != "started" or row["updated_at"] > threshold:
                    continue
                if _approval_active(approvals, row["approval_id"]):
                    continue
                row["status"] = "failed"
                row["maybe_side_effect"] = True
                row["last_error"] = STALE_STARTED_ERROR
                row["updated_at"] = time.monotonic()
                count += 1
        return count

    def list_stale_started(self, stale_seconds: int, approvals) -> list[dict]:
        threshold = time.monotonic() - max(1, int(stale_seconds))
        rows = []
        with self._lock:
            for row in self._items.values():
                if row["status"] != "started" or row["updated_at"] > threshold:
                    continue
                if _approval_active(approvals, row["approval_id"]):
                    continue
                try:
                    approval = approvals.get(row["approval_id"])
                except Exception:  # noqa: BLE001 - 视图允许审批已不存在
                    approval = {}
                rows.append({
                    "tenant_id": row["tenant_id"],
                    "approval_id": row["approval_id"],
                    "tool_name": row["tool_name"],
                    "args_hash": row["args_hash"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "maybe_side_effect": row["maybe_side_effect"],
                    "updated_at": row["updated_at"],
                    "approval_status": approval.get("status"),
                    "approval_lease_until": approval.get("lease_until"),
                })
        return sorted(rows, key=lambda r: r["updated_at"])

    def get(self, tenant_id, approval_id, tool_name, args):
        return self._items.get(effect_key(tenant_id, approval_id, tool_name, args))

    def list_items(self):
        return [dict(v) for v in self._items.values()]

    def delete_by_approval(self, approval_id):
        for key in list(self._items):
            if key[1] == approval_id:
                del self._items[key]


class PostgresToolEffectRepository:
    def __init__(self, connector):
        self._c = connector

    def begin(self, tenant_id, approval_id, tool_name, args):
        tenant = tenant_id or "default"
        digest = args_hash(args)
        inserted = self._c.execute_rowcount(
            "INSERT INTO agent_tool_effect "
            "(tenant_id, approval_id, tool_name, args_hash, status, attempt) "
            "VALUES (?, ?, ?, ?, 'started', 1) ON CONFLICT DO NOTHING",
            (tenant, approval_id, tool_name, digest),
        )
        if inserted:
            return {"decision": EXECUTE, "attempt": 1, "status": "started",
                    "result_ref": None, "maybe_side_effect": False}

        row = self._c.fetch_one(
            "SELECT status, attempt, result_ref, maybe_side_effect FROM agent_tool_effect "
            "WHERE tenant_id = ? AND approval_id = ? AND tool_name = ? AND args_hash = ?",
            (tenant, approval_id, tool_name, digest))
        if row is None:  # 竞态下被删（当前无删除路径），按可执行处理
            return {"decision": EXECUTE, "attempt": 1, "status": "started",
                    "result_ref": None, "maybe_side_effect": False}
        if row["status"] == "succeeded":
            return {"decision": CACHED, "attempt": row["attempt"], "status": "succeeded",
                    "result_ref": row["result_ref"],
                    "maybe_side_effect": bool(row["maybe_side_effect"])}
        if row["status"] == "started":
            return {"decision": CONFLICT, "attempt": row["attempt"], "status": "started",
                    "result_ref": None, "maybe_side_effect": bool(row["maybe_side_effect"]),
                    "reason": "an execution is in progress or crashed"}

        # status='failed'：条件更新只允许一次重试（attempt 1 -> 2）。
        claimed = self._c.execute_rowcount(
            "UPDATE agent_tool_effect SET status = 'started', attempt = attempt + 1, "
            "result_ref = NULL, maybe_side_effect = FALSE, updated_at = now() "
            "WHERE tenant_id = ? AND approval_id = ? AND tool_name = ? AND args_hash = ? "
            "AND status = 'failed' AND attempt < ?",
            (tenant, approval_id, tool_name, digest, MAX_TOOL_EFFECT_ATTEMPTS),
        )
        if claimed:
            return {"decision": EXECUTE, "attempt": int(row["attempt"]) + 1,
                    "status": "started", "result_ref": None, "maybe_side_effect": False}
        return {"decision": CONFLICT, "attempt": row["attempt"], "status": "failed",
                "result_ref": row.get("result_ref"),
                "maybe_side_effect": bool(row["maybe_side_effect"]),
                "reason": "retry budget exhausted"}

    def succeed(self, tenant_id, approval_id, tool_name, args, ref):
        return bool(self._c.execute_rowcount(
            "UPDATE agent_tool_effect SET status = 'succeeded', result_ref = ?, "
            "maybe_side_effect = FALSE, last_error = NULL, updated_at = now() "
            "WHERE tenant_id = ? AND approval_id = ? AND tool_name = ? AND args_hash = ? "
            "AND status = 'started'",
            (ref, tenant_id or "default", approval_id, tool_name, args_hash(args)),
        ))

    def fail(self, tenant_id, approval_id, tool_name, args, *, maybe_side_effect: bool):
        return bool(self._c.execute_rowcount(
            "UPDATE agent_tool_effect SET status = 'failed', maybe_side_effect = ?, "
            "last_error = NULL, updated_at = now() "
            "WHERE tenant_id = ? AND approval_id = ? AND tool_name = ? AND args_hash = ? "
            "AND status = 'started'",
            (bool(maybe_side_effect), tenant_id or "default", approval_id, tool_name,
             args_hash(args)),
        ))

    def reclaim_stale_started(self, stale_seconds: int, approvals=None) -> int:
        """条件 UPDATE：status=started + 已过期 + approval 无活跃租约。"""
        return self._c.execute_rowcount(
            "UPDATE agent_tool_effect AS e "
            "SET status = 'failed', maybe_side_effect = TRUE, "
            "last_error = 'stale started effect reclaimed', updated_at = now() "
            "WHERE e.status = 'started' "
            "AND e.updated_at < now() - (CAST(? AS double precision) "
            "* INTERVAL '1 second') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM agent_approval AS a WHERE a.id = e.approval_id "
            "  AND a.status = 'in_progress' AND a.lease_until > now())",
            (max(1, int(stale_seconds)),),
        )

    def list_stale_started(self, stale_seconds: int, approvals=None) -> list[dict]:
        return self._c.fetch_all(
            "SELECT e.tenant_id, e.approval_id, e.tool_name, e.args_hash, e.status, "
            "e.attempt, e.maybe_side_effect, e.updated_at, "
            "a.status AS approval_status, a.lease_until AS approval_lease_until "
            "FROM agent_tool_effect AS e "
            "LEFT JOIN agent_approval AS a ON a.id = e.approval_id "
            "WHERE e.status = 'started' "
            "AND e.updated_at < now() - (CAST(? AS double precision) "
            "* INTERVAL '1 second') "
            "AND (a.id IS NULL OR a.status <> 'in_progress' OR a.lease_until <= now()) "
            "ORDER BY e.updated_at",
            (max(1, int(stale_seconds)),),
        )

    def get(self, tenant_id, approval_id, tool_name, args):
        return self._c.fetch_one(
            "SELECT tenant_id, approval_id, tool_name, args_hash, status, attempt, "
            "result_ref, maybe_side_effect, last_error, created_at, updated_at "
            "FROM agent_tool_effect "
            "WHERE tenant_id = ? AND approval_id = ? AND tool_name = ? AND args_hash = ?",
            (tenant_id or "default", approval_id, tool_name, args_hash(args)))

    def list_items(self):
        return self._c.fetch_all(
            "SELECT tenant_id, approval_id, tool_name, args_hash, status, attempt, "
            "result_ref, maybe_side_effect, last_error, created_at, updated_at "
            "FROM agent_tool_effect ORDER BY created_at")

    def delete_by_approval(self, approval_id):
        self._c.execute("DELETE FROM agent_tool_effect WHERE approval_id = ?",
                        (approval_id,))


class ToolEffectStore:
    """仓储包装；外部只依赖这层接口。"""

    def __init__(self, repo):
        self._repo = repo

    def begin(self, tenant_id, approval_id, tool_name, args):
        return self._repo.begin(tenant_id, approval_id, tool_name, args)

    def succeed(self, tenant_id, approval_id, tool_name, args, ref):
        return self._repo.succeed(tenant_id, approval_id, tool_name, args, ref)

    def fail(self, tenant_id, approval_id, tool_name, args, *, maybe_side_effect: bool):
        return self._repo.fail(tenant_id, approval_id, tool_name, args,
                               maybe_side_effect=maybe_side_effect)

    def reclaim_stale_started(self, approvals=None, stale_seconds=None) -> int:
        seconds = tool_effect_stale_seconds() if stale_seconds is None else int(stale_seconds)
        count = self._repo.reclaim_stale_started(seconds, approvals)
        if count:
            from observability import metrics as _metrics

            _metrics.inc_tool_effect_reclaimed(count)
        return count

    def list_stale_started(self, approvals=None, stale_seconds=None) -> list[dict]:
        seconds = tool_effect_stale_seconds() if stale_seconds is None else int(stale_seconds)
        return self._repo.list_stale_started(seconds, approvals)

    def get(self, tenant_id, approval_id, tool_name, args):
        return self._repo.get(tenant_id, approval_id, tool_name, args)

    def list_items(self):
        return self._repo.list_items()

    def delete_by_approval(self, approval_id):
        self._repo.delete_by_approval(approval_id)


def build_tool_effect_store(connector=None) -> ToolEffectStore:
    connector = connector or get_agent_connector()
    if connector is not None:
        return ToolEffectStore(PostgresToolEffectRepository(connector))
    return ToolEffectStore(InMemoryToolEffectRepository())
