"""Batch 5: Prometheus /metrics 与 OTel 适配；无依赖时优雅降级。"""
import pytest


def test_redaction_covers_camel_case_secret_fields():
    from observability.redaction import redact

    payload = {
        "clientSecret": "client-value",
        "apiKey": "key-value",
        "accessToken": "token-value",
        "nested": {"dbPassword": "password-value", "safe": "visible"},
    }
    assert redact(payload) == {
        "clientSecret": "[REDACTED]",
        "apiKey": "[REDACTED]",
        "accessToken": "[REDACTED]",
        "nested": {"dbPassword": "[REDACTED]", "safe": "visible"},
    }


def test_metrics_uses_official_prometheus_registry():
    from prometheus_client import CollectorRegistry

    from observability import metrics

    backend = metrics.configure_metrics(registry=CollectorRegistry())
    try:
        assert metrics.metrics_available() is True
        metrics.inc_task_status("done")
        metrics.inc_approval_reclaimed()
        metrics.inc_approval_lease_lost()
        metrics.inc_approval_heartbeat_failed()
        payload, content_type = metrics.render_metrics()
        text = payload.decode()
        assert "agent_task_total{status=\"done\"} 1" in text
        assert "approval_reclaimed_total 1" in text
        assert "approval_lease_lost_total 1" in text
        assert "approval_heartbeat_failed_total 1" in text
        assert "text/plain" in content_type
        assert backend.value("approval_reclaimed_total") == 1

        from fastapi.testclient import TestClient

        from apps.api.main import app
        client = TestClient(app)
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "approval_reclaimed_total 1" in r.text
    finally:
        metrics.reset_metrics()


def test_metrics_renders_when_prometheus_client_available():
    pytest.importorskip("prometheus_client")
    from observability import metrics
    metrics.reset_metrics()
    assert metrics.metrics_available()
    metrics.inc_task_status("done")
    metrics.record_tool_call("metrics.get", True)
    metrics.record_trace_event("task_status_changed")
    payload, content_type = metrics.render_metrics()
    text = payload.decode()
    assert "agent_task_total" in text
    assert "agent_tool_calls_total" in text
    assert "agent_trace_events_total" in text
    assert "text/plain" in content_type

    from fastapi.testclient import TestClient

    from apps.api.main import app
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "agent_task_total" in r.text


def test_metrics_recording_uses_isolated_official_registry():
    from prometheus_client import CollectorRegistry

    from observability import metrics

    backend = metrics.configure_metrics(registry=CollectorRegistry())
    try:
        metrics.inc_task_status("failed")
        metrics.record_tool_call("unknown", False)
        metrics.record_trace_event("x")
        assert backend.value("agent_task_total", {"status": "failed"}) == 1
        assert backend.value("agent_tool_calls_total",
                             {"tool": "unknown", "result": "error"}) == 1
        assert backend.value("agent_trace_events_total", {"stage": "x"}) == 1
    finally:
        metrics.reset_metrics()


class _FakeSpan:
    def __init__(self):
        self.attributes = {}
        self.events = []
        self.status = None
        self.ended = False
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        self.ended = True
        return False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes or {}))

    def set_status(self, status):
        self.status = status

    def end(self):
        self.ended = True


class _FakeTracerImpl:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name):
        span = _FakeSpan()
        span.name = name
        self.spans.append(span)
        return span


class _FakeStatus:
    def __init__(self, code, description=""):
        self.code = code
        self.description = description


class _FakeTraceApi:
    Status = _FakeStatus

    class StatusCode:
        OK = "OK"
        ERROR = "ERROR"

    def __init__(self):
        self._tracer = _FakeTracerImpl()

    def get_tracer(self, name):
        return self._tracer

    def set_tracer_provider(self, provider):
        self.provider = provider


def test_otel_adapter_maps_events_and_status():
    from observability.otel import OtelTraceAdapter
    from observability.tracing import Tracer

    api = _FakeTraceApi()
    adapter = OtelTraceAdapter(trace_api=api)
    Tracer.otel = adapter
    old = Tracer()
    try:
        tid = old.start_trace("订单 5xx")
        old.log_event(tid, "tool_call", {"tool": "metrics.get"})
        old.end_trace(tid, "failed")
        span = api._tracer.spans[0]
        assert span.attributes["agent.task"] == "订单 5xx"
        assert span.attributes["agent.status"] == "failed"
        assert span.events[0][0] == "tool_call"
        assert span.events[0][1]["agent.tool"] == "metrics.get"
        assert span.status.code == "ERROR"
        assert span.ended is True
        assert span.entered is True
        assert span.exited is True
    finally:
        Tracer.otel = None


def test_build_otel_adapter_exporter_paths(monkeypatch, caplog):
    import logging

    import observability.otel as otel

    caplog.set_level(logging.INFO)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.setattr(otel, "otel_available", lambda: False)
    assert otel._protocol_for("http://127.0.0.1:4317") == "grpc"
    assert otel._protocol_for("http://127.0.0.1:4318") == "http"
    assert otel.build_otel_adapter() is None
    assert "in-memory Tracer" in caplog.text

    # API available but no endpoint => keep memory path.
    monkeypatch.setattr(otel, "otel_available", lambda: True)
    assert otel.build_otel_adapter() is None
    assert "OTEL_EXPORTER_OTLP_ENDPOINT not set" in caplog.text

    # Endpoint set but exporter package missing => keep memory path.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    monkeypatch.setattr(otel, "_load_otlp_exporter", lambda protocol="http": None)
    assert otel.build_otel_adapter() is None
    assert "exporter-otlp is not installed" in caplog.text

    # Endpoint + fake exporter/provider => adapter is created and exporter used.
    calls = {}

    class FakeProvider(_FakeTraceApi):
        def __init__(self):
            super().__init__()
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    def fake_exporter(endpoint):
        calls["endpoint"] = endpoint
        return object()

    def fake_provider():
        provider = FakeProvider()
        calls["provider"] = provider
        return provider

    adapter = otel.build_otel_adapter(
        endpoint="http://127.0.0.1:4318/v1/traces",
        exporter_factory=fake_exporter,
        trace_api=_FakeTraceApi(),
        provider_factory=fake_provider,
        processor_factory=FakeProcessor,
    )
    assert adapter is not None
    assert calls["endpoint"] == "http://127.0.0.1:4318/v1/traces"
    assert calls["provider"].processors
    assert isinstance(calls["provider"].processors[0], FakeProcessor)
    assert calls["provider"].processors[0].exporter is not None

    # grpc:// is normalised to http:// before it reaches the OTLP exporter.
    normalized = otel.build_otel_adapter(
        endpoint="grpc://127.0.0.1:4317",
        exporter_factory=fake_exporter,
        trace_api=_FakeTraceApi(),
        provider_factory=fake_provider,
        processor_factory=FakeProcessor,
    )
    assert normalized is not None
    assert calls["endpoint"] == "http://127.0.0.1:4317"


def test_protocol_override_and_invalid_fallback(monkeypatch):
    """Explicit protocol wins; unknown values fall back to the port heuristic."""
    import observability.otel as otel

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    assert otel._protocol_for("http://127.0.0.1:4318") == "grpc"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    assert otel._protocol_for("http://127.0.0.1:4317") == "http"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http")
    assert otel._protocol_for("http://127.0.0.1:4317") == "http"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "invalid")
    assert otel._protocol_for("http://127.0.0.1:4317") == "grpc"
    assert otel._protocol_for("http://127.0.0.1:4318") == "http"


def test_trace_ids_are_unique_across_tracer_instances():
    from observability.tracing import Tracer

    first = Tracer().start_trace("one")
    second = Tracer().start_trace("two")
    assert first != second


def test_default_metrics_registry_includes_standard_collectors():
    from observability import metrics

    metrics.reset_metrics()
    try:
        payload, _ = metrics.render_metrics()
        assert b"python_gc_objects_collected_total" in payload
        assert b"python_info" in payload
    finally:
        metrics.reset_metrics()


def test_tracer_keeps_memory_trace_without_otel():
    from observability.tracing import Tracer
    Tracer.otel = None
    t = Tracer()
    tid = t.start_trace("x")
    t.log_event(tid, "stage", {"a": 1})
    t.end_trace(tid, "done")
    tr = t.get_trace(tid)
    assert tr["status"] == "done"
    assert tr["events"][0]["stage"] == "stage"
