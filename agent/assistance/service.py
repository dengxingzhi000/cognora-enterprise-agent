from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from observability import metrics

from .features import AssistanceFeedbackRequest, AssistanceOutcomeRequest, AssistancePredictRequest
from .knowledge import AssistanceKnowledgeService
from .model import AssistanceModelError
from .predictor import AssistancePredictor
from .store import InMemoryAssistanceStore, PostgresAssistanceStore


class AssistancePredictionNotFound(LookupError):
    pass


class AssistanceKnowledgeNotFound(LookupError):
    pass


class AssistanceStoreError(RuntimeError):
    pass


class AssistanceService:
    def __init__(
        self,
        store: InMemoryAssistanceStore | PostgresAssistanceStore,
        predictor: AssistancePredictor,
        knowledge: AssistanceKnowledgeService | None = None,
    ):
        self._store = store
        self._predictor = predictor
        self._knowledge = knowledge or AssistanceKnowledgeService()

    def predict(
        self,
        tenant_id: str,
        user_id: str,
        role: str,
        request: AssistancePredictRequest,
    ) -> dict:
        started = time.perf_counter()
        try:
            history = self._store.history_summary(
                tenant_id, user_id, request.signals.module, request.signals.workflow
            )
            server_facts = self._store.server_facts(tenant_id, user_id, request.session_id)
        except Exception as exc:
            raise AssistanceStoreError("assistance context could not be read") from exc
        recent_events = [item.model_dump() for item in request.recent_events]
        context = {
            "role": role,
            "recent_events": recent_events,
            "history": history,
            "server_facts": server_facts,
        }
        try:
            result = self._predictor.predict(tenant_id, request.signals, context)
        except AssistanceModelError:
            raise
        knowledge = []
        if result.decision == "OFFER_ASSISTANCE" or request.signals.explicit_help_request:
            try:
                knowledge = self._knowledge.recommend(
                    tenant_id,
                    role,
                    request.signals,
                    recent_events,
                    server_facts,
                )
            except Exception:
                knowledge = []
        prediction_id = str(uuid4())
        created_at = datetime.now(UTC).isoformat()
        record = {
            "prediction_id": prediction_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": role,
            "session_id": request.session_id,
            "signals": request.signals.model_dump(),
            "recent_events": context["recent_events"],
            "history_snapshot": history,
            "server_facts_snapshot": server_facts,
            "knowledge_snapshot": knowledge,
            "probability": result.probability,
            "threshold": result.threshold,
            "decision": result.decision,
            "model_version": result.model_version,
            "feature_version": result.feature_version,
            "created_at": created_at,
        }
        try:
            self._store.save_prediction(record)
        except Exception as exc:
            raise AssistanceStoreError("prediction could not be stored") from exc
        metrics.inc_assistance_prediction(result.decision)
        metrics.observe_assistance_prediction(
            result.probability, time.perf_counter() - started
        )
        metrics.set_assistance_drift(result.probability_mean_delta)
        if knowledge:
            metrics.inc_assistance_knowledge_recommendation(len(knowledge))
        return {
            "prediction_id": prediction_id,
            "probability": result.probability,
            "threshold": result.threshold,
            "decision": result.decision,
            "suggested_action": result.suggested_action,
            "model_version": result.model_version,
            "feature_version": result.feature_version,
            "training_source": result.training_source,
            "created_at": created_at,
            "requires_human_confirmation": request.signals.high_risk_action,
            "knowledge": knowledge,
            "knowledge_count": len(knowledge),
            "context_summary": {
                "recent_events": len(request.recent_events),
                "server_outcomes": server_facts["server_outcome_count"],
                "history_predictions_7d": history["predictions_7d"],
            },
        }

    def feedback(
        self,
        tenant_id: str,
        user_id: str,
        prediction_id: str,
        request: AssistanceFeedbackRequest,
    ) -> dict:
        try:
            prediction = self._store.get_prediction(prediction_id, tenant_id, user_id)
        except Exception as exc:
            raise AssistanceStoreError("prediction could not be read") from exc
        if prediction is None:
            raise AssistancePredictionNotFound
        if request.knowledge_article_id and not any(
            item.get("article_id") == request.knowledge_article_id
            for item in prediction.get("knowledge_snapshot") or []
        ):
            raise AssistanceKnowledgeNotFound
        record = {
            "prediction_id": prediction_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "needed_assistance": request.needed_assistance,
            "assistance_used": request.assistance_used,
            "task_succeeded": request.task_succeeded,
            "source": request.source,
            "knowledge_helpful": request.knowledge_helpful,
            "knowledge_article_id": request.knowledge_article_id,
        }
        try:
            self._store.save_feedback(record)
        except Exception as exc:
            raise AssistanceStoreError("feedback could not be stored") from exc
        metrics.inc_assistance_feedback(request.source)
        if request.knowledge_helpful is not None:
            metrics.inc_assistance_knowledge_feedback(request.knowledge_helpful)
        return {"prediction_id": prediction_id, "status": "recorded"}

    def record_outcome(
        self,
        tenant_id: str,
        user_id: str,
        request: AssistanceOutcomeRequest,
    ) -> dict:
        if request.prediction_id:
            try:
                prediction = self._store.get_prediction(
                    request.prediction_id, tenant_id, user_id
                )
            except Exception as exc:
                raise AssistanceStoreError("prediction could not be read") from exc
            if (
                prediction is None
                or prediction["session_id"] != request.session_id
                or prediction["signals"].get("module") != request.module
                or prediction["signals"].get("workflow") != request.workflow
            ):
                raise AssistancePredictionNotFound
        outcome_id = str(uuid4())
        occurred_at = request.occurred_at or datetime.now(UTC)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        else:
            occurred_at = occurred_at.astimezone(UTC)
        record = {
            "outcome_id": outcome_id,
            "prediction_id": request.prediction_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": request.session_id,
            "module": request.module,
            "workflow": request.workflow,
            "outcome": request.outcome,
            "error_code": request.error_code,
            "latency_ms": request.latency_ms,
            "occurred_at": occurred_at,
        }
        try:
            self._store.save_outcome(record)
        except Exception as exc:
            raise AssistanceStoreError("outcome could not be stored") from exc
        return {"outcome_id": outcome_id, "status": "recorded"}

    def record_prediction_outcome(
        self,
        tenant_id: str,
        user_id: str,
        prediction_id: str,
        outcome: Literal["TASK_COMPLETED", "TASK_FAILED", "TASK_ABANDONED", "REPEATED_FAILURE"],
        latency_ms: int,
    ) -> dict:
        try:
            prediction = self._store.get_prediction(prediction_id, tenant_id, user_id)
        except Exception as exc:
            raise AssistanceStoreError("prediction could not be read") from exc
        if prediction is None:
            raise AssistancePredictionNotFound
        request = AssistanceOutcomeRequest(
            prediction_id=prediction_id,
            session_id=prediction["session_id"],
            module=prediction["signals"]["module"],
            workflow=prediction["signals"]["workflow"],
            outcome=outcome,
            error_code=None,
            latency_ms=latency_ms,
        )
        return self.record_outcome(tenant_id, user_id, request)

    def model_status(self, tenant_id: str) -> dict:
        try:
            return self._predictor.status(tenant_id)
        except AssistanceModelError:
            raise

    def dashboard_data(self, tenant_id: str) -> dict:
        try:
            coverage = self._store.coverage_summary(tenant_id)
            usage = self._store.knowledge_usage(tenant_id)
            model = self._predictor.status(tenant_id)
        except AssistanceModelError:
            raise
        except Exception as exc:
            raise AssistanceStoreError("assistance dashboard data could not be read") from exc
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "model": model,
            "coverage": coverage,
            "knowledge": {
                "top_articles": usage[:10],
                "helpful_rate": coverage["knowledge_helpful_rate"],
            },
        }
