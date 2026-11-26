"""Tenant-scoped fixed-window rate and token quota guards."""
from __future__ import annotations

import threading
import time


class QuotaExceeded(RuntimeError):
    pass


class InMemoryQuotaRepository:
    def __init__(self):
        self._windows: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def consume(self, tenant_id: str, window: str, now: float, window_seconds: int,
                limit: int, amount: int = 1) -> bool:
        key = (tenant_id, window)
        with self._lock:
            events = self._windows.setdefault(key, [])
            events[:] = [event for event in events if event > now - window_seconds]
            if len(events) + amount > limit:
                return False
            events.extend([now] * amount)
            return True


class PostgresQuotaRepository:
    def __init__(self, connector):
        self._c = connector
        self._c.execute(
            "CREATE TABLE IF NOT EXISTS agent_quota_counter ("
            "tenant_id TEXT NOT NULL, window_name TEXT NOT NULL, "
            "window_start TIMESTAMPTZ NOT NULL, count BIGINT NOT NULL, "
            "PRIMARY KEY (tenant_id, window_name))"
        )

    def consume(self, tenant_id: str, window: str, now: float, window_seconds: int,
                limit: int, amount: int = 1) -> bool:
        row = self._c.fetch_one(
            "INSERT INTO agent_quota_counter "
            "(tenant_id, window_name, window_start, count) VALUES (?, ?, to_timestamp(?), ?) "
            "ON CONFLICT (tenant_id, window_name) DO UPDATE SET "
            "count = CASE WHEN agent_quota_counter.window_start < to_timestamp(?) "
            "THEN ? ELSE agent_quota_counter.count + ? END, "
            "window_start = CASE WHEN agent_quota_counter.window_start < to_timestamp(?) "
            "THEN to_timestamp(?) ELSE agent_quota_counter.window_start END "
            "RETURNING count",
            (tenant_id, window, now, amount, now - window_seconds, amount, amount,
             now - window_seconds, now),
        )
        return bool(row and int(row["count"]) <= limit)


class QuotaGuard:
    def __init__(self, repo, requests_per_minute: int = 60, tokens_per_day: int = 1_000_000):
        self._repo = repo
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.tokens_per_day = max(1, int(tokens_per_day))

    def check_request(self, tenant_id: str) -> None:
        now = time.time()
        if not self._repo.consume(tenant_id, "requests:1m", now, 60, self.requests_per_minute):
            raise QuotaExceeded("tenant request rate limit exceeded")

    def consume_tokens(self, tenant_id: str, tokens: int) -> None:
        amount = max(0, int(tokens))
        if not amount:
            return
        now = time.time()
        if not self._repo.consume(tenant_id, "tokens:1d", now, 86400,
                                  self.tokens_per_day, amount):
            raise QuotaExceeded("tenant daily token budget exceeded")


def build_quota_guard(connector=None) -> QuotaGuard:
    import os
    repo = PostgresQuotaRepository(connector) if connector is not None else InMemoryQuotaRepository()
    return QuotaGuard(
        repo,
        requests_per_minute=int(os.getenv("AGENT_RATE_LIMIT_PER_MINUTE", "60")),
        tokens_per_day=int(os.getenv("AGENT_TOKEN_BUDGET_PER_DAY", "1000000")),
    )
