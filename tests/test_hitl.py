"""Stage 5 / v3: HITL 闭环 — 审批决策 + 从 paused 任务恢复。"""
import pytest


def _mock_schema(monkeypatch):
    from infrastructure.pg import connector as conn_mod
    from infrastructure.pg import schema as schema_mod
    monkeypatch.setattr(conn_mod, "get_connector", lambda: None)
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)


def _mock_planner(monkeypatch):
    import apps.api.main as main_mod

    def fake_planner(state):
        if state.observations:
            return {"action": "finish", "answer": "审批后继续完成"}
        return {"action": "call_tool", "tool": "scm.purchase.create",
                "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}

    monkeypatch.setattr(main_mod, "build_deepseek_planner", lambda client, model: fake_planner)


@pytest.fixture
def paused_task(monkeypatch):
    """经公开 /chat 进入 paused，返回 (client, chat_payload)。"""
    from fastapi.testclient import TestClient
    from apps.api.main import app

    _mock_planner(monkeypatch)
    _mock_schema(monkeypatch)
    client = TestClient(app)
    r = client.post("/chat", json={"message": "buy X", "tenant_id": "t1", "user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["approval_id"].startswith("apr-")
    return client, body


def test_decide_approve_returns_status(paused_task):
    client, body = paused_task
    aid = body["approval_id"]
    r = client.post(f"/approval/{aid}/decide",
                    json={"decision": "approve", "approver": "mgr-1", "tenant_id": "t1"})
    assert r.status_code == 200
    out = r.json()
    assert out["approval_id"] == aid
    assert out["status"] == "approved"
    assert out["tool"] == "scm.purchase.create"
    assert out["approver"] == "mgr-1"


def test_decide_unknown_approval_404(paused_task):
    client, _ = paused_task
    r = client.post("/approval/apr-does-not-exist/decide",
                    json={"decision": "approve", "approver": "mgr-1", "tenant_id": "t1"})
    assert r.status_code == 404


def test_decide_twice_conflicts(paused_task):
    client, body = paused_task
    aid = body["approval_id"]
    payload = {"decision": "approve", "approver": "mgr-1", "tenant_id": "t1"}
    assert client.post(f"/approval/{aid}/decide", json=payload).status_code == 200
    second = client.post(f"/approval/{aid}/decide", json=payload)
    assert second.status_code == 409


def test_decide_cross_tenant_denied(paused_task):
    client, body = paused_task
    aid = body["approval_id"]
    r = client.post(f"/approval/{aid}/decide",
                    json={"decision": "approve", "approver": "mgr-1", "tenant_id": "t2"})
    assert r.status_code == 403


def test_resume_unknown_task_404(paused_task):
    client, _ = paused_task
    r = client.post("/chat/does-not-exist/resume", json={"tenant_id": "t1"})
    assert r.status_code == 404


def test_resume_while_approval_pending_409(paused_task):
    client, body = paused_task
    r = client.post(f"/chat/{body['task_id']}/resume", json={"tenant_id": "t1"})
    assert r.status_code == 409


def test_resume_other_tenant_gets_404(paused_task):
    client, body = paused_task
    r = client.post(f"/chat/{body['task_id']}/resume", json={"tenant_id": "t2"})
    assert r.status_code == 404


def test_resume_without_body_does_not_422(paused_task):
    """body 可省略；默认 tenant=t1，仍因审批未决返回 409。"""
    client, body = paused_task
    r = client.post(f"/chat/{body['task_id']}/resume")
    assert r.status_code == 409


def test_paused_tasks_are_tenant_scoped(monkeypatch):
    """同一 task_id 在两个租户下互不覆盖。"""
    from fastapi.testclient import TestClient
    from apps.api.main import app

    _mock_planner(monkeypatch)
    _mock_schema(monkeypatch)
    client = TestClient(app)

    task_id = "shared-task-id"
    t1 = client.post("/chat", json={"message": "buy", "tenant_id": "t1",
                                    "user_id": "u1", "task_id": task_id})
    t2 = client.post("/chat", json={"message": "buy", "tenant_id": "t2",
                                    "user_id": "u2", "task_id": task_id})
    assert t1.status_code == 200 and t1.json()["status"] == "paused"
    assert t2.status_code == 200 and t2.json()["status"] == "paused"

    for chat_body, tenant in ((t1.json(), "t1"), (t2.json(), "t2")):
        decided = client.post(
            f"/approval/{chat_body['approval_id']}/decide",
            json={"decision": "approve", "approver": "mgr-1", "tenant_id": tenant},
        )
        assert decided.status_code == 200
        resumed = client.post(f"/chat/{task_id}/resume", json={"tenant_id": tenant})
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "done"


def test_resume_after_approval_completes_task(paused_task):
    client, body = paused_task
    aid = body["approval_id"]
    task_id = body["task_id"]
    assert client.post(f"/approval/{aid}/decide",
                       json={"decision": "approve", "approver": "mgr-1",
                             "tenant_id": "t1"}).status_code == 200

    r = client.post(f"/chat/{task_id}/resume", json={"tenant_id": "t1"})
    assert r.status_code == 200
    out = r.json()
    assert out["task_id"] == task_id
    assert out["status"] == "done"
    assert aid in out["reply"]
    assert out["executed"] is True
    assert out["approval_id"] == aid
    assert out["execution_result"]

    # 已消费的任务不能再次恢复
    assert client.post(f"/chat/{task_id}/resume", json={"tenant_id": "t1"}).status_code in (404, 409)


def test_resume_after_reject_cancels_task(paused_task):
    client, body = paused_task
    aid = body["approval_id"]
    task_id = body["task_id"]
    assert client.post(f"/approval/{aid}/decide",
                       json={"decision": "reject", "approver": "mgr-1",
                             "tenant_id": "t1"}).status_code == 200

    r = client.post(f"/chat/{task_id}/resume", json={"tenant_id": "t1"})
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "cancelled"
    assert "rejected" in out["reply"]
    assert aid in out["reply"]


def test_guarded_executor_executes_with_matching_approved_id():
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine

    user = {"tenant_id": "t1", "role": "admin", "user_id": "u1"}
    args = {"sku": "X", "tenant_id": "t1"}
    store = ApprovalStore()
    aid = store.request(user, "scm.purchase.create", args)
    store.approve(aid, "mgr-1")

    state = AgentState(task="buy X")
    state.context["tenant_id"] = "t1"
    out = guarded_executor(
        {"action": "call_tool", "tool": "scm.purchase.create", "args": args},
        state, user=user, policy=PolicyEngine(), approvals=store,
        approved_approval_id=aid,
    )
    # 占位写口被真正调用（返回占位字符串），而不是再次挂起
    assert out["result"] == "need approval: scm.purchase.create pending human review"
    assert "pause_reason" not in state.context
    assert len(store._items) == 1


def test_guarded_executor_ignores_approved_id_for_other_tool():
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine

    user = {"tenant_id": "t1", "role": "admin", "user_id": "u1"}
    store = ApprovalStore()
    aid = store.request(user, "scm.order.cancel",
                        {"order_no": "O-1", "tenant_id": "t1"})
    store.approve(aid, "mgr-1")

    state = AgentState(task="buy X")
    state.context["tenant_id"] = "t1"
    out = guarded_executor(
        {"action": "call_tool", "tool": "scm.purchase.create",
         "args": {"sku": "X", "tenant_id": "t1"}},
        state, user=user, policy=PolicyEngine(), approvals=store,
        approved_approval_id=aid,
    )
    assert "pending human review" in out["result"]
    assert state.context.get("pause_reason") == "need_approval"
    assert len(store._items) == 2  # 新工具必须产生新的审批单


def test_guarded_executor_ignores_approved_id_for_other_args():
    from agent.runtime.state import AgentState
    from agent.tools.executor import guarded_executor
    from security.approval import ApprovalStore
    from security.policy import PolicyEngine

    user = {"tenant_id": "t1", "role": "admin", "user_id": "u1"}
    store = ApprovalStore()
    aid = store.request(user, "scm.purchase.create",
                        {"sku": "A", "tenant_id": "t1"})
    store.approve(aid, "mgr-1")

    state = AgentState(task="buy X")
    state.context["tenant_id"] = "t1"
    out = guarded_executor(
        {"action": "call_tool", "tool": "scm.purchase.create",
         "args": {"sku": "B", "tenant_id": "t1"}},
        state, user=user, policy=PolicyEngine(), approvals=store,
        approved_approval_id=aid,
    )
    assert state.context.get("pause_reason") == "need_approval"
    assert len(store._items) == 2


def test_approval_consume_blocks_replay():
    from security.approval import ApprovalStore

    user = {"tenant_id": "t1", "role": "admin", "user_id": "u1"}
    store = ApprovalStore()
    aid = store.request(user, "scm.purchase.create", {"sku": "X"})
    store.approve(aid, "mgr-1")
    assert store.consume(aid)["consumed"] is True
    with pytest.raises(ValueError):
        store.consume(aid)
