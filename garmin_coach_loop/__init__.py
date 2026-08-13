"""Deterministic contracts and private state for Garmin Coach Loop."""

from .store import (
    STORE_SCHEMA_VERSION,
    StateStoreError,
    apply_decision,
    apply_delivery_observations,
    default_state_dir,
    doctor_store,
    init_store,
    read_current_plan,
    resolve_state_dir,
    status_store,
)

from .validation import (
    COACH_CONTEXT_SCHEMA_VERSION,
    DECISION_EVENT_SCHEMA_VERSION,
    PLAN_STATE_SCHEMA_VERSION,
    validate_bundle,
    validate_coach_context,
    validate_decision_event,
    validate_plan_state,
)

__all__ = [
    "COACH_CONTEXT_SCHEMA_VERSION",
    "DECISION_EVENT_SCHEMA_VERSION",
    "PLAN_STATE_SCHEMA_VERSION",
    "STORE_SCHEMA_VERSION",
    "StateStoreError",
    "apply_decision",
    "apply_delivery_observations",
    "default_state_dir",
    "doctor_store",
    "init_store",
    "read_current_plan",
    "resolve_state_dir",
    "status_store",
    "validate_bundle",
    "validate_coach_context",
    "validate_decision_event",
    "validate_plan_state",
]
