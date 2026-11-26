import threading
import time

from apps.api.async_tasks import AsyncTaskQueue, InMemoryTaskRepository


def test_async_queue_runs_and_scopes_results():
    queue = AsyncTaskQueue(InMemoryTaskRepository(), max_workers=1)
    done = threading.Event()

    def work():
        done.set()
        return {"reply": "ok"}

    task_id = queue.submit("t1", "u1", "s1", work, external_task_id="agent-1")
    assert done.wait(2)
    deadline = time.time() + 2
    while time.time() < deadline:
        record = queue.get(task_id, "t1", "u1")
        if record and record.status == "done":
            break
        time.sleep(0.01)
    assert record.result == {"reply": "ok"}
    assert record.external_task_id == "agent-1"
    assert queue.get(task_id, "t2", "u1") is None
    assert queue.get(task_id, "t1", "u2") is None
    queue.shutdown()


def test_async_queue_does_not_reclaim_live_lease():
    from apps.api.async_tasks import AsyncTaskRecord

    repo = InMemoryTaskRepository()
    now = time.time()
    repo.create(AsyncTaskRecord(
        "task-live", "t1", "u1", None, "running", None, None, now, now,
        owner="pod-a", lease_until=now + 60,
    ))
    queue = AsyncTaskQueue(repo, max_workers=1)
    queue.fail_running()
    assert queue.get("task-live", "t1", "u1").status == "running"
    queue.shutdown()


def test_expired_owner_cannot_overwrite_reclaimed_task():
    from apps.api.async_tasks import AsyncTaskRecord

    repo = InMemoryTaskRepository()
    now = time.time()
    repo.create(AsyncTaskRecord(
        "task-expired", "t1", "u1", None, "running", None, None, now, now,
        owner="old-pod", lease_until=now - 1,
    ))
    queue = AsyncTaskQueue(repo, max_workers=1)
    assert repo.update("task-expired", "done", result={"bad": True}, owner="old-pod") is False
    queue.fail_running()
    assert repo.update("task-expired", "done", result={"bad": True}, owner="old-pod") is False
    assert queue.get("task-expired", "t1", "u1").status == "failed"
    queue.shutdown()


def test_durable_queue_recovers_payload_without_process_callable():
    from apps.api.async_tasks import AsyncTaskRecord

    class DurableRepo(InMemoryTaskRepository):
        durable = True

        def __init__(self):
            super().__init__()
            self.jobs = {}

        def create(self, record, job=None):
            self.jobs[record.task_id] = job
            return super().create(record, job)

        def get_job(self, task_id):
            return self.jobs.get(task_id)

    repo = DurableRepo()
    now = time.time()
    repo.create(
        AsyncTaskRecord("task-recover", "t1", "u1", None, "queued", None, None, now, now),
        job={"kind": "chat", "payload": {"message": "hello"}},
    )
    queue = AsyncTaskQueue(
        repo, max_workers=1, job_runner=lambda kind, payload: {"kind": kind, "message": payload["message"]},
    )
    deadline = time.time() + 2
    while time.time() < deadline and queue.get("task-recover", "t1", "u1").status != "done":
        time.sleep(0.01)
    assert queue.get("task-recover", "t1", "u1").result == {"kind": "chat", "message": "hello"}
    queue.shutdown()


def test_task_lease_is_renewed_during_long_running_work():
    class DurableRepo(InMemoryTaskRepository):
        durable = True

    repo = DurableRepo()
    release = threading.Event()
    started = threading.Event()

    def work():
        started.set()
        release.wait(3)
        return {"ok": True}

    queue = AsyncTaskQueue(repo, max_workers=1, lease_seconds=1)
    task_id = queue.submit("t1", "u1", None, work)
    assert started.wait(1)
    time.sleep(1.2)
    assert queue.get(task_id, "t1", "u1").status == "running"
    release.set()
    deadline = time.time() + 2
    while time.time() < deadline and queue.get(task_id, "t1", "u1").status != "done":
        time.sleep(0.01)
    assert queue.get(task_id, "t1", "u1").status == "done"
    queue.shutdown()


def test_durable_and_local_work_share_global_worker_bound():
    class DurableRepo(InMemoryTaskRepository):
        durable = True

        def __init__(self):
            super().__init__()
            self.jobs = {}

        def create(self, record, job=None):
            self.jobs[record.task_id] = job
            return super().create(record, job)

        def get_job(self, task_id):
            return self.jobs.get(task_id)

    repo = DurableRepo()
    release = threading.Event()
    local_started = threading.Event()
    durable_started = threading.Event()

    def local_work():
        local_started.set()
        release.wait(3)
        return {"ok": True}

    queue = AsyncTaskQueue(
        repo, max_workers=1,
        job_runner=lambda kind, payload: durable_started.set() or {"ok": True},
    )
    local_id = queue.submit("t1", "u1", None, local_work)
    assert local_started.wait(1)
    durable_id = queue.submit(
        "t1", "u1", None, None, job={"kind": "chat", "payload": {}},
    )
    time.sleep(0.2)
    assert not durable_started.is_set()
    release.set()
    deadline = time.time() + 2
    while time.time() < deadline:
        if (queue.get(local_id, "t1", "u1").status == "done"
                and queue.get(durable_id, "t1", "u1").status == "done"):
            break
        time.sleep(0.01)
    assert durable_started.is_set()
    assert queue.get(durable_id, "t1", "u1").status == "done"
    queue.shutdown()


def test_async_queue_rejects_overflow():
    import pytest

    from apps.api.async_tasks import TaskQueueFull

    queue = AsyncTaskQueue(InMemoryTaskRepository(), max_workers=1, queue_size=1)
    release = threading.Event()
    started = threading.Event()

    def blocked():
        started.set()
        release.wait(2)

    first = queue.submit("t1", "u1", None, blocked)
    assert started.wait(1)
    queued = queue.submit("t1", "u1", None, blocked)
    assert queued
    with pytest.raises(TaskQueueFull):
        queue.submit("t1", "u1", None, blocked)
    release.set()
    deadline = time.time() + 2
    while time.time() < deadline and queue.get(first, "t1", "u1").status != "done":
        time.sleep(0.01)
    queue.shutdown()

    from apps.api.async_tasks import AsyncTaskRecord

    repo = InMemoryTaskRepository()
    now = time.time()
    repo.create(AsyncTaskRecord("task-old", "t1", "u1", None, "running", None, None, now, now))
    queue = AsyncTaskQueue(repo, max_workers=1)
    queue.fail_running()
    assert queue.get("task-old", "t1", "u1").status == "failed"
    queue.shutdown()
