"""Tenant-scoped async task status and SSE routes."""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from security.internal_auth import trusted_identity

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _identity(request: Request):
    identity = trusted_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="trusted identity required")
    return identity


def _payload(record) -> dict:
    return {
        "task_id": record.task_id,
        "session_id": record.session_id,
        "status": record.status,
        "result": record.result,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "external_task_id": record.external_task_id,
    }


def create_task_router(queue):
    queue_getter = queue if callable(queue) else lambda: queue

    @router.get("")
    def list_tasks(request: Request, limit: int = 50):
        identity = _identity(request)
        limit = min(100, max(1, limit))
        return [_payload(item) for item in queue_getter().list(identity.tenant_id, identity.user_id, limit)]

    @router.get("/{task_id}")
    def get_task(task_id: str, request: Request):
        identity = _identity(request)
        record = queue_getter().get(task_id, identity.tenant_id, identity.user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _payload(record)

    @router.get("/{task_id}/events")
    def task_events(task_id: str, request: Request):
        identity = _identity(request)

        async def stream():
            deadline = time.monotonic() + 130
            last_status = None
            while time.monotonic() < deadline:
                record = await asyncio.to_thread(
                    queue_getter().get, task_id, identity.tenant_id, identity.user_id,
                )
                if record is None:
                    yield "event: error\ndata: {\"code\":\"TASK_NOT_FOUND\"}\n\n"
                    return
                if record.status != last_status:
                    yield f"event: status\ndata: {json.dumps(_payload(record), default=str)}\n\n"
                    last_status = record.status
                if record.status in ("done", "failed", "cancelled"):
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.25)
            yield "event: error\ndata: {\"code\":\"TASK_STREAM_TIMEOUT\"}\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
