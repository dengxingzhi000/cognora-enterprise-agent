"""Prometheus 指标（可选依赖）。未安装 prometheus-client 时提供 503 降级。"""
from __future__ import annotations

import threading


class MetricsUnavailable(RuntimeError):
    pass


class NoopMetrics:
    available = False

    def inc_task_status(self, status: str) -> None:
        return None

    def record_tool_call(self, tool: str, ok: bool) -> None:
        return None

    def record_trace_event(self, stage: str) -> None:
        return None

    def render(self) -> tuple[bytes, str]:
        raise MetricsUnavailable(
            "prometheus-client is not installed; run `pip install -e .[observability]`"
        )


class PrometheusMetrics:
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

    def inc_task_status(self, status: str) -> None:
        self._task.labels(status=status or "unknown").inc()

    def record_tool_call(self, tool: str, ok: bool) -> None:
        self._tool.labels(tool=tool or "unknown", result="ok" if ok else "error").inc()

    def record_trace_event(self, stage: str) -> None:
        self._event.labels(stage=stage or "unknown").inc()

    def render(self) -> tuple[bytes, str]:
        return self._module.generate_latest(), self._module.CONTENT_TYPE_LATEST


_LOCK = threading.Lock()
_BACKEND = None


def _load_backend():
    global _BACKEND
    with _LOCK:
        if _BACKEND is not None:
            return _BACKEND
        try:
            from types import SimpleNamespace
            from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY,  # noqa: F401
                                           Counter, generate_latest)
            module = SimpleNamespace(
                Counter=Counter,
                generate_latest=generate_latest,
                CONTENT_TYPE_LATEST=CONTENT_TYPE_LATEST,
                REGISTRY=REGISTRY,
            )
            _BACKEND = PrometheusMetrics(module)
        except Exception:
            _BACKEND = NoopMetrics()
        return _BACKEND


def configure_metrics(backend=None):
    """显式注入（测试/自定义后端）；``None`` 触发自动探测。"""
    global _BACKEND
    with _LOCK:
        _BACKEND = backend if backend is not None else NoopMetrics()
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
