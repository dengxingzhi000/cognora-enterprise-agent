"""LangGraph-native expense workflow with optional durable human review."""
import uuid
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from agent.tools.executor import ToolTimeout, execute_with_timeout
from security.approval import (
    ApprovalConflict,
    ApprovalStore,
    build_approval_store,
    start_lease_heartbeat,
)
from workflow.checkpoint import native_saver_of
from workflow.nodes import expense as n

WORKFLOW_RETRY_ATTEMPTS = 3
WORKFLOW_RETRY_BACKOFF_SECONDS = [1, 2]
WORKFLOW_NODE_TIMEOUT_SECONDS = 5.0


class ExpenseState(TypedDict, total=False):
    expense: dict
    requester_user_id: str
    policy_threshold: float
    trace: list[str]
    amount_ok: bool
    policy_text: str
    need_human: bool
    _fallback_human: bool
    _fallback_node: str
    _fallback_error: str
    _done: list[str]
    _loop_failed: bool
    _failed_node: str


def _thread_id(config) -> str:
    return (config or {}).get("configurable", {}).get("thread_id", str(uuid.uuid4()))


class _ExpenseWorkflow:
    """Response-shaping facade over a compiled LangGraph application."""

    def __init__(self, compiled, checkpointer, approvals,
                 policy_threshold: float, durable: bool, tenant_id: str = "default"):
        self.compiled = compiled
        self.checkpointer = checkpointer
        self.approval_store = approvals
        self.approvals = approvals
        self.policy_threshold = policy_threshold
        self.durable = durable
        self.tenant_id = tenant_id

    def _checkpoint_values(self, config: dict):
        saver = native_saver_of(self.checkpointer)
        if saver is None:
            return None
        raw = saver.get_tuple(config)
        checkpoint = getattr(raw, "checkpoint", raw)
        if not isinstance(checkpoint, dict):
            return None
        values = checkpoint.get("channel_values", checkpoint)
        root = values.get("__root__", values)
        return root if isinstance(root, dict) else None

    def _checkpoint_expense(self, config: dict):
        values = self._checkpoint_values(config)
        return values.get("expense") if values else None

    def _approval_for_thread(self, thread_id: str):
        matches = [
            item for item in self.approvals.list_items()
            if item.get("user", {}).get("tenant_id") == self.tenant_id
            and item.get("args", {}).get("thread_id") == thread_id
        ]
        return matches[-1] if matches else None

    def _ensure_pending(self, thread_id: str, expense: dict, requester_user_id: str):
        try:
            existing = self._approval_for_thread(thread_id)
            if existing and existing.get("status") in ("pending", "approved", "in_progress"):
                if (existing.get("args", {}).get("expense") != expense
                        or existing.get("user", {}).get("user_id") != requester_user_id):
                    raise ValueError("workflow_thread_conflict")
                return existing["id"]
            return self.approvals.request(
                user={"thread_id": thread_id, "tenant_id": self.tenant_id,
                      "user_id": requester_user_id},
                tool="expense_approval",
                args={"expense": expense or {}, "thread_id": thread_id,
                      "tenant_id": self.tenant_id},
            )
        except ValueError:
            raise
        except Exception:
            return None

    @staticmethod
    def _shape(result: dict, *, paused: bool = False) -> dict:
        state = dict(result or {})
        inner = state.pop("__interrupt__", None)
        decision = "paused_human_review" if paused else (
            "human_review" if state.get("need_human") else "auto_approve"
        )
        trace = list(state.get("trace", []))
        trace.append("paused" if paused else decision)
        payload = {"decision": decision, "state": state, "trace": trace}
        if inner is not None:
            payload["interrupt"] = inner
        return payload

    def invoke(self, state, config=None):
        thread_id = _thread_id(config)
        approval_thread_id = (config or {}).get("configurable", {}).get(
            "approval_thread_id", thread_id,
        )
        cfg = {"configurable": {"thread_id": thread_id,
                                "approval_thread_id": approval_thread_id}}
        requester_user_id = (state or {}).get("requester_user_id", "") if isinstance(state, dict) else ""
        if requester_user_id:
            cfg["configurable"]["requester_user_id"] = requester_user_id
        if state is None:
            self.approvals.reclaim_expired_claims()
            approval = self._approval_for_thread(approval_thread_id)
            expected_approval_id = (config or {}).get("configurable", {}).get("approval_id")
            if approval is None:
                return {"decision": "failed", "trace": [], "reason": "approval_missing"}
            if expected_approval_id and approval["id"] != expected_approval_id:
                return {"decision": "failed", "trace": [], "reason": "approval_mismatch"}
            checkpoint = self._checkpoint_values(cfg) or {}
            checkpoint_expense = checkpoint.get("expense")
            if checkpoint_expense is None:
                return {"decision": "failed", "trace": [], "reason": "checkpoint_missing"}
            checkpoint_requester = checkpoint.get("requester_user_id", "")
            if (checkpoint_requester
                    and approval.get("user", {}).get("user_id") != checkpoint_requester):
                return {"decision": "failed", "trace": [], "reason": "approval_requester_mismatch"}
            if approval.get("args", {}).get("expense") != checkpoint_expense:
                return {"decision": "failed", "trace": [],
                        "reason": "approval_expense_mismatch"}
            if (approval.get("approver")
                    and approval.get("user", {}).get("user_id") == approval.get("approver")):
                return {"decision": "failed", "trace": [], "reason": "self_approval"}
            if approval["status"] == "rejected":
                return {"decision": "cancelled", "trace": [], "reason": "approval_rejected"}
            if approval["status"] == "pending":
                return {"decision": "paused_human_review", "trace": ["paused"],
                        "approval_id": approval["id"]}
            if approval["status"] != "approved":
                return {"decision": "failed", "trace": [],
                        "reason": f"approval is {approval['status']}"}
            try:
                claimed = self.approvals.claim(approval["id"])
            except ApprovalConflict:
                return {"decision": "paused_human_review", "trace": ["paused"],
                        "approval_id": approval["id"]}
            heartbeat = start_lease_heartbeat(self.approvals, approval["id"], claimed["claim_token"])
            try:
                result = self.compiled.invoke(
                    Command(resume={"approved": True, "approval_id": approval["id"]}),
                    config=cfg,
                )
            except Exception:
                self.approvals.fail(approval["id"], claimed["claim_token"], "workflow resume failed")
                raise
            finally:
                if heartbeat is not None:
                    heartbeat.stop()
            if heartbeat is not None and heartbeat.lost:
                return {"decision": "failed", "trace": [], "reason": "approval_lease_lost"}
            self.approvals.complete(approval["id"], claimed["claim_token"])
            return self._shape(result)

        expense = state.get("expense", {}) if isinstance(state, dict) else {}
        existing = self._checkpoint_values(cfg)
        if existing and (existing.get("expense") != expense
                         or existing.get("requester_user_id", "") != requester_user_id):
            raise ValueError("workflow_thread_conflict")
        result = self.compiled.invoke(
            {"expense": expense, "requester_user_id": requester_user_id,
             "policy_threshold": self.policy_threshold, "trace": []},
            config=cfg,
        )
        paused = "__interrupt__" in result
        if paused or (self.durable and result.get("need_human")):
            self._ensure_pending(approval_thread_id, expense, requester_user_id)
        return self._shape(result, paused=paused or (self.durable and result.get("need_human", False)))


def build_expense_graph(policy_threshold: float = 5000, checkpointer=None, _store=None,
                         approval_store=None, tenant_id: str = "default"):
    store = checkpointer or _store or MemorySaver()
    durable = checkpointer is not None or _store is not None
    approvals = (approval_store if approval_store is not None
                 else (build_approval_store() if durable else ApprovalStore()))

    def guarded(node: str, fn, state: ExpenseState) -> dict:
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(WORKFLOW_RETRY_ATTEMPTS),
                wait=wait_exponential(multiplier=1, max=WORKFLOW_RETRY_BACKOFF_SECONDS[-1]),
                retry=retry_if_exception(lambda exc: not isinstance(exc, ToolTimeout)),
                reraise=True,
            ):
                with attempt:
                    out = execute_with_timeout(lambda: fn(dict(state)),
                                               timeout_seconds=WORKFLOW_NODE_TIMEOUT_SECONDS)
                    if state.get("_fallback_human"):
                        out["_fallback_human"] = True
                        out["need_human"] = True
                    return out
            raise RuntimeError("retry loop produced no result")
        except ToolTimeout:
            out = dict(state)
            out.pop("__interrupt__", None)
            out.setdefault("trace", []).extend(["timeout", "fallback", "human"])
            out["_fallback_node"] = node
            out["_fallback_error"] = "tool timeout"
            out["need_human"] = True
            out["_fallback_human"] = True
            return out
        except Exception as exc:  # noqa: BLE001 - retry exhaustion falls back to human
            out = dict(state)
            out.pop("__interrupt__", None)
            out.setdefault("trace", []).extend(["timeout", "retry", "fallback", "human"])
            out["_fallback_node"] = node
            out["_fallback_error"] = f"{type(exc).__name__}: {exc}"
            out["need_human"] = True
            out["_fallback_human"] = True
            return out

    def fetch(state: ExpenseState):
        return guarded("fetch_expense", n.fetch_expense, state)

    def check(state: ExpenseState):
        return guarded("check_amount", n.check_amount, state)

    def policy(state: ExpenseState):
        return guarded("retrieve_policy", n.retrieve_policy, state)

    def judge(state: ExpenseState, config=None):
        thread_id = (config or {}).get("configurable", {}).get(
            "approval_thread_id", _thread_id(config),
        )
        out = guarded("judge_rule", n.judge_rule, state)
        if durable and out.get("need_human"):
            interrupt({"thread_id": thread_id, "need_human": True})
        return out

    graph: StateGraph[ExpenseState] = StateGraph(ExpenseState)
    graph.add_node("fetch", fetch)
    graph.add_node("check", check)
    graph.add_node("policy", policy)
    graph.add_node("judge", judge)
    graph.set_entry_point("fetch")
    graph.add_edge("fetch", "check")
    graph.add_edge("check", "policy")
    graph.add_edge("policy", "judge")
    graph.add_edge("judge", END)
    compiled = graph.compile(checkpointer=native_saver_of(store))
    return _ExpenseWorkflow(
        compiled, store, approvals, policy_threshold, durable, tenant_id,
    )


def run_expense_workflow(expense: dict, policy_threshold: float = 5000) -> dict:
    workflow = build_expense_graph(policy_threshold=policy_threshold)
    return workflow.invoke(
        {"expense": expense},
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )
