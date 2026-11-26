"""HMAC-SHA256-V3 authentication for trusted internal BFF callers.

The signature covers the exact request target, body bytes and delegated user
credentials before FastAPI parses either. A verified identity is stored on the
ASGI scope; routes must use it instead of caller-controlled body fields whenever
signature enforcement is enabled.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

MAX_TIMESTAMP_SKEW_MS = 300_000
NONCE_TTL_SECONDS = 300
_REQUIRED_HEADERS = (
    "x-app-id",
    "x-timestamp",
    "x-nonce",
    "x-signature",
    "x-tenant-id",
    "x-user-id",
    "x-user-role",
)


class InternalAuthError(Exception):
    """Safe-to-return authentication failure."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class TrustedIdentity:
    app_id: str
    tenant_id: str
    user_id: str
    role: str
    platform_token: str = field(default="", repr=False)
    device_id: str = ""
    idempotency_key: str = ""


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def internal_signature_required() -> bool:
    return _env_enabled("AGENT_INTERNAL_SIGNATURE_REQUIRED")


def signature_secret() -> str | None:
    """Resolve one shared secret; the platform name is the compatibility alias."""
    for name in ("AGENT_INTERNAL_SIGNATURE_SECRET", "API_SECRET_INTERNAL_SERVICE"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def validate_internal_signature_config() -> None:
    """Fail at startup on missing or weak integrated-channel credentials."""
    resolved = signature_secret()
    if internal_signature_required() and not resolved:
        raise RuntimeError(
            "AGENT_INTERNAL_SIGNATURE_REQUIRED=true requires "
            "AGENT_INTERNAL_SIGNATURE_SECRET or API_SECRET_INTERNAL_SERVICE"
        )
    if resolved is not None and len(resolved) < 32:
        raise RuntimeError("internal signature secret must contain at least 32 characters")


def _bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def canonical_payload(
    method: str,
    raw_path: str | bytes,
    raw_query: str | bytes,
    body: bytes,
    timestamp: str,
    nonce: str,
    app_id: str,
    tenant_id: str,
    user_id: str,
    role: str,
    platform_token: str = "",
    device_id: str = "",
    idempotency_key: str = "",
) -> bytes:
    """Build the exact thirteen-field V3 payload, including its final newline."""
    signed_text = {
        "method": method, "raw_path": raw_path, "raw_query": raw_query,
        "timestamp": timestamp, "nonce": nonce, "app_id": app_id,
        "tenant_id": tenant_id, "user_id": user_id, "role": role,
        "platform_token": platform_token, "device_id": device_id,
        "idempotency_key": idempotency_key,
    }
    for name, value in signed_text.items():
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if b"\n" in raw or b"\r" in raw:
            raise ValueError(f"{name} must not contain a newline")
    query = _bytes(raw_query)
    canonical_query = b"&".join(sorted(part for part in query.split(b"&") if part))
    token_hash = (
        hashlib.sha256(platform_token.encode("utf-8")).hexdigest()
        if platform_token.strip() else ""
    )
    fields = (
        method.upper().encode("ascii"),
        _bytes(raw_path),
        canonical_query,
        hashlib.sha256(body).hexdigest().encode("ascii"),
        timestamp.encode("utf-8"),
        nonce.encode("utf-8"),
        app_id.encode("utf-8"),
        tenant_id.encode("utf-8"),
        user_id.encode("utf-8"),
        role.encode("utf-8"),
        token_hash.encode("ascii"),
        device_id.encode("utf-8"),
        idempotency_key.encode("utf-8"),
    )
    return b"\n".join(fields) + b"\n"


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): value for name, value in headers.items()}


def verify_signature(
    method: str,
    raw_path: str | bytes,
    raw_query: str | bytes,
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: str | None = None,
    now_ms: int | None = None,
) -> TrustedIdentity:
    """Verify syntax, freshness and HMAC, returning only signed identity fields."""
    values = _normalized_headers(headers)
    missing = [name for name in _REQUIRED_HEADERS if not values.get(name)]
    if missing:
        raise InternalAuthError(401, "missing internal signature headers")

    timestamp = values["x-timestamp"]
    nonce = values["x-nonce"]
    app_id = values["x-app-id"]
    tenant_id = values["x-tenant-id"]
    user_id = values["x-user-id"]
    role = values["x-user-role"]
    signature = values["x-signature"]
    platform_token = values.get("x-platform-token", "")
    device_id = values.get("x-device-id", "")
    idempotency_key = values.get("idempotency-key", "")

    if any("\n" in value or "\r" in value for value in values.values()):
        raise InternalAuthError(401, "invalid internal signature headers")
    try:
        timestamp_ms = int(timestamp)
    except (TypeError, ValueError):
        raise InternalAuthError(401, "invalid internal signature timestamp") from None
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if abs(current_ms - timestamp_ms) > MAX_TIMESTAMP_SKEW_MS:
        raise InternalAuthError(401, "internal signature timestamp is outside the allowed window")

    # Malformed credentials are 401.  A well-formed digest that does not match
    # means the signed request material was altered (or the secret is wrong), so
    # report 403 without disclosing which field failed.
    if len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
        raise InternalAuthError(401, "invalid internal signature")
    shared_secret = signature_secret() if secret is None else secret
    if not shared_secret:
        raise InternalAuthError(401, "internal signature is not configured")
    try:
        payload = canonical_payload(
            method, raw_path, raw_query, body,
            timestamp, nonce, app_id, tenant_id, user_id, role,
            platform_token=platform_token, device_id=device_id,
            idempotency_key=idempotency_key,
        )
    except (ValueError, UnicodeEncodeError):
        raise InternalAuthError(401, "invalid internal signature request target") from None
    expected = hmac.new(
        shared_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InternalAuthError(403, "internal signature mismatch")
    return TrustedIdentity(
        app_id=app_id, tenant_id=tenant_id, user_id=user_id, role=role,
        platform_token=platform_token, device_id=device_id,
        idempotency_key=idempotency_key,
    )


class InMemoryNonceStore:
    """Process-local replay cache used only when Agent Postgres is unavailable."""

    def __init__(self, ttl_seconds: int = NONCE_TTL_SECONDS):
        self._ttl = max(1, int(ttl_seconds))
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            expired = [key for key, deadline in self._expires_at.items() if deadline <= current]
            for key in expired:
                self._expires_at.pop(key, None)
            if nonce in self._expires_at:
                return False
            self._expires_at[nonce] = current + self._ttl
            return True


class PostgresNonceStore:
    """Cluster-wide atomic nonce consumption through the shared PG connector."""

    def __init__(self, connector, ttl_seconds: int = NONCE_TTL_SECONDS):
        self._connector = connector
        self._ttl = max(1, int(ttl_seconds))

    def consume(self, nonce: str) -> bool:
        self._connector.execute(
            "DELETE FROM agent_internal_nonce WHERE expires_at <= now()"
        )
        row = self._connector.fetch_one(
            "INSERT INTO agent_internal_nonce (nonce, expires_at) "
            "VALUES (?, now() + (CAST(? AS INTEGER) * INTERVAL '1 second')) "
            "ON CONFLICT (nonce) DO NOTHING RETURNING nonce",
            (nonce, self._ttl),
        )
        return row is not None


def build_nonce_store(dsn: str | None = None, connector=None) -> InMemoryNonceStore | PostgresNonceStore:
    """Use AGENT_PG_DSN when available; otherwise keep the security cache local."""
    from infrastructure.pg.connector import get_agent_connector

    if connector is None:
        connector = get_agent_connector(dsn)
    if connector is None:
        return InMemoryNonceStore()
    return PostgresNonceStore(connector)


class InternalSignatureService:
    def __init__(self, nonce_store=None):
        self._fixed_nonce_store = nonce_store
        self._nonce_store = nonce_store
        self._nonce_dsn: str | None = None
        self._lock = threading.Lock()

    def _store(self):
        if self._fixed_nonce_store is not None:
            return self._fixed_nonce_store
        from infrastructure.pg.connector import agent_dsn

        dsn = agent_dsn()
        if self._nonce_store is not None and self._nonce_dsn == dsn:
            return self._nonce_store
        with self._lock:
            if self._nonce_store is None or self._nonce_dsn != dsn:
                self._nonce_store = build_nonce_store(dsn)
                self._nonce_dsn = dsn
        return self._nonce_store

    def authenticate(
        self,
        method: str,
        raw_path: str | bytes,
        raw_query: str | bytes,
        body: bytes,
        headers: Mapping[str, str],
    ) -> TrustedIdentity:
        identity = verify_signature(method, raw_path, raw_query, body, headers)
        nonce = _normalized_headers(headers)["x-nonce"]
        try:
            consumed = self._store().consume(nonce)
        except InternalAuthError:
            raise
        except Exception:  # noqa: BLE001 - replay protection must fail closed
            logger.exception("internal nonce store unavailable")
            raise InternalAuthError(503, "internal nonce store unavailable") from None
        if not consumed:
            raise InternalAuthError(409, "internal signature nonce already used")
        return identity


_SERVICE = InternalSignatureService()


def get_internal_signature_service() -> InternalSignatureService:
    return _SERVICE


def is_protected_path(path: str) -> bool:
    return (
        path == "/chat"
        or path.startswith("/chat/")
        or path == "/writing-assistant"
        or path == "/ops"
        or path.startswith("/ops/")
        or path.startswith("/approval/")
        or path == "/capabilities"
        or path == "/workflows"
        or path.startswith("/workflows/")
        or path == "/workflow"
        or path.startswith("/workflow/")
        or path == "/tasks"
        or path.startswith("/tasks/")
        or path == "/knowledge"
        or path.startswith("/knowledge/")
        or path == "/assistance"
        or path.startswith("/assistance/")
    )


def trusted_identity(request) -> TrustedIdentity | None:
    """Return the middleware-verified identity, or None in standalone mode."""
    return getattr(request.state, "internal_identity", None)


class InternalSignatureMiddleware:
    """Authenticate raw ASGI requests before routing/body validation."""

    def __init__(self, app, service: InternalSignatureService | None = None):
        self.app = app
        self.service = service or get_internal_signature_service()

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not internal_signature_required()
            or not is_protected_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        messages: deque = deque()
        chunks: list[bytes] = []
        body_size = 0
        max_body_bytes = max(1, int(os.getenv("AGENT_INTERNAL_MAX_BODY_BYTES", "1048576")))
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                body = message.get("body", b"")
                body_size += len(body)
                if body_size > max_body_bytes:
                    await JSONResponse(
                        {"detail": "internal request body is too large"}, status_code=413,
                    )(scope, receive, send)
                    return
                chunks.append(body)
                more_body = bool(message.get("more_body", False))
            else:
                more_body = False
        body = b"".join(chunks)
        raw_path = scope.get("raw_path") or scope.get("path", "").encode("utf-8")
        if isinstance(raw_path, bytes):
            raw_path = raw_path.split(b"?", 1)[0]
        else:
            raw_path = raw_path.split("?", 1)[0]
        headers = {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        try:
            identity = self.service.authenticate(
                scope.get("method", "GET"), raw_path,
                scope.get("query_string", b""), body, headers,
            )
        except InternalAuthError as exc:
            await JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code
            )(scope, receive, send)
            return

        scope.setdefault("state", {})["internal_identity"] = identity

        async def replay_receive():
            if messages:
                return messages.popleft()
            return await receive()

        await self.app(scope, replay_receive, send)
