"""Prometheus metrics backed exclusively by the official client library."""
from __future__ import annotations

import threading

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)


class MetricsUnavailable(RuntimeError):
    """Raised when the required Prometheus client cannot be loaded."""


class PrometheusMetrics:
    available = True

    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry if registry is not None else REGISTRY
        if registry is None:
            for collector in (ProcessCollector, PlatformCollector):
                try:
                    self.registry.register(collector())
                except ValueError:
                    pass
        self._task = self._counter(
            "agent_task_total", "Agent task terminal status", ["status"])
        self._tool = self._counter(
            "agent_tool_calls_total", "Tool calls by tool name and result",
            ["tool", "result"])
        self._event = self._counter(
            "agent_trace_events_total", "Trace events by stage", ["stage"])
        self._reclaimed = self._counter(
            "approval_reclaimed_total", "Expired approval claims reclaimed")
        self._lease_lost = self._counter(
            "approval_lease_lost_total", "Approval leases lost during execution")
        self._heartbeat_failed = self._counter(
            "approval_heartbeat_failed_total", "Approval heartbeat renewal failures")
        self._effect_reclaimed = self._counter(
            "tool_effect_reclaimed_total", "Stale started write-tool effects reclaimed")
        self._assistance_prediction = self._counter(
            "agent_assistance_predictions_total", "AI assistance predictions by decision", ["decision"])
        self._assistance_feedback = self._counter(
            "agent_assistance_feedback_total", "AI assistance feedback by source", ["source"])
        self._assistance_probability = self._histogram(
            "agent_assistance_probability",
            "AI assistance predicted probability",
            [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0],
        )
        self._assistance_duration = self._histogram(
            "agent_assistance_prediction_duration_seconds",
            "AI assistance prediction latency",
            [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
        )
        self._assistance_knowledge_recommendations = self._counter(
            "agent_assistance_knowledge_recommendations_total",
            "Intervention knowledge recommendations returned",
        )
        self._assistance_knowledge_feedback = self._counter(
            "agent_assistance_knowledge_feedback_total",
            "Intervention knowledge feedback by helpfulness",
            ["helpful"],
        )
        self._assistance_drift = self._gauge(
            "agent_assistance_probability_drift",
            "Live mean probability delta from the model baseline",
        )

    def _counter(self, name: str, doc: str, labels: list[str] | None = None):
        try:
            return Counter(name, doc, labels or [], registry=self.registry)
        except ValueError:
            return self.registry._names_to_collectors[name]

    def _histogram(self, name: str, doc: str, buckets: list[float]):
        try:
            return Histogram(name, doc, buckets=buckets, registry=self.registry)
        except ValueError:
            return self.registry._names_to_collectors[name]

    def _gauge(self, name: str, doc: str):
        try:
            return Gauge(name, doc, registry=self.registry)
        except ValueError:
            return self.registry._names_to_collectors[name]

    def value(self, name: str, labels: dict | None = None) -> float:
        return float(self.registry.get_sample_value(name, labels or {}) or 0)

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

    def inc_assistance_prediction(self, decision: str) -> None:
        allowed = {"OFFER_ASSISTANCE", "DO_NOT_INTERRUPT"}
        self._assistance_prediction.labels(decision=decision if decision in allowed else "unknown").inc()

    def inc_assistance_feedback(self, source: str) -> None:
        allowed = {"OPERATOR", "OUTCOME", "REVIEWER"}
        self._assistance_feedback.labels(source=source if source in allowed else "unknown").inc()

    def observe_assistance_prediction(self, probability: float, duration_seconds: float) -> None:
        self._assistance_probability.observe(max(0.0, min(1.0, probability)))
        self._assistance_duration.observe(max(0.0, duration_seconds))

    def inc_assistance_knowledge_recommendation(self, amount: int = 1) -> None:
        self._assistance_knowledge_recommendations.inc(int(amount))

    def inc_assistance_knowledge_feedback(self, helpful: bool) -> None:
        self._assistance_knowledge_feedback.labels(
            helpful="yes" if helpful else "no"
        ).inc()

    def set_assistance_drift(self, delta: float) -> None:
        self._assistance_drift.set(delta)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


_LOCK = threading.Lock()
_BACKEND: PrometheusMetrics | None = None


def _load_backend() -> PrometheusMetrics:
    global _BACKEND
    with _LOCK:
        if _BACKEND is None:
            _BACKEND = PrometheusMetrics()
        return _BACKEND


def configure_metrics(backend=None, registry: CollectorRegistry | None = None):
    """Inject a backend or an isolated official registry for tests."""
    global _BACKEND
    with _LOCK:
        _BACKEND = backend if backend is not None else PrometheusMetrics(registry)
        return _BACKEND


def reset_metrics() -> None:
    global _BACKEND
    with _LOCK:
        _BACKEND = None


def metrics_available() -> bool:
    try:
        return _load_backend().available
    except MetricsUnavailable:
        return False


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


def inc_assistance_prediction(decision: str) -> None:
    try:
        _load_backend().inc_assistance_prediction(decision)
    except Exception:  # noqa: BLE001
        pass


def inc_assistance_feedback(source: str) -> None:
    try:
        _load_backend().inc_assistance_feedback(source)
    except Exception:  # noqa: BLE001
        pass


def observe_assistance_prediction(probability: float, duration_seconds: float) -> None:
    try:
        _load_backend().observe_assistance_prediction(probability, duration_seconds)
    except Exception:  # noqa: BLE001
        pass


def inc_assistance_knowledge_recommendation(amount: int = 1) -> None:
    try:
        _load_backend().inc_assistance_knowledge_recommendation(amount)
    except Exception:  # noqa: BLE001
        pass


def inc_assistance_knowledge_feedback(helpful: bool) -> None:
    try:
        _load_backend().inc_assistance_knowledge_feedback(helpful)
    except Exception:  # noqa: BLE001
        pass


def set_assistance_drift(delta: float) -> None:
    try:
        _load_backend().set_assistance_drift(delta)
    except Exception:  # noqa: BLE001
        pass
