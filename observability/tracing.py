"""内存Tracer：User→Planner→Tool→Reflection→Final全链路。OTel 可选适配。"""
import copy
import threading
import time
import uuid

from observability.redaction import redact


class Tracer:
    _shared: "Tracer | None" = None
    _traces: dict[str, dict] = {}
    _lock = threading.RLock()
    _max_traces = 1000
    store = None
    # OTel 适配器（可选）；为 None 时只走内存。由 apps/api/main.py 启动时注入。
    otel = None

    def __init__(self):
        self._traces = type(self)._traces
        Tracer._shared = self

    def _adapter(self):
        return getattr(type(self), "otel", None)

    def start_trace(self, task: str, tenant_id: str | None = None) -> str:
        tid = f"tr-{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._traces[tid] = {"id": tid, "task": task, "tenant_id": tenant_id,
                                 "events": [], "status": "running", "start": time.perf_counter()}
            while len(self._traces) > self._max_traces:
                self._traces.pop(next(iter(self._traces)))
        try:
            adapter = self._adapter()
            if adapter is not None:
                adapter.start_trace(tid, redact(task, max_chars=500))
        except Exception:  # noqa: BLE001 - OTel 失败不能阻断业务
            pass
        return tid

    def log_event(self, trace_id: str, stage: str, data: dict):
        with self._lock:
            self._traces[trace_id]["events"].append(
                {"stage": stage, "data": data or {}, "ts": time.perf_counter()})
        try:
            adapter = self._adapter()
            if adapter is not None:
                adapter.log_event(trace_id, stage, redact(data or {}))
        except Exception:  # noqa: BLE001
            pass
        try:
            from observability.metrics import record_trace_event
            record_trace_event(stage)
        except Exception:  # noqa: BLE001
            pass

    def end_trace(self, trace_id: str, status: str = "done"):
        with self._lock:
            tr = self._traces[trace_id]
            tr["status"] = status
            tr["end"] = time.perf_counter()
            tr["latency_ms"] = (tr["end"] - tr["start"]) * 1000
        try:
            adapter = self._adapter()
            if adapter is not None:
                adapter.end_trace(trace_id, status)
        except Exception:  # noqa: BLE001
            pass
        store = type(self).store
        if store is not None and tr.get("tenant_id"):
            try:
                store.save(copy.deepcopy(tr))
            except Exception:  # noqa: BLE001 - trace persistence cannot break the request
                pass

    def get_trace(self, trace_id: str) -> dict:
        with self._lock:
            tr = copy.deepcopy(self._traces[trace_id])
        tr.setdefault("latency_ms", 0.0)
        return tr


def configure_trace_store(store) -> None:
    Tracer.store = store
