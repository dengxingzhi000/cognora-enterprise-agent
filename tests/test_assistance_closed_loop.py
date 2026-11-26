from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.assistance.features import AssistancePredictRequest, AssistanceSignals
from agent.assistance.knowledge import AssistanceKnowledgeService
from agent.assistance.model import fit_assistance_model, generate_bootstrap_records
from agent.assistance.predictor import AssistancePrediction, AssistancePredictor
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


def test_curated_knowledge_is_scoped_by_role_and_workflow():
    service = AssistanceKnowledgeService(knowledge_service=None)

    employee = service.recommend(
        tenant_id="tenant-a",
        role="employee",
        signals=signals(),
        recent_events=[
            {
                "type": "API_ERROR",
                "offset_ms": 1000,
                "duration_ms": 0,
                "severity": 2,
                "error_code": "SERVER_ERROR",
            }
        ],
        server_facts={"server_error_count": 1},
    )
    admin = service.recommend(
        tenant_id="tenant-a",
        role="admin",
        signals=signals(),
        recent_events=[],
        server_facts={},
    )

    assert employee
    assert all(
        item["permission"] in {"employee", "manager", "finance", "admin"}
        for item in employee
    )
    assert all("steps" in item and item["steps"] for item in employee)
    assert any(item["article_id"] == "order-create-server-error" for item in employee)
    assert all(item["permission"] != "admin" for item in employee)
    assert admin


def test_prediction_returns_permission_scoped_intervention_knowledge():
    class InterveningPredictor:
        def predict(self, tenant_id, signals, context=None):
            return AssistancePrediction(
                probability=0.9,
                threshold=0.49,
                decision="OFFER_ASSISTANCE",
                suggested_action="OPEN_ASSISTANT",
                model_version="bootstrap-v2",
                feature_version=2,
                training_source="synthetic_bootstrap",
            )

    service = AssistanceService(
        InMemoryAssistanceStore(),
        InterveningPredictor(),
        knowledge=AssistanceKnowledgeService(knowledge_service=None),
    )
    request = AssistancePredictRequest(
        session_id="knowledge-session",
        signals=signals(),
        recent_events=[],
    )

    result = service.predict("tenant-a", "user-a", "employee", request)

    assert result["knowledge_count"] >= 1
    assert result["knowledge"][0]["steps"]
    assert all(item["permission"] != "admin" for item in result["knowledge"])


def test_knowledge_feedback_and_coverage_close_the_loop():
    store = InMemoryAssistanceStore()
    store.save_prediction(
        {
            "prediction_id": "prediction-1",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "role": "employee",
            "session_id": "session-1",
            "signals": signals().model_dump(),
            "recent_events": [],
            "history_snapshot": {},
            "server_facts_snapshot": {},
            "knowledge_snapshot": [
                {"article_id": "order-create-server-error", "title": "Order 5xx"}
            ],
            "probability": 0.8,
            "threshold": 0.49,
            "decision": "OFFER_ASSISTANCE",
            "model_version": "bootstrap-v2",
            "feature_version": 2,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    service = AssistanceService(store, AssistancePredictor())

    service.feedback(
        "tenant-a",
        "user-a",
        "prediction-1",
        SimpleNamespace(
            needed_assistance=True,
            assistance_used=True,
            task_succeeded=False,
            source="OPERATOR",
            knowledge_helpful=True,
            knowledge_article_id="order-create-server-error",
        ),
    )

    coverage = store.coverage_summary("tenant-a")
    assert coverage["predictions"] == 1
    assert coverage["feedback"] == 1
    assert coverage["feedback_coverage"] == 1.0
    assert coverage["knowledge_helpful_rate"] == 1.0
    dashboard = service.dashboard_data("tenant-a")
    assert dashboard["coverage"]["feedback_coverage"] == 1.0
    assert dashboard["knowledge"]["top_articles"][0]["article_id"] == "order-create-server-error"
    assert dashboard["model"]["model_version"] == "bootstrap-v2"


def test_training_reports_workflow_tenant_source_and_time_slices():
    records = generate_bootstrap_records(n=2400, seed=42)
    _, metrics = fit_assistance_model(
        records,
        model_version="tenant-slice-test",
        training_source="synthetic_bootstrap",
        min_samples=500,
        min_per_class=50,
        seed=42,
    )

    assert {"by_tenant", "by_workflow", "by_source", "by_period"} <= set(
        metrics["slice_metrics"]
    )


def test_dashboard_data_endpoint_returns_closed_loop_metrics(monkeypatch):
    import apps.api.assistance_router as router
    import apps.api.main as main

    monkeypatch.setattr(main, "_ASSISTANCE_STORE", InMemoryAssistanceStore())
    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", AssistancePredictor())
    identity = SimpleNamespace(tenant_id="tenant-a", user_id="user-a", role="admin")
    monkeypatch.setattr(router, "trusted_identity", lambda request: identity)

    response = TestClient(main.app).get("/assistance/dashboard/data")

    assert response.status_code == 200
    assert response.json()["coverage"]["feedback_coverage"] == 0.0
    assert response.json()["knowledge"]["top_articles"] == []
    assert response.json()["model"]["feature_version"] == 2
