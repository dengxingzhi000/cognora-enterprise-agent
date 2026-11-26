from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .features import AssistanceSignals
from .model import AssistanceModelError, load_active_manifest, load_bundle

_TENANT_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MODEL_VERSION = re.compile(r"tenant-[A-Za-z0-9_-]{1,100}")


@dataclass(frozen=True)
class AssistancePrediction:
    probability: float
    threshold: float
    decision: str
    suggested_action: str
    model_version: str
    feature_version: int
    training_source: str
    probability_mean_delta: float = 0.0


class AssistancePredictor:
    def __init__(
        self,
        model_dir: str | Path | None = None,
        bootstrap_path: str | Path | None = None,
        bundle: dict[str, Any] | None = None,
    ):
        self._model_dir = Path(model_dir) if model_dir else None
        self._bootstrap_path = (
            Path(bootstrap_path)
            if bootstrap_path
            else Path(str(files("agent.assistance").joinpath("models/bootstrap-v2.joblib")))
        )
        self._fixed_bundle = bundle
        self._bootstrap: dict[str, Any] | None = None
        self._tenant_models: dict[tuple[str, str], dict[str, Any]] = {}
        self._manifest_signature: tuple[int, int] | None = None
        self._manifest: dict[str, str] = {}
        self._live_stats: dict[str, dict[str, float | int]] = {}
        self._lock = threading.Lock()

    def _load_bootstrap(self) -> dict[str, Any]:
        with self._lock:
            if self._fixed_bundle is not None:
                return self._fixed_bundle
            if self._bootstrap is None:
                self._bootstrap = load_bundle(self._bootstrap_path)
            return self._bootstrap

    def _active_models(self) -> dict[str, str]:
        if self._model_dir is None:
            return {}
        manifest = self._model_dir / "active.json"
        try:
            stat = manifest.stat()
        except FileNotFoundError:
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if signature != self._manifest_signature:
                self._manifest = load_active_manifest(manifest)
                self._manifest_signature = signature
            return dict(self._manifest)

    def bundle_for(self, tenant_id: str) -> dict[str, Any]:
        version = self._active_models().get(tenant_id)
        if version is None:
            return self._load_bootstrap()
        if not _TENANT_KEY.fullmatch(tenant_id) or not _MODEL_VERSION.fullmatch(version):
            raise AssistanceModelError("active model reference is invalid")
        key = (tenant_id, version)
        if self._model_dir is None:
            raise AssistanceModelError("tenant model directory is not configured")
        with self._lock:
            if key not in self._tenant_models:
                bundle = load_bundle(self._model_dir / tenant_id / f"{version}.joblib")
                if bundle.get("tenant_id") != tenant_id or bundle.get("model_version") != version:
                    raise AssistanceModelError("active model metadata does not match its tenant")
                self._tenant_models[key] = bundle
            return self._tenant_models[key]

    def is_ready(self) -> bool:
        try:
            self._load_bootstrap()
            return True
        except AssistanceModelError:
            return False

    def predict(
        self,
        tenant_id: str,
        signals: AssistanceSignals,
        context: dict | None = None,
    ) -> AssistancePrediction:
        bundle = self.bundle_for(tenant_id)
        try:
            probability = float(
                bundle["pipeline"].predict_proba([signals.feature_dict(context or {})])[0, 1]
            )
        except Exception as exc:
            raise AssistanceModelError("model inference failed") from exc
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise AssistanceModelError("model returned an invalid probability")
        threshold = float(bundle["threshold"])
        intervene = probability >= threshold
        model_version = str(bundle["model_version"])
        with self._lock:
            stats = self._live_stats.setdefault(model_version, {"count": 0, "mean": 0.0})
            count = int(stats["count"]) + 1
            mean = float(stats["mean"]) + (probability - float(stats["mean"])) / count
            self._live_stats[model_version] = {"count": count, "mean": mean}
        baseline = float(bundle.get("baseline_probability_mean", 0.0))
        delta = mean - baseline if count else 0.0
        return AssistancePrediction(
            probability=round(probability, 6),
            threshold=threshold,
            decision="OFFER_ASSISTANCE" if intervene else "DO_NOT_INTERRUPT",
            suggested_action="OPEN_ASSISTANT" if intervene else "CONTINUE_WITHOUT_ASSISTANCE",
            model_version=model_version,
            feature_version=int(bundle["feature_version"]),
            training_source=str(bundle["training_source"]),
            probability_mean_delta=delta,
        )

    def status(self, tenant_id: str) -> dict:
        bundle = self.bundle_for(tenant_id)
        model_version = str(bundle["model_version"])
        with self._lock:
            live = dict(self._live_stats.get(model_version, {"count": 0, "mean": 0.0}))
        baseline = float(bundle.get("baseline_probability_mean", 0.0))
        live_mean = float(live["mean"])
        delta = live_mean - baseline if live["count"] else 0.0
        return {
            "model_version": model_version,
            "feature_version": int(bundle["feature_version"]),
            "training_source": str(bundle["training_source"]),
            "tenant_id": bundle.get("tenant_id"),
            "trained_at": bundle.get("trained_at"),
            "threshold": float(bundle["threshold"]),
            "false_positive_cost": float(bundle.get("false_positive_cost", 1.0)),
            "false_negative_cost": float(bundle.get("false_negative_cost", 2.0)),
            "baseline_positive_rate": float(bundle.get("baseline_positive_rate", 0.0)),
            "baseline_probability_mean": baseline,
            "live_count": int(live["count"]),
            "live_probability_mean": live_mean,
            "probability_mean_delta": delta,
            "drift_warning": int(live["count"]) >= 100 and abs(delta) >= 0.10,
            "dataset": bundle.get("dataset", {}),
            "metrics": bundle.get("metrics", {}),
        }
