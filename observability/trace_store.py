"""Postgres-backed trace summaries for cross-replica explanation."""
from __future__ import annotations

import json

from observability.redaction import redact


class PostgresTraceStore:
    def __init__(self, connector):
        self._c = connector
        self._c.execute(
            "CREATE TABLE IF NOT EXISTS agent_trace ("
            "trace_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload JSONB NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        self._c.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_trace_tenant ON agent_trace(tenant_id, created_at DESC)"
        )

    def save(self, trace: dict) -> None:
        start = trace.get("start", 0.0)
        events = [
            {
                "stage": event.get("stage", "event"),
                "data": redact(event.get("data") or {}),
                "offset_ms": round((event.get("ts", start) - start) * 1000, 3),
            }
            for event in trace.get("events", [])[-50:]
        ]
        payload = {
            "id": trace.get("id"),
            "task": redact(trace.get("task", ""), max_chars=500),
            "tenant_id": trace.get("tenant_id"),
            "status": trace.get("status"),
            "latency_ms": trace.get("latency_ms", 0.0),
            "event_count": len(trace.get("events", [])),
            "events": events,
        }
        self._c.execute(
            "INSERT INTO agent_trace (trace_id, tenant_id, payload) VALUES (?, ?, CAST(? AS JSONB)) "
            "ON CONFLICT (trace_id) DO UPDATE SET payload = EXCLUDED.payload",
            (trace["id"], trace.get("tenant_id") or "default", json.dumps(payload, ensure_ascii=False)),
        )

    def get(self, trace_id: str, tenant_id: str) -> dict | None:
        row = self._c.fetch_one(
            "SELECT payload FROM agent_trace WHERE trace_id = ? AND tenant_id = ?",
            (trace_id, tenant_id),
        )
        if not row:
            return None
        payload = row["payload"]
        payload["start"] = 0.0
        payload["events"] = [
            {**event, "ts": event.get("offset_ms", 0.0) / 1000}
            for event in payload.get("events", [])
        ]
        return payload


def build_trace_store(connector):
    return PostgresTraceStore(connector) if connector is not None else None
