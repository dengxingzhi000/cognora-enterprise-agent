"""Batch 2: 审批单与挂起任务持久化（InMemory 默认 / Postgres 可选）。"""
import pytest


def test_approval_store_inmemory_repository_roundtrip():
    from security.approval import ApprovalStore, InMemoryApprovalRepository
    repo = InMemoryApprovalRepository()
    store = ApprovalStore(repo)
    aid = store.request({"tenant_id": "t1", "user_id": "u1"}, "scm.purchase.create",
                        {"sku": "X", "tenant_id": "t1"})
    assert store.get(aid)["status"] == "pending"
    assert store.approve(aid, "mgr-1")["status"] == "approved"
    assert store.list_items()[0]["id"] == aid
    claimed = store.claim(aid)
    assert claimed["status"] == "in_progress"
    assert store.complete(aid, claimed["claim_token"])["consumed"] is True


def test_build_approval_store_falls_back_without_dsn(monkeypatch):
    for name in ("AGENT_PG_DSN", "PG_DSN", "POSTGRES_DSN", "AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    from security.approval import build_approval_store, InMemoryApprovalRepository
    store = build_approval_store()
    assert isinstance(store._repo, InMemoryApprovalRepository)


def test_build_paused_store_falls_back_without_dsn(monkeypatch):
    for name in ("AGENT_PG_DSN", "PG_DSN", "POSTGRES_DSN", "AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    from agent.runtime.paused_tasks import build_paused_task_store, InMemoryPausedTaskRepository
    store = build_paused_task_store()
    assert isinstance(store._repo, InMemoryPausedTaskRepository)


def test_pg_dsn_alone_does_not_activate_agent_persistence(monkeypatch):
    """AGENT_PG_DSN 是唯一开关；旧 PG_DSN 不能意外开启审批/挂起持久化。"""
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.delenv("AGENT_PG_OFFLINE", raising=False)
    monkeypatch.setenv("PG_DSN", "postgresql://unused:unused@127.0.0.1:1/nope")
    from security.approval import build_approval_store, InMemoryApprovalRepository
    from agent.runtime.paused_tasks import build_paused_task_store, InMemoryPausedTaskRepository
    assert isinstance(build_approval_store()._repo, InMemoryApprovalRepository)
    assert isinstance(build_paused_task_store()._repo, InMemoryPausedTaskRepository)


def test_episodic_default_ignores_legacy_pg_dsn(monkeypatch):
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.delenv("AGENT_PG_OFFLINE", raising=False)
    monkeypatch.setenv("PG_DSN", "postgresql://unused:unused@127.0.0.1:1/nope")
    from agent.memory.episodic import get_episodic_store, reset_episodic_store
    reset_episodic_store()
    store = get_episodic_store()
    assert store._store._c is None


def _paused_state(task_id="task-p1", tenant="t1"):
    from agent.runtime.state import AgentState
    from agent.runtime.agent_task import AgentTask

    state = AgentState(task="采购下单需要审批", task_id=task_id)
    state.context["tenant_id"] = tenant
    state.context["user"] = {"tenant_id": tenant, "role": "admin", "user_id": "u1"}
    state.context["approval_id"] = "apr-1"
    state.context["approval_tool"] = "scm.purchase.create"
    state.context["approval_args"] = {"sku": "X", "tenant_id": tenant}
    state.status = "paused"
    state.answer = "paused for approval"
    state.observations.append({"tool": "scm.purchase.create", "result": "need approval"})
    at = AgentTask.from_state(state, tenant_id=tenant)
    at.transition("running", "loop_start")
    at.transition("paused", "need_approval")
    state.context["agent_task"] = at
    return state


def test_paused_task_store_roundtrip_preserves_agent_state():
    from agent.runtime.paused_tasks import PausedTaskStore
    store = PausedTaskStore()
    state = _paused_state()
    store.save("t1", "task-p1", state)

    restored = store.get("t1", "task-p1")
    assert restored is not None
    assert restored.status == "paused"
    assert restored.answer == "paused for approval"
    assert restored.context["tenant_id"] == "t1"
    assert restored.context["approval_id"] == "apr-1"
    assert restored.context["approval_args"] == {"sku": "X", "tenant_id": "t1"}
    at = restored.context["agent_task"]
    assert at.task_id == "task-p1"
    assert at.status == "paused"
    assert at.history[-1]["to"] == "paused"

    store.delete("t1", "task-p1")
    assert store.get("t1", "task-p1") is None


def test_paused_task_store_is_tenant_scoped():
    from agent.runtime.paused_tasks import PausedTaskStore
    store = PausedTaskStore()
    store.save("t1", "same-id", _paused_state("same-id", "t1"))
    store.save("t2", "same-id", _paused_state("same-id", "t2"))
    assert store.get("t1", "same-id").context["tenant_id"] == "t1"
    assert store.get("t2", "same-id").context["tenant_id"] == "t2"


def test_chat_resume_path_uses_persistent_store(monkeypatch):
    """公开 API 仍走同一路径；rebuild store 后（模拟进程重启）仍能恢复。"""
    from fastapi.testclient import TestClient
    import apps.api.main as main_mod
    from infrastructure.pg import connector as conn_mod
    from infrastructure.pg import schema as schema_mod
    from agent.runtime.paused_tasks import PausedTaskStore

    monkeypatch.setattr(conn_mod, "get_connector", lambda: None)
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)
    monkeypatch.setattr(main_mod, "_PAUSED_TASKS", PausedTaskStore())

    def fake_planner(state):
        return {"action": "call_tool", "tool": "scm.purchase.create",
                "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}

    monkeypatch.setattr(main_mod, "build_deepseek_planner", lambda client, model: fake_planner)
    client = TestClient(main_mod.app)
    r = client.post("/chat", json={"message": "buy X", "tenant_id": "t1", "user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"

    # 模拟另一个实例：从同一 store 取出（InMemory 进程内共享；PG 实现跨进程）。
    saved = main_mod._PAUSED_TASKS.get("t1", body["task_id"])
    assert saved is not None and saved.status == "paused"
