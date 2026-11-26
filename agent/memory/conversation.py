"""Tenant/user-scoped conversation persistence with InMemory and Postgres stores."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationMessage:
    session_id: str
    tenant_id: str
    user_id: str
    role: str
    content: str
    created_at: float


class InMemoryConversationRepository:
    def __init__(self):
        self._items: list[ConversationMessage] = []
        self._lock = threading.Lock()

    def append(self, session_id, tenant_id, user_id, role, content):
        item = ConversationMessage(session_id, tenant_id, user_id, role, content, time.time())
        with self._lock:
            self._items.append(item)
        return item

    def list(self, session_id, tenant_id, user_id, limit=50):
        with self._lock:
            items = [item for item in self._items
                     if item.session_id == session_id and item.tenant_id == tenant_id
                     and item.user_id == user_id]
        return items[-max(1, int(limit)):]

    def delete(self, session_id, tenant_id, user_id):
        with self._lock:
            self._items = [item for item in self._items
                           if not (item.session_id == session_id and item.tenant_id == tenant_id
                                   and item.user_id == user_id)]


class PostgresConversationRepository:
    def __init__(self, connector):
        self._c = connector
        self._c.execute(
            "CREATE TABLE IF NOT EXISTS agent_conversation_message ("
            "id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        self._c.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_conversation_scope "
            "ON agent_conversation_message(tenant_id, user_id, session_id, created_at)"
        )

    def append(self, session_id, tenant_id, user_id, role, content):
        row = self._c.fetch_one(
            "INSERT INTO agent_conversation_message "
            "(session_id, tenant_id, user_id, role, content) VALUES (?, ?, ?, ?, ?) "
            "RETURNING session_id, tenant_id, user_id, role, content, created_at",
            (session_id, tenant_id, user_id, role, content),
        )
        return ConversationMessage(
            row["session_id"], row["tenant_id"], row["user_id"], row["role"],
            row["content"], row["created_at"].timestamp(),
        )

    def list(self, session_id, tenant_id, user_id, limit=50):
        rows = self._c.fetch_all(
            "SELECT session_id, tenant_id, user_id, role, content, created_at "
            "FROM agent_conversation_message WHERE session_id = ? AND tenant_id = ? "
            "AND user_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, tenant_id, user_id, max(1, int(limit))),
        )
        return [ConversationMessage(
            row["session_id"], row["tenant_id"], row["user_id"], row["role"],
            row["content"], row["created_at"].timestamp(),
        ) for row in reversed(rows)]

    def delete(self, session_id, tenant_id, user_id):
        self._c.execute(
            "DELETE FROM agent_conversation_message WHERE session_id = ? "
            "AND tenant_id = ? AND user_id = ?",
            (session_id, tenant_id, user_id),
        )


class ConversationStore:
    def __init__(self, repo):
        self._repo = repo

    def append(self, session_id, tenant_id, user_id, role, content):
        return self._repo.append(session_id, tenant_id, user_id, role, content)

    def list(self, session_id, tenant_id, user_id, limit=50):
        return self._repo.list(session_id, tenant_id, user_id, limit)

    def delete(self, session_id, tenant_id, user_id):
        return self._repo.delete(session_id, tenant_id, user_id)


def new_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:16]}"


def build_conversation_store(connector=None) -> ConversationStore:
    if connector is not None:
        return ConversationStore(PostgresConversationRepository(connector))
    return ConversationStore(InMemoryConversationRepository())
