"""内存Tracer：User→Planner→Tool→Reflection→Final全链路。OTel 可选适配。"""
import time
from itertools import count


class Tracer:
    _shared: "Tracer | None" = None
    # OTel 适配器（可选）；为 None 时只走内存。由 apps/api/main.py 启动时注入。
    otel = None

    def __init__(self):
        self._traces: dict[str, dict] = {}
        self._seq = count(1)
        Tracer._shared = self

    def _adapter(self):
        return getattr(type(self), "otel", None)

    def start_trace(self, task: str) -> str:
        tid = f"tr-{next(self._seq)}"
        self._traces[tid] = {"id": tid, "task": task, "events": [],
                             "status": "running", "start": time.perf_counter()}
        try:
            adapter = self._adapter()
            if adapter is not None:
                adapter.start_trace(tid, task)
        except Exception:  # noqa: BLE001 - OTel 失败不能阻断业务
            pass
        return tid

    def log_event(self, trace_id: str, stage: str, data: dict):
        self._traces[trace_id]["events"].append(
            {"stage": stage, "data": data or {}, "ts": time.perf_counter()})
        try:
            adapter = self._adapter()
            if adapter is not None:
                adapter.log_event(trace_id, stage, data)
        except Exception:  # noqa: BLE001
            pass
        try:
            from observability.metrics import record_trace_event
            record_trace_event(stage)
        except Exception:  # noqa: BLE001
            pass

    def end_trace(self, trace_id: str, status: str = "done"):
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

    def get_trace(self, trace_id: str) -> dict:
        tr = self._traces[trace_id]
        tr.setdefault("latency_ms", 0.0)
        return tr
