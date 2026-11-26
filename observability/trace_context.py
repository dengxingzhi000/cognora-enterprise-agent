"""W3C trace context propagation for outbound SCM calls."""
from __future__ import annotations

import contextvars
import uuid

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "traceparent", default=None,
)


def new_traceparent() -> str:
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return f"00-{trace_id}-{span_id}-01"


def bind_traceparent(value: str | None) -> contextvars.Token:
    return _current.set(value)


def reset_traceparent(token: contextvars.Token) -> None:
    _current.reset(token)


def current_traceparent() -> str | None:
    return _current.get()
