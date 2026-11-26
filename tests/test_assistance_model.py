import math

import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from agent.assistance.features import AssistanceSignals
from agent.assistance.model import (
    AssistanceModelError,
    fit_assistance_model,
    generate_bootstrap_records,
    load_bundle,
    save_bundle,
    write_active_manifest,
)
from agent.assistance.predictor import AssistancePredictor


def signals(**overrides):
    values = {
        "module": "order",
        "workflow": "create_order",
        "session_duration_seconds": 300,
        "active_seconds": 240,
        "idle_seconds": 60,
        "event_count": 20,
        "error_count": 0,
        "unique_error_count": 0,
        "repeated_error_count": 0,
        "retry_count": 0,
        "back_navigation_count": 0,
        "context_switch_count": 0,
        "validation_failure_count": 0,
        "permission_denied_count": 0,
        "form_completion_ratio": 0.98,
        "help_open_count": 0,
        "previous_task_count": 50,
        "previous_success_rate": 0.96,
        "recent_assistance_count": 0,
        "explicit_help_request": False,
        "high_risk_action": False,
    }
    values.update(overrides)
    return AssistanceSignals(**values)


@pytest.fixture(scope="module")
def trained_model():
    records = generate_bootstrap_records(n=2400, seed=42)
    return fit_assistance_model(
        records,
        model_version="test-v1",
        training_source="operator_feedback",
        min_samples=500,
        min_per_class=50,
        seed=42,
    )


def test_training_produces_calibrated_probabilities_and_metrics(trained_model):
    bundle, metrics = trained_model

    assert bundle["feature_version"] == 2
    assert 0 < bundle["threshold"] < 1
    assert metrics["roc_auc"] >= 0.7
    assert metrics["precision"] >= 0.60
    assert metrics["recall"] >= 0.5
    assert metrics["brier"] <= 0.2

    predictor = AssistancePredictor(bundle=bundle)
    low = predictor.predict("tenant-1", signals())
    high = predictor.predict(
        "tenant-1",
        signals(
            session_duration_seconds=1200,
            active_seconds=500,
            idle_seconds=700,
            event_count=80,
            error_count=12,
            unique_error_count=4,
            repeated_error_count=9,
            retry_count=11,
            back_navigation_count=8,
            validation_failure_count=7,
            form_completion_ratio=0.25,
            help_open_count=4,
            previous_success_rate=0.55,
            explicit_help_request=True,
        ),
    )
    assert math.isfinite(low.probability)
    assert 0 <= low.probability <= 1
    assert high.probability > low.probability
    assert high.decision in {"OFFER_ASSISTANCE", "DO_NOT_INTERRUPT"}


def test_unknown_categories_are_predictable(trained_model):
    bundle, _ = trained_model
    predictor = AssistancePredictor(bundle=bundle)

    result = predictor.predict(
        "tenant-1", signals(module="new_module", workflow="never_seen_workflow")
    )

    assert 0 <= result.probability <= 1
    assert result.feature_version == 2


def test_feature_version_mismatch_is_rejected(trained_model, tmp_path):
    bundle, _ = trained_model
    invalid = dict(bundle)
    invalid["feature_version"] = 999
    path = tmp_path / "model.joblib"
    save_bundle(path, invalid)

    with pytest.raises(AssistanceModelError):
        load_bundle(path)


def test_tenant_model_activation_is_isolated(trained_model, tmp_path):
    bundle, _ = trained_model
    tenant_bundle = dict(bundle, model_version="tenant-v1", tenant_id="tenant-a")
    bootstrap = dict(bundle, model_version="bootstrap-v2", tenant_id=None)
    predictor = AssistancePredictor(model_dir=tmp_path, bundle=bootstrap)
    save_bundle(tmp_path / "tenant-a" / "tenant-v1.joblib", tenant_bundle)
    write_active_manifest(tmp_path / "active.json", "tenant-a", "tenant-v1")

    assert predictor.predict("tenant-a", signals()).model_version == "tenant-v1"
    assert predictor.predict("tenant-b", signals()).model_version == "bootstrap-v2"


def test_packaged_bootstrap_model_is_available():
    predictor = AssistancePredictor()

    assert predictor.is_ready() is True
    result = predictor.predict("tenant-1", signals())
    assert result.model_version == "bootstrap-v2"
    assert result.training_source == "synthetic_bootstrap"
    assert 0 <= result.probability <= 1
