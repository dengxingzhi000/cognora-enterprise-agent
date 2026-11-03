"""Batch 1: EpisodicMemoryProvider 真实现 — 写入、相似检索、租户隔离、空数据。"""
from agent.memory.episodic import EpisodicMemoryStore, infer_task_type, reset_episodic_store
from agent.memory.store import MemoryStore


def _connector_store():
    from infrastructure.pg.connector import InMemoryConnector
    c = InMemoryConnector()
    c.execute("CREATE TABLE memory_episodic (id INT, tenant_id TEXT, department TEXT, "
              "permission TEXT, version INT, body TEXT, updated_at TEXT)")
    return MemoryStore(c), c


def test_record_and_recall_by_similarity_on_memory_fallback():
    es = EpisodicMemoryStore(MemoryStore())
    es.record(tenant_id="t1", task_id="task-1", task_type="procurement",
              from_status="running", to_status="failed", reason="supplier_timeout",
              tools=["scm.supplier.get"], task_text="采购异常 PO-001，供应商延迟")
    es.record(tenant_id="t1", task_id="task-2", task_type="expense",
              from_status="running", to_status="done", reason="policy_check",
              tools=["knowledge.search"], task_text="午餐补贴咨询")
    hits = es.recall("t1", "继续处理上次那个采购问题", top_k=1)
    assert len(hits) == 1
    assert "PO-001" in hits[0]["task_text"]


def test_record_and_recall_with_connector():
    store, _ = _connector_store()
    es = EpisodicMemoryStore(store)
    es.record(tenant_id="t1", task_id="task-3", task_type="ops",
              from_status="running", to_status="paused", reason="need_approval",
              tools=["metrics.get", "logs.search"], task_text="订单服务 5xx 故障排查")
    hits = es.recall("t1", "订单服务 5xx 又报错了", tools=["metrics.get"], top_k=2)
    assert hits
    assert hits[0]["task_id"] == "task-3"
    assert hits[0]["to"] == "paused"


def test_cross_tenant_isolation():
    es = EpisodicMemoryStore(MemoryStore())
    es.record(tenant_id="t1", task_id="a", task_type="ops", from_status=None,
              to_status="done", task_text="订单 5xx 超时")
    es.record(tenant_id="t2", task_id="b", task_type="ops", from_status=None,
              to_status="done", task_text="订单 5xx 超时")
    assert [h["task_id"] for h in es.recall("t1", "订单 5xx")] == ["a"]
    assert [h["task_id"] for h in es.recall("t2", "订单 5xx")] == ["b"]
    assert es.list("t3") == []


def test_empty_data_returns_empty_recall():
    es = EpisodicMemoryStore(MemoryStore())
    assert es.recall("t1", "任意问题") == []
    assert es.list("t1") == []


def test_infer_task_type_and_similarity():
    assert infer_task_type("采购 PO-001 延迟") == "procurement"
    assert infer_task_type("订单 5xx 超时") == "ops"
    assert infer_task_type("合同赔偿条款") == "contract"
    assert infer_task_type("无关文本") == "general"
    from agent.memory.episodic import similarity
    assert similarity("采购 PO-001", "采购异常 PO-001 延迟") > similarity("采购 PO-001", "午餐补贴")


def test_loop_writes_task_transitions_to_episodic(monkeypatch):
    reset_episodic_store()
    from agent.runtime.loop import run
    from agent.runtime.state import AgentState

    st = AgentState(task="订单服务 5xx 故障")
    st.task_id = "episodic-loop-1"
    st.context["tenant_id"] = "t1"

    final = run(st, planner=lambda s: {"action": "finish", "answer": "done"})
    from agent.memory.episodic import get_episodic_store
    hits = [b for b in get_episodic_store().list("t1") if b.get("task_id") == "episodic-loop-1"]
    statuses = [b["to"] for b in hits]
    assert final.status == "done"
    assert "running" in statuses and "done" in statuses
    assert all(b["tenant_id"] == "t1" for b in hits)


def test_provider_formats_recalled_episode():
    store, _ = _connector_store()
    es = EpisodicMemoryStore(store)
    es.record(tenant_id="t1", task_id="task-p", task_type="ops", from_status="running",
              to_status="failed", reason="timeout", tools=["logs.search"],
              task_text="订单服务 5xx 超时")
    from agent.context.memory_providers import EpisodicMemoryProvider
    provider = EpisodicMemoryProvider(store=store, permission="tenant")
    segs = provider.collect({"tenant_id": "t1", "task": "订单 5xx 又故障了"})
    assert segs and "类似历史任务" in segs[0]["text"]
    assert "logs.search" in segs[0]["text"]
