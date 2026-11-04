"""Batch 2 集成测试：真实 Postgres 读写。

仅在显式设置 AGENT_TEST_PG_DSN 时运行（CI 与离线环境跳过）。
推荐指向独立库，例如：
  AGENT_TEST_PG_DSN=postgresql://<user>:<password>@192.168.0.121:5432/db_agent
"""
import os
import uuid

import pytest

DSN = os.environ.get("AGENT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="AGENT_TEST_PG_DSN not set; real Postgres integration test skipped"
)


@pytest.fixture(scope="module")
def connector():
    from infrastructure.pg.connector import PGConnector
    from infrastructure.pg.schema import ensure_schema

    c = PGConnector(DSN, pool_size=2)
    assert c.healthcheck()
    ensure_schema(c)
    return c


def test_agent_pg_dsn_activates_postgres_factories(monkeypatch):
    monkeypatch.setenv("AGENT_PG_DSN", DSN)
    from security.approval import build_approval_store, PostgresApprovalRepository
    from agent.runtime.paused_tasks import build_paused_task_store, PostgresPausedTaskRepository
    assert isinstance(build_approval_store()._repo, PostgresApprovalRepository)
    assert isinstance(build_paused_task_store()._repo, PostgresPausedTaskRepository)


def test_pg_connector_qmark_placeholders_and_postgresql_dsn(connector):
    """回归：postgresql:// DSN 自动切 psycopg3，qmark 参数可绑定。"""
    assert connector.fetch_one("SELECT ? AS x", (1,)) == {"x": 1}
    row = connector.fetch_one("SELECT tenant_id FROM agent_paused_task LIMIT 1")
    assert row is None or "tenant_id" in row


def test_approval_postgres_roundtrip(connector):
    from security.approval import ApprovalStore, PostgresApprovalRepository

    repo = PostgresApprovalRepository(connector)
    store = ApprovalStore(repo)
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    aid = store.request({"tenant_id": tenant, "user_id": "u1"},
                        "scm.purchase.create", {"sku": "X", "tenant_id": tenant})
    try:
        item = store.get(aid)
        assert item["user"]["tenant_id"] == tenant
        assert item["args"] == {"sku": "X", "tenant_id": tenant}
        assert item["status"] == "pending"

        approved = store.approve(aid, "mgr-1")
        assert approved["status"] == "approved"
        assert approved["approver"] == "mgr-1"

        claimed = store.claim(aid)
        assert claimed["status"] == "in_progress"

        completed = store.complete(aid)
        assert completed["consumed"] is True
        with pytest.raises(ValueError):
            store.complete(aid)

        listed = [i for i in store.list_items() if i["id"] == aid]
        assert len(listed) == 1
    finally:
        connector.execute("DELETE FROM agent_approval WHERE id = ?", (aid,))


def test_paused_task_postgres_roundtrip_and_tenant_scope(connector):
    from agent.runtime.paused_tasks import PausedTaskStore, PostgresPausedTaskRepository
    from agent.runtime.state import AgentState
    from agent.runtime.agent_task import AgentTask

    repo = PostgresPausedTaskRepository(connector)
    store = PausedTaskStore(repo)
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"

    state = AgentState(task="采购审批挂起", task_id=task_id)
    state.context["tenant_id"] = tenant
    state.context["approval_id"] = "apr-pg-1"
    state.status = "paused"
    at = AgentTask.from_state(state, tenant_id=tenant)
    at.transition("running", "loop_start")
    at.transition("paused", "need_approval")
    state.context["agent_task"] = at
    try:
        store.save(tenant, task_id, state)
        restored = store.get(tenant, task_id)
        assert restored is not None
        assert restored.status == "paused"
        assert restored.context["approval_id"] == "apr-pg-1"
        assert restored.context["agent_task"].status == "paused"

        # 其他租户同名 task_id 查不到
        assert store.get("other-tenant", task_id) is None

        store.delete(tenant, task_id)
        assert store.get(tenant, task_id) is None
    finally:
        connector.execute(
            "DELETE FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
            (tenant, task_id),
        )
