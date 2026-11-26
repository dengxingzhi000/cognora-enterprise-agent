"""Tenant-bound expense workflow execution service."""
from __future__ import annotations

import uuid

from security.approval import build_approval_store
from workflow.checkpoint import get_checkpointer
from workflow.graph import build_expense_graph


def _approval_id(approvals, thread_id: str, tenant_id: str) -> str | None:
    matches = [
        item for item in approvals.list_items()
        if item.get("user", {}).get("tenant_id") == tenant_id
        and item.get("args", {}).get("thread_id") == thread_id
        and item.get("status") in ("pending", "approved", "in_progress")
    ]
    return matches[-1]["id"] if matches else None


def execute_expense(expense: dict, tenant_id: str, thread_id: str | None = None,
                    policy_threshold: float = 5000, checkpointer=None,
                    approval_store=None, user_id: str | None = None) -> dict:
    thread_id = thread_id or str(uuid.uuid4())
    checkpoint_id = f"{tenant_id}:{thread_id}"
    owned_checkpointer = checkpointer is None
    checkpointer = checkpointer or get_checkpointer()
    approvals = approval_store or build_approval_store()
    try:
        workflow = build_expense_graph(
            policy_threshold=policy_threshold,
            checkpointer=checkpointer,
            approval_store=approvals,
            tenant_id=tenant_id,
        )
        result = workflow.invoke(
            {"expense": expense, "requester_user_id": user_id or ""},
            config={"configurable": {"thread_id": checkpoint_id,
                                      "approval_thread_id": thread_id}},
        )
    finally:
        if owned_checkpointer and hasattr(checkpointer, "close"):
            checkpointer.close()
    if result.get("decision") == "paused_human_review":
        result["approval_id"] = _approval_id(approvals, thread_id, tenant_id)
    result["thread_id"] = thread_id
    result["tenant_id"] = tenant_id
    return result


def resume_expense(thread_id: str, tenant_id: str, approval_id: str | None = None,
                   checkpointer=None, approval_store=None, user_id: str | None = None) -> dict:
    owned_checkpointer = checkpointer is None
    checkpointer = checkpointer or get_checkpointer()
    approvals = approval_store or build_approval_store()
    checkpoint_id = f"{tenant_id}:{thread_id}"
    approval_id = approval_id or _approval_id(approvals, thread_id, tenant_id)
    try:
        if approval_id:
            try:
                approval = approvals.get(approval_id)
            except KeyError as exc:
                raise ValueError("approval_not_found") from exc
            if approval.get("user", {}).get("tenant_id") != tenant_id:
                raise ValueError("approval_tenant_mismatch")
            if approval.get("args", {}).get("thread_id") != thread_id:
                raise ValueError("approval_thread_mismatch")
            requester = approval.get("user", {}).get("user_id")
            if user_id is not None and requester != user_id:
                raise ValueError("workflow_owner_mismatch")
            if requester and approval.get("approver") == requester:
                raise ValueError("self_approval")
        workflow = build_expense_graph(
            checkpointer=checkpointer,
            approval_store=approvals,
            tenant_id=tenant_id,
        )
        result = workflow.invoke(
            None, config={"configurable": {"thread_id": checkpoint_id,
                                            "approval_thread_id": thread_id,
                                            "approval_id": approval_id}},
        )
    finally:
        if owned_checkpointer and hasattr(checkpointer, "close"):
            checkpointer.close()
    result["thread_id"] = thread_id
    result["tenant_id"] = tenant_id
    return result
