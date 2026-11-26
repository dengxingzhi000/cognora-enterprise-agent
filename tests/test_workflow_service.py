from langgraph.checkpoint.memory import MemorySaver

from security.approval import ApprovalStore
from workflow.service import execute_expense, resume_expense


def test_execute_expense_pauses_with_tenant_bound_approval():
    checkpointer = MemorySaver()
    approvals = ApprovalStore()
    result = execute_expense(
        {"id": "E1", "amount": 10000}, "t1",
        thread_id="wf-1", checkpointer=checkpointer, approval_store=approvals,
    )
    assert result["decision"] == "paused_human_review"
    approval = approvals.get(result["approval_id"])
    assert approval["user"]["tenant_id"] == "t1"
    assert approval["args"]["tenant_id"] == "t1"
    assert approval["args"]["thread_id"] == "wf-1"


def test_resume_requires_approval_and_consumes_it():
    checkpointer = MemorySaver()
    approvals = ApprovalStore()
    first = execute_expense(
        {"id": "E1", "amount": 10000}, "t1",
        thread_id="wf-2", checkpointer=checkpointer, approval_store=approvals,
    )
    aid = first["approval_id"]
    pending = resume_expense(
        "wf-2", "t1", approval_id=aid,
        checkpointer=checkpointer, approval_store=approvals,
    )
    assert pending["decision"] == "paused_human_review"

    approvals.approve(aid, "manager")
    done = resume_expense(
        "wf-2", "t1", approval_id=aid,
        checkpointer=checkpointer, approval_store=approvals,
    )
    assert done["decision"] == "human_review"
    assert approvals.get(aid)["consumed"] is True


def test_same_thread_id_is_isolated_across_tenants():
    checkpointer = MemorySaver()
    approvals = ApprovalStore()
    first = execute_expense(
        {"id": "E-t1", "amount": 10000}, "t1", thread_id="shared",
        checkpointer=checkpointer, approval_store=approvals,
    )
    second = execute_expense(
        {"id": "E-t2", "amount": 10000}, "t2", thread_id="shared",
        checkpointer=checkpointer, approval_store=approvals,
    )
    assert first["approval_id"] != second["approval_id"]
    approvals.approve(first["approval_id"], "manager")
    done = resume_expense(
        "shared", "t1", approval_id=first["approval_id"],
        checkpointer=checkpointer, approval_store=approvals,
    )
    assert done["decision"] == "human_review"
    still_paused = resume_expense(
        "shared", "t2", approval_id=second["approval_id"],
        checkpointer=checkpointer, approval_store=approvals,
    )
    assert still_paused["decision"] == "paused_human_review"


def test_workflow_binds_requester_and_rejects_self_approval():
    checkpointer = MemorySaver()
    approvals = ApprovalStore()
    first = execute_expense(
        {"id": "E-owner", "amount": 10000}, "t1", thread_id="wf-owner",
        checkpointer=checkpointer, approval_store=approvals, user_id="requester",
    )
    aid = first["approval_id"]
    assert approvals.get(aid)["user"]["user_id"] == "requester"
    approvals.approve(aid, "requester")
    import pytest
    with pytest.raises(ValueError, match="self_approval"):
        resume_expense("wf-owner", "t1", approval_id=aid,
                       checkpointer=checkpointer, approval_store=approvals,
                       user_id="requester")


def test_workflow_thread_rejects_conflicting_expense_or_owner():
    checkpointer = MemorySaver()
    approvals = ApprovalStore()
    execute_expense(
        {"id": "E-bound", "amount": 10000}, "t1", thread_id="wf-bound",
        checkpointer=checkpointer, approval_store=approvals, user_id="owner-a",
    )
    import pytest
    with pytest.raises(ValueError, match="workflow_thread_conflict"):
        execute_expense(
            {"id": "E-other", "amount": 10000}, "t1", thread_id="wf-bound",
            checkpointer=checkpointer, approval_store=approvals, user_id="owner-a",
        )
    with pytest.raises(ValueError, match="workflow_thread_conflict"):
        execute_expense(
            {"id": "E-bound", "amount": 10000}, "t1", thread_id="wf-bound",
            checkpointer=checkpointer, approval_store=approvals, user_id="owner-b",
        )


def test_resume_rejects_cross_tenant_or_thread_approval():
    import pytest

    checkpointer = MemorySaver()
    approvals = ApprovalStore()
    first = execute_expense(
        {"id": "E1", "amount": 10000}, "t1",
        thread_id="wf-3", checkpointer=checkpointer, approval_store=approvals,
    )
    aid = first["approval_id"]
    approvals.approve(aid, "manager")
    with pytest.raises(ValueError, match="approval_tenant_mismatch"):
        resume_expense("wf-3", "t2", approval_id=aid,
                       checkpointer=checkpointer, approval_store=approvals)
    with pytest.raises(ValueError, match="approval_thread_mismatch"):
        resume_expense("other", "t1", approval_id=aid,
                       checkpointer=checkpointer, approval_store=approvals)
