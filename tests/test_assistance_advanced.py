from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.assistance.features import (
    FEATURE_VERSION,
    AssistanceEvent,
    AssistancePredictRequest,
    AssistanceSignals,
)
from agent.assistance.model import (
    fit_assistance_model,
    generate_bootstrap_records,
    promote_active_model,
    rollback_active_model,
)
from agent.assistance.predictor import AssistancePredictor
from agent.assistance.service import AssistanceService
from agent.assistance.store import InMemoryAssistanceStore


def signals():
    return AssistanceSignals(
        module="order",
        workflow="create_order",
        session_duration_seconds=300,
        active_seconds=240,
        idle_seconds=60,
        event_count=20,
        error_count=3,
        unique_error_count=2,
        repeated_error_count=2,
        retry_count=3,
        back_navigation_count=1,
        context_switch_count=1,
        validation_failure_count=1,
        permission_denied_count=0,
        form_completion_ratio=0.7,
        help_open_count=1,
        previous_task_count=30,
        previous_success_rate=0.85,
        recent_assistance_count=0,
        explicit_help_request=False,
        high_risk_action=False,
    )


def event(event_type, offset_ms, **overrides):
    values = {
        "type": event_type,
        "offset_ms": offset_ms,
        "duration_ms": 0,
        "severity": 0,
        "error_code": None,
    }
    values.update(overrides)
    return AssistanceEvent(**values)


def test_event_sequence_and_server_context_become_features():
    request = AssistancePredictRequest(
        session_id="sequence-session",
        signals=signals(),
        recent_events=[
            event("API_ERROR", 1000, severity=2, error_code="SERVER_ERROR"),
            event("RETRY", 2000),
            event("HELP_OPEN", 3000),
            event("TASK_SUCCESS", 4000),
        ],
    )
    features = request.signals.feature_dict(
        {
            "role": "finance",
            "recent_events": [item.model_dump() for item in request.recent_events],
            "history": {
                "predictions_7d": 4,
                "interventions_7d": 2,
                "needed_feedback_7d": 2,
                "assistance_acceptance_rate_7d": 0.5,
                "seconds_since_last_prediction": 120,
            },
            "server_facts": {
                "server_outcome_count": 2,
                "server_error_count": 1,
                "server_timeout_count": 0,
                "server_rejection_count": 0,
                "server_failed_count": 0,
                "server_p95_latency_ms": 800,
            },
        }
    )

    assert FEATURE_VERSION == 2
    assert features["role"] == "finance"
    assert features["seq_error_burst_max"] >= 1
    assert features["seq_retry_after_error_count"] >= 1
    assert features["seq_help_after_error_count"] >= 1
    assert features["seq_recovery_count"] >= 1
    assert features["history_predictions_7d"] == 4
    assert features["server_error_count"] == 1
    assert features["server_p95_latency_ms"] == 800


@pytest.fixture(scope="module")
def advanced_model():
    records = generate_bootstrap_records(n=2400, seed=42)
    return fit_assistance_model(
        records,
        model_version="tenant-advanced-test",
        training_source="synthetic_bootstrap",
        min_samples=500,
        min_per_class=50,
        false_positive_cost=1.0,
        false_negative_cost=3.0,
        seed=42,
    )


def test_cost_threshold_slice_metrics_and_drift_status(advanced_model):
    bundle, metrics = advanced_model
    assert metrics["false_positive_cost"] == 1.0
    assert metrics["false_negative_cost"] == 3.0
    assert metrics["expected_cost"] >= 0
    assert "by_role" in metrics["slice_metrics"]
    assert "by_module" in metrics["slice_metrics"]
    assert "by_risk" in metrics["slice_metrics"]
    assert bundle["baseline_probability_mean"] > 0

    predictor = AssistancePredictor(bundle=bundle)
    predictor.predict("tenant-1", signals())
    predictor.predict("tenant-1", signals())
    status = predictor.status("tenant-1")
    assert status["live_count"] == 2
    assert status["baseline_probability_mean"] == bundle["baseline_probability_mean"]
    assert status["live_probability_mean"] > 0


def test_model_promotion_and_rollback(tmp_path):
    promote_active_model(tmp_path / "active.json", "tenant-a", "tenant-v1")
    promote_active_model(tmp_path / "active.json", "tenant-a", "tenant-v2")

    from agent.assistance.model import load_active_manifest

    assert load_active_manifest(tmp_path / "active.json")["tenant-a"] == "tenant-v2"
    rollback_active_model(tmp_path / "active.json", "tenant-a")
    assert load_active_manifest(tmp_path / "active.json")["tenant-a"] == "tenant-v1"


def test_server_outcomes_can_supply_conservative_training_labels():
    store = InMemoryAssistanceStore()
    now = datetime.now(UTC).isoformat()
    prediction = {
        "prediction_id": "prediction-1",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "role": "employee",
        "session_id": "session-1",
        "signals": signals().model_dump(),
        "recent_events": [],
        "history_snapshot": {},
        "server_facts_snapshot": {},
        "probability": 0.8,
        "threshold": 0.5,
        "decision": "OFFER_ASSISTANCE",
        "model_version": "bootstrap-v2",
        "feature_version": 2,
        "created_at": now,
    }
    store.save_prediction(prediction)
    store.save_outcome(
        {
            "outcome_id": "outcome-1",
            "prediction_id": "prediction-1",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-1",
            "module": "order",
            "workflow": "create_order",
            "outcome": "TASK_ABANDONED",
            "error_code": None,
            "latency_ms": 1000,
            "occurred_at": now,
        }
    )

    assert store.training_records("tenant-a") == []
    rows = store.training_records("tenant-a", include_outcome_labels=True)
    assert len(rows) == 1
    assert rows[0]["needed_assistance"] is True
    assert rows[0]["source"] == "SERVER_OUTCOME"
    assert store.history_summary("tenant-a", "user-a", "order", "create_order")[
        "predictions_7d"
    ] == 1
    facts = store.server_facts("tenant-a", "user-a", "session-1")
    assert facts["server_outcome_count"] == 1
    assert facts["server_failed_count"] == 1


def test_agent_completion_can_supply_a_verified_negative_label():
    store = InMemoryAssistanceStore()
    now = datetime.now(UTC).isoformat()
    store.save_prediction(
        {
            "prediction_id": "prediction-chat",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "role": "employee",
            "session_id": "chat-session",
            "signals": signals().model_dump(),
            "recent_events": [],
            "history_snapshot": {},
            "server_facts_snapshot": {},
            "probability": 0.2,
            "threshold": 0.49,
            "decision": "DO_NOT_INTERRUPT",
            "model_version": "bootstrap-v2",
            "feature_version": 2,
            "created_at": now,
        }
    )
    service = AssistanceService(store, AssistancePredictor())

    service.record_prediction_outcome(
        "tenant-a", "user-a", "prediction-chat", "TASK_COMPLETED", 120
    )

    rows = store.training_records("tenant-a", include_outcome_labels=True)
    assert len(rows) == 1
    assert rows[0]["needed_assistance"] is False
    assert rows[0]["source"] == "SERVER_OUTCOME"


def test_chat_completion_automatically_records_server_outcome(monkeypatch):
    import apps.api.main as main

    store = InMemoryAssistanceStore()
    now = datetime.now(UTC).isoformat()
    store.save_prediction(
        {
            "prediction_id": "prediction-auto-chat",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "role": "employee",
            "session_id": "auto-chat-session",
            "signals": signals().model_dump(),
            "recent_events": [],
            "history_snapshot": {},
            "server_facts_snapshot": {},
            "probability": 0.2,
            "threshold": 0.49,
            "decision": "DO_NOT_INTERRUPT",
            "model_version": "bootstrap-v2",
            "feature_version": 2,
            "created_at": now,
        }
    )
    monkeypatch.setattr(main, "_ASSISTANCE_STORE", store)
    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", AssistancePredictor())

    class Conversations:
        def list(self, *args, **kwargs):
            return []

        def append(self, *args, **kwargs):
            return None

    class Quota:
        def check_request(self, tenant_id):
            return None

        def consume_tokens(self, tenant_id, tokens):
            return None

    def fake_run(state, **kwargs):
        state.status = "done"
        state.answer = "ok"
        return state

    monkeypatch.setattr(main.app.state, "conversations", Conversations(), raising=False)
    monkeypatch.setattr(main.app.state, "chat_quota", Quota(), raising=False)
    monkeypatch.setattr(main._runtime_loop, "run", fake_run)
    monkeypatch.setattr(main, "get_default_client", lambda: (None, "deepseek-chat"))
    identity = SimpleNamespace(tenant_id="tenant-a", user_id="user-a", role="employee")

    result = main._chat(
        main.ChatRequest(
            message="hello", assistance_prediction_id="prediction-auto-chat"
        ),
        identity,
    )

    assert result["status"] == "done"
    assert store.server_facts("tenant-a", "user-a", "auto-chat-session")[
        "server_outcome_count"
    ] == 1


def test_internal_outcome_endpoint_requires_trusted_identity(monkeypatch):
    import apps.api.assistance_router as router
    import apps.api.main as main

    monkeypatch.setattr(main, "_ASSISTANCE_STORE", InMemoryAssistanceStore())
    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", AssistancePredictor())
    monkeypatch.setattr(router, "trusted_identity", lambda request: None)
    client = TestClient(main.app)
    payload = {
        "session_id": "session-1",
        "module": "order",
        "workflow": "create_order",
        "outcome": "TASK_COMPLETED",
        "error_code": None,
        "latency_ms": 100,
    }

    response = client.post("/assistance/outcomes", json=payload)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ASSISTANCE_INTERNAL_IDENTITY_REQUIRED"


def test_server_outcome_enriches_following_prediction(monkeypatch):
    import apps.api.assistance_router as router
    import apps.api.main as main

    monkeypatch.setattr(main, "_ASSISTANCE_STORE", InMemoryAssistanceStore())
    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", AssistancePredictor())
    identity = SimpleNamespace(tenant_id="tenant-a", user_id="user-a", role="employee")
    monkeypatch.setattr(router, "trusted_identity", lambda request: identity)
    client = TestClient(main.app)
    prediction = client.post(
        "/assistance/predict",
        json={"session_id": "session-1", "signals": signals().model_dump()},
    ).json()
    outcome = client.post(
        "/assistance/outcomes",
        json={
            "prediction_id": prediction["prediction_id"],
            "session_id": "session-1",
            "module": "order",
            "workflow": "create_order",
            "outcome": "TASK_ABANDONED",
            "error_code": "BUSINESS_REJECTION",
            "latency_ms": 800,
        },
    )

    assert outcome.status_code == 200
    followup = client.post(
        "/assistance/predict",
        json={"session_id": "session-1", "signals": signals().model_dump()},
    )
    assert followup.status_code == 200
    assert followup.json()["context_summary"]["server_outcomes"] == 1
    assert followup.json()["context_summary"]["history_predictions_7d"] == 1


def test_model_status_endpoint_returns_safe_metadata(monkeypatch):
    import apps.api.assistance_router as router
    import apps.api.main as main

    monkeypatch.setattr(main, "_ASSISTANCE_STORE", InMemoryAssistanceStore())
    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", AssistancePredictor())
    identity = SimpleNamespace(tenant_id="tenant-a", user_id="user-a", role="admin")
    monkeypatch.setattr(router, "trusted_identity", lambda request: identity)
    client = TestClient(main.app)

    response = client.get("/assistance/model/status")

    assert response.status_code == 200
    assert response.json()["model_version"] == "bootstrap-v2"
    assert "pipeline" not in response.json()
