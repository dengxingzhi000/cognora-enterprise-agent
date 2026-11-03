"""OpenTelemetry 适配层（可选依赖）。

内存 ``Tracer`` 始终保留；只有同时满足以下条件时才创建真实 OTLP exporter：
1. ``opentelemetry-api`` / ``opentelemetry-sdk`` 可导入；
2. ``OTEL_EXPORTER_OTLP_ENDPOINT`` 已配置；
3. ``opentelemetry-exporter-otlp`` 可导入。

任何一项缺失都记录日志并继续走内存 Tracer，不会阻断启动。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def otel_available() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401
        return True
    except Exception:
        return False


class OtelTraceAdapter:
    """把 Tracer 的 trace/event 语义映射到 OTel span。"""

    def __init__(self, trace_api=None, tracer_provider=None):
        if trace_api is None:
            import opentelemetry.trace as trace_api  # type: ignore
        self._trace = trace_api
        source = tracer_provider or trace_api
        self._tracer = source.get_tracer("enterprise-agent")
        self._provider = tracer_provider
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

    def shutdown(self) -> None:
        if self._provider is not None and hasattr(self._provider, "shutdown"):
            self._provider.shutdown()


def _protocol_for(endpoint: str) -> str:
    """OTLP gRPC 常用 4317，HTTP 常用 4318；显式协议优先。"""
    explicit = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").lower()
    if explicit in ("grpc", "http/protobuf", "http"):
        return "grpc" if explicit == "grpc" else "http"
    return "grpc" if endpoint.rstrip("/").endswith(":4317") else "http"


def _load_otlp_exporter(protocol: str = "http"):
    try:
        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter
    except Exception:
        return None


def _load_tracer_provider():
    try:
        from opentelemetry.sdk.trace import TracerProvider
        return TracerProvider
    except Exception:
        return None


def _load_span_processor():
    try:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        return SimpleSpanProcessor
    except Exception:
        return None


def build_otel_adapter(endpoint: str | None = None, exporter_factory=None,
                       trace_api=None, provider_factory=None,
                       processor_factory=None):
    """按依赖 + endpoint 决定 OTLP 或内存路径；失败只记日志。"""
    endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if trace_api is None and not otel_available():
        log.info("opentelemetry API not installed; using in-memory Tracer")
        return None
    if not endpoint:
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; using in-memory Tracer")
        return None

    protocol = _protocol_for(endpoint)
    exporter_factory = exporter_factory or _load_otlp_exporter(protocol)
    if exporter_factory is None:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT set but "
                    "opentelemetry-exporter-otlp is not installed; "
                    "using in-memory Tracer")
        return None
    provider_factory = provider_factory or _load_tracer_provider()
    processor_factory = processor_factory or _load_span_processor()
    if provider_factory is None or processor_factory is None:
        log.warning("opentelemetry SDK is unavailable; using in-memory Tracer")
        return None

    try:
        if trace_api is None:
            import opentelemetry.trace as trace_api  # type: ignore
        provider = provider_factory()
        exporter_endpoint = ("http://" + endpoint[len("grpc://"):]
                             if endpoint.startswith("grpc://") else endpoint)
        exporter = exporter_factory(exporter_endpoint)
        provider.add_span_processor(processor_factory(exporter))
        # 让 adapter 使用同一个 provider；全局 provider 仅作为 best effort。
        try:
            trace_api.set_tracer_provider(provider)
        except Exception:  # noqa: BLE001 - 已设置过时不阻断
            pass
        log.info("OTLP span exporter enabled (protocol=%s)", protocol)
        return OtelTraceAdapter(trace_api=trace_api, tracer_provider=provider)
    except Exception as exc:  # noqa: BLE001 - exporter 失败不能阻断业务
        log.warning("OTLP exporter init failed (%s); using in-memory Tracer", exc)
        return None
