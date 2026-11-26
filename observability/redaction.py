"""Shared bounded redaction for trace and analysis output."""
from __future__ import annotations

import re

SENSITIVE_KEYS = {
    "password", "secret", "token", "access_token", "refresh_token", "authorization",
    "api_key", "apikey", "dsn", "credential", "credentials", "clientsecret",
    "accesstoken", "refreshtoken", "privatekey",
}


def _sensitive_key(key) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in SENSITIVE_KEYS or normalized.endswith(("password", "secret", "token", "apikey"))


def redact(value, max_chars: int = 2000):
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _sensitive_key(key)
                       else redact(item, max_chars))
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [redact(item, max_chars) for item in value[:50]]
    if isinstance(value, str):
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
        text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
        text = re.sub(r"\b1[3-9]\d{9}\b", "[REDACTED_PHONE]", text)
        return text[:max_chars]
    return value
