import os
import uuid

import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from agent.assistance.store import (
    PostgresAssistanceStore,
    build_assistance_store,
)


def prediction(tenant="tenant-a", user="user-a"):
    return {
        "prediction_id": str(uuid.uuid4()),
        "tenant_id": tenant,
        "user_id": user,
        "session_id": "session-1",
        "signals": {"module": "order", "error_count": 2},
        "probability": 0.82,
        "threshold": 0.65,
        "decision": "OFFER_ASSISTANCE",
        "model_version": "bootstrap-v2",
        "feature_version": 2,
        "created_at": "2026-11-25T00:00:00+00:00",
    }


def feedback(item):
    return {
        "prediction_id": item["prediction_id"],
        "tenant_id": item["tenant_id"],
        "user_id": item["user_id"],
        "needed_assistance": True,
        "assistance_used": True,
        "task_succeeded": False,
        "source": "OPERATOR",
    }


def test_in_memory_store_enforces_tenant_and_user_scope():
    store = build_assistance_store(None)
    own = prediction()
    other = prediction(tenant="tenant-b", user="user-b")
    store.save_prediction(own)
    store.save_prediction(other)

    assert store.get_prediction(own["prediction_id"], "tenant-a", "user-a") == own
    assert store.get_prediction(own["prediction_id"], "tenant-b", "user-a") is None
    assert store.get_prediction(own["prediction_id"], "tenant-a", "user-b") is None

    store.save_feedback(feedback(own))
    assert len(store.training_records("tenant-a")) == 1
    assert store.training_records("tenant-b") == []


def test_postgres_store_round_trip_and_training_query():
    dsn = os.getenv("AGENT_TEST_PG_DSN")
    if not dsn:
        pytest.skip("AGENT_TEST_PG_DSN is not configured")

    from infrastructure.pg.connector import PGConnector
    from infrastructure.pg.schema import ensure_schema

    connector = PGConnector(dsn)
    ensure_schema(connector)
    store = PostgresAssistanceStore(connector)
    tenant = f"assistance-test-{uuid.uuid4()}"
    item = prediction(tenant=tenant)
    try:
        store.save_prediction(item)
        stored = store.get_prediction(item["prediction_id"], tenant, "user-a")
        assert stored["signals"] == item["signals"]
        assert store.get_prediction(item["prediction_id"], "other-tenant", "user-a") is None

        store.save_feedback(feedback(item))
        rows = store.training_records(tenant)
        assert len(rows) == 1
        assert rows[0]["needed_assistance"] is True
        assert rows[0]["session_id"] == "session-1"
    finally:
        connector.execute("DELETE FROM agent_assistance_outcome WHERE tenant_id = ?", (tenant,))
        connector.execute("DELETE FROM agent_assistance_feedback WHERE tenant_id = ?", (tenant,))
        connector.execute("DELETE FROM agent_assistance_prediction WHERE tenant_id = ?", (tenant,))
