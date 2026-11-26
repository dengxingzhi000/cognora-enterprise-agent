"""Authenticated, tenant-bound workflow execution routes."""
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from security.internal_auth import TrustedIdentity, trusted_identity
from workflow.registry import WorkflowError, WorkflowRegistry
from workflow.service import execute_expense, resume_expense

router = APIRouter(prefix="/workflow", tags=["workflow"])
_APPROVAL_STORE_GETTER = None
_CHECKPOINTER_GETTER = None


def _stores():
    approvals = _APPROVAL_STORE_GETTER() if _APPROVAL_STORE_GETTER else None
    checkpointer = _CHECKPOINTER_GETTER() if _CHECKPOINTER_GETTER else None
    return approvals, checkpointer


class ExpenseExecuteRequest(BaseModel):
    expense: dict


class ExpenseResumeRequest(BaseModel):
    approval_id: str | None = None


def _identity(request: Request):
    identity = trusted_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="trusted identity required")
    if identity.role not in ("admin", "manager", "finance"):
        raise HTTPException(status_code=403, detail="workflow execution role not allowed")
    return identity


@router.post("/expense/execute")
def execute_workflow(payload: ExpenseExecuteRequest, request: Request):
    identity = _identity(request)
    approvals, checkpointer = _stores()
    try:
        return execute_expense(
            payload.expense, identity.tenant_id,
            checkpointer=checkpointer, approval_store=approvals, user_id=identity.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/expense/{thread_id}/resume")
def resume_workflow(thread_id: str, payload: ExpenseResumeRequest, request: Request):
    identity = _identity(request)
    approvals, checkpointer = _stores()
    try:
        return resume_expense(
            thread_id, identity.tenant_id, approval_id=payload.approval_id,
            checkpointer=checkpointer, approval_store=approvals, user_id=identity.user_id,
        )
    except ValueError as exc:
        status = 404 if str(exc) == "approval_not_found" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def create_expense_workflow_router(approval_store_getter=None, checkpointer_getter=None) -> APIRouter:
    global _APPROVAL_STORE_GETTER, _CHECKPOINTER_GETTER
    _APPROVAL_STORE_GETTER = approval_store_getter
    _CHECKPOINTER_GETTER = checkpointer_getter
    return router


def create_workflow_registry_router(registry_getter: Callable[[], WorkflowRegistry]) -> APIRouter:
    """Unified ``/workflows`` catalog and read-only run API."""
    router = APIRouter(prefix="/workflows", tags=["workflows"])

    def signed_identity(request: Request) -> TrustedIdentity:
        identity = trusted_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="trusted identity required")
        return identity

    @router.get("")
    def list_workflows(identity: Annotated[TrustedIdentity, Depends(signed_identity)]):
        return {"workflows": registry_getter().catalog()}

    @router.post("/{name}/runs")
    def run_workflow(
        name: str,
        payload: Annotated[dict, Body()],
        identity: Annotated[TrustedIdentity, Depends(signed_identity)],
    ):
        try:
            return registry_getter().run(name, payload, identity)
        except WorkflowError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    return router
