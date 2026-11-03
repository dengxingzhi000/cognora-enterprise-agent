"""情景记忆：AgentTask 状态迁移落盘 + 跨任务相似检索。

存储复用 ``MemoryStore`` 的 ``memory_episodic`` 抽象：
- 有 connector（PG 或 InMemoryConnector）时写入 ``memory_episodic`` 表；
- 没有 connector 时退回进程内列表，保证离线测试与本地开发可用。

检索是确定性的词面相似度（2-gram + 任务类型 + 工具名加权），
不依赖外部向量库。
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone

from infrastructure.pg.connector import OperationalError
from .store import MemoryStore

LAYER = "episodic"

_TASK_TYPES: dict[str, tuple[str, ...]] = {
    "ops": ("故障", "5xx", "错误", "超时", "订单", "服务", "error", "timeout", "down"),
    "procurement": ("采购", "供应商", "补货", "PO-"),
    "expense": ("报销", "发票", "差旅"),
    "contract": ("合同", "条款", "赔偿", "预付款"),
    "sales": ("销售", "下降", "gmv", "营收"),
}

_KEYWORDS = ("PO-001", "采购", "报销", "5xx", "5000", "订单", "超时", "供应商", "合同", "销售")


def infer_task_type(text: str) -> str:
    low = (text or "").lower()
    for name, keys in _TASK_TYPES.items():
        if any(k.lower() in low for k in keys):
            return name
    return "general"


def _ngrams(text: str) -> set[str]:
    clean = (text or "").lower()
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}


def similarity(query: str, text: str) -> float:
    """0 起步的词面相似度；同租户内排序足够，不做 embedding。"""
    if not query or not text:
        return 0.0
    q = query.lower()
    t = text.lower()
    score = 0.0
    if q in t:
        score += 10.0
    score += len(_ngrams(query) & _ngrams(text))
    for token in _KEYWORDS:
        if token.lower() in q and token.lower() in t:
            score += 5.0
    return score


class EpisodicMemoryStore:
    """``memory_episodic`` 的读写门面（connector 优先，进程内兜底）。"""

    def __init__(self, store: MemoryStore | None = None, permission: str = "tenant",
                 limit: int = 200):
        self._store = store
        self.permission = permission
        self.limit = limit
        self._memory: list[dict] = []

    # --- 写入 ---
    def record(self, *, tenant_id: str, task_id: str, task_type: str,
               from_status: str | None, to_status: str, reason: str = "",
               tools: list[str] | None = None, task_text: str = "") -> dict:
        if not tenant_id:
            raise ValueError("tenant_id required")
        body = {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "task_type": task_type or "general",
            "from": from_status,
            "to": to_status,
            "reason": reason,
            "tools": list(tools or []),
            "task_text": task_text,
            "summary": f"[{to_status}] {task_text[:120]}",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if self._has_connector():
            try:
                self._store.put(LAYER, tenant_id, body, permission=self.permission)
            except Exception as e:  # noqa: BLE001 - 记忆写入不能阻断主流程
                warnings.warn(f"episodic record failed: {e}")
        else:
            self._memory.append(body)
            if len(self._memory) > self.limit * 5:
                self._memory = self._memory[-self.limit * 5:]
        return body

    def record_task_transition(self, task, reason: str = "") -> dict | None:
        """把 AgentTask 的一次状态迁移写入 episodics。"""
        state = getattr(task, "state", None)
        context = getattr(state, "context", {}) or {}
        tenant_id = context.get("tenant_id") or getattr(task, "tenant_id", None) or "default"
        history = getattr(task, "history", []) or []
        from_status = history[-1].get("from") if history else None
        tools = [c.get("tool") for c in getattr(state, "tool_calls", []) if c.get("tool")]
        task_text = getattr(state, "task", "") or ""
        try:
            return self.record(
                tenant_id=tenant_id,
                task_id=getattr(task, "task_id", "") or "",
                task_type=infer_task_type(task_text),
                from_status=from_status,
                to_status=getattr(task, "status", "unknown"),
                reason=reason,
                tools=tools,
                task_text=task_text,
            )
        except Exception as e:  # noqa: BLE001
            warnings.warn(f"episodic record_task_transition failed: {e}")
            return None

    # --- 读取 ---
    def list(self, tenant_id: str, limit: int | None = None) -> list[dict]:
        limit = limit or self.limit
        if self._has_connector():
            try:
                rows = self._store.query(LAYER, tenant_id, permission=self.permission,
                                         limit=limit)
            except OperationalError as e:
                raise e
            out: list[dict] = []
            for row in rows:
                body = row.get("body")
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except (TypeError, ValueError):
                        continue
                if isinstance(body, dict) and (body.get("tenant_id") in (None, tenant_id)):
                    body = dict(body)
                    body["tenant_id"] = tenant_id
                    out.append(body)
            return out[:limit]
        return [dict(b) for b in self._memory if b.get("tenant_id") == tenant_id][:limit]

    def recall(self, tenant_id: str, query: str, task_type: str | None = None,
               tools: list[str] | None = None, top_k: int = 3) -> list[dict]:
        if not query or not query.strip():
            return []
        wanted_type = task_type or infer_task_type(query)
        wanted_tools = {t for t in (tools or []) if t}
        scored: list[tuple[float, dict]] = []
        for body in self.list(tenant_id, limit=self.limit):
            text = " ".join(str(body.get(k, "")) for k in ("task_text", "reason", "summary"))
            text += " " + " ".join(body.get("tools") or [])
            score = similarity(query, text)
            if body.get("task_type") == wanted_type:
                score += 2.0
            score += 1.5 * len(wanted_tools & set(body.get("tools") or []))
            if score > 0:
                scored.append((score, body))
        scored.sort(key=lambda pair: (-pair[0], pair[1].get("ts", "")))
        return [body for _, body in scored[:top_k]]

    def _has_connector(self) -> bool:
        return self._store is not None and getattr(self._store, "_c", None) is not None


_DEFAULT: EpisodicMemoryStore | None = None


def get_episodic_store() -> EpisodicMemoryStore:
    global _DEFAULT
    if _DEFAULT is None:
        # AGENT_PG_DSN 是唯一开关；没有可用 agent 库时退回进程内列表。
        from infrastructure.pg.connector import get_agent_connector
        _DEFAULT = EpisodicMemoryStore(MemoryStore(get_agent_connector()))
    return _DEFAULT


def reset_episodic_store() -> None:
    """测试用：清空默认 store（connector 与进程内列表）。"""
    global _DEFAULT
    _DEFAULT = None


def record_task_transition(task, reason: str = "") -> dict | None:
    """Runtime 埋点入口；任何异常都不能阻断状态机。"""
    try:
        return get_episodic_store().record_task_transition(task, reason)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"episodic default store unavailable: {e}")
        return None
