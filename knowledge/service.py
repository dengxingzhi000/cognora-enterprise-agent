"""Shared knowledge service and role-derived access control."""
from __future__ import annotations

import os
import re
import threading

from .document import Document
from .ingestion import ingest
from .pgvector_store import build_knowledge_store
from .seed import seed_default_store

ALL_PERMISSIONS = ("employee", "finance", "manager", "admin")
ROLE_PERMISSIONS = {
    "employee": ("employee",),
    "manager": ("employee", "manager"),
    "finance": ("employee", "finance"),
    "admin": ALL_PERMISSIONS,
}
_WRITE_ROLES = frozenset({"manager", "finance", "admin"})
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_DEPARTMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_service: KnowledgeService | None = None
_service_lock = threading.Lock()


def permissions_for_role(role: str | None) -> list[str]:
    """Return a fresh ACL list; unknown roles receive no knowledge access."""
    return list(ROLE_PERMISSIONS.get(role or "", ()))


def _validate_key(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_KEY.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


class KnowledgeService:
    def __init__(self, store=None, *, seed: bool | None = None):
        self.store = store if store is not None else build_knowledge_store()
        if seed is None:
            seed = bool(os.getenv("AGENT_KNOWLEDGE_SEED_TENANT_ID"))
        if seed:
            seed_default_store(self.store, os.getenv("AGENT_KNOWLEDGE_SEED_TENANT_ID"))

    def list_documents(self, tenant_id: str, role: str) -> list[dict]:
        return self.store.list_documents(tenant_id, permissions_for_role(role))

    def search(self, query: str, tenant_id: str, role: str, top_k: int = 3) -> list:
        return self.store.search(
            query, tenant_id=tenant_id,
            allowed_permissions=permissions_for_role(role), top_k=top_k,
        )

    def add_document(self, *, document_id: str, version: str, department: str,
                     permission: str, text: str, tenant_id: str, role: str) -> dict:
        allowed = permissions_for_role(role)
        if role not in _WRITE_ROLES or permission not in allowed:
            raise PermissionError("role cannot manage this knowledge permission")
        _validate_key(document_id, "document_id")
        _validate_key(version, "version")
        if not isinstance(department, str) or not _SAFE_DEPARTMENT.fullmatch(department):
            raise ValueError("invalid department")
        if permission not in ALL_PERMISSIONS:
            raise ValueError("invalid permission")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be blank")
        document = Document(
            text=text.strip(), tenant_id=tenant_id, department=department,
            document_id=document_id, version=version, permission=permission,
        )
        chunks = ingest(document)
        self.store.replace_document(
            tenant_id, document_id, version, chunks,
            allowed_permissions=allowed,
        )
        return {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "version": version,
            "department": department,
            "permission": permission,
            "chunk_count": len(chunks),
        }

    def delete_document(self, document_id: str, version: str,
                        tenant_id: str, role: str) -> int:
        if role not in _WRITE_ROLES:
            raise PermissionError("role cannot manage knowledge documents")
        _validate_key(document_id, "document_id")
        _validate_key(version, "version")
        allowed_writes = permissions_for_role(role)
        deleted = self.store.delete_document(
            tenant_id, document_id, version, allowed_permissions=allowed_writes,
        )
        if deleted:
            return deleted
        if self.store.document_exists(tenant_id, document_id, version):
            raise PermissionError("role cannot manage this knowledge permission")
        raise KeyError("knowledge document not found")


def get_knowledge_service() -> KnowledgeService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = KnowledgeService()
    return _service
