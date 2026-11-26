"""Signed, tenant-scoped knowledge document management API."""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AliasChoices, BaseModel, Field, field_validator

from knowledge.pgvector_store import PgVectorUnavailable
from security.internal_auth import TrustedIdentity, trusted_identity

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_DEPARTMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class KnowledgeDocumentRequest(BaseModel):
    document_id: str = Field(validation_alias=AliasChoices("document_id", "documentId"),
                             pattern=_KEY_PATTERN)
    version: str = Field(pattern=_KEY_PATTERN)
    department: str = Field(pattern=_DEPARTMENT_PATTERN)
    permission: Literal["employee", "finance", "manager", "admin"]
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


def _signed_identity(request: Request) -> TrustedIdentity:
    """Require the identity verified by the internal-signature middleware."""
    identity = trusted_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="trusted identity required")
    return identity


def _service(service_getter):
    try:
        return service_getter()
    except PgVectorUnavailable as exc:
        raise HTTPException(status_code=503, detail="knowledge backend unavailable") from exc


def create_knowledge_router(service_getter: Callable) -> APIRouter:
    router = APIRouter(prefix="/knowledge", tags=["knowledge"])

    @router.get("/documents")
    def list_documents(
        identity: Annotated[TrustedIdentity, Depends(_signed_identity)],
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        service = _service(service_getter)
        try:
            documents = service.list_documents(identity.tenant_id, identity.role)
        except PgVectorUnavailable as exc:
            raise HTTPException(status_code=503, detail="knowledge backend unavailable") from exc
        return {"documents": documents[offset:offset + limit], "total": len(documents),
                "limit": limit, "offset": offset}

    @router.post("/documents", status_code=201)
    def ingest_document(
        payload: KnowledgeDocumentRequest,
        identity: Annotated[TrustedIdentity, Depends(_signed_identity)],
    ):
        service = _service(service_getter)
        try:
            return service.add_document(
                **payload.model_dump(), tenant_id=identity.tenant_id, role=identity.role,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PgVectorUnavailable as exc:
            raise HTTPException(status_code=503, detail="knowledge backend unavailable") from exc

    @router.delete("/documents/{document_id}/{version}")
    def delete_document(
        document_id: str,
        version: str,
        identity: Annotated[TrustedIdentity, Depends(_signed_identity)],
    ):
        service = _service(service_getter)
        try:
            deleted = service.delete_document(
                document_id, version, identity.tenant_id, identity.role,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PgVectorUnavailable as exc:
            raise HTTPException(status_code=503, detail="knowledge backend unavailable") from exc
        return {"deleted": deleted}

    @router.get("/search")
    def search_knowledge(
        identity: Annotated[TrustedIdentity, Depends(_signed_identity)],
        query: str = Query(min_length=1),
        top_k: int = Query(default=3, ge=1, le=20),
    ):
        service = _service(service_getter)
        try:
            hits = service.search(query, identity.tenant_id, identity.role, top_k=top_k)
        except PgVectorUnavailable as exc:
            raise HTTPException(status_code=503, detail="knowledge backend unavailable") from exc
        return {"results": [{
            "text": hit.text,
            "tenant_id": hit.tenant_id,
            "department": hit.department,
            "document_id": hit.document_id,
            "version": hit.version,
            "permission": hit.permission,
            "chunk_id": hit.chunk_id,
        } for hit in hits]}

    return router
