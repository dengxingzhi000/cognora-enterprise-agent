import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from agent.assistance.model import AssistanceModelError
from agent.assistance.predictor import AssistancePredictor
from agent.assistance.store import InMemoryAssistanceStore

SECRET = "test-assistance-signature-secret-0123456789"
TENANT = "tenant-assistance"
USER = "user-assistance"


def payload():
    return {
        "session_id": "session-1",
        "tenant_id": "body-tenant",
        "user_id": "body-user",
        "signals": {
            "module": "order",
            "workflow": "create_order",
            "session_duration_seconds": 300,
            "active_seconds": 240,
            "idle_seconds": 60,
            "event_count": 20,
            "error_count": 4,
            "unique_error_count": 2,
            "repeated_error_count": 3,
            "retry_count": 4,
            "back_navigation_count": 2,
            "context_switch_count": 1,
            "validation_failure_count": 2,
            "permission_denied_count": 0,
            "form_completion_ratio": 0.7,
            "help_open_count": 1,
            "previous_task_count": 30,
            "previous_success_rate": 0.85,
            "recent_assistance_count": 0,
            "explicit_help_request": False,
            "high_risk_action": False,
        },
    }


def signed_headers(method, path, body, *, tenant_id=TENANT, user_id=USER):
    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex
    fields = [
        method.upper(),
        path,
        "",
        hashlib.sha256(body).hexdigest(),
        timestamp,
        nonce,
        "platform-agent-bff",
        tenant_id,
        user_id,
        "employee",
        "",
        "",
        "",
    ]
    signature = hmac.new(
        SECRET.encode(), ("\n".join(fields) + "\n").encode(), hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-App-Id": "platform-agent-bff",
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id,
        "X-User-Role": "employee",
    }


@pytest.fixture
def assistance_app(monkeypatch):
    import apps.api.main as main

    monkeypatch.delenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", raising=False)
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.setattr(main, "_ASSISTANCE_STORE", InMemoryAssistanceStore())
    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", AssistancePredictor())
    return main, TestClient(main.app)


def test_prediction_uses_signed_identity_and_returns_model_metadata(
    assistance_app, monkeypatch
):
    main, _ = assistance_app
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_SECRET", SECRET)
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "true")
    client = TestClient(main.app)
    body = json.dumps(payload(), separators=(",", ":")).encode()

    response = client.post(
        "/assistance/predict",
        content=body,
        headers=signed_headers("POST", "/assistance/predict", body),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["prediction_id"]
    assert 0 <= result["probability"] <= 1
    assert result["decision"] in {"OFFER_ASSISTANCE", "DO_NOT_INTERRUPT"}
    assert result["feature_version"] == 2
    assert result["requires_human_confirmation"] is False
    stored = main._ASSISTANCE_STORE.get_prediction(result["prediction_id"], TENANT, USER)
    assert stored is not None
    assert stored["tenant_id"] == TENANT
    assert stored["user_id"] == USER


def test_high_risk_action_always_requires_human_confirmation(assistance_app):
    _, client = assistance_app
    request = payload()
    request["signals"]["high_risk_action"] = True

    response = client.post("/assistance/predict", json=request)

    assert response.status_code == 200
    assert response.json()["requires_human_confirmation"] is True


def test_feedback_is_scoped_to_prediction_owner(assistance_app, monkeypatch):
    main, _ = assistance_app
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_SECRET", SECRET)
    monkeypatch.setenv("AGENT_INTERNAL_SIGNATURE_REQUIRED", "true")
    client = TestClient(main.app)
    body = json.dumps(payload(), separators=(",", ":")).encode()
    prediction = client.post(
        "/assistance/predict",
        content=body,
        headers=signed_headers("POST", "/assistance/predict", body),
    ).json()
    feedback = {
        "needed_assistance": True,
        "assistance_used": True,
        "task_succeeded": False,
        "source": "OPERATOR",
    }
    path = f"/assistance/predictions/{prediction['prediction_id']}/feedback"
    feedback_body = json.dumps(feedback, separators=(",", ":")).encode()

    denied = client.post(
        path,
        content=feedback_body,
        headers=signed_headers("POST", path, feedback_body, user_id="other-user"),
    )
    assert denied.status_code == 404

    accepted = client.post(
        path,
        content=feedback_body,
        headers=signed_headers("POST", path, feedback_body),
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "recorded"
    rows = main._ASSISTANCE_STORE.training_records(TENANT)
    assert len(rows) == 1
    assert rows[0]["needed_assistance"] is True


def test_model_failure_is_sanitized_503(assistance_app, monkeypatch):
    main, _ = assistance_app

    class BrokenPredictor:
        def predict(self, tenant_id, signals, context=None):
            raise AssistanceModelError("leak-marker model path")

    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", BrokenPredictor())
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post("/assistance/predict", json=payload())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ASSISTANCE_MODEL_UNAVAILABLE"
    assert "leak-marker" not in response.text


def test_invalid_signals_are_rejected(assistance_app):
    _, client = assistance_app
    request = payload()
    request["signals"]["form_completion_ratio"] = 1.1

    response = client.post("/assistance/predict", json=request)

    assert response.status_code == 422


def test_required_assistance_model_is_reported_in_readiness(assistance_app, monkeypatch):
    main, _ = assistance_app
    monkeypatch.setenv("AGENT_REQUIRE_ASSISTANCE_MODEL", "true")

    class NotReady:
        def is_ready(self):
            return False

    monkeypatch.setattr(main, "_ASSISTANCE_PREDICTOR", NotReady())

    response = TestClient(main.app).get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["dependencies"]["assistance_model"] is False
