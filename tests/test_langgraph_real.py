"""Batch 6: LangGraph 真图路径 + 内存 / Postgres checkpoint。"""
import os
import uuid

import pytest

langgraph = pytest.importorskip("langgraph")


def test_real_state_graph_path_with_memory_saver():
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph

    cp = get_checkpointer(dsn=None)
    g = build_expense_graph(policy_threshold=5000, checkpointer=cp)
    assert type(g).__name__ == "_RealAdapter"
    out = g.invoke({"expense": {"id": "E-real-1", "amount": 100}},
                   config={"configurable": {"thread_id": "real-auto-1"}})
    assert out["decision"] == "auto_approve"
    assert "fetch" in out["trace"] and "judge_rule" in out["trace"]


def test_real_graph_pause_and_resume_with_memory_saver():
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph

    cp = get_checkpointer(dsn=None)
    tid = "real-pause-" + uuid.uuid4().hex[:8]
    g = build_expense_graph(policy_threshold=5000, checkpointer=cp)
    first = g.invoke({"expense": {"id": "E-real-2", "amount": 6230}},
                     config={"configurable": {"thread_id": tid}})
    assert first["decision"] == "paused_human_review"
    raw = cp.native.get_tuple({"configurable": {"thread_id": tid}})
    assert raw is not None and raw.pending_writes

    g2 = build_expense_graph(policy_threshold=5000, checkpointer=cp)
    done = g2.invoke(None, config={"configurable": {"thread_id": tid}})
    assert done["decision"] == "human_review"
    assert "judge_rule" in done["trace"]


@pytest.mark.skipif(not os.environ.get("AGENT_TEST_PG_DSN"),
                    reason="AGENT_TEST_PG_DSN not set; real Postgres checkpoint skipped")
def test_postgres_checkpoint_persist_read_and_resume():
    from workflow.checkpoint import get_checkpointer
    from workflow.graph_lang import build_expense_graph

    dsn = os.environ["AGENT_TEST_PG_DSN"]
    tid = "pg-real-" + uuid.uuid4().hex[:8]
    cp = get_checkpointer(dsn=dsn)
    cp2 = None
    try:
        assert cp.is_native, "PostgresSaver must be active, not the memory fallback"
        g = build_expense_graph(policy_threshold=5000, checkpointer=cp)
        first = g.invoke({"expense": {"id": "E-pg-1", "amount": 6230}},
                         config={"configurable": {"thread_id": tid}})
        assert first["decision"] == "paused_human_review"

        raw = cp.native.get_tuple({"configurable": {"thread_id": tid}})
        assert raw is not None
        assert raw.checkpoint["channel_values"]["__root__"]["expense"]["id"] == "E-pg-1"

        # 新 saver 连接按同一 thread_id 读取 checkpoint 并恢复。
        cp2 = get_checkpointer(dsn=dsn)
        assert cp2.get(tid) is not None
        g2 = build_expense_graph(policy_threshold=5000, checkpointer=cp2)
        done = g2.invoke(None, config={"configurable": {"thread_id": tid}})
        assert done["decision"] == "human_review"
        assert done["state"]["need_human"] is True
        assert not (cp2.native.get_tuple({"configurable": {"thread_id": tid}}).pending_writes)
    finally:
        if cp2 is not None:
            cp2.close()
        cp.close()
