"""OpenTelemetry 适配层（可选依赖）。

内存 ``Tracer`` 始终保留；OTel 可用时额外把同一批 trace 事件写入 span。
无依赖时 ``build_otel_adapter`` 返回 None，调用方继续走内存实现。
"""
from __future__ import annotations


def otel_available() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401
        return True
    except Exception:
        return False


class OtelTraceAdapter:
    """把 Tracer 的 trace/event 语义映射到 OTel span。"""

    def __init__(self, trace_api=None):
        if trace_api is None:
            import opentelemetry.trace as trace_api  # type: ignore
        self._trace = trace_api
        self._tracer = trace_api.get_tracer("enterprise-agent")
        self._spans: dict[str, object] = {}

    def start_trace(self, trace_id: str, task: str):
        span = self._tracer.start_as_current_span("agent.task")
        span.set_attribute("agent.task", task or "")
        span.set_attribute("agent.trace_id", trace_id)
        self._spans[trace_id] = span
        return span

    def log_event(self, trace_id: str, stage: str, data: dict) -> None:
        span = self._spans.get(trace_id)
        if span is None:
            return
        attrs = {f"agent.{k}": str(v) for k, v in (data or {}).items()}
        span.add_event(stage or "event", attrs)

    def end_trace(self, trace_id: str, status: str = "done") -> None:
        span = self._spans.pop(trace_id, None)
        if span is None:
            return
        status_code = self._trace.StatusCode.ERROR if status in ("failed", "error") \
            else self._trace.StatusCode.OK
        span.set_status(self._trace.Status(status_code, status))
        span.set_attribute("agent.status", status)
        span.end()


def build_otel_adapter():
    if not otel_available():
        return None
    try:
        return OtelTraceAdapter()
    except Exception:  # noqa: BLE001 - 缺失 SDK/exporter 时保持内存路径
        return None
