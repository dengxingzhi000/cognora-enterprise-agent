from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

FEATURE_VERSION = 2
MODULE_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"
WORKFLOW_PATTERN = r"[a-z0-9][a-z0-9_-]{0,127}"
SESSION_PATTERN = r"[A-Za-z0-9._:-]{1,128}"
ROLE_PATTERN = r"[a-z0-9_-]{0,32}"
EVENT_TYPES = (
    "PAGE_VIEW",
    "FORM_EDIT",
    "VALIDATION_ERROR",
    "API_ERROR",
    "RETRY",
    "BACK_NAVIGATION",
    "CONTEXT_SWITCH",
    "PERMISSION_DENIED",
    "HELP_OPEN",
    "IDLE",
    "TASK_SUCCESS",
    "TASK_FAILURE",
    "CANCEL",
)
ERROR_CODES = (
    "VALIDATION",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "NOT_FOUND",
    "CONFLICT",
    "RATE_LIMITED",
    "SERVER_ERROR",
    "TIMEOUT",
    "NETWORK",
    "BUSINESS_REJECTION",
    "UNKNOWN",
)


class AssistanceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal[
        "PAGE_VIEW",
        "FORM_EDIT",
        "VALIDATION_ERROR",
        "API_ERROR",
        "RETRY",
        "BACK_NAVIGATION",
        "CONTEXT_SWITCH",
        "PERMISSION_DENIED",
        "HELP_OPEN",
        "IDLE",
        "TASK_SUCCESS",
        "TASK_FAILURE",
        "CANCEL",
    ]
    offset_ms: StrictInt = Field(ge=0, le=604800000)
    duration_ms: StrictInt = Field(ge=0, le=604800000)
    severity: StrictInt = Field(ge=0, le=3)
    error_code: Literal[
        "VALIDATION",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "NOT_FOUND",
        "CONFLICT",
        "RATE_LIMITED",
        "SERVER_ERROR",
        "TIMEOUT",
        "NETWORK",
        "BUSINESS_REJECTION",
        "UNKNOWN",
    ] | None = None


class AssistanceSignals(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    module: str = Field(pattern=MODULE_PATTERN)
    workflow: str = Field(pattern=WORKFLOW_PATTERN)
    session_duration_seconds: StrictInt = Field(ge=0, le=604800)
    active_seconds: StrictInt = Field(ge=0, le=604800)
    idle_seconds: StrictInt = Field(ge=0, le=604800)
    event_count: StrictInt = Field(ge=0, le=100000)
    error_count: StrictInt = Field(ge=0, le=10000)
    unique_error_count: StrictInt = Field(ge=0, le=1000)
    repeated_error_count: StrictInt = Field(ge=0, le=10000)
    retry_count: StrictInt = Field(ge=0, le=10000)
    back_navigation_count: StrictInt = Field(ge=0, le=10000)
    context_switch_count: StrictInt = Field(ge=0, le=10000)
    validation_failure_count: StrictInt = Field(ge=0, le=10000)
    permission_denied_count: StrictInt = Field(ge=0, le=1000)
    form_completion_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    help_open_count: StrictInt = Field(ge=0, le=1000)
    previous_task_count: StrictInt = Field(ge=0, le=1000000)
    previous_success_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    recent_assistance_count: StrictInt = Field(ge=0, le=10000)
    explicit_help_request: StrictBool
    high_risk_action: StrictBool

    @model_validator(mode="after")
    def validate_durations(self) -> Self:
        if self.active_seconds + self.idle_seconds > self.session_duration_seconds:
            raise ValueError("active_seconds + idle_seconds cannot exceed session_duration_seconds")
        if (
            self.unique_error_count > self.error_count
            or self.repeated_error_count > self.error_count
        ):
            raise ValueError("error breakdown counts cannot exceed error_count")
        event_counts = (
            self.error_count,
            self.retry_count,
            self.back_navigation_count,
            self.context_switch_count,
            self.validation_failure_count,
            self.permission_denied_count,
            self.help_open_count,
        )
        if max(event_counts) > self.event_count:
            raise ValueError("signal counts cannot exceed event_count")
        return self

    def feature_dict(self, context: dict | None = None) -> dict[str, float | int | str]:
        context = context or {}
        duration = max(self.session_duration_seconds, 1)
        events = max(self.event_count, 1)
        errors = max(self.error_count, 1)
        values: dict[str, float | int | str] = {
            "module": self.module,
            "workflow": self.workflow,
            "session_duration_seconds": self.session_duration_seconds,
            "active_seconds": self.active_seconds,
            "idle_seconds": self.idle_seconds,
            "event_count": self.event_count,
            "error_count": self.error_count,
            "unique_error_count": self.unique_error_count,
            "repeated_error_count": self.repeated_error_count,
            "retry_count": self.retry_count,
            "back_navigation_count": self.back_navigation_count,
            "context_switch_count": self.context_switch_count,
            "validation_failure_count": self.validation_failure_count,
            "permission_denied_count": self.permission_denied_count,
            "form_completion_ratio": self.form_completion_ratio,
            "help_open_count": self.help_open_count,
            "previous_task_count": self.previous_task_count,
            "previous_success_rate": self.previous_success_rate,
            "recent_assistance_count": self.recent_assistance_count,
            "explicit_help_request": int(self.explicit_help_request),
            "high_risk_action": int(self.high_risk_action),
            "duration_minutes": self.session_duration_seconds / 60,
            "active_ratio": self.active_seconds / duration,
            "idle_ratio": self.idle_seconds / duration,
            "error_rate": self.error_count / events,
            "retry_rate": self.retry_count / events,
            "back_navigation_rate": self.back_navigation_count / events,
            "context_switch_rate": self.context_switch_count / events,
            "help_open_rate": self.help_open_count / events,
            "repeated_error_ratio": self.repeated_error_count / errors,
            "validation_failure_ratio": self.validation_failure_count / errors,
            "permission_denied_ratio": self.permission_denied_count / errors,
            "completion_deficit": 1 - self.form_completion_ratio,
            "log_error_count": math.log1p(self.error_count),
            "log_retry_count": math.log1p(self.retry_count),
            "log_back_navigation_count": math.log1p(self.back_navigation_count),
            "log_idle_seconds": math.log1p(self.idle_seconds),
            "error_retry_interaction": math.log1p(self.error_count) * math.log1p(self.retry_count),
        }
        values.update(
            _sequence_features(context.get("recent_events") or [], self.session_duration_seconds)
        )
        values.update(_history_features(context.get("history") or {}))
        values.update(_server_features(context.get("server_facts") or {}))
        role = str(context.get("role") or "unknown").lower()
        values["role"] = role if _safe_role(role) else "unknown"
        return values


def _safe_role(value: str) -> bool:
    return len(value) <= 32 and all(
        character.isalnum() or character in "_-" for character in value
    )


def _sequence_features(
    raw_events: list[dict], duration_seconds: int
) -> dict[str, float | int | str]:
    events = []
    for raw in raw_events:
        if not isinstance(raw, dict) or raw.get("type") not in EVENT_TYPES:
            continue
        try:
            offset = min(max(0, int(raw.get("offset_ms", 0))), duration_seconds * 1000)
            duration = max(0, int(raw.get("duration_ms", 0)))
            severity = max(0, min(3, int(raw.get("severity", 0))))
        except (TypeError, ValueError):
            continue
        events.append(
            {
                "type": raw["type"],
                "offset_ms": offset,
                "duration_ms": duration,
                "severity": severity,
                "error_code": raw.get("error_code"),
            }
        )
    events.sort(key=lambda item: item["offset_ms"])
    duration_ms = duration_seconds * 1000
    types = [item["type"] for item in events]
    error_offsets = [
        item["offset_ms"]
        for item in events
        if item["type"] in {"API_ERROR", "VALIDATION_ERROR"}
    ]
    burst = 0
    right = 0
    for left, offset in enumerate(error_offsets):
        while right < len(error_offsets) and error_offsets[right] <= offset + 60000:
            right += 1
        burst = max(burst, right - left)
    retry_after_error = sum(
        1
        for item in events
        if item["type"] == "RETRY"
        and any(0 <= item["offset_ms"] - offset <= 60000 for offset in error_offsets)
    )
    help_after_error = sum(
        1
        for item in events
        if item["type"] == "HELP_OPEN"
        and any(0 <= item["offset_ms"] - offset <= 60000 for offset in error_offsets)
    )
    recovery = 0
    seen_error = False
    for item in events:
        if item["type"] in {"API_ERROR", "VALIDATION_ERROR"}:
            seen_error = True
        elif item["type"] == "TASK_SUCCESS" and seen_error:
            recovery += 1
            seen_error = False
    values: dict[str, float | int | str] = {
        "seq_length": len(events),
        "seq_unique_types": len(set(types)),
        "seq_error_events": len(error_offsets),
        "seq_retry_events": types.count("RETRY"),
        "seq_back_events": types.count("BACK_NAVIGATION"),
        "seq_help_events": types.count("HELP_OPEN"),
        "seq_idle_events": types.count("IDLE"),
        "seq_cancel_events": types.count("CANCEL"),
        "seq_max_severity": max((item["severity"] for item in events), default=0),
        "seq_error_burst_max": burst,
        "seq_retry_after_error_count": retry_after_error,
        "seq_help_after_error_count": help_after_error,
        "seq_recovery_count": recovery,
        "seq_span_ms": (events[-1]["offset_ms"] - events[0]["offset_ms"]) if events else 0,
        "seq_last_error_age_ms": (
            max(0, duration_ms - error_offsets[-1]) if error_offsets else duration_ms
        ),
        "seq_event_rate_per_minute": len(events) / max(duration_seconds / 60, 1 / 60),
        "seq_last_event": types[-1] if types else "NONE",
    }
    truncated = types[-40:]
    for index in range(max(0, len(truncated) - 1)):
        values[f"seq_bigram_{index}"] = f"{truncated[index]}>{truncated[index + 1]}"
    for index in range(max(0, len(truncated) - 2)):
        values[f"seq_trigram_{index}"] = ">".join(truncated[index : index + 3])
    if truncated:
        values["seq_last_3"] = ">".join(truncated[-3:])
    return values


def _history_features(history: dict) -> dict[str, float | int]:
    return {
        "history_predictions_7d": _nonnegative_int(history.get("predictions_7d")),
        "history_interventions_7d": _nonnegative_int(history.get("interventions_7d")),
        "history_needed_feedback_7d": _nonnegative_int(history.get("needed_feedback_7d")),
        "history_acceptance_rate_7d": _unit_float(history.get("assistance_acceptance_rate_7d")),
        "history_recency_seconds": _nonnegative_int(history.get("seconds_since_last_prediction")),
    }


def _server_features(facts: dict) -> dict[str, float | int]:
    return {
        "server_outcome_count": _nonnegative_int(facts.get("server_outcome_count")),
        "server_error_count": _nonnegative_int(facts.get("server_error_count")),
        "server_timeout_count": _nonnegative_int(facts.get("server_timeout_count")),
        "server_rejection_count": _nonnegative_int(facts.get("server_rejection_count")),
        "server_failed_count": _nonnegative_int(facts.get("server_failed_count")),
        "server_p95_latency_ms": _nonnegative_int(facts.get("server_p95_latency_ms")),
        "server_log_latency_ms": math.log1p(_nonnegative_int(facts.get("server_p95_latency_ms"))),
    }


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _unit_float(value) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0)))
    except (TypeError, ValueError):
        return 0.0


class AssistancePredictRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(pattern=SESSION_PATTERN)
    signals: AssistanceSignals
    recent_events: list[AssistanceEvent] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def validate_event_offsets(self) -> Self:
        limit = self.signals.session_duration_seconds * 1000
        if any(item.offset_ms > limit for item in self.recent_events):
            raise ValueError("event offset_ms cannot exceed session_duration_seconds")
        return self


class AssistanceFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    needed_assistance: StrictBool
    assistance_used: StrictBool
    task_succeeded: StrictBool
    source: Literal["OPERATOR", "OUTCOME", "REVIEWER"]
    knowledge_helpful: StrictBool | None = None
    knowledge_article_id: str | None = Field(
        default=None, pattern=r"[A-Za-z0-9._:-]{1,128}"
    )


class AssistanceOutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prediction_id: str | None = Field(default=None, pattern=r"[A-Za-z0-9._-]{1,128}")
    session_id: str = Field(pattern=SESSION_PATTERN)
    module: str = Field(pattern=MODULE_PATTERN)
    workflow: str = Field(pattern=WORKFLOW_PATTERN)
    outcome: Literal["TASK_COMPLETED", "TASK_FAILED", "TASK_ABANDONED", "REPEATED_FAILURE"]
    error_code: Literal[
        "VALIDATION",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "NOT_FOUND",
        "CONFLICT",
        "RATE_LIMITED",
        "SERVER_ERROR",
        "TIMEOUT",
        "NETWORK",
        "BUSINESS_REJECTION",
        "UNKNOWN",
    ] | None = None
    latency_ms: StrictInt = Field(ge=0, le=604800000)
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def validate_occurred_at(self) -> Self:
        if self.occurred_at is None:
            return self
        occurred = self.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if occurred > now + timedelta(minutes=5):
            raise ValueError("occurred_at cannot be in the future")
        if occurred < now - timedelta(days=30):
            raise ValueError("occurred_at is outside the retention window")
        return self
