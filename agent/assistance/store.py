from __future__ import annotations

import copy
import json
import math
import threading
from datetime import UTC, datetime, timedelta


def _json_load(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return None


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _outcome_label(outcome: str, error_code: str | None) -> bool | None:
    if outcome == "TASK_ABANDONED" or outcome == "REPEATED_FAILURE":
        return True
    if outcome == "TASK_COMPLETED" and error_code is None:
        return False
    return None


def _training_row(prediction: dict, label: bool, source: str) -> dict:
    return {
        "session_id": prediction["session_id"],
        "tenant_id": prediction.get("tenant_id") or "unknown",
        "user_id": prediction["user_id"],
        "role": prediction.get("role") or "unknown",
        "created_at": prediction.get("created_at"),
        "signals": copy.deepcopy(prediction["signals"]),
        "recent_events": copy.deepcopy(prediction.get("recent_events") or []),
        "history_snapshot": copy.deepcopy(prediction.get("history_snapshot") or {}),
        "server_facts_snapshot": copy.deepcopy(prediction.get("server_facts_snapshot") or {}),
        "needed_assistance": label,
        "source": source,
    }


class InMemoryAssistanceStore:
    def __init__(self):
        self._predictions: dict[str, dict] = {}
        self._feedback: dict[str, dict] = {}
        self._outcomes: dict[str, dict] = {}
        self._lock = threading.Lock()

    def save_prediction(self, record: dict) -> None:
        with self._lock:
            self._predictions[record["prediction_id"]] = copy.deepcopy(record)

    def get_prediction(self, prediction_id: str, tenant_id: str, user_id: str) -> dict | None:
        with self._lock:
            record = self._predictions.get(prediction_id)
            if record is None or record["tenant_id"] != tenant_id or record["user_id"] != user_id:
                return None
            return copy.deepcopy(record)

    def save_feedback(self, record: dict) -> None:
        with self._lock:
            self._feedback[record["prediction_id"]] = copy.deepcopy(record)

    def save_outcome(self, record: dict) -> None:
        with self._lock:
            self._outcomes[record["outcome_id"]] = copy.deepcopy(record)

    def history_summary(
        self,
        tenant_id: str,
        user_id: str,
        module: str,
        workflow: str,
        now: datetime | None = None,
    ) -> dict:
        current = now or datetime.now(UTC)
        cutoff = current - timedelta(days=7)
        with self._lock:
            records = []
            for prediction in self._predictions.values():
                created = _parse_datetime(prediction.get("created_at"))
                if (
                    prediction["tenant_id"] == tenant_id
                    and prediction["user_id"] == user_id
                    and prediction["signals"].get("module") == module
                    and prediction["signals"].get("workflow") == workflow
                    and created is not None
                    and created >= cutoff
                ):
                    records.append(prediction)
            interventions = [item for item in records if item["decision"] == "OFFER_ASSISTANCE"]
            needed = sum(
                1
                for item in interventions
                if self._feedback.get(item["prediction_id"], {}).get("needed_assistance")
            )
            accepted = sum(
                1
                for item in interventions
                if self._feedback.get(item["prediction_id"], {}).get("assistance_used")
            )
            parsed_dates = [
                parsed for item in records
                if (parsed := _parse_datetime(item["created_at"])) is not None
            ]
            last = max(parsed_dates, default=None)
            return {
                "predictions_7d": len(records),
                "interventions_7d": len(interventions),
                "needed_feedback_7d": needed,
                "assistance_acceptance_rate_7d": (
                    accepted / len(interventions) if interventions else 0.0
                ),
                "seconds_since_last_prediction": (
                    max(0, int((current - last).total_seconds())) if last else 604800
                ),
            }

    def server_facts(self, tenant_id: str, user_id: str, session_id: str) -> dict:
        with self._lock:
            outcomes = [
                item
                for item in self._outcomes.values()
                if item["tenant_id"] == tenant_id
                and item["user_id"] == user_id
                and item["session_id"] == session_id
            ]
        latencies = sorted(int(item["latency_ms"]) for item in outcomes)
        p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else 0
        return {
            "server_outcome_count": len(outcomes),
            "server_error_count": sum(
                1 for item in outcomes if item["error_code"] in {"SERVER_ERROR", "NETWORK"}
            ),
            "server_timeout_count": sum(1 for item in outcomes if item["error_code"] == "TIMEOUT"),
            "server_rejection_count": sum(
                1
                for item in outcomes
                if item["error_code"] in {"BUSINESS_REJECTION", "CONFLICT", "RATE_LIMITED"}
            ),
            "server_failed_count": sum(
                1
                for item in outcomes
                if item["outcome"] in {"TASK_FAILED", "TASK_ABANDONED", "REPEATED_FAILURE"}
            ),
            "server_p95_latency_ms": p95,
        }

    def training_records(self, tenant_id: str, include_outcome_labels: bool = False) -> list[dict]:
        with self._lock:
            rows = []
            labeled: set[str] = set()
            for prediction_id, feedback in self._feedback.items():
                prediction = self._predictions.get(prediction_id)
                if prediction is None or prediction["tenant_id"] != tenant_id:
                    continue
                if (
                    feedback["tenant_id"] != tenant_id
                    or feedback["user_id"] != prediction["user_id"]
                ):
                    continue
                rows.append(
                    _training_row(
                        prediction, feedback["needed_assistance"], feedback["source"]
                    )
                )
                labeled.add(prediction_id)
            if include_outcome_labels:
                latest: dict[str, dict] = {}
                for outcome in self._outcomes.values():
                    outcome_prediction_id = outcome.get("prediction_id")
                    if (
                        not outcome_prediction_id
                        or outcome["tenant_id"] != tenant_id
                        or outcome_prediction_id in labeled
                    ):
                        continue
                    current: dict | None = latest.get(outcome_prediction_id)
                    if current is None or str(outcome["occurred_at"]) > str(current["occurred_at"]):
                        latest[outcome_prediction_id] = outcome
                for prediction_id, outcome in latest.items():
                    prediction = self._predictions.get(prediction_id)
                    if prediction is None or prediction["tenant_id"] != tenant_id:
                        continue
                    label = _outcome_label(outcome["outcome"], outcome["error_code"])
                    if label is not None:
                        rows.append(_training_row(prediction, label, "SERVER_OUTCOME"))
            return rows

    def coverage_summary(self, tenant_id: str) -> dict:
        with self._lock:
            predictions = [
                item for item in self._predictions.values() if item["tenant_id"] == tenant_id
            ]
            prediction_ids = {item["prediction_id"] for item in predictions}
            feedback = [
                item
                for item in self._feedback.values()
                if item["prediction_id"] in prediction_ids
                and item["tenant_id"] == tenant_id
            ]
            outcomes = [
                item for item in self._outcomes.values() if item["tenant_id"] == tenant_id
            ]
            knowledge_feedback = [
                item for item in feedback if item.get("knowledge_helpful") is not None
            ]
            helpful = sum(1 for item in knowledge_feedback if item.get("knowledge_helpful"))
            return {
                "predictions": len(predictions),
                "feedback": len(feedback),
                "outcomes": len(outcomes),
                "feedback_coverage": len(feedback) / len(predictions) if predictions else 0.0,
                "knowledge_feedback": len(knowledge_feedback),
                "knowledge_helpful_rate": (
                    helpful / len(knowledge_feedback) if knowledge_feedback else 0.0
                ),
            }

    def knowledge_usage(self, tenant_id: str) -> list[dict]:
        with self._lock:
            usage: dict[str, dict] = {}
            for prediction in self._predictions.values():
                if prediction["tenant_id"] != tenant_id:
                    continue
                for article in prediction.get("knowledge_snapshot") or []:
                    article_id = str(article.get("article_id") or "unknown")
                    item = usage.setdefault(
                        article_id,
                        {
                            "article_id": article_id,
                            "title": article.get("title", ""),
                            "recommendations": 0,
                        },
                    )
                    item["recommendations"] += 1
            return sorted(
                usage.values(),
                key=lambda item: (-item["recommendations"], item["article_id"]),
            )


class PostgresAssistanceStore:
    def __init__(self, connector):
        self._connector = connector

    def save_prediction(self, record: dict) -> None:
        self._connector.execute(
            "INSERT INTO agent_assistance_prediction "
            "(prediction_id, tenant_id, user_id, role, session_id, signals, recent_events, "
            " history_snapshot, server_facts_snapshot, knowledge_snapshot, probability, threshold, "
            " decision, model_version, feature_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, CAST(? AS JSONB), CAST(? AS JSONB), CAST(? AS JSONB), "
            " CAST(? AS JSONB), CAST(? AS JSONB), ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (prediction_id) DO NOTHING",
            (
                record["prediction_id"],
                record["tenant_id"],
                record["user_id"],
                record.get("role") or "unknown",
                record["session_id"],
                json.dumps(record["signals"], separators=(",", ":")),
                json.dumps(record.get("recent_events") or [], separators=(",", ":")),
                json.dumps(record.get("history_snapshot") or {}, separators=(",", ":")),
                json.dumps(record.get("server_facts_snapshot") or {}, separators=(",", ":")),
                json.dumps(record.get("knowledge_snapshot") or [], separators=(",", ":")),
                record["probability"],
                record["threshold"],
                record["decision"],
                record["model_version"],
                record["feature_version"],
                record["created_at"],
            ),
        )

    def get_prediction(self, prediction_id: str, tenant_id: str, user_id: str) -> dict | None:
        row = self._connector.fetch_one(
            "SELECT prediction_id, tenant_id, user_id, role, session_id, signals, recent_events, "
            "history_snapshot, server_facts_snapshot, knowledge_snapshot, probability, threshold, "
            "decision, model_version, feature_version, created_at "
            "FROM agent_assistance_prediction "
            "WHERE prediction_id = ? AND tenant_id = ? AND user_id = ?",
            (prediction_id, tenant_id, user_id),
        )
        if not row:
            return None
        for key in (
            "signals",
            "recent_events",
            "history_snapshot",
            "server_facts_snapshot",
            "knowledge_snapshot",
        ):
            row[key] = _json_load(row.get(key))
        return row

    def save_feedback(self, record: dict) -> None:
        self._connector.execute(
            "INSERT INTO agent_assistance_feedback "
            "(prediction_id, tenant_id, user_id, needed_assistance, assistance_used, "
            " task_succeeded, source, knowledge_helpful, knowledge_article_id, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now(), now()) "
            "ON CONFLICT (prediction_id) DO UPDATE SET "
            "needed_assistance = EXCLUDED.needed_assistance, "
            "assistance_used = EXCLUDED.assistance_used, "
            "task_succeeded = EXCLUDED.task_succeeded, "
            "source = EXCLUDED.source, "
            "knowledge_helpful = EXCLUDED.knowledge_helpful, "
            "knowledge_article_id = EXCLUDED.knowledge_article_id, updated_at = now() "
            "WHERE agent_assistance_feedback.tenant_id = EXCLUDED.tenant_id "
            "AND agent_assistance_feedback.user_id = EXCLUDED.user_id",
            (
                record["prediction_id"],
                record["tenant_id"],
                record["user_id"],
                record["needed_assistance"],
                record["assistance_used"],
                record["task_succeeded"],
                record["source"],
                record.get("knowledge_helpful"),
                record.get("knowledge_article_id"),
            ),
        )

    def save_outcome(self, record: dict) -> None:
        self._connector.execute(
            "INSERT INTO agent_assistance_outcome "
            "(outcome_id, prediction_id, tenant_id, user_id, session_id, module, workflow, "
            " outcome, error_code, latency_ms, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["outcome_id"],
                record.get("prediction_id"),
                record["tenant_id"],
                record["user_id"],
                record["session_id"],
                record["module"],
                record["workflow"],
                record["outcome"],
                record["error_code"],
                record["latency_ms"],
                record["occurred_at"],
            ),
        )

    def history_summary(
        self,
        tenant_id: str,
        user_id: str,
        module: str,
        workflow: str,
        now: datetime | None = None,
    ) -> dict:
        row = self._connector.fetch_one(
            "SELECT COUNT(*) AS predictions, "
            "COUNT(*) FILTER (WHERE p.decision = 'OFFER_ASSISTANCE') AS interventions, "
            "COUNT(f.needed_assistance) FILTER (WHERE f.needed_assistance) AS needed, "
            "COUNT(f.assistance_used) FILTER (WHERE f.assistance_used) AS accepted, "
            "MAX(p.created_at) AS last_prediction_at "
            "FROM agent_assistance_prediction p "
            "LEFT JOIN agent_assistance_feedback f ON f.prediction_id = p.prediction_id "
            "WHERE p.tenant_id = ? AND p.user_id = ? "
            "AND p.signals->>'module' = ? AND p.signals->>'workflow' = ? "
            "AND p.created_at >= now() - INTERVAL '7 days'",
            (tenant_id, user_id, module, workflow),
        ) or {}
        current = now or datetime.now(UTC)
        last = _parse_datetime(row.get("last_prediction_at"))
        interventions = int(row.get("interventions") or 0)
        return {
            "predictions_7d": int(row.get("predictions") or 0),
            "interventions_7d": interventions,
            "needed_feedback_7d": int(row.get("needed") or 0),
            "assistance_acceptance_rate_7d": (
                int(row.get("accepted") or 0) / interventions if interventions else 0.0
            ),
            "seconds_since_last_prediction": (
                max(0, int((current - last).total_seconds())) if last else 604800
            ),
        }

    def server_facts(self, tenant_id: str, user_id: str, session_id: str) -> dict:
        row = self._connector.fetch_one(
            "SELECT COUNT(*) AS outcomes, "
            "COUNT(*) FILTER (WHERE error_code IN ('SERVER_ERROR', 'NETWORK')) AS errors, "
            "COUNT(*) FILTER (WHERE error_code = 'TIMEOUT') AS timeouts, "
            "COUNT(*) FILTER (WHERE error_code IN "
            " ('BUSINESS_REJECTION', 'CONFLICT', 'RATE_LIMITED')) AS rejections, "
            "COUNT(*) FILTER (WHERE outcome IN "
            " ('TASK_FAILED', 'TASK_ABANDONED', 'REPEATED_FAILURE')) AS failed, "
            "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
            "FROM agent_assistance_outcome "
            "WHERE tenant_id = ? AND user_id = ? AND session_id = ?",
            (tenant_id, user_id, session_id),
        ) or {}
        return {
            "server_outcome_count": int(row.get("outcomes") or 0),
            "server_error_count": int(row.get("errors") or 0),
            "server_timeout_count": int(row.get("timeouts") or 0),
            "server_rejection_count": int(row.get("rejections") or 0),
            "server_failed_count": int(row.get("failed") or 0),
            "server_p95_latency_ms": int(float(row.get("p95") or 0)),
        }

    def training_records(self, tenant_id: str, include_outcome_labels: bool = False) -> list[dict]:
        rows = self._connector.fetch_all(
            "SELECT p.tenant_id, p.created_at, p.session_id, p.user_id, p.role, p.signals, "
            "p.recent_events, p.history_snapshot, p.server_facts_snapshot, "
            "f.needed_assistance, f.source "
            "FROM agent_assistance_prediction p "
            "JOIN agent_assistance_feedback f ON f.prediction_id = p.prediction_id "
            "WHERE p.tenant_id = ? AND f.tenant_id = p.tenant_id AND f.user_id = p.user_id",
            (tenant_id,),
        )
        result = [
            self._training_row(row, bool(row["needed_assistance"]), row["source"])
            for row in rows
        ]
        if include_outcome_labels:
            outcome_rows = self._connector.fetch_all(
                "SELECT p.tenant_id, p.created_at, p.session_id, p.user_id, p.role, p.signals, "
                "p.recent_events, p.history_snapshot, p.server_facts_snapshot, "
                "o.outcome, o.error_code "
                "FROM agent_assistance_prediction p "
                "JOIN LATERAL ("
                " SELECT outcome, error_code, occurred_at FROM agent_assistance_outcome "
                " WHERE prediction_id = p.prediction_id AND tenant_id = p.tenant_id "
                " AND user_id = p.user_id ORDER BY occurred_at DESC LIMIT 1"
                ") o ON TRUE "
                "WHERE p.tenant_id = ? AND NOT EXISTS ("
                " SELECT 1 FROM agent_assistance_feedback f WHERE f.prediction_id = p.prediction_id"
                ")",
                (tenant_id,),
            )
            for row in outcome_rows:
                label = _outcome_label(row["outcome"], row["error_code"])
                if label is not None:
                    result.append(self._training_row(row, label, "SERVER_OUTCOME"))
        return result

    def coverage_summary(self, tenant_id: str) -> dict:
        row = self._connector.fetch_one(
            "SELECT "
            "(SELECT COUNT(*) FROM agent_assistance_prediction "
            " WHERE tenant_id = ?) AS predictions, "
            "(SELECT COUNT(*) FROM agent_assistance_feedback WHERE tenant_id = ?) AS feedback, "
            "(SELECT COUNT(*) FROM agent_assistance_outcome WHERE tenant_id = ?) AS outcomes, "
            "(SELECT COUNT(*) FROM agent_assistance_feedback WHERE tenant_id = ? "
            " AND knowledge_helpful IS NOT NULL) AS knowledge_feedback, "
            "(SELECT COUNT(*) FROM agent_assistance_feedback WHERE tenant_id = ? "
            " AND knowledge_helpful) AS knowledge_helpful",
            (tenant_id, tenant_id, tenant_id, tenant_id, tenant_id),
        ) or {}
        predictions = int(row.get("predictions") or 0)
        feedback = int(row.get("feedback") or 0)
        knowledge_feedback = int(row.get("knowledge_feedback") or 0)
        return {
            "predictions": predictions,
            "feedback": feedback,
            "outcomes": int(row.get("outcomes") or 0),
            "feedback_coverage": feedback / predictions if predictions else 0.0,
            "knowledge_feedback": knowledge_feedback,
            "knowledge_helpful_rate": (
                int(row.get("knowledge_helpful") or 0) / knowledge_feedback
                if knowledge_feedback
                else 0.0
            ),
        }

    def knowledge_usage(self, tenant_id: str) -> list[dict]:
        return self._connector.fetch_all(
            "SELECT article->>'article_id' AS article_id, "
            "MIN(article->>'title') AS title, COUNT(*) AS recommendations "
            "FROM agent_assistance_prediction p "
            "CROSS JOIN LATERAL jsonb_array_elements(p.knowledge_snapshot) article "
            "WHERE p.tenant_id = ? "
            "GROUP BY article->>'article_id' "
            "ORDER BY recommendations DESC, article_id ASC",
            (tenant_id,),
        )

    @staticmethod
    def _training_row(row: dict, label: bool, source: str) -> dict:
        return {
            "session_id": row["session_id"],
            "tenant_id": row.get("tenant_id") or "unknown",
            "user_id": row["user_id"],
            "role": row.get("role") or "unknown",
            "created_at": row.get("created_at"),
            "signals": _json_load(row.get("signals")),
            "recent_events": _json_load(row.get("recent_events")) or [],
            "history_snapshot": _json_load(row.get("history_snapshot")) or {},
            "server_facts_snapshot": _json_load(row.get("server_facts_snapshot")) or {},
            "needed_assistance": label,
            "source": source,
        }


def build_assistance_store(connector) -> InMemoryAssistanceStore | PostgresAssistanceStore:
    if connector is None:
        return InMemoryAssistanceStore()
    return PostgresAssistanceStore(connector)
