"""SCM HTTP wrapper with delegated identity or standalone shared-account mode."""
import contextvars
import os
import time as _time
import uuid
from typing import Any

import httpx

from observability.trace_context import current_traceparent
from observability.tracing import Tracer

from .auth import TokenCache
from .context import current_scm_context

scm_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scm_trace_id", default=None
)


class ScmClient:
    def __init__(self, gateway_url="", auth_url="", username="", password="", timeout=5, backoff_seconds=None):
        self.gateway_url = gateway_url or os.environ.get("SCM_GATEWAY_URL", "")
        self.auth_url = auth_url or os.environ.get("SCM_AUTH_URL", "")
        self.username = username or os.environ.get("SCM_USERNAME", "")
        self.password = password or os.environ.get("SCM_PASSWORD", "")
        self.timeout = int(os.environ.get("SCM_TIMEOUT_SECONDS", str(timeout)))
        self.backoff_seconds = backoff_seconds if backoff_seconds is not None else float(
            os.environ.get("SCM_RETRY_BACKOFF_SECONDS", "0.3"))
        self._tokens = TokenCache()

    def _send(self, method: str, path: str, **kw):
        url = self.gateway_url.rstrip("/") + path
        headers = dict(kw.pop("headers", {}) or {})
        identity = current_scm_context()
        if identity is not None:
            if not identity.platform_token:
                return (401, {
                    "code": "SCM_AUTHENTICATION_FAILED",
                    "error": "delegated platform token is missing",
                })
            headers["Authorization"] = f"Bearer {identity.platform_token}"
            headers["X-Tenant-Id"] = identity.tenant_id
            if identity.device_id:
                headers["X-Device-ID"] = identity.device_id
            if identity.idempotency_key:
                headers["Idempotency-Key"] = identity.idempotency_key
        else:
            token = self._tokens.get()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("X-Request-Id", str(uuid.uuid4()))
        traceparent = current_traceparent()
        if traceparent:
            headers.setdefault("traceparent", traceparent)
        tid = scm_trace_id.get()
        if tid and Tracer._shared is not None:
            try:
                Tracer._shared.log_event(tid, "scm_call",
                                         {"method": method, "path": path})
            except Exception:
                pass
        r = httpx.request(method, url, headers=headers, timeout=self.timeout, **kw)
        try:
            body = r.json()
        except Exception:
            body = {"text": r.text[:2000]}
        if tid and Tracer._shared is not None:
            try:
                Tracer._shared.log_event(tid, "scm_result",
                                         {"method": method, "path": path,
                                          "code": r.status_code})
            except Exception:
                pass
        return (r.status_code, body)

    def _login(self):
        try:
            r = httpx.post(
                self.auth_url.rstrip("/") + "/login",
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
            data = r.json()
            token = data.get("token", "")
            if token:
                self._tokens.set(token, int(data.get("expires_in", 3600)))
        except Exception:
            pass

    def get(self, path: str, params: dict | None = None, headers: dict | None = None):
        return self._request("GET", path, params=params, headers=headers)

    def post(self, path: str, json: dict | None = None, params: dict | None = None,
             headers: dict | None = None):
        return self._request("POST", path, params=params, json=json, headers=headers)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None, headers: dict | None = None):
        params = params or {}
        kwargs: dict[str, Any] = {"params": params, "headers": dict(headers or {})}
        if method == "POST":
            kwargs["json"] = json
        try:
            code, body = self._send(method, path, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException):
            if self.backoff_seconds > 0:
                _time.sleep(self.backoff_seconds)
            try:
                code, body = self._send(method, path, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as e2:
                return (0, {"error": f"transport after retry: {e2}", "path": path})
            return (code, body)

        if 500 <= code < 600:
            if self.backoff_seconds > 0:
                _time.sleep(self.backoff_seconds)
            code, body = self._send(method, path, **kwargs)
            if code == 0 or 500 <= code < 600:
                return (0, {"error": f"5xx after retry: {code}", "path": path})

        if code == 401:
            if current_scm_context() is not None:
                return (401, {
                    "code": "SCM_AUTHENTICATION_FAILED",
                    "error": "delegated SCM authentication failed",
                })
            self._login()
            try:
                code, body = self._send(method, path, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as e2:
                return (0, {"error": f"transport after 401-retry: {e2}", "path": path})
        return (code, body)

    @classmethod
    def from_env(cls):
        return cls()
