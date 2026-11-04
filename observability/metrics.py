"""Prometheus 指标（可选依赖）。

安装 ``prometheus-client`` 时用官方 registry；未安装时用进程内计数器，
仍然以 Prometheus 文本格式暴露 ``/metrics``（只是没有 exporter 聚合）。
"""
from __future__ import annotations

import threading


class MetricsUnavailable(RuntimeError):
    """保留给自定义后端；内存后端不会抛出。"""


MEMORY_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MemoryMetrics:
    """无 exporter 时的内存计数器；可在 /metrics 上读到真实数值。"""

    available = False

    def __init__(self):
        self._lock = threading.Lock()
        self._series: dict[tuple, dict] = {}

    def _inc(self, name: str, labels: dict | None = None, amount: int = 1) -> None:
        labels = labels or {}
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            entry = self._series.setdefault(
                key, {"name": name, "labels": labels, "value": 0})
            entry["value"] += int(amount)

    def value(self, name: str, labels: dict | None = None) -> int:
        """测试/诊断用：读取某个序列的当前值（不存在为 0）。"""
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            return int(self._series.get(key, {}).get("value", 0))

    def inc_task_status(self, status: str) -> None:
        self._inc("agent_task_total", {"status": status or "unknown"})

    def record_tool_call(self, tool: str, ok: bool) -> None:
        self._inc("agent_tool_calls_total",
                  {"tool": tool or "unknown", "result": "ok" if ok else "error"})

    def record_trace_event(self, stage: str) -> None:
        self._inc("agent_trace_events_total", {"stage": stage or "unknown"})

    def inc_approval_reclaimed(self, amount: int = 1) -> None:
        self._inc("approval_reclaimed_total", amount=amount)

    def inc_approval_lease_lost(self) -> None:
        self._inc("approval_lease_lost_total")

    def inc_approval_heartbeat_failed(self) -> None:
        self._inc("approval_heartbeat_failed_total")

    def inc_tool_effect_reclaimed(self, amount: int = 1) -> None:
        self._inc("tool_effect_reclaimed_total", amount=amount)

    def render(self) -> tuple[bytes, str]:
        lines = []
        with self._lock:
            for entry in self._series.values():
                labels = entry["labels"]
                label_text = ""
                if labels:
                    pairs = ",".join(f'{k}="{_escape(v)}"'
                                     for k, v in sorted(labels.items()))
                    label_text = "{" + pairs + "}"
                lines.append(f'{entry["name"]}{label_text} {entry["value"]}')
        body = "\n".join(sorted(lines)) + "\n"
        return body.encode(), MEMORY_CONTENT_TYPE


class _PrometheusMetrics:
    available = True

    def __init__(self, module):
        self._module = module
        try:
            self._task = module.Counter(
                "agent_task_total", "Agent task terminal status", ["status"])
            self._tool = module.Counter(
                "agent_tool_calls_total", "Tool calls by tool name and result", ["tool", "result"])
            self._event = module.Counter(
                "agent_trace_events_total", "Trace events by stage", ["stage"])
        except Exception:  # noqa: BLE001 - duplicate registration on reload
            registry = module.REGISTRY
            self._task = registry._names_to_collectors["agent_task_total"]
            self._tool = registry._names_to_collectors["agent_tool_calls_total"]
            self._event = registry._names_to_collectors["agent_trace_events_total"]
        self._reclaimed = self._counter(module, "approval_reclaimed_total",
                                        "Expired approval claims reclaimed")
        self._lease_lost = self._counter(module, "approval_lease_lost_total",
                                         "Approval leases lost during execution")
        self._heartbeat_failed = self._counter(
            module, "approval_heartbeat_failed_total",
            "Approval heartbeat renewal failures")
        self._effect_reclaimed = self._counter(
            module, "tool_effect_reclaimed_total",
            "Stale started write-tool effects reclaimed")

    @staticmethod
    def _counter(module, name: str, doc: str):
        try:
            return module.Counter(name, doc)
        except Exception:  # noqa: BLE001 - duplicate registration on reload
            return module.REGISTRY._names_to_collectors[name]

    def inc_task_status(self, status: str) -> None:
        self._task.labels(status=status or "unknown").inc()

    def record_tool_call(self, tool: str, ok: bool) -> None:
        self._tool.labels(tool=tool or "unknown", result="ok" if ok else "error").inc()

    def record_trace_event(self, stage: str) -> None:
        self._event.labels(stage=stage or "unknown").inc()

    def inc_approval_reclaimed(self, amount: int = 1) -> None:
        self._reclaimed.inc(int(amount))

    def inc_approval_lease_lost(self) -> None:
        self._lease_lost.inc()

    def inc_approval_heartbeat_failed(self) -> None:
        self._heartbeat_failed.inc()

    def inc_tool_effect_reclaimed(self, amount: int = 1) -> None:
        self._effect_reclaimed.inc(int(amount))

    def render(self) -> tuple[bytes, str]:
        return self._module.generate_latest(), self._module.CONTENT_TYPE_LATEST


_LOCK = threading.Lock()
_BACKEND = None


def _build_prometheus():
    from types import SimpleNamespace
    from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY,  # noqa: F401
                                   Counter, generate_latest)
    module = SimpleNamespace(
        Counter=Counter,
        generate_latest=generate_latest,
        CONTENT_TYPE_LATEST=CONTENT_TYPE_LATEST,
        REGISTRY=REGISTRY,
    )
    return _PrometheusMetrics(module)


def _load_backend():
    global _BACKEND
    with _LOCK:
        if _BACKEND is not None:
            return _BACKEND
        try:
            _BACKEND = _build_prometheus()
        except Exception:  # noqa: BLE001 - 无依赖时回落内存计数
            _BACKEND = MemoryMetrics()
        return _BACKEND


def configure_metrics(backend=None, memory: bool = False):
    """显式注入（测试/自定义后端）；``None`` 触发自动探测，``memory=True`` 强制内存后端。"""
    global _BACKEND
    with _LOCK:
        if memory:
            _BACKEND = MemoryMetrics()
        else:
            _BACKEND = backend if backend is not None else MemoryMetrics()
    return _BACKEND


def reset_metrics() -> None:
    global _BACKEND
    with _LOCK:
        _BACKEND = None


def metrics_available() -> bool:
    return bool(_load_backend().available)


def render_metrics() -> tuple[bytes, str]:
    return _load_backend().render()


def inc_task_status(status: str) -> None:
    try:
        _load_backend().inc_task_status(status)
    except Exception:  # noqa: BLE001 - 指标失败不影响业务
        pass


def record_tool_call(tool: str, ok: bool = True) -> None:
    try:
        _load_backend().record_tool_call(tool, ok)
    except Exception:  # noqa: BLE001
        pass


def record_trace_event(stage: str) -> None:
    try:
        _load_backend().record_trace_event(stage)
    except Exception:  # noqa: BLE001
        pass


def inc_approval_reclaimed(amount: int = 1) -> None:
    try:
        _load_backend().inc_approval_reclaimed(amount)
    except Exception:  # noqa: BLE001
        pass


def inc_approval_lease_lost() -> None:
    try:
        _load_backend().inc_approval_lease_lost()
    except Exception:  # noqa: BLE001
        pass


def inc_approval_heartbeat_failed() -> None:
    try:
        _load_backend().inc_approval_heartbeat_failed()
    except Exception:  # noqa: BLE001
        pass


def inc_tool_effect_reclaimed(amount: int = 1) -> None:
    try:
        _load_backend().inc_tool_effect_reclaimed(amount)
    except Exception:  # noqa: BLE001
        pass
