from __future__ import annotations

import json
import math
import os
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from joblib import dump, load
from sklearn.calibration import CalibratedClassifierCV, FrozenEstimator
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.metrics import (
    brier_score_loss,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline

from .features import FEATURE_VERSION, AssistanceSignals


class AssistanceModelError(RuntimeError):
    pass


def _as_signals(value: AssistanceSignals | dict) -> AssistanceSignals:
    return value if isinstance(value, AssistanceSignals) else AssistanceSignals(**value)


def _record_context(record: dict) -> dict:
    return {
        "role": record.get("role") or "unknown",
        "recent_events": record.get("recent_events") or [],
        "history": record.get("history_snapshot") or {},
        "server_facts": record.get("server_facts_snapshot") or {},
    }


def _base_pipeline(seed: int) -> Pipeline:
    return Pipeline(
        [
            ("features", DictVectorizer(sparse=False)),
            (
                "classifier",
                HistGradientBoostingClassifier(
                    learning_rate=0.05,
                    max_iter=80,
                    max_leaf_nodes=3,
                    min_samples_leaf=40,
                    l2_regularization=10.0,
                    early_stopping=False,
                    random_state=seed,
                ),
            ),
        ]
    )


def _split_indices(records: list[dict], labels: list[int], seed: int):
    groups = [
        str(record.get("user_id") or record.get("session_id") or f"group-{index}")
        for index, record in enumerate(records)
    ]
    if len(set(groups)) < 5:
        raise AssistanceModelError("at least five independent sessions are required")
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
    folds = list(splitter.split([[0] for _ in records], labels, groups))
    test_indices = set(folds[0][1].tolist())
    calibration_indices = set(folds[1][1].tolist())
    train_indices = [
        index
        for index in range(len(records))
        if index not in test_indices | calibration_indices
    ]
    return train_indices, sorted(calibration_indices), sorted(test_indices)


def _select_threshold(
    probabilities: list[float],
    labels: list[int],
    false_positive_cost: float,
    false_negative_cost: float,
) -> float:
    eligible = []
    for value in range(30, 91):
        threshold = value / 100
        predictions = [int(probability >= threshold) for probability in probabilities]
        if not any(predictions):
            continue
        precision = precision_score(labels, predictions, zero_division=0)
        recall = recall_score(labels, predictions, zero_division=0)
        if precision < 0.70 or recall < 0.50:
            continue
        false_positives = sum(
            1 for label, pred in zip(labels, predictions, strict=True) if label == 0 and pred == 1
        )
        false_negatives = sum(
            1 for label, pred in zip(labels, predictions, strict=True) if label == 1 and pred == 0
        )
        cost = (
            false_positives * false_positive_cost
            + false_negatives * false_negative_cost
        )
        f1 = f1_score(labels, predictions, zero_division=0)
        eligible.append((cost, -f1, -precision, -abs(threshold - 0.65), threshold))
    if not eligible:
        raise AssistanceModelError(
            "calibration data cannot meet precision, recall and cost constraints"
        )
    return min(eligible)[4]


def _metric_summary(
    labels: list[int],
    probabilities: list[float],
    predictions: list[int],
    false_positive_cost: float,
    false_negative_cost: float,
) -> dict[str, float | int]:
    false_positives = sum(
        1
        for label, pred in zip(labels, predictions, strict=True)
        if label == 0 and pred == 1
    )
    false_negatives = sum(
        1
        for label, pred in zip(labels, predictions, strict=True)
        if label == 1 and pred == 0
    )
    return {
        "precision": float(precision_score(labels, predictions, zero_division=0)),
        "recall": float(recall_score(labels, predictions, zero_division=0)),
        "f1": float(f1_score(labels, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(labels, probabilities)),
        "brier": float(brier_score_loss(labels, probabilities)),
        "expected_cost": float(
            (false_positives * false_positive_cost + false_negatives * false_negative_cost)
            / max(len(labels), 1)
        ),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "support": len(labels),
    }


def _slice_metrics(
    records: list[dict],
    indices: list[int],
    labels: list[int],
    probabilities: list[float],
    predictions: list[int],
    false_positive_cost: float,
    false_negative_cost: float,
) -> dict[str, dict[str, dict[str, float | int]]]:
    slices: dict[str, dict[str, list[int]]] = {
        "by_tenant": {},
        "by_role": {},
        "by_module": {},
        "by_workflow": {},
        "by_risk": {},
        "by_source": {},
        "by_period": {},
    }
    created_dates = []
    for record in records:
        value = record.get("created_at")
        if isinstance(value, datetime):
            created_dates.append(value if value.tzinfo else value.replace(tzinfo=UTC))
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                created_dates.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
            except ValueError:
                pass
    latest = max(created_dates, default=None)
    for position, index in enumerate(indices):
        record = records[index]
        signal = record["signals"]
        created = record.get("created_at")
        if isinstance(created, datetime):
            created_date = created if created.tzinfo else created.replace(tzinfo=UTC)
        elif isinstance(created, str):
            try:
                created_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created_date.tzinfo is None:
                    created_date = created_date.replace(tzinfo=UTC)
            except ValueError:
                created_date = None
        else:
            created_date = None
        if created_date is None or latest is None:
            period = "unknown"
        else:
            period = "recent_7d" if latest - created_date <= timedelta(days=7) else "older"
        keys = {
            "by_tenant": str(record.get("tenant_id") or "unknown"),
            "by_role": str(record.get("role") or "unknown"),
            "by_module": str(signal.get("module") or "unknown"),
            "by_workflow": str(signal.get("workflow") or "unknown"),
            "by_risk": "high" if signal.get("high_risk_action") else "normal",
            "by_source": str(record.get("source") or "unknown"),
            "by_period": period,
        }
        for slice_name, key in keys.items():
            slices[slice_name].setdefault(key, []).append(position)
    result: dict[str, dict[str, dict[str, float | int]]] = {}
    for slice_name, groups in slices.items():
        result[slice_name] = {}
        for key, positions in groups.items():
            slice_labels = [labels[position] for position in positions]
            if len(positions) < 10 or len(set(slice_labels)) < 2:
                continue
            slice_probabilities = [probabilities[position] for position in positions]
            slice_predictions = [predictions[position] for position in positions]
            result[slice_name][key] = _metric_summary(
                slice_labels,
                slice_probabilities,
                slice_predictions,
                false_positive_cost,
                false_negative_cost,
            )
    return result


def fit_assistance_model(
    records: list[dict],
    *,
    model_version: str,
    training_source: str,
    tenant_id: str | None = None,
    min_samples: int = 200,
    min_per_class: int = 30,
    false_positive_cost: float = 1.0,
    false_negative_cost: float = 2.0,
    seed: int = 42,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if training_source not in {"operator_feedback", "synthetic_bootstrap"}:
        raise AssistanceModelError("unsupported training source")
    if false_positive_cost <= 0 or false_negative_cost <= 0:
        raise AssistanceModelError("error costs must be positive")
    if len(records) < min_samples:
        raise AssistanceModelError("not enough labeled records")
    labels = [int(bool(record["needed_assistance"])) for record in records]
    if min(labels.count(0), labels.count(1)) < min_per_class:
        raise AssistanceModelError("not enough examples in each class")

    features = [
        _as_signals(record["signals"]).feature_dict(_record_context(record))
        for record in records
    ]
    train_indices, calibration_indices, test_indices = _split_indices(records, labels, seed)
    train_features = [features[index] for index in train_indices]
    train_labels = [labels[index] for index in train_indices]
    calibration_features = [features[index] for index in calibration_indices]
    calibration_labels = [labels[index] for index in calibration_indices]
    test_features = [features[index] for index in test_indices]
    test_labels = [labels[index] for index in test_indices]
    if len(set(calibration_labels)) < 2 or len(set(test_labels)) < 2:
        raise AssistanceModelError("calibration and test splits must contain both classes")

    base = _base_pipeline(seed)
    base.fit(train_features, train_labels)
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="sigmoid")
    calibrated.fit(calibration_features, calibration_labels)
    calibration_probabilities = calibrated.predict_proba(calibration_features)[:, 1].tolist()
    threshold = _select_threshold(
        calibration_probabilities,
        calibration_labels,
        false_positive_cost,
        false_negative_cost,
    )
    test_probabilities = calibrated.predict_proba(test_features)[:, 1].tolist()
    test_predictions = [int(probability >= threshold) for probability in test_probabilities]
    metrics: dict[str, Any] = {
        **_metric_summary(
            test_labels,
            test_probabilities,
            test_predictions,
            false_positive_cost,
            false_negative_cost,
        ),
        "threshold": threshold,
        "false_positive_cost": float(false_positive_cost),
        "false_negative_cost": float(false_negative_cost),
        "samples": len(records),
        "positives": sum(labels),
        "train_samples": len(train_indices),
        "calibration_samples": len(calibration_indices),
        "test_samples": len(test_indices),
        "baseline_positive_rate": sum(labels) / len(labels),
        "baseline_probability_mean": sum(test_probabilities) / len(test_probabilities),
        "slice_metrics": _slice_metrics(
            records,
            test_indices,
            test_labels,
            test_probabilities,
            test_predictions,
            false_positive_cost,
            false_negative_cost,
        ),
    }
    if metrics["roc_auc"] < 0.70 or metrics["precision"] < 0.60 or metrics["recall"] < 0.50:
        raise AssistanceModelError(f"model quality gates failed: {metrics}")
    if metrics["brier"] > 0.20:
        raise AssistanceModelError("model calibration quality gate failed")

    bundle = {
        "model_version": model_version,
        "feature_version": FEATURE_VERSION,
        "training_source": training_source,
        "tenant_id": tenant_id,
        "synthetic_not_production_validated": training_source == "synthetic_bootstrap",
        "trained_at": datetime.now(UTC).isoformat(),
        "threshold": threshold,
        "false_positive_cost": float(false_positive_cost),
        "false_negative_cost": float(false_negative_cost),
        "baseline_probability_mean": metrics["baseline_probability_mean"],
        "baseline_positive_rate": metrics["baseline_positive_rate"],
        "dataset": {
            "samples": len(records),
            "positives": sum(labels),
            "negatives": len(records) - sum(labels),
        },
        "metrics": metrics,
        "pipeline": calibrated,
    }
    return bundle, metrics


def _bootstrap_events(
    rng: random.Random,
    duration_ms: int,
    error_count: int,
    retry_count: int,
    back_count: int,
    validation_failures: int,
    help_open: int,
) -> list[dict]:
    groups = [["PAGE_VIEW", "FORM_EDIT"]]
    groups.append(["API_ERROR"] * min(error_count, 6))
    groups.append(["VALIDATION_ERROR"] * min(validation_failures, 3))
    groups.append(["RETRY"] * min(retry_count, 6))
    groups.append(["BACK_NAVIGATION"] * min(back_count, 4))
    groups.append(["HELP_OPEN"] * min(help_open, 3))
    groups.append(["TASK_SUCCESS"])
    types = [item for group in groups for item in group]
    events = []
    for index, event_type in enumerate(types):
        offset = int((index + 1) / (len(types) + 1) * duration_ms)
        is_error = event_type in {"API_ERROR", "VALIDATION_ERROR"}
        events.append(
            {
                "type": event_type,
                "offset_ms": offset,
                "duration_ms": rng.randint(0, 3000) if event_type == "IDLE" else 0,
                "severity": rng.randint(1, 3) if is_error else 0,
                "error_code": (
                    rng.choice(["SERVER_ERROR", "TIMEOUT", "NETWORK", "BUSINESS_REJECTION"])
                    if event_type == "API_ERROR"
                    else "VALIDATION"
                    if event_type == "VALIDATION_ERROR"
                    else None
                ),
            }
        )
    return events


def generate_bootstrap_records(n: int = 2400, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    contexts = [
        ("order", "create_order"),
        ("order", "cancel_order"),
        ("inventory", "adjust_stock"),
        ("purchase", "create_purchase_order"),
        ("supplier", "review_supplier"),
        ("finance", "create_invoice"),
        ("warehouse", "create_outbound_order"),
        ("system", "assign_role"),
    ]
    records = []
    for index in range(n):
        module, workflow = rng.choice(contexts)
        role = rng.choice(["employee", "employee", "manager", "finance", "admin"])
        difficulty = rng.betavariate(1.4, 3.2)
        if rng.random() < 0.18:
            difficulty = max(difficulty, rng.uniform(0.65, 0.95))
        error_count = min(25, int(rng.expovariate(1 / (0.12 + 5.0 * difficulty**2))))
        retry_count = min(30, int(rng.expovariate(1 / (0.08 + 6.0 * difficulty**2))))
        back_count = min(20, int(rng.expovariate(1 / (0.05 + 4.0 * difficulty**2))))
        unique_errors = min(error_count, int(error_count * rng.uniform(0.2, 0.7)))
        repeated_errors = min(error_count, int(error_count * rng.uniform(0.25, 0.9)))
        validation_failures = min(20, int(rng.expovariate(1 / (0.05 + 3.0 * difficulty**2))))
        permission_denied = int(rng.random() < 0.03 + 0.18 * difficulty)
        help_open = 0
        if rng.random() < 0.02 + 0.24 * difficulty:
            help_open = rng.randint(1, 4)
        explicit_help = rng.random() < 0.01 + 0.28 * difficulty
        completion = max(
            0.05,
            min(1.0, rng.betavariate(2 + 7 * (1 - difficulty), 1 + 4 * difficulty)),
        )
        previous_success = max(
            0.3, min(1.0, 0.97 - 0.38 * difficulty + rng.gauss(0, 0.04))
        )
        duration = rng.randint(120, 1200)
        active = int(duration * rng.uniform(0.55, 0.95))
        idle = duration - active
        event_count = (
            max(error_count, retry_count, back_count, validation_failures, help_open)
            + rng.randint(8, 35)
        )
        recent_events = _bootstrap_events(
            rng,
            duration * 1000,
            error_count,
            retry_count,
            back_count,
            validation_failures,
            help_open,
        )
        predictions_7d = rng.randint(0, 4) + int(difficulty > 0.6)
        interventions_7d = min(predictions_7d, rng.randint(0, predictions_7d))
        needed_7d = min(interventions_7d, rng.randint(0, interventions_7d))
        history = {
            "predictions_7d": predictions_7d,
            "interventions_7d": interventions_7d,
            "needed_feedback_7d": needed_7d,
            "assistance_acceptance_rate_7d": (
                needed_7d / interventions_7d if interventions_7d else 0.0
            ),
            "seconds_since_last_prediction": rng.randint(60, 604800),
        }
        server_errors = int(rng.random() < 0.05 + 0.25 * difficulty)
        server_facts = {
            "server_outcome_count": rng.randint(0, 3),
            "server_error_count": server_errors,
            "server_timeout_count": int(rng.random() < 0.02 + 0.08 * difficulty),
            "server_rejection_count": int(rng.random() < 0.03 + 0.12 * difficulty),
            "server_failed_count": int(rng.random() < 0.03 + 0.18 * difficulty),
            "server_p95_latency_ms": rng.randint(100, 1500) + int(difficulty * 2500),
        }
        observed_score = (
            0.55 * math.log1p(error_count)
            + 0.45 * math.log1p(retry_count)
            + 0.35 * math.log1p(back_count)
            + 0.30 * math.log1p(validation_failures)
            + 0.80 * int(explicit_help)
            + 0.50 * math.log1p(help_open)
            + 0.45 * permission_denied
            + 0.90 * (1 - completion)
            + 0.35 * (1 - previous_success)
            + 0.30 * math.log1p(server_errors)
            + 0.20 * min(len(recent_events), 20) / 20
        )
        latent = -3.0 + 1.25 * observed_score + rng.gauss(0, 0.25)
        probability = 1 / (1 + math.exp(-latent))
        records.append(
            {
                "session_id": f"bootstrap-{index}",
                "tenant_id": "synthetic",
                "created_at": (
                    datetime(2026, 11, 25, tzinfo=UTC) - timedelta(days=index % 28)
                ).isoformat(),
                "role": role,
                "recent_events": recent_events,
                "history_snapshot": history,
                "server_facts_snapshot": server_facts,
                "signals": {
                    "module": module,
                    "workflow": workflow,
                    "session_duration_seconds": duration,
                    "active_seconds": active,
                    "idle_seconds": idle,
                    "event_count": event_count,
                    "error_count": error_count,
                    "unique_error_count": unique_errors,
                    "repeated_error_count": repeated_errors,
                    "retry_count": retry_count,
                    "back_navigation_count": back_count,
                    "context_switch_count": rng.randint(0, 8),
                    "validation_failure_count": validation_failures,
                    "permission_denied_count": permission_denied,
                    "form_completion_ratio": completion,
                    "help_open_count": help_open,
                    "previous_task_count": rng.randint(0, 200),
                    "previous_success_rate": previous_success,
                    "recent_assistance_count": rng.randint(0, 3),
                    "explicit_help_request": explicit_help,
                    "high_risk_action": rng.random() < 0.15,
                },
                "needed_assistance": rng.random() < probability,
            }
        )
    return records


def save_bundle(path: str | Path, bundle: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        dump(bundle, temporary, compress=3)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load_bundle(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        if not source.is_file():
            raise AssistanceModelError("model artifact is missing")
        bundle = load(source)
        if not isinstance(bundle, dict):
            raise AssistanceModelError("model artifact has an invalid format")
        if bundle.get("feature_version") != FEATURE_VERSION:
            raise AssistanceModelError("model feature version is unsupported")
        model_version = bundle.get("model_version")
        if not isinstance(model_version, str) or not re.fullmatch(
            r"bootstrap-v[0-9]+|tenant-[A-Za-z0-9_-]{1,100}", model_version
        ):
            raise AssistanceModelError("model version is invalid")
        if bundle.get("training_source") not in {"operator_feedback", "synthetic_bootstrap"}:
            raise AssistanceModelError("model training source is invalid")
        tenant_id = bundle.get("tenant_id")
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant_id)
        ):
            raise AssistanceModelError("model tenant is invalid")
        raw_threshold = bundle.get("threshold")
        if not isinstance(raw_threshold, (int, float, str)):
            raise AssistanceModelError("model threshold is invalid")
        threshold = float(raw_threshold)
        if not math.isfinite(threshold) or not 0 < threshold < 1:
            raise AssistanceModelError("model threshold is invalid")
        if "pipeline" not in bundle:
            raise AssistanceModelError("model pipeline is missing")
        return bundle
    except AssistanceModelError:
        raise
    except Exception as exc:
        raise AssistanceModelError("model artifact could not be loaded") from exc


def load_active_manifest_document(path: str | Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload.get("models"), dict):
            raise AssistanceModelError("active model manifest is invalid")
        history = payload.get("history", {})
        if not isinstance(history, dict):
            raise AssistanceModelError("active model history is invalid")
        return {"models": payload["models"], "history": history}
    except AssistanceModelError:
        raise
    except Exception as exc:
        raise AssistanceModelError("active model manifest could not be loaded") from exc


def load_active_manifest(path: str | Path) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in load_active_manifest_document(path)["models"].items()
    }


def _write_manifest(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def promote_active_model(path: str | Path, tenant_id: str, model_version: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant_id):
        raise AssistanceModelError("tenant key is invalid")
    if not re.fullmatch(r"tenant-[A-Za-z0-9_-]{1,100}", model_version):
        raise AssistanceModelError("model version is invalid")
    target = Path(path)
    document = (
        load_active_manifest_document(target)
        if target.exists()
        else {"models": {}, "history": {}}
    )
    history = document["history"].setdefault(tenant_id, [])
    current = document["models"].get(tenant_id)
    if current and current not in history:
        history.append(current)
    history.append(model_version)
    document["models"][tenant_id] = model_version
    _write_manifest(target, document)


def rollback_active_model(path: str | Path, tenant_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant_id):
        raise AssistanceModelError("tenant key is invalid")
    target = Path(path)
    document = load_active_manifest_document(target)
    history = document["history"].get(tenant_id, [])
    if len(history) < 2:
        raise AssistanceModelError("no previous model is available")
    history.pop()
    document["models"][tenant_id] = history[-1]
    _write_manifest(target, document)
    return history[-1]


def write_active_manifest(path: str | Path, tenant_id: str, model_version: str) -> None:
    promote_active_model(path, tenant_id, model_version)
