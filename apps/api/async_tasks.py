"""Bounded background task queue with tenant-scoped durable status and leases."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class TaskQueueFull(RuntimeError):
    pass


@dataclass(frozen=True)
class AsyncTaskRecord:
    task_id: str
    tenant_id: str
    user_id: str
    session_id: str | None
    status: str
    result: dict | None
    error: str | None
    created_at: float
    updated_at: float
    owner: str | None = None
    lease_until: float = 0.0
    external_task_id: str | None = None


class InMemoryTaskRepository:
    durable = False
    def __init__(self):
        self._items: dict[str, AsyncTaskRecord] = {}
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, record, job=None):
        with self._lock:
            self._items[record.task_id] = record
            if job is not None:
                self._jobs[record.task_id] = job
        return record

    def claim_next(self, owner, lease_seconds):
        with self._lock:
            item = next((item for item in self._items.values() if item.status == "queued"), None)
            task_id = item.task_id if item is not None else None
        if task_id is None:
            return None
        return task_id if self.claim(task_id, owner, lease_seconds) else None

    def renew(self, task_id, owner, lease_seconds):
        now = time.time()
        with self._lock:
            item = self._items.get(task_id)
            if item is None or item.status != "running" or item.owner != owner or item.lease_until < now:
                return False
            self._items[task_id] = AsyncTaskRecord(
                item.task_id, item.tenant_id, item.user_id, item.session_id,
                item.status, item.result, item.error, item.created_at, now, owner,
                now + lease_seconds, item.external_task_id,
            )
            return True

    def get_job(self, task_id):
        with self._lock:
            return self._jobs.get(task_id)

    def fail_queued(self, task_id, error):
        with self._lock:
            item = self._items.get(task_id)
            if item is None or item.status != "queued":
                return False
            self._items[task_id] = AsyncTaskRecord(
                item.task_id, item.tenant_id, item.user_id, item.session_id,
                "failed", None, error, item.created_at, time.time(),
            )
            return True

    def claim(self, task_id, owner, lease_seconds):
        with self._lock:
            item = self._items[task_id]
            if item.status != "queued":
                return False
            self._items[task_id] = AsyncTaskRecord(
                item.task_id, item.tenant_id, item.user_id, item.session_id,
                "running", None, None, item.created_at, time.time(), owner,
                time.time() + lease_seconds, item.external_task_id,
            )
            return True

    def update(self, task_id, status, result=None, error=None, owner=None):
        with self._lock:
            current = self._items[task_id]
            if current.status != "running":
                return False
            if owner is not None and (current.owner != owner or current.lease_until < time.time()):
                return False
            self._items[task_id] = AsyncTaskRecord(
                current.task_id, current.tenant_id, current.user_id, current.session_id,
                status, result, error, current.created_at, time.time(), current.owner,
                current.lease_until, current.external_task_id,
            )
            return True

    def get(self, task_id, tenant_id, user_id):
        with self._lock:
            item = self._items.get(task_id)
            if item and item.tenant_id == tenant_id and item.user_id == user_id:
                return item
        return None

    def list(self, tenant_id, user_id, limit=50):
        with self._lock:
            items = [item for item in self._items.values()
                     if item.tenant_id == tenant_id and item.user_id == user_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)[:limit]

    def reclaim_expired(self):
        now = time.time()
        with self._lock:
            for task_id, item in list(self._items.items()):
                if item.status == "running" and item.lease_until < now:
                    self._items[task_id] = AsyncTaskRecord(
                        item.task_id, item.tenant_id, item.user_id, item.session_id,
                        "failed", None, "worker lease expired", item.created_at, now,
                        owner=None, lease_until=0.0, external_task_id=item.external_task_id,
                    )


class PostgresTaskRepository:
    durable = True
    def __init__(self, connector):
        self._c = connector
        self._c.execute(
            "CREATE TABLE IF NOT EXISTS agent_async_task ("
            "task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, "
            "session_id TEXT, status TEXT NOT NULL, result JSONB, error TEXT, owner TEXT, "
            "external_task_id TEXT, payload JSONB, lease_until TIMESTAMPTZ, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        self._c.execute(
            "ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS owner TEXT"
        )
        self._c.execute(
            "ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS external_task_id TEXT"
        )
        self._c.execute(
            "ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ"
        )
        self._c.execute(
            "ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS payload JSONB"
        )
        self._c.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_async_task_scope "
            "ON agent_async_task(tenant_id, user_id, created_at DESC)"
        )

    @staticmethod
    def _row(row):
        if row is None:
            return None
        return AsyncTaskRecord(
            row["task_id"], row["tenant_id"], row["user_id"], row["session_id"],
            row["status"], row["result"], row["error"],
            row["created_at"].timestamp(), row["updated_at"].timestamp(), row.get("owner"),
            row["lease_until"].timestamp() if row.get("lease_until") else 0.0,
            row.get("external_task_id"),
        )

    def create(self, record, job=None):
        payload = json.dumps(job, ensure_ascii=False, default=str) if job is not None else None
        self._c.execute(
            "INSERT INTO agent_async_task "
            "(task_id, tenant_id, user_id, session_id, status, external_task_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, CAST(? AS JSONB))",
            (record.task_id, record.tenant_id, record.user_id, record.session_id,
             record.status, record.external_task_id, payload),
        )
        return record

    def claim_next(self, owner, lease_seconds):
        row = self._c.fetch_one(
            "UPDATE agent_async_task SET status = 'running', owner = ?, "
            "lease_until = now() + (CAST(? AS INTEGER) * INTERVAL '1 second'), "
            "updated_at = now() WHERE task_id = ("
            "SELECT task_id FROM agent_async_task WHERE status = 'queued' "
            "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING task_id",
            (owner, lease_seconds),
        )
        return row["task_id"] if row else None

    def renew(self, task_id, owner, lease_seconds):
        return bool(self._c.execute_rowcount(
            "UPDATE agent_async_task SET lease_until = now() + "
            "(CAST(? AS INTEGER) * INTERVAL '1 second'), updated_at = now() "
            "WHERE task_id = ? AND status = 'running' AND owner = ? AND lease_until > now()",
            (lease_seconds, task_id, owner),
        ))

    def get_job(self, task_id):
        row = self._c.fetch_one(
            "SELECT payload FROM agent_async_task WHERE task_id = ?", (task_id,),
        )
        return row.get("payload") if row else None

    def fail_queued(self, task_id, error):
        return bool(self._c.execute_rowcount(
            "UPDATE agent_async_task SET status = 'failed', error = ?, updated_at = now() "
            "WHERE task_id = ? AND status = 'queued'",
            (error, task_id),
        ))

    def claim(self, task_id, owner, lease_seconds):
        return bool(self._c.execute_rowcount(
            "UPDATE agent_async_task SET status = 'running', owner = ?, "
            "lease_until = now() + (CAST(? AS INTEGER) * INTERVAL '1 second'), "
            "updated_at = now() WHERE task_id = ? AND status = 'queued'",
            (owner, lease_seconds, task_id),
        ))

    def update(self, task_id, status, result=None, error=None, owner=None):
        payload = json.dumps(result, ensure_ascii=False, default=str) if result is not None else None
        return bool(self._c.execute_rowcount(
            "UPDATE agent_async_task SET status = ?, result = CAST(? AS JSONB), "
            "error = ?, updated_at = now() WHERE task_id = ? AND status = 'running' "
            "AND owner IS NOT DISTINCT FROM ? AND lease_until > now()",
            (status, payload, error, task_id, owner),
        ))

    def get(self, task_id, tenant_id, user_id):
        return self._row(self._c.fetch_one(
            "SELECT task_id, tenant_id, user_id, session_id, status, result, error, "
            "created_at, updated_at, owner, lease_until, external_task_id FROM agent_async_task "
            "WHERE task_id = ? AND tenant_id = ? AND user_id = ?",
            (task_id, tenant_id, user_id),
        ))

    def list(self, tenant_id, user_id, limit=50):
        return [self._row(row) for row in self._c.fetch_all(
            "SELECT task_id, tenant_id, user_id, session_id, status, result, error, "
            "created_at, updated_at, owner, lease_until, external_task_id FROM agent_async_task "
            "WHERE tenant_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, user_id, max(1, int(limit))),
        )]

    def reclaim_expired(self):
        self._c.execute(
            "UPDATE agent_async_task SET status = 'failed', error = 'worker lease expired', "
            "owner = NULL, lease_until = NULL, updated_at = now() "
            "WHERE status = 'running' AND lease_until < now()"
        )


class AsyncTaskQueue:
    def __init__(self, repo, max_workers: int = 4, queue_size: int | None = None,
                 lease_seconds: int = 600, job_runner: Callable[[str, dict], object] | None = None):
        self._repo = repo
        self._job_runner = job_runner
        self._max_workers = max(1, int(max_workers))
        self._queue_size = max(1, int(queue_size or self._max_workers * 4))
        self._lease_seconds = max(1, int(lease_seconds))
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=self._queue_size)
        self._callables: dict[str, Callable[[], object]] = {}
        self._callables_lock = threading.Lock()
        self._stop = threading.Event()
        self._execution_slots = threading.BoundedSemaphore(self._max_workers)
        self._durable_threads: list[threading.Thread] = []
        self._threads = [
            threading.Thread(target=self._worker, name=f"agent-task-{index}", daemon=True)
            for index in range(self._max_workers)
        ]
        for thread in self._threads:
            thread.start()
        self._dispatcher = None
        if getattr(repo, "durable", False):
            self._dispatcher = threading.Thread(
                target=self._dispatch_durable, name="agent-task-dispatcher", daemon=True,
            )
            self._dispatcher.start()

    def _run_claimed(self, task_id, owner):
        try:
            self._run(task_id, owner)
        finally:
            self._execution_slots.release()

    def _dispatch_durable(self):
        reclaim_interval = max(1.0, min(30.0, self._lease_seconds / 3))
        next_reclaim = time.monotonic() + reclaim_interval
        while not self._stop.is_set():
            self._durable_threads = [thread for thread in self._durable_threads if thread.is_alive()]
            if time.monotonic() >= next_reclaim:
                try:
                    self._repo.reclaim_expired()
                except Exception:
                    logger.exception("durable task lease reclaim failed")
                next_reclaim = time.monotonic() + reclaim_interval
            if not self._execution_slots.acquire(timeout=0.25):
                continue
            owner = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
            try:
                claimed = self._repo.claim_next(owner, self._lease_seconds)
            except Exception:
                self._execution_slots.release()
                logger.exception("durable task claim failed")
                self._stop.wait(1)
                continue
            if claimed:
                thread = threading.Thread(
                    target=self._run_claimed, args=(claimed, owner),
                    name=f"agent-durable-{claimed}", daemon=True,
                )
                self._durable_threads.append(thread)
                thread.start()
            else:
                self._execution_slots.release()
                self._stop.wait(0.25)

    def _worker(self):
        while not self._stop.is_set():
            try:
                task_id, claimed_owner = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            acquired = False
            while not self._stop.is_set():
                if self._execution_slots.acquire(timeout=0.25):
                    acquired = True
                    break
            if not acquired:
                self._queue.task_done()
                continue
            try:
                owner = claimed_owner or f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
                if claimed_owner is not None or self._repo.claim(task_id, owner, self._lease_seconds):
                    self._run(task_id, owner)
            finally:
                self._execution_slots.release()
                self._queue.task_done()

    def _run(self, task_id, owner):
        if not self._repo.renew(task_id, owner, self._lease_seconds):
            return
        fn = self._callables.pop(task_id, None)
        if fn is None and hasattr(self._repo, "get_job"):
            job = self._repo.get_job(task_id)
            if job is None:
                self._repo.update(task_id, "failed", error="job_payload_missing", owner=owner)
                return
            runner = self._job_runner
            if runner is None:
                self._repo.update(task_id, "failed", error="job_runner_unavailable", owner=owner)
                return

            def run_job():
                return runner(job["kind"], job["payload"])

            fn = run_job
        if fn is None:
            self._repo.update(task_id, "failed", error="job_payload_missing", owner=owner)
            return
        stop_heartbeat = threading.Event()
        lease_lost = threading.Event()

        def heartbeat():
            interval = max(0.05, self._lease_seconds / 3)
            while not stop_heartbeat.wait(interval):
                try:
                    renewed = self._repo.renew(task_id, owner, self._lease_seconds)
                except Exception:
                    logger.exception("task lease renewal failed for %s", task_id)
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat, name=f"agent-task-lease-{task_id}", daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = fn()
            if not isinstance(result, dict):
                result = {"result": result}
            if not lease_lost.is_set():
                self._repo.update(task_id, "done", result=result, owner=owner)
        except Exception as exc:  # noqa: BLE001 - raw error never crosses API boundary
            if not lease_lost.is_set():
                self._repo.update(task_id, "failed", error=type(exc).__name__, owner=owner)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)

    def submit(self, tenant_id: str, user_id: str, session_id: str | None, fn,
               external_task_id: str | None = None, job: dict | None = None):
        task_id = f"task-{uuid.uuid4().hex[:16]}"
        now = time.time()
        record = AsyncTaskRecord(
            task_id, tenant_id, user_id, session_id, "queued", None, None, now, now,
            external_task_id=external_task_id,
        )
        self._repo.create(record, job=job)
        if job is not None and getattr(self._repo, "durable", False):
            return task_id
        with self._callables_lock:
            if fn is not None and job is None:
                self._callables[task_id] = fn
        try:
            self._queue.put_nowait((task_id, None))
        except queue.Full:
            with self._callables_lock:
                self._callables.pop(task_id, None)
            self._repo.fail_queued(task_id, "queue_full")
            raise TaskQueueFull("agent task queue is full") from None
        return task_id

    def get(self, task_id, tenant_id, user_id):
        return self._repo.get(task_id, tenant_id, user_id)

    def list(self, tenant_id, user_id, limit=50):
        return self._repo.list(tenant_id, user_id, limit)

    def reclaim_expired(self):
        self._repo.reclaim_expired()

    def fail_running(self):
        self.reclaim_expired()

    def shutdown(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1)
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=1)
        for thread in self._durable_threads:
            thread.join(timeout=1)


def build_task_queue(connector=None, job_runner=None) -> AsyncTaskQueue:
    repo = PostgresTaskRepository(connector) if connector is not None else InMemoryTaskRepository()
    return AsyncTaskQueue(
        repo,
        max_workers=int(os.getenv("AGENT_TASK_WORKERS", "4")),
        queue_size=int(os.getenv("AGENT_TASK_QUEUE_SIZE", str(int(os.getenv("AGENT_TASK_WORKERS", "4")) * 4))),
        lease_seconds=int(os.getenv("AGENT_TASK_LEASE_SECONDS", "600")),
        job_runner=job_runner,
    )
