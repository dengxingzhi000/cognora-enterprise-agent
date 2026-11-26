"""Request-scoped delegated identity for SCM calls.

The platform token stays in this ContextVar only. It is copied into timeout
workers by ``execute_with_timeout`` and must never be added to AgentState or a
paused-task payload.
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from security.internal_auth import TrustedIdentity


@dataclass(frozen=True)
class ScmRequestContext:
    tenant_id: str
    user_id: str
    role: str
    platform_token: str = field(default="", repr=False)
    device_id: str = ""
    idempotency_key: str = ""

    @classmethod
    def from_identity(cls, identity: TrustedIdentity) -> ScmRequestContext:
        return cls(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            role=identity.role,
            platform_token=identity.platform_token,
            device_id=identity.device_id,
            idempotency_key=identity.idempotency_key,
        )


_current_context: contextvars.ContextVar[ScmRequestContext | None] = contextvars.ContextVar(
    "scm_request_context", default=None
)


def current_scm_context() -> ScmRequestContext | None:
    return _current_context.get()


def bind_identity(identity: TrustedIdentity | None) -> contextvars.Token:
    return _current_context.set(
        ScmRequestContext.from_identity(identity) if identity is not None else None
    )


def reset_identity(token: contextvars.Token) -> None:
    _current_context.reset(token)


@contextmanager
def scm_identity_context(identity: TrustedIdentity | None) -> Iterator[ScmRequestContext | None]:
    token = bind_identity(identity)
    try:
        yield current_scm_context()
    finally:
        reset_identity(token)
