"""Dependency-free structural and semantic validation for Coach Loop V1.

The language model may propose the three public artifacts. This module owns the stable
safety boundary. It does not authenticate, read a live provider, persist state, or publish.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Iterable

from .delivery_content import delivery_session_content
from .intent_text import prescribed_token_in_coach_note, prescribed_token_in_intent
from .prescription import LANGUAGES as PRESCRIPTION_LANGUAGES, render_prescription


COACH_CONTEXT_SCHEMA_VERSION = "1.2"
PLAN_STATE_SCHEMA_VERSION = "1.0"
DECISION_EVENT_SCHEMA_VERSION = "1.0"

# "partial" is a legacy grade: no current builder emits it on freshness.recovery
# (_recovery_freshness in source_intervals.py grades recency only, mechanically --
# sufficiency became the coach's judgment, read from coverage's observed_days and
# last_observed, issue #95). It stays in the vocabulary because doctor_store
# revalidates the full stored history against this module on every read, and history
# written before that change carries it.
FRESHNESS = {"fresh", "partial", "stale", "failed", "unknown"}
COVERAGE_STATUS = {"complete", "partial", "missing"}
# Cross-training sports carry the same three execution models as everything else and
# deliver nothing yet: `_publish_supported` answers false for them until a delivery
# representation is verified per sport, so adding one here changes what a plan may hold
# and what an athlete may report, never what reaches the watch.
SPORTS = {
    "running",
    "strength",
    "mobility",
    "recovery",
    "rest",
    "cycling",
    "swimming",
    "hiking",
    "rowing",
}
ADAPTATIONS = {
    "aerobic_base",
    "threshold",
    "vo2",
    "strength",
    "hypertrophy",
    "power",
    "recovery",
}
BODY_STRESS = {"lower", "upper", "full", "systemic"}
COSTS = {"easy", "moderate", "hard"}
PRIORITIES = {"anchor", "flexible", "optional"}
# A later decision can overturn an earlier one for three very different reasons, and
# collapsing them loses the only signal that separates a coaching mistake from the
# product changing its mind underneath a training block that was going fine.
SUPERSEDE_KINDS = {"corrected", "new_evidence", "policy_changed"}
# Fields whose movement changes what the athlete actually does, or what the athlete is
# told. `purpose` sits in the second half, not the first: it never changes a number the
# watch executes, but delivery_content.delivery_session_content already carries it,
# because a movement_list session reaches Intervals as a calendar entry whose name is
# built from purpose. Rewording it is a deliverable change to what the athlete sees, so
# it belongs here beside the fields that move training itself -- unlike `fallback`
# wording, which names no delivered artifact and stays outside this set. `coach_note` is
# here on exactly that argument (issue #56): it is delivered content, it reaches the
# athlete's calendar, and attaching one to a session that is otherwise kept is the case
# the field exists for -- so a revision that only adds a note is a real change to what the
# athlete is told, not prose the plan could have skipped a version over.
MATERIAL_SESSION_FIELDS = frozenset({
    "scheduled_date", "sport", "adaptation", "planned_minutes", "priority",
    "cost", "body_stress", "hard", "plan", "time_window", "execution",
    "match_status", "purpose", "coach_note",
})
INITIATIVES = {"proactive", "reactive"}
DAILY_ACTIONS = {"keep", "reduce", "move", "replace", "rest", "human_review"}
ACTIONABLE_MATCH_STATUSES = {"planned", "moved", "replaced"}
# The two ways an activity may be attached to a planned session firmly enough to
# reconcile it. "matched" is the provider's own pairing; "owned" is the product's
# evidence that it delivered the session and that the day admits one reading. Both are
# defined by ``context_core._match_actuals_to_plan``; the rules the "owned" tier rests on
# live here so the matcher and the reconcile gate cannot drift apart.
ATTACHED_MATCH_CONFIDENCES = {"matched", "owned"}
# The duration band an ownership-backed attachment must fall inside. Wide on the upper
# side on purpose -- training past the prescription is still that session, and how far
# past is the coach's judgment, not this rule's. The lower bound is the one doing real
# work: a 15-minute shakeout is not the 55-minute quality session it shares a date with,
# and calling it one would report a session as trained that was not.
OWNED_DURATION_MIN_RATIO = 0.5
OWNED_DURATION_MAX_RATIO = 2.0
REASON_CODES = {
    "actual_load_above_plan",
    "actual_load_below_plan",
    "multi_signal_recovery_down",
    "recovery_signal_mixed",
    "lower_body_stress_conflict",
    "quality_session_conflict",
    "schedule_or_equipment_changed",
    "goal_priority_changed",
    "data_stale_or_missing",
    "pain_or_illness_flag",
    "plan_kept_no_material_change",
    # Mechanical transitions have dedicated codes and deterministic semantic checks;
    # they are not coaching judgment disguised as ordinary review reasons.
    "planned_actual_reconciled",
    "delivery_verified",
    "delivery_withdrawn",
}
MODE_ACTIONS = {
    "plan_cycle": {"create", "adjust"},
    "plan_week": {"create", "adjust"},
    "revisit_today": DAILY_ACTIONS,
    "review_week": {"keep", "adjust", "human_review"},
    "review_cycle": {"continue", "adjust", "pivot", "stop", "human_review"},
    "record_delivery": {"record"},
}
ATHLETE_BASELINE_FIELDS = (
    "threshold_pace_sec_per_km",
    "max_hr",
    "easy_hr_ceiling",
    "longest_recent_run_km",
    "weekly_volume_km_4wk_avg",
    "max_session_minutes",
    "strength_loads",
)
# The same seven fields split by numeric type, so every vocabulary that accepts a
# baseline value (initialization, a hosted baseline change) parses it identically
# instead of keeping its own copy of which field is which.
ATHLETE_BASELINE_INTEGER_FIELDS = (
    "threshold_pace_sec_per_km",
    "max_hr",
    "easy_hr_ceiling",
    "max_session_minutes",
)
ATHLETE_BASELINE_NUMBER_FIELDS = ("longest_recent_run_km", "weekly_volume_km_4wk_avg")
STRENGTH_LOAD_FIELDS = ("exercise", "load_kg", "assist_kg", "scheme")
# The athlete names their lifts in their own language; display_name is how that name
# binds back to this measured anchor. Optional: a movement named with the canonical
# exercise key matches on its own.
STRENGTH_LOAD_OPTIONAL_FIELDS = ("display_name",)

# The three execution models a session may be planned under (issue #93). `kind` decides
# which validation runs; `sport` does not -- running and swimming are one model, yoga and
# a rest day are another. Only the models this product has today are defined, so adding a
# sport later is one `sport` value reusing one of these and no branch here.
SESSION_PLAN_KINDS = {"time_axis", "movement_list", "unstructured"}
SESSION_PLAN_FIELDS = {
    "time_axis": ("kind", "name", "steps"),
    "movement_list": ("kind", "movements"),
    # Nothing beyond its own kind: an unstructured session declares no numbers, so there
    # is nothing for an evidence check to read -- and equally nothing a pace or a load
    # could be smuggled in through.
    "unstructured": ("kind",),
}

# One movement of a movement_list plan: the plan-side counterpart of a strength_load,
# named the same way so the two compare field to field instead of a pattern re-deriving
# the plan's own numbers out of the sentence that reported them.
# `exercise` and `display_name` are two different jobs and both are required. The first
# is the canonical key that compares field to field against a baseline entry and is never
# printed; the second is the only name the athlete ever sees, through the rendered
# prescription and the calendar entry a strength session reaches the watch as. Optional
# here would mean falling back to `exercise`, which puts "back_squat" on a watch face --
# a field whose default is the defect is not optional. Contrast
# STRENGTH_LOAD_OPTIONAL_FIELDS, where display_name is a matching alias and its absence
# has a safe meaning: the canonical key matches on its own.
STRENGTH_MOVEMENT_FIELDS = (
    "exercise", "display_name", "sets", "reps", "load_kg", "assist_kg", "load_basis",
)
# Why a load is what it is. The two loadless bases are the point of the field: an absent
# load_kg is either a bodyweight movement or a number the athlete still owes, and the
# difference has to be a recorded value rather than a word a pattern goes looking for.
# There is no RPE basis: no structured field holds an RPE number, and adding a basis with
# nowhere to put its figure would be an unanchored load wearing a label.
STRENGTH_LOAD_BASES = {"measured_baseline", "bodyweight", "pending_confirmation"}

# strength_execution (issue #37): the standalone optional evidence group described in
# source_personal_os.fetch_strength_execution. Exact keys throughout -- unlike
# athlete_baseline, nothing here predates the field existing, so there is no
# backward-compatibility reason to allow an `optional=` set.
STRENGTH_EXECUTION_FIELDS = ("source", "window_start", "window_end", "sessions")
STRENGTH_EXECUTION_SESSION_FIELDS = ("date", "exercise", "category", "sets", "notes", "source")
STRENGTH_EXECUTION_SET_FIELDS = ("set", "weight_kg", "assist_kg", "reps", "rpe")

# segment_execution: per-segment execution for recent runs, read from the base source
# one level finer than recent_actuals. Exact keys, same rationale as above. Nothing
# here is compared against the session's prescribed steps -- the provider's grouping
# does not correspond to them, and deciding which segments are the work is a reading
# of the numbers rather than a rule this validator could own.
MOVEMENT_HISTORY_FIELDS = ("source", "window_start", "window_end", "movements")
MOVEMENT_HISTORY_MOVEMENT_FIELDS = ("exercise", "display_name", "baseline", "occurrences")
MOVEMENT_HISTORY_BASELINE_FIELDS = ("load_kg", "assist_kg", "scheme")
MOVEMENT_HISTORY_OCCURRENCE_FIELDS = (
    "date", "prescribed", "performed_sets", "load_rollup", "notes", "source",
)
# The raw spelling a merged occurrence was stored under, present only when it differs
# from the group's name. Storage is keyed on the raw spelling (athlete_evidence.
# exercise_key), so this is the name a correction or retraction of that row must use.
MOVEMENT_HISTORY_OCCURRENCE_OPTIONAL_FIELDS = ("reported_as",)
MOVEMENT_HISTORY_PRESCRIPTION_FIELDS = ("sets", "reps", "load_kg", "assist_kg", "load_basis")
# load_rollup: the per-load arithmetic derived from the occurrence's own performed_sets
# -- reps at each distinct load, the session total, and which load was heaviest. See
# garmin_coach_loop.context_core._load_rollup for how it is computed.
MOVEMENT_HISTORY_LOAD_ROLLUP_FIELDS = ("by_load", "total_reps", "top_load")
MOVEMENT_HISTORY_LOAD_ROLLUP_ROW_FIELDS = ("weight_kg", "assist_kg", "reps")
MOVEMENT_HISTORY_TOP_LOAD_FIELDS = ("weight_kg", "assist_kg", "held_every_set")

# baseline_evidence (issue #32): every athlete_baseline field's claim beside the recent
# evidence and how many observations back it. Exact keys per row kind, which is itself
# the no-verdict guarantee -- there is no key a stale flag, confidence score, or
# suggested value could legally sit in. The six scalar fields each appear exactly once;
# strength rows repeat per movement through the same row shape.
BASELINE_EVIDENCE_ROW_FIELDS = (
    "field", "baseline", "observed", "observations", "window_start", "window_end",
)
BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS = (
    "field", "exercise", "display_name", "baseline", "observed", "observations",
    "window_start", "window_end",
)
BASELINE_EVIDENCE_WEEK_FIELDS = ("week_start", "through", "km", "runs")
BASELINE_EVIDENCE_LOAD_FIELDS = ("load_kg", "assist_kg", "sessions", "first", "last")
BASELINE_EVIDENCE_OBSERVED_FIELDS = {
    "threshold_pace_sec_per_km": ("fastest_average_pace_sec_per_km", "date", "distance_km"),
    "max_hr": ("highest_average_hr", "date", "sport"),
    "easy_hr_ceiling": ("average_hr_low", "average_hr_high"),
    "longest_recent_run_km": ("longest_run_km", "date"),
    "weekly_volume_km_4wk_avg": ("weeks",),
    "max_session_minutes": ("longest_session_minutes", "date", "sport"),
    "strength_loads": ("loads",),
}

SEGMENT_EXECUTION_FIELDS = ("source", "window_start", "window_end", "activities")
SEGMENT_EXECUTION_ACTIVITY_FIELDS = ("activity_id", "date", "sport", "segments")
SEGMENT_EXECUTION_SEGMENT_FIELDS = (
    "index",
    "provider_type",
    "distance_m",
    "moving_time_sec",
    "average_pace_sec_per_km",
    "average_hr",
    "max_hr",
    "min_hr",
    "elevation_gain_m",
)

# recovery_signals (issue #37 slice 2): the standalone optional evidence group
# described in source_personal_os.fetch_recovery_signals. Exact keys throughout, same
# rationale as STRENGTH_EXECUTION_FIELDS above -- nothing here predates the field
# existing.
# body_measurements / reported_activities: the two conversational evidence groups. Both
# carry the same group envelope every other standalone group uses, and both are optional
# on the context rather than required -- a context built before they existed is still a
# context, exactly as athlete_profile is. `null` means this athlete has stated none, which
# is the ordinary starting state and never an error.
#
# Neither row shape has an activity id, a match confidence, or a completion, and that is
# the guarantee rather than an omission: there is no key a reconciler could read one of
# these out of, so a reported session cannot be turned into a provider actual by any
# amount of downstream code.
BODY_MEASUREMENTS_FIELDS = ("source", "window_start", "window_end", "measurements")
BODY_MEASUREMENT_FIELDS = ("date", "weight_kg", "body_fat_pct", "source")
REPORTED_ACTIVITIES_FIELDS = ("source", "window_start", "window_end", "activities")
# subjective_states (issue #188): what the athlete said about how they felt, dated. Same
# envelope, same optionality, and a row shape that is deliberately two fields and a
# timestamp -- no scale, no severity, no category. Anything more would be a reading of the
# sentence made in this layer, and the whole point of storing the sentence is that the
# reading belongs to the coach.
SUBJECTIVE_STATES_FIELDS = ("source", "window_start", "window_end", "states")
SUBJECTIVE_STATE_FIELDS = ("date", "note", "recorded_at")
REPORTED_ACTIVITY_FIELDS = (
    "date",
    "sport",
    "duration_minutes",
    "distance_km",
    "subjective_feel",
    "note",
    "source",
    "imported_from",
    "provider_actual_same_day",
)

RECOVERY_SIGNALS_FIELDS = ("source", "window_start", "window_end", "days")
# Every reading a day may carry. Nothing here is a Garmin-only name any more: sleep
# score, sleep duration, resting heart rate and a single night's HRV are what a wearable
# of any make shows on its own screen, and the athlete reading one of them out loud is as
# valid a route in as an export.
RECOVERY_SIGNALS_DAY_OBSERVATION_FIELDS = (
    "readiness_score",
    "readiness_level",
    "hrv_status",
    "hrv_7d_avg_ms",
    "acute_load",
    "recovery_time_sec",
    "body_battery_high",
    "body_battery_low",
    "avg_stress",
    "sleep_score",
    "sleep_duration_sec",
    "sleep_history_score",
    "hrv_last_night_ms",
    "resting_hr_bpm",
)
# The whole stored day: the one key an upload must carry, plus every reading it may. A
# context commit is written with all of them, null where nothing was observed, which is
# why the upload boundary fills the absent ones rather than the client being asked to.
RECOVERY_SIGNALS_DAY_FIELDS = ("date", *RECOVERY_SIGNALS_DAY_OBSERVATION_FIELDS)

# training_history (issue #101): the athlete's complete, unwindowed evidence rolled up
# into monthly buckets -- a coarse trend view the 42-day cycle window cannot provide, and
# shaped to stay coarse. No activity_id, no match_confidence, no per-session row: the row
# shape is the guarantee that a month bucket can never be read as recent_actuals's
# per-session truth (AGENTS.md 3).
TRAINING_HISTORY_FIELDS = (
    "source",
    "months",
    "truncated",
    "earliest_observed_month",
    "movement_longevity",
    "movement_longevity_truncated",
)
TRAINING_HISTORY_MONTH_FIELDS = (
    "month",
    "sport",
    "session_count",
    "total_minutes",
    "total_km",
    "provenance_counts",
)
# Fixed keys rather than a sparse mapping, so a reader never has to ask whether a missing
# key means zero or means the field does not exist here -- the same reasoning
# ``constraints.red_flags`` already applies to a fixed set of named booleans.
TRAINING_HISTORY_PROVENANCE_FIELDS = (
    "athlete_reported",
    "athlete_imported",
    "prescribed_confirmed",
)
TRAINING_HISTORY_MOVEMENT_FIELDS = ("exercise", "display_name", "earliest", "heaviest")
# Same shape earliest and heaviest both carry: a date plus the load reading
# ``_load_rollup``'s top_load already uses one level up, so the two never need a second
# vocabulary for "how much was lifted".
TRAINING_HISTORY_OBSERVATION_FIELDS = ("date", "weight_kg", "assist_kg", "held_every_set")
# Present only when the observation's stored spelling differs from the merged group's
# name -- the same contract as movement_history's occurrence field of the same name.
TRAINING_HISTORY_OBSERVATION_OPTIONAL_FIELDS = ("reported_as",)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def owned_duration_within_band(planned_minutes: Any, actual_minutes: Any) -> bool:
    """Whether an actual's duration keeps it inside the ownership-backed band.

    Unknown duration on either side is not a pass: an attachment that auto-writes a
    completion must rest on observed numbers, never on a missing one read as agreement.
    """
    planned = _finite_number(planned_minutes)
    actual = _finite_number(actual_minutes)
    if planned is None or actual is None or planned <= 0:
        return False
    return OWNED_DURATION_MIN_RATIO * planned <= actual <= OWNED_DURATION_MAX_RATIO * planned


def product_delivered(session: dict[str, Any]) -> bool:
    """Whether the product itself put this session on the athlete's calendar.

    This is the ownership evidence the "owned" tier rests on: an approval bound to one
    exact proposal, a verified read-back, and the event id the store recorded from it.
    """
    execution = session.get("execution")
    if not isinstance(execution, dict):
        return False
    external_id = execution.get("external_id")
    return (
        execution.get("delivery_state") == "intervals_accepted"
        and external_id is not None
        and bool(str(external_id))
    )


def _report(errors: list[str], warnings: list[str]) -> dict[str, Any]:
    return {
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": warnings,
    }


def _mapping(value: Any, field: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return {}
    return value


def _list(value: Any, field: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{field} must be an array")
        return []
    return value


def _keys(
    value: dict[str, Any],
    field: str,
    required: Iterable[str],
    errors: list[str],
    *,
    optional: Iterable[str] = (),
) -> None:
    """Check required keys are present and no unexpected key appears.

    ``optional`` keys are allowed but not demanded. This exists for fields added after
    a plan was already persisted: the store is append-only with integrity receipts, so
    historical commits cannot be rewritten to carry a newly required key -- making such
    a key required would render every existing plan unreadable.
    """
    required_keys = set(required)
    allowed = required_keys | set(optional)
    for key in sorted(required_keys - value.keys()):
        errors.append(f"{field}.{key} is required")
    for key in sorted(value.keys() - allowed):
        errors.append(f"{field}.{key} is not allowed")


def _nonempty(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")


def _integer(value: Any, field: str, errors: list[str], *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{field} must be an integer >= {minimum}")


def _enum(value: Any, field: str, allowed: set[str], errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"{field} must be one of {', '.join(sorted(allowed))}")


def _integer_or_null(
    value: Any,
    field: str,
    errors: list[str],
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    """Validate an athlete_baseline-style field where null means "not yet known".

    Null must never be treated as zero or as a passing value elsewhere; it only means
    the corresponding consistency check has nothing to compare against and must skip.
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} must be an integer or null")
        return
    if minimum is not None and value < minimum:
        errors.append(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        errors.append(f"{field} must be <= {maximum}")


def _number_or_null(
    value: Any,
    field: str,
    errors: list[str],
    *,
    minimum: float | None = None,
) -> None:
    """Validate a nullable numeric athlete_baseline field. See _integer_or_null for why null is kept distinct from zero."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field} must be a number or null")
        return
    if minimum is not None and value < minimum:
        errors.append(f"{field} must be >= {minimum}")


def _number(value: Any, field: str, errors: list[str], *, minimum: float | None = None) -> None:
    """Validate a number a record cannot omit -- an observation that exists has a value."""
    if value is None:
        errors.append(f"{field} must be a number")
        return
    _number_or_null(value, field, errors, minimum=minimum)


def _string_or_null(value: Any, field: str, errors: list[str]) -> None:
    """Validate a nullable string. Null means the provider did not label it; an empty
    string would be a label of nothing, which is a different and meaningless claim."""
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string or null")


def _validate_athlete_baseline(value: Any, field: str, errors: list[str]) -> None:
    """Validate an athlete_baseline object's shape.

    Shared by `CoachContext.athlete_baseline` (the decision-time snapshot) and
    `PlanState.athlete_baseline` (the authoritative current baseline) so the two
    objects can never validate against different rules. See _integer_or_null for why
    null must stay null rather than be treated as a default.
    """
    baseline = _mapping(value, field, errors)
    _keys(baseline, field, ATHLETE_BASELINE_FIELDS, errors)
    _integer_or_null(
        baseline.get("threshold_pace_sec_per_km"),
        f"{field}.threshold_pace_sec_per_km",
        errors,
        minimum=1,
    )
    _integer_or_null(baseline.get("max_hr"), f"{field}.max_hr", errors, minimum=1)
    _integer_or_null(baseline.get("easy_hr_ceiling"), f"{field}.easy_hr_ceiling", errors, minimum=1)
    _number_or_null(
        baseline.get("longest_recent_run_km"), f"{field}.longest_recent_run_km", errors, minimum=0
    )
    _number_or_null(
        baseline.get("weekly_volume_km_4wk_avg"), f"{field}.weekly_volume_km_4wk_avg", errors, minimum=0
    )
    _integer_or_null(
        baseline.get("max_session_minutes"), f"{field}.max_session_minutes", errors, minimum=1
    )
    strength_loads = _list(baseline.get("strength_loads"), f"{field}.strength_loads", errors)
    for index, raw in enumerate(strength_loads):
        item_field = f"{field}.strength_loads[{index}]"
        load = _mapping(raw, item_field, errors)
        _keys(
            load,
            item_field,
            STRENGTH_LOAD_FIELDS,
            errors,
            optional=STRENGTH_LOAD_OPTIONAL_FIELDS,
        )
        _nonempty(load.get("exercise"), f"{item_field}.exercise", errors)
        _number_or_null(load.get("load_kg"), f"{item_field}.load_kg", errors, minimum=0)
        _number_or_null(load.get("assist_kg"), f"{item_field}.assist_kg", errors, minimum=0)
        if load.get("scheme") is not None:
            _nonempty(load.get("scheme"), f"{item_field}.scheme", errors)
        if load.get("display_name") is not None:
            _nonempty(load.get("display_name"), f"{item_field}.display_name", errors)


def _date(value: Any, field: str, errors: list[str]) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an ISO date")
        return None


def _date_or_null(value: Any, field: str, errors: list[str]) -> None:
    """Validate a nullable ISO date. Null means the signal never carried a real value
    in the window, an acquisition fact rather than a verdict (issue #95)."""
    if value is None:
        return
    if not isinstance(value, str):
        errors.append(f"{field} must be an ISO date or null")
        return
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field} must be an ISO date or null")


def _year_month(value: Any, field: str, errors: list[str]) -> None:
    """Validate a ``YYYY-MM`` calendar-month label -- ``training_history``'s own unit,
    distinct from ``_date``'s ``YYYY-MM-DD``: a bucket names the month it covers, not one
    day inside it."""
    if not isinstance(value, str):
        errors.append(f"{field} must be a YYYY-MM month")
        return
    try:
        dt.datetime.strptime(value, "%Y-%m")
    except ValueError:
        errors.append(f"{field} must be a YYYY-MM month")


def _timestamp(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{field} must be an ISO-8601 timestamp with timezone")
        return
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field} must be an ISO-8601 timestamp with timezone")
        return
    if parsed.tzinfo is None:
        errors.append(f"{field} must include a timezone offset")


def _string_array(
    value: Any,
    field: str,
    errors: list[str],
    *,
    minimum: int = 0,
    allowed: set[str] | None = None,
) -> list[str]:
    items = _list(value, field, errors)
    if len(items) < minimum:
        errors.append(f"{field} must contain at least {minimum} item(s)")
    result: list[str] = []
    for index, item in enumerate(items):
        _nonempty(item, f"{field}[{index}]", errors)
        if isinstance(item, str):
            if allowed is not None and item not in allowed:
                errors.append(f"{field}[{index}] is unsupported")
            result.append(item)
    if len(result) != len(set(result)):
        errors.append(f"{field} must not contain duplicates")
    return result


def _validate_coverage(value: Any, field: str, errors: list[str]) -> None:
    coverage = _mapping(value, field, errors)
    # last_observed is optional (issue #95): a context built before it existed, or a
    # coverage_entry() call this repo never asked to carry it (gateway.py's
    # coverage_activities), both still validate.
    _keys(
        coverage,
        field,
        ("observed_days", "expected_days", "status"),
        errors,
        optional=("last_observed",),
    )
    _integer(coverage.get("observed_days"), f"{field}.observed_days", errors)
    _integer(coverage.get("expected_days"), f"{field}.expected_days", errors, minimum=1)
    _enum(coverage.get("status"), f"{field}.status", COVERAGE_STATUS, errors)
    if "last_observed" in coverage:
        _date_or_null(coverage.get("last_observed"), f"{field}.last_observed", errors)
    observed = coverage.get("observed_days")
    expected = coverage.get("expected_days")
    status = coverage.get("status")
    if isinstance(observed, int) and isinstance(expected, int) and not isinstance(observed, bool):
        if observed > expected:
            errors.append(f"{field}.observed_days cannot exceed expected_days")
        derived = "missing" if observed == 0 else "complete" if observed == expected else "partial"
        if status in COVERAGE_STATUS and status != derived:
            errors.append(f"{field}.status must be {derived} for {observed}/{expected} days")


def _validate_trend(value: Any, field: str, errors: list[str]) -> None:
    trend = _mapping(value, field, errors)
    _keys(trend, field, ("status", "observed_days", "expected_days"), errors)
    _enum(
        trend.get("status"),
        f"{field}.status",
        {"above_baseline", "within_baseline", "below_baseline", "mixed", "unknown"},
        errors,
    )
    _integer(trend.get("observed_days"), f"{field}.observed_days", errors)
    _integer(trend.get("expected_days"), f"{field}.expected_days", errors, minimum=1)
    if (
        isinstance(trend.get("observed_days"), int)
        and isinstance(trend.get("expected_days"), int)
        and trend["observed_days"] > trend["expected_days"]
    ):
        errors.append(f"{field}.observed_days cannot exceed expected_days")


def _validate_strength_execution_set(value: Any, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, STRENGTH_EXECUTION_SET_FIELDS, errors)
    _integer(item.get("set"), f"{field}.set", errors, minimum=1)
    _number_or_null(item.get("weight_kg"), f"{field}.weight_kg", errors)
    _number_or_null(item.get("assist_kg"), f"{field}.assist_kg", errors)
    _integer_or_null(item.get("reps"), f"{field}.reps", errors)
    _number_or_null(item.get("rpe"), f"{field}.rpe", errors)


def _validate_strength_execution_session(value: Any, field: str, errors: list[str]) -> None:
    session = _mapping(value, field, errors)
    _keys(session, field, STRENGTH_EXECUTION_SESSION_FIELDS, errors)
    _date(session.get("date"), f"{field}.date", errors)
    _nonempty(session.get("exercise"), f"{field}.exercise", errors)
    # ``category`` is optional metadata, not a fact the athlete owes anyone. A local
    # strength log supplies it, a plan or the provider's session label usually implies
    # it, and where none of those apply the honest value is null -- asking the athlete
    # which body part bench press trains is a form, not a conversation.
    _string_or_null(session.get("category"), f"{field}.category", errors)
    _nonempty(session.get("source"), f"{field}.source", errors)
    sets = _list(session.get("sets"), f"{field}.sets", errors)
    for index, raw in enumerate(sets):
        _validate_strength_execution_set(raw, f"{field}.sets[{index}]", errors)
    _string_array(session.get("notes"), f"{field}.notes", errors)


def _validate_movement_history_prescription(value, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, MOVEMENT_HISTORY_PRESCRIPTION_FIELDS, errors)
    _integer_or_null(item.get("sets"), f"{field}.sets", errors, minimum=1)
    _integer_or_null(item.get("reps"), f"{field}.reps", errors, minimum=1)
    _number_or_null(item.get("load_kg"), f"{field}.load_kg", errors, minimum=0)
    _number_or_null(item.get("assist_kg"), f"{field}.assist_kg", errors, minimum=0)
    _string_or_null(item.get("load_basis"), f"{field}.load_basis", errors)


def _validate_movement_history_load_rollup_row(value, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, MOVEMENT_HISTORY_LOAD_ROLLUP_ROW_FIELDS, errors)
    # Same nullable-number shape as strength_execution_set's own weight_kg/assist_kg
    # (no floor enforced there either) -- a rollup row is an echo of that field, and a
    # bound the source itself does not carry must not reject an echo of it.
    _number_or_null(item.get("weight_kg"), f"{field}.weight_kg", errors)
    _number_or_null(item.get("assist_kg"), f"{field}.assist_kg", errors)
    _integer_or_null(item.get("reps"), f"{field}.reps", errors)


def _validate_movement_history_top_load(value, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, MOVEMENT_HISTORY_TOP_LOAD_FIELDS, errors)
    _number_or_null(item.get("weight_kg"), f"{field}.weight_kg", errors)
    _number_or_null(item.get("assist_kg"), f"{field}.assist_kg", errors)
    if not isinstance(item.get("held_every_set"), bool):
        errors.append(f"{field}.held_every_set must be a boolean")


def _validate_movement_history_load_rollup(value, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, MOVEMENT_HISTORY_LOAD_ROLLUP_FIELDS, errors)
    for index, raw in enumerate(_list(item.get("by_load"), f"{field}.by_load", errors)):
        _validate_movement_history_load_rollup_row(raw, f"{field}.by_load[{index}]", errors)
    # Null is not "zero reps" -- it is what an occurrence with an incompletely recorded
    # set count must say instead of a total that only looks exact (AGENTS.md 3).
    _integer_or_null(item.get("total_reps"), f"{field}.total_reps", errors)
    top_load = item.get("top_load")
    if top_load is not None:
        _validate_movement_history_top_load(top_load, f"{field}.top_load", errors)


def _validate_movement_history_occurrence(value, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(
        item, field, MOVEMENT_HISTORY_OCCURRENCE_FIELDS, errors,
        optional=MOVEMENT_HISTORY_OCCURRENCE_OPTIONAL_FIELDS,
    )
    _date(item.get("date"), f"{field}.date", errors)
    if item.get("reported_as") is not None:
        _nonempty(item.get("reported_as"), f"{field}.reported_as", errors)
    # Which of the two places this row came from. Required, because a movement's rows can
    # mix a local strength log's measurements with the athlete's own recollection, and
    # this group exists to be read row against row.
    _nonempty(item.get("source"), f"{field}.source", errors)
    prescribed = item.get("prescribed")
    if prescribed is not None:
        entries = _list(prescribed, f"{field}.prescribed", errors)
        if not entries:
            # Null already says "nothing prescribed". An empty list would be a second
            # spelling of the same fact, and two spellings drift.
            errors.append(f"{field}.prescribed must be null rather than an empty list")
        for index, raw in enumerate(entries):
            _validate_movement_history_prescription(raw, f"{field}.prescribed[{index}]", errors)
    for index, raw in enumerate(_list(item.get("performed_sets"), f"{field}.performed_sets", errors)):
        _validate_strength_execution_set(raw, f"{field}.performed_sets[{index}]", errors)
    _validate_movement_history_load_rollup(item.get("load_rollup"), f"{field}.load_rollup", errors)
    _string_array(item.get("notes"), f"{field}.notes", errors)


def _validate_movement_history_movement(value, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, MOVEMENT_HISTORY_MOVEMENT_FIELDS, errors)
    _nonempty(item.get("exercise"), f"{field}.exercise", errors)
    _string_or_null(item.get("display_name"), f"{field}.display_name", errors)
    baseline = item.get("baseline")
    if baseline is not None:
        anchor_item = _mapping(baseline, f"{field}.baseline", errors)
        _keys(anchor_item, f"{field}.baseline", MOVEMENT_HISTORY_BASELINE_FIELDS, errors)
        _number_or_null(anchor_item.get("load_kg"), f"{field}.baseline.load_kg", errors, minimum=0)
        _number_or_null(anchor_item.get("assist_kg"), f"{field}.baseline.assist_kg", errors, minimum=0)
        _string_or_null(anchor_item.get("scheme"), f"{field}.baseline.scheme", errors)
    occurrences = _list(item.get("occurrences"), f"{field}.occurrences", errors)
    if not occurrences:
        errors.append(f"{field}.occurrences must not be empty")
    for index, raw in enumerate(occurrences):
        _validate_movement_history_occurrence(raw, f"{field}.occurrences[{index}]", errors)


def _validate_movement_history(value, field: str, errors: list[str]) -> None:
    """Validate the per-movement strength history group.

    Structure only. It never compares an occurrence against its prescription, never
    scores a trend, and never decides whether a movement is progressing -- the group
    exists so a coach can make that read, and encoding it here would replace the
    judgment with a rule (AGENTS.md 1).
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, MOVEMENT_HISTORY_FIELDS, errors)
    _string_or_null(group.get("source"), f"{field}.source", errors)
    for key in ("window_start", "window_end"):
        if group.get(key) is not None:
            _date(group.get(key), f"{field}.{key}", errors)
    for index, raw in enumerate(_list(group.get("movements"), f"{field}.movements", errors)):
        _validate_movement_history_movement(raw, f"{field}.movements[{index}]", errors)


def _validate_baseline_evidence_week(value: Any, field: str, errors: list[str]) -> None:
    week = _mapping(value, field, errors)
    _keys(week, field, BASELINE_EVIDENCE_WEEK_FIELDS, errors)
    start = _date(week.get("week_start"), f"{field}.week_start", errors)
    through = _date(week.get("through"), f"{field}.through", errors)
    if start is not None and start.weekday() != 0:
        errors.append(f"{field}.week_start must be a Monday")
    if start is not None and through is not None:
        if not (start <= through <= start + dt.timedelta(days=6)):
            errors.append(f"{field}.through must fall inside its own week")
    _number_or_null(week.get("km"), f"{field}.km", errors, minimum=0)
    _integer(week.get("runs"), f"{field}.runs", errors)


def _validate_baseline_evidence_load(value: Any, field: str, errors: list[str]) -> None:
    load = _mapping(value, field, errors)
    _keys(load, field, BASELINE_EVIDENCE_LOAD_FIELDS, errors)
    _number_or_null(load.get("load_kg"), f"{field}.load_kg", errors, minimum=0)
    _number_or_null(load.get("assist_kg"), f"{field}.assist_kg", errors, minimum=0)
    _integer(load.get("sessions"), f"{field}.sessions", errors, minimum=1)
    first = _date(load.get("first"), f"{field}.first", errors)
    last = _date(load.get("last"), f"{field}.last", errors)
    if first is not None and last is not None and first > last:
        errors.append(f"{field}.first must not be after last")


def _validate_baseline_evidence_observed(
    kind: str, value: Any, field: str, errors: list[str]
) -> None:
    observed = _mapping(value, field, errors)
    _keys(observed, field, BASELINE_EVIDENCE_OBSERVED_FIELDS[kind], errors)
    if kind == "threshold_pace_sec_per_km":
        _number(
            observed.get("fastest_average_pace_sec_per_km"),
            f"{field}.fastest_average_pace_sec_per_km", errors, minimum=1,
        )
        _date(observed.get("date"), f"{field}.date", errors)
        _number_or_null(observed.get("distance_km"), f"{field}.distance_km", errors, minimum=0)
    elif kind == "max_hr":
        _number(observed.get("highest_average_hr"), f"{field}.highest_average_hr", errors, minimum=1)
        _date(observed.get("date"), f"{field}.date", errors)
        _nonempty(observed.get("sport"), f"{field}.sport", errors)
    elif kind == "easy_hr_ceiling":
        low = observed.get("average_hr_low")
        high = observed.get("average_hr_high")
        _number(low, f"{field}.average_hr_low", errors, minimum=1)
        _number(high, f"{field}.average_hr_high", errors, minimum=1)
        if (
            isinstance(low, (int, float)) and not isinstance(low, bool)
            and isinstance(high, (int, float)) and not isinstance(high, bool)
            and low > high
        ):
            errors.append(f"{field}.average_hr_low must not exceed average_hr_high")
    elif kind == "longest_recent_run_km":
        _number(observed.get("longest_run_km"), f"{field}.longest_run_km", errors, minimum=0)
        _date(observed.get("date"), f"{field}.date", errors)
    elif kind == "weekly_volume_km_4wk_avg":
        weeks = _list(observed.get("weeks"), f"{field}.weeks", errors)
        if isinstance(observed.get("weeks"), list) and not weeks:
            # Null observed already says "nothing to compare". An empty list would be a
            # second spelling of the same fact, and two spellings drift.
            errors.append(f"{field}.weeks must not be empty")
        for index, raw in enumerate(weeks):
            _validate_baseline_evidence_week(raw, f"{field}.weeks[{index}]", errors)
    elif kind == "max_session_minutes":
        _number(
            observed.get("longest_session_minutes"),
            f"{field}.longest_session_minutes", errors, minimum=1,
        )
        _date(observed.get("date"), f"{field}.date", errors)
        _nonempty(observed.get("sport"), f"{field}.sport", errors)
    elif kind == "strength_loads":
        loads = _list(observed.get("loads"), f"{field}.loads", errors)
        if isinstance(observed.get("loads"), list) and not loads:
            errors.append(f"{field}.loads must not be empty")
        for index, raw in enumerate(loads):
            _validate_baseline_evidence_load(raw, f"{field}.loads[{index}]", errors)


def _validate_baseline_evidence_row(value: Any, field: str, errors: list[str]) -> None:
    row = _mapping(value, field, errors)
    kind = row.get("field")
    if kind not in ATHLETE_BASELINE_FIELDS:
        errors.append(f"{field}.field must name an athlete_baseline field")
        return
    if kind == "strength_loads":
        _keys(row, field, BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS, errors)
        _nonempty(row.get("exercise"), f"{field}.exercise", errors)
        _string_or_null(row.get("display_name"), f"{field}.display_name", errors)
        baseline = row.get("baseline")
        if baseline is not None:
            anchor = _mapping(baseline, f"{field}.baseline", errors)
            _keys(anchor, f"{field}.baseline", MOVEMENT_HISTORY_BASELINE_FIELDS, errors)
            _number_or_null(anchor.get("load_kg"), f"{field}.baseline.load_kg", errors, minimum=0)
            _number_or_null(anchor.get("assist_kg"), f"{field}.baseline.assist_kg", errors, minimum=0)
            _string_or_null(anchor.get("scheme"), f"{field}.baseline.scheme", errors)
    else:
        _keys(row, field, BASELINE_EVIDENCE_ROW_FIELDS, errors)
        _number_or_null(row.get("baseline"), f"{field}.baseline", errors, minimum=0)
    observations = row.get("observations")
    _integer(observations, f"{field}.observations", errors)
    for key in ("window_start", "window_end"):
        if row.get(key) is not None:
            _date(row.get(key), f"{field}.{key}", errors)
    if (row.get("window_start") is None) != (row.get("window_end") is None):
        errors.append(f"{field} window_start and window_end must be both dates or both null")

    observed = row.get("observed")
    counted = isinstance(observations, int) and not isinstance(observations, bool)
    if observed is None:
        # "Nothing observed" and a positive count cannot both be true of one row.
        if counted and observations != 0:
            errors.append(f"{field}.observed null requires observations 0")
        return
    _validate_baseline_evidence_observed(kind, observed, f"{field}.observed", errors)
    if not (counted and isinstance(observed, dict)):
        return
    if kind == "weekly_volume_km_4wk_avg":
        weeks = observed.get("weeks")
        if isinstance(weeks, list) and observations != len(weeks):
            errors.append(f"{field}.observations must equal the number of weeks listed")
    elif kind == "strength_loads":
        loads = observed.get("loads")
        if isinstance(loads, list):
            sessions = [
                item.get("sessions") for item in loads if isinstance(item, dict)
            ]
            if all(
                isinstance(count, int) and not isinstance(count, bool)
                for count in sessions
            ) and observations != sum(sessions):
                errors.append(f"{field}.observations must equal the sessions its loads carry")
    elif observations < 1:
        errors.append(f"{field}.observed requires observations >= 1")


def _validate_baseline_evidence(value: Any, field: str, errors: list[str]) -> None:
    """Validate the per-field baseline-versus-evidence comparison (issue #32).

    Structure only, and the structure is the guarantee: exact keys per row kind leave
    no place for a stale flag, a confidence score, or a suggested value to sit. The
    group states claims, observations and counts; which side is right is the coaching
    judgment it exists to inform, and encoding that here would decide it (AGENTS.md 4).
    """
    rows = _list(value, field, errors)
    for index, raw in enumerate(rows):
        _validate_baseline_evidence_row(raw, f"{field}[{index}]", errors)
    scalar_fields = tuple(name for name in ATHLETE_BASELINE_FIELDS if name != "strength_loads")
    counts = {name: 0 for name in scalar_fields}
    for raw in rows:
        if isinstance(raw, dict) and raw.get("field") in counts:
            counts[raw.get("field")] += 1
    for name in scalar_fields:
        if counts[name] != 1:
            errors.append(f"{field} must carry exactly one row for {name}")


def _validate_segment_execution_segment(value: Any, field: str, errors: list[str]) -> None:
    segment = _mapping(value, field, errors)
    _keys(segment, field, SEGMENT_EXECUTION_SEGMENT_FIELDS, errors)
    _integer(segment.get("index"), f"{field}.index", errors, minimum=0)
    _string_or_null(segment.get("provider_type"), f"{field}.provider_type", errors)
    _number_or_null(segment.get("distance_m"), f"{field}.distance_m", errors)
    _integer_or_null(segment.get("moving_time_sec"), f"{field}.moving_time_sec", errors)
    _integer_or_null(
        segment.get("average_pace_sec_per_km"), f"{field}.average_pace_sec_per_km", errors
    )
    _number_or_null(segment.get("average_hr"), f"{field}.average_hr", errors)
    _number_or_null(segment.get("max_hr"), f"{field}.max_hr", errors)
    _number_or_null(segment.get("min_hr"), f"{field}.min_hr", errors)
    _number_or_null(segment.get("elevation_gain_m"), f"{field}.elevation_gain_m", errors)


def _validate_segment_execution_activity(value: Any, field: str, errors: list[str]) -> None:
    activity = _mapping(value, field, errors)
    _keys(activity, field, SEGMENT_EXECUTION_ACTIVITY_FIELDS, errors)
    _nonempty(activity.get("activity_id"), f"{field}.activity_id", errors)
    _date(activity.get("date"), f"{field}.date", errors)
    _nonempty(activity.get("sport"), f"{field}.sport", errors)
    segments = _list(activity.get("segments"), f"{field}.segments", errors)
    if not segments:
        # An activity with no segments is not reported at all, so an empty list here
        # means something upstream built a record with nothing in it.
        errors.append(f"{field}.segments must not be empty")
    for index, raw in enumerate(segments):
        _validate_segment_execution_segment(raw, f"{field}.segments[{index}]", errors)


def _validate_segment_execution(value: Any, field: str, errors: list[str]) -> None:
    """Validate the per-segment execution group.

    ``null`` means no source could produce it -- either a source with no segment data
    at all, or one that read the window and found none. Both leave the coach on
    whole-session averages, and the context says so once in ``unknowns``. The key
    itself is always present.

    This checks structure only. It never decides which segments were the prescribed
    work, never compares a segment's pace against the session's target, and never
    computes a completion rate: the provider's grouping does not line up with the
    prescribed steps, and reading it is coaching judgment (AGENTS.md 1).
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, SEGMENT_EXECUTION_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    _date(group.get("window_start"), f"{field}.window_start", errors)
    _date(group.get("window_end"), f"{field}.window_end", errors)
    activities = _list(group.get("activities"), f"{field}.activities", errors)
    for index, raw in enumerate(activities):
        _validate_segment_execution_activity(raw, f"{field}.activities[{index}]", errors)


def _validate_strength_execution(value: Any, field: str, errors: list[str]) -> None:
    """Validate the standalone optional strength_execution evidence group (issue #37).

    ``null`` means "not configured" (no ``--health-db`` and no env var) and is
    always valid -- the key itself is still required on every context, only its
    value may be null. An object means a configured read, however empty:
    ``sessions: []`` is a valid "looked, nothing in the window" result, distinct
    from null's "never looked". See ``source_personal_os.fetch_strength_execution``
    for what produces this shape; this validator only checks structure, never
    compares a set's weight against ``athlete_baseline.strength_loads`` -- that
    comparison is coaching judgment, not a deterministic rule (issue #3 direction).
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, STRENGTH_EXECUTION_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    _date(group.get("window_start"), f"{field}.window_start", errors)
    _date(group.get("window_end"), f"{field}.window_end", errors)
    sessions = _list(group.get("sessions"), f"{field}.sessions", errors)
    for index, raw in enumerate(sessions):
        _validate_strength_execution_session(raw, f"{field}.sessions[{index}]", errors)


def _validate_recovery_signals_day(value: Any, field: str, errors: list[str]) -> None:
    # The day is required; every reading on it is optional. A missing key and an explicit
    # null say the same thing -- this was not observed -- so demanding the null was asking
    # a client holding one reading to write out all the ones it does not hold (issue
    # #187). Nothing downstream reads an absent key differently from a null one, and the
    # gateway fills the absent ones in before a context is built, so a stored context is
    # unchanged.
    day = _mapping(value, field, errors)
    _keys(
        day,
        field,
        ("date",),
        errors,
        optional=RECOVERY_SIGNALS_DAY_OBSERVATION_FIELDS,
    )
    _date(day.get("date"), f"{field}.date", errors)
    if day.get("readiness_level") is not None:
        _nonempty(day.get("readiness_level"), f"{field}.readiness_level", errors)
    # hrv_status is deliberately not enum-constrained: Garmin's own vocabulary (NONE /
    # BALANCED / UNBALANCED / LOW / POOR today) is theirs to extend, and "NONE" is
    # itself real information ("still learning this athlete"), not a value to reject.
    if day.get("hrv_status") is not None:
        _nonempty(day.get("hrv_status"), f"{field}.hrv_status", errors)
    _number_or_null(day.get("readiness_score"), f"{field}.readiness_score", errors)
    _number_or_null(day.get("hrv_7d_avg_ms"), f"{field}.hrv_7d_avg_ms", errors)
    _number_or_null(day.get("acute_load"), f"{field}.acute_load", errors)
    _number_or_null(day.get("recovery_time_sec"), f"{field}.recovery_time_sec", errors)
    _number_or_null(day.get("body_battery_high"), f"{field}.body_battery_high", errors)
    _number_or_null(day.get("body_battery_low"), f"{field}.body_battery_low", errors)
    _number_or_null(day.get("avg_stress"), f"{field}.avg_stress", errors)
    _number_or_null(day.get("sleep_score"), f"{field}.sleep_score", errors)
    _number_or_null(day.get("sleep_duration_sec"), f"{field}.sleep_duration_sec", errors)
    _number_or_null(day.get("sleep_history_score"), f"{field}.sleep_history_score", errors)
    _number_or_null(day.get("hrv_last_night_ms"), f"{field}.hrv_last_night_ms", errors)
    _number_or_null(day.get("resting_hr_bpm"), f"{field}.resting_hr_bpm", errors)


def _validate_recovery_signals(value: Any, field: str, errors: list[str]) -> None:
    """Validate the standalone optional recovery_signals evidence group (issue #37
    slice 2). Mirrors ``_validate_strength_execution``: ``null`` means "not
    configured" (no ``--health-db`` and no env var) and is always valid; an object
    means a configured read, however empty -- ``days: []`` is a valid "looked,
    nothing in the window" result, distinct from null's "never looked". See
    ``source_personal_os.fetch_recovery_signals`` for what produces this shape; this
    validator only checks structure -- no threshold, no readiness rule, no comparison
    against any other field. Whether a given readiness/acute-load/Body-Battery
    reading should change a plan is coaching judgment, not a deterministic rule
    (issue #3 direction; a readiness rule here was already tried and withdrawn once,
    since over-reacting to one day's number is exactly the failure mode the #39 test
    arm is watching for).
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, RECOVERY_SIGNALS_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    _date(group.get("window_start"), f"{field}.window_start", errors)
    _date(group.get("window_end"), f"{field}.window_end", errors)
    days = _list(group.get("days"), f"{field}.days", errors)
    for index, raw in enumerate(days):
        _validate_recovery_signals_day(raw, f"{field}.days[{index}]", errors)


def validate_recovery_signals(value: Any) -> dict[str, Any]:
    """Validate one standalone recovery evidence group without interpreting it.

    The public entry exists for request boundaries that accept an already-sanitized
    group from a client. It is deliberately the same structural reader used inside a
    full CoachContext: an uploaded reading does not earn a looser schema, and this
    function still computes no readiness score, threshold, trend, or coaching action.
    """
    errors: list[str] = []
    _validate_recovery_signals(value, "recovery_signals", errors)
    return {
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": [],
    }


def _validate_body_measurements(value: Any, field: str, errors: list[str]) -> None:
    """The athlete's own weigh-ins, or ``null`` when they have reported none.

    Structure only, and no arithmetic anywhere: no trend, no rate of change, no comparison
    against a previous row or a target. A series is what the coach reads; a direction is
    what the coach decides (AGENTS.md 4, 5). A day may hold either figure alone, because
    stepping on a scale is not measuring body composition.
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, BODY_MEASUREMENTS_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    _date(group.get("window_start"), f"{field}.window_start", errors)
    _date(group.get("window_end"), f"{field}.window_end", errors)
    for index, raw in enumerate(_list(group.get("measurements"), f"{field}.measurements", errors)):
        row_field = f"{field}.measurements[{index}]"
        row = _mapping(raw, row_field, errors)
        _keys(row, row_field, BODY_MEASUREMENT_FIELDS, errors)
        _date(row.get("date"), f"{row_field}.date", errors)
        _number_or_null(row.get("weight_kg"), f"{row_field}.weight_kg", errors, minimum=0)
        _number_or_null(row.get("body_fat_pct"), f"{row_field}.body_fat_pct", errors, minimum=0)
        _nonempty(row.get("source"), f"{row_field}.source", errors)


def _validate_reported_activities(value: Any, field: str, errors: list[str]) -> None:
    """Sessions the athlete says they trained and no device recorded, or ``null``.

    Beside ``recent_actuals``, never inside it. The row shape is the enforcement: no
    ``activity_id``, no ``match_confidence``, no ``completion``, no ``planned_session_id``
    -- there is nowhere for a reconciler to read an attachment out of, so one of these can
    never become a provider-backed actual and a week's training can never be counted as
    both a report and an activity (AGENTS.md 8).

    ``sport`` is the plan's own vocabulary minus rest, the same set ``recent_actuals``
    uses. ``subjective_feel`` is the same 1-5 scale, so one reported session and one
    recorded session describe effort identically.

    ``provider_actual_same_day`` is an observation, not an attachment: it says the
    provider also holds an activity of this sport on this day -- the late-sync case
    where one session would otherwise stand in the context twice -- and names no
    activity, so there is still nothing here a reconciler could act on.

    ``source`` and ``imported_from`` say which of the athlete's two ways of stating a
    session this row came from: describing it, or uploading a file that held it. An
    upload is still the athlete's own record and still not a provider actual -- it adds
    no id, no confidence and no completion, so the enforcement above is unchanged by it --
    but a coach reading a progression needs to know whether numbers came from a device's
    export or from somebody's memory of the session.
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, REPORTED_ACTIVITIES_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    _date(group.get("window_start"), f"{field}.window_start", errors)
    _date(group.get("window_end"), f"{field}.window_end", errors)
    for index, raw in enumerate(_list(group.get("activities"), f"{field}.activities", errors)):
        row_field = f"{field}.activities[{index}]"
        row = _mapping(raw, row_field, errors)
        _keys(row, row_field, REPORTED_ACTIVITY_FIELDS, errors)
        _date(row.get("date"), f"{row_field}.date", errors)
        _enum(row.get("sport"), f"{row_field}.sport", SPORTS - {"rest"}, errors)
        _integer(row.get("duration_minutes"), f"{row_field}.duration_minutes", errors, minimum=1)
        _number_or_null(row.get("distance_km"), f"{row_field}.distance_km", errors, minimum=0)
        _integer_or_null(
            row.get("subjective_feel"), f"{row_field}.subjective_feel", errors, minimum=1, maximum=5
        )
        _string_or_null(row.get("note"), f"{row_field}.note", errors)
        _nonempty(row.get("source"), f"{row_field}.source", errors)
        # Which upload supplied the session, when one did. Null on a spoken summary, and
        # therefore checked as nullable rather than required-nonempty: an athlete who has
        # never uploaded anything has a context full of nulls here, which is not a
        # thinner context, it is the ordinary one.
        _string_or_null(row.get("imported_from"), f"{row_field}.imported_from", errors)
        # Written on every row by assembly, False included, so a coach can tell
        # "checked, nothing there" from "never checked".
        if not isinstance(row.get("provider_actual_same_day"), bool):
            errors.append(f"{row_field}.provider_actual_same_day must be boolean")


def _validate_subjective_states(value: Any, field: str, errors: list[str]) -> None:
    """What the athlete said about how they felt, or ``null`` when they have said nothing.

    Structure only, and the structure is the guarantee. There is no severity field, no
    scale, and no category, so there is nothing here for a later reader to threshold on --
    which is what keeps a stored sentence from becoming a stored score. Nothing in this
    module counts these rows, measures a run of them, or lines one up against a recovery
    reading: "this is the third week of it" is a reading, and readings are the coach's
    (AGENTS.md 4, 5).

    Nor is this the symptom channel. Pain, illness, chest pain, dizziness and unusual
    symptoms travel as ``constraints.red_flags``, where an explicit true limits the day
    deterministically. This validator does not inspect the words to work out which of the
    two arrived -- reading prose to decide whether it was a symptom is diagnosing from
    text, and it would fail on exactly the phrasing that mattered.
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, SUBJECTIVE_STATES_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    _date(group.get("window_start"), f"{field}.window_start", errors)
    _date(group.get("window_end"), f"{field}.window_end", errors)
    for index, raw in enumerate(_list(group.get("states"), f"{field}.states", errors)):
        row_field = f"{field}.states[{index}]"
        row = _mapping(raw, row_field, errors)
        _keys(row, row_field, SUBJECTIVE_STATE_FIELDS, errors)
        _date(row.get("date"), f"{row_field}.date", errors)
        _nonempty(row.get("note"), f"{row_field}.note", errors)
        _timestamp(row.get("recorded_at"), f"{row_field}.recorded_at", errors)


def _validate_athlete_profile(value: Any, field: str, errors: list[str]) -> None:
    """The timezone and language the athlete stated, or ``null`` when they stated neither.

    Structure only, and deliberately tolerant of half a profile: an athlete may say where
    they are without saying what they read, so either field may be null while the record
    exists. What is refused is a value that would be read as a statement and is not one --
    a blank timezone, or a language nothing can render.
    """
    if value is None:
        return
    profile = _mapping(value, field, errors)
    _keys(profile, field, ("timezone", "language", "recorded_at", "source"), errors)
    if profile.get("timezone") is not None:
        _nonempty(profile.get("timezone"), f"{field}.timezone", errors)
    if profile.get("language") is not None:
        _enum(profile.get("language"), f"{field}.language", set(PRESCRIPTION_LANGUAGES), errors)


def _validate_training_history_provenance_counts(value: Any, field: str, errors: list[str]) -> None:
    counts = _mapping(value, field, errors)
    _keys(counts, field, TRAINING_HISTORY_PROVENANCE_FIELDS, errors)
    for name in TRAINING_HISTORY_PROVENANCE_FIELDS:
        _integer(counts.get(name), f"{field}.{name}", errors, minimum=0)


def _validate_training_history_month(value: Any, field: str, errors: list[str]) -> None:
    row = _mapping(value, field, errors)
    _keys(row, field, TRAINING_HISTORY_MONTH_FIELDS, errors)
    _year_month(row.get("month"), f"{field}.month", errors)
    _enum(row.get("sport"), f"{field}.sport", SPORTS - {"rest"}, errors)
    _integer(row.get("session_count"), f"{field}.session_count", errors, minimum=1)
    # Null is not zero minutes/km -- it is "nothing in this bucket stated the figure",
    # which for a strength bucket built only from per-exercise reports (no duration or
    # distance field at all) is the ordinary case, not a data-quality problem (AGENTS.md
    # 3).
    _integer_or_null(row.get("total_minutes"), f"{field}.total_minutes", errors, minimum=1)
    _number_or_null(row.get("total_km"), f"{field}.total_km", errors, minimum=0)
    _validate_training_history_provenance_counts(
        row.get("provenance_counts"), f"{field}.provenance_counts", errors
    )


def _validate_training_history_observation(value: Any, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(
        item, field, TRAINING_HISTORY_OBSERVATION_FIELDS, errors,
        optional=TRAINING_HISTORY_OBSERVATION_OPTIONAL_FIELDS,
    )
    _date(item.get("date"), f"{field}.date", errors)
    if item.get("reported_as") is not None:
        _nonempty(item.get("reported_as"), f"{field}.reported_as", errors)
    _number_or_null(item.get("weight_kg"), f"{field}.weight_kg", errors)
    _number_or_null(item.get("assist_kg"), f"{field}.assist_kg", errors)
    if not isinstance(item.get("held_every_set"), bool):
        errors.append(f"{field}.held_every_set must be a boolean")


def _validate_training_history_movement(value: Any, field: str, errors: list[str]) -> None:
    item = _mapping(value, field, errors)
    _keys(item, field, TRAINING_HISTORY_MOVEMENT_FIELDS, errors)
    _nonempty(item.get("exercise"), f"{field}.exercise", errors)
    _string_or_null(item.get("display_name"), f"{field}.display_name", errors)
    _validate_training_history_observation(item.get("earliest"), f"{field}.earliest", errors)
    heaviest = item.get("heaviest")
    if heaviest is not None:
        _validate_training_history_observation(heaviest, f"{field}.heaviest", errors)


def _validate_training_history(value: Any, field: str, errors: list[str]) -> None:
    """The athlete's complete evidence history, rolled up into monthly buckets, or
    ``null`` when nothing long-range has been reported (issue #101).

    Structure only, and the row shape is the guarantee: no ``activity_id``, no
    ``match_confidence``, nothing a reconciler could read an attachment out of, so a
    coarse monthly bucket can never be misread as ``recent_actuals``'s per-session truth.
    Nothing here computes a trend, a percentage change, or a verdict about whether
    training increased or fell off -- two numbers and their months are the coaching
    evidence, and which way they point is the coach's reading (AGENTS.md 1, 4).
    """
    if value is None:
        return
    group = _mapping(value, field, errors)
    _keys(group, field, TRAINING_HISTORY_FIELDS, errors)
    _nonempty(group.get("source"), f"{field}.source", errors)
    months = _list(group.get("months"), f"{field}.months", errors)
    if not months:
        # Null already says "nothing long-range reported". An empty list would be a
        # second spelling of the same fact, and two spellings drift (same rule
        # movement_history_occurrence's own "prescribed" field follows).
        errors.append(f"{field}.months must not be empty when the group is present")
    for index, raw in enumerate(months):
        _validate_training_history_month(raw, f"{field}.months[{index}]", errors)
    if not isinstance(group.get("truncated"), bool):
        errors.append(f"{field}.truncated must be a boolean")
    _year_month(group.get("earliest_observed_month"), f"{field}.earliest_observed_month", errors)
    for index, raw in enumerate(
        _list(group.get("movement_longevity"), f"{field}.movement_longevity", errors)
    ):
        _validate_training_history_movement(raw, f"{field}.movement_longevity[{index}]", errors)
    if not isinstance(group.get("movement_longevity_truncated"), bool):
        errors.append(f"{field}.movement_longevity_truncated must be a boolean")


def validate_coach_context(context: dict[str, Any]) -> dict[str, Any]:
    """Validate sanitized context shape without interpreting unknown as recovery."""

    errors: list[str] = []
    warnings: list[str] = []
    required = (
        "schema_version",
        "context_id",
        "as_of",
        "timezone",
        "sources",
        "freshness",
        "coverage",
        "goal_context",
        "review_frame",
        "constraints",
        "athlete_baseline",
        "baseline_evidence",
        "recent_actuals",
        "recovery_trends",
        "current_calendar",
        "cycle_sessions",
        "measurement_evidence",
        "strength_execution",
        "recovery_signals",
        "segment_execution",
        "movement_history",
        "unknowns",
        "privacy",
    )
    # ``athlete_profile`` is optional rather than required: every context this code builds
    # carries it, and a context built before it existed is still a context. Demanding it
    # would refuse the one artifact a caller cannot rebuild -- the context it was handed
    # in an earlier turn and is now confirming a decision against. The conversational
    # evidence groups, and the two standing-statement groups beside them, are optional for
    # exactly that reason and no other.
    _keys(
        context,
        "context",
        required,
        errors,
        optional=(
            "athlete_profile",
            "body_measurements",
            "reported_activities",
            "subjective_states",
            "long_term_goals",
            "training_preferences",
            "training_history",
        ),
    )
    if context.get("schema_version") != COACH_CONTEXT_SCHEMA_VERSION:
        errors.append(f"context.schema_version must be {COACH_CONTEXT_SCHEMA_VERSION}")
    _nonempty(context.get("context_id"), "context.context_id", errors)
    _timestamp(context.get("as_of"), "context.as_of", errors)
    _nonempty(context.get("timezone"), "context.timezone", errors)
    _validate_athlete_profile(context.get("athlete_profile"), "context.athlete_profile", errors)

    sources = _list(context.get("sources"), "context.sources", errors)
    if not sources:
        errors.append("context.sources must contain at least one sanitized source")
    source_fields = ("source", "mode", "doctor_status", "observed_at", "data_through", "sanitized")
    source_names: list[str] = []
    for index, raw in enumerate(sources):
        field = f"context.sources[{index}]"
        source = _mapping(raw, field, errors)
        _keys(source, field, source_fields, errors)
        _nonempty(source.get("source"), f"{field}.source", errors)
        _nonempty(source.get("mode"), f"{field}.mode", errors)
        if isinstance(source.get("source"), str):
            source_names.append(source["source"])
        if source.get("doctor_status") != "passed":
            errors.append(f"{field}.doctor_status must be passed")
        _timestamp(source.get("observed_at"), f"{field}.observed_at", errors)
        if source.get("data_through") is not None:
            _date(source.get("data_through"), f"{field}.data_through", errors)
        if source.get("sanitized") is not True:
            errors.append(f"{field}.sanitized must be true")
    if len(source_names) != len(set(source_names)):
        errors.append("context.sources source values must be unique")

    freshness = _mapping(context.get("freshness"), "context.freshness", errors)
    _keys(freshness, "context.freshness", ("activities", "recovery", "calendar"), errors)
    for field in ("activities", "recovery", "calendar"):
        _enum(freshness.get(field), f"context.freshness.{field}", FRESHNESS, errors)
        if freshness.get(field) != "fresh":
            warnings.append(f"{field} freshness is {freshness.get(field)}; do not infer missing data")

    coverage = _mapping(context.get("coverage"), "context.coverage", errors)
    coverage_fields = ("sleep", "hrv", "resting_hr")
    _keys(coverage, "context.coverage", coverage_fields, errors)
    for field in coverage_fields:
        _validate_coverage(coverage.get(field), f"context.coverage.{field}", errors)

    goal = _mapping(context.get("goal_context"), "context.goal_context", errors)
    _keys(
        goal,
        "context.goal_context",
        (
            "plan_id",
            "plan_version",
            "primary_goal",
            "maintenance_goal",
            "measurement_protocol",
            "measurement",
        ),
        errors,
    )
    _nonempty(goal.get("plan_id"), "context.goal_context.plan_id", errors)
    _integer(goal.get("plan_version"), "context.goal_context.plan_version", errors, minimum=1)
    _nonempty(goal.get("primary_goal"), "context.goal_context.primary_goal", errors)
    if goal.get("maintenance_goal") is not None:
        _nonempty(goal.get("maintenance_goal"), "context.goal_context.maintenance_goal", errors)
    # Required and non-null: PlanState cannot exist without it, so an absent protocol here
    # would be a build bug, and reporting it as "unknown" would let a review quietly judge
    # the outcome against something the cycle never declared.
    _nonempty(goal.get("measurement_protocol"), "context.goal_context.measurement_protocol", errors)
    # Present and nullable, unlike the prose above: a cycle may genuinely have declared no
    # runnable measurement, and null says so. Absent would be a build that forgot to ask.
    if goal.get("measurement") is not None:
        _keys(
            _mapping(goal["measurement"], "context.goal_context.measurement", errors),
            "context.goal_context.measurement",
            ("reference_session_id", "measurement_week_start", "compare"),
            errors,
        )
    _validate_measurement_evidence(context, goal, errors)

    # The natural-week and cycle coordinates a review is framed on. Dates only: which week
    # the athlete is in, which week just ended, and how far into the declared cycle today
    # sits. Nothing here judges any of it.
    frame = _mapping(context.get("review_frame"), "context.review_frame", errors)
    frame_dates = (
        "week_start",
        "week_end",
        "previous_week_start",
        "previous_week_end",
        "cycle_start",
        "cycle_end",
        # The earliest day this context reports session by session (issue #233). A date
        # like the others, and checked like the others; what it bounds is asserted
        # below, where the rows it bounds are validated.
        "detail_horizon_start",
    )
    _keys(frame, "context.review_frame", (*frame_dates, "cycle_day"), errors)
    parsed_frame = {
        field: _date(frame.get(field), f"context.review_frame.{field}", errors)
        for field in frame_dates
    }
    # The one invariant the field's meaning rests on: a natural week starts on Monday. A
    # rolling seven-day span landing here would read as the athlete's week and silently
    # answer a different question.
    if parsed_frame["week_start"] is not None and parsed_frame["week_start"].weekday() != 0:
        errors.append("context.review_frame.week_start must be a Monday")
    if frame.get("cycle_day") is not None:
        _integer(frame.get("cycle_day"), "context.review_frame.cycle_day", errors, minimum=1)
    # The horizon may never fall after the previous week's Monday: the week review reads
    # those rows, and reconciliation matches the plan's own week against them. It may
    # fall after cycle_start, and that is not an error -- an overrunning cycle can
    # declare a start before the six weeks the provider was read on, and the horizon is
    # clamped to what was actually read rather than naming a day nobody looked at.
    horizon = parsed_frame["detail_horizon_start"]
    previous_week_start = parsed_frame["previous_week_start"]
    if horizon is not None and previous_week_start is not None and horizon > previous_week_start:
        errors.append(
            "context.review_frame.detail_horizon_start is later than previous_week_start"
        )

    constraints = _mapping(context.get("constraints"), "context.constraints", errors)
    constraint_fields = (
        "available_days",
        "unavailable_days",
        "availability_source",
        "session_minutes",
        "red_flags",
        "leg_fatigue",
        "soreness",
        "schedule_changed",
        "equipment_changed",
    )
    # Optional for the same reason as the groups above: a context built before it existed
    # is still a context, and an empty list is what "none stated" already looks like.
    _keys(
        constraints,
        "context.constraints",
        constraint_fields,
        errors,
        optional=("week_constraints",),
    )
    if constraints.get("week_constraints") is not None:
        _string_array(
            constraints.get("week_constraints"),
            "context.constraints.week_constraints",
            errors,
        )
    weekdays = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    available = _string_array(
        constraints.get("available_days"),
        "context.constraints.available_days",
        errors,
        allowed=weekdays,
    )
    # ``unavailable_days`` (issue #28) is a separate statement, not the complement of the
    # one above: an athlete names the day they lost without thereby confirming the rest.
    # The one thing that cannot be true is a day appearing in both.
    unavailable = _string_array(
        constraints.get("unavailable_days"),
        "context.constraints.unavailable_days",
        errors,
        allowed=weekdays,
    )
    contradictory = sorted(set(available) & set(unavailable))
    if contradictory:
        errors.append(
            "context.constraints names "
            f"{', '.join(contradictory)} as both available and unavailable"
        )
    # Which of the two authors spoke: this turn's request, the athlete's stored evidence,
    # or neither. Null is not "unknown provenance" -- it is the absence of any statement,
    # and it is the only value that may sit beside an empty available_days.
    if constraints.get("availability_source") not in ("request", "athlete_evidence", None):
        errors.append(
            "context.constraints.availability_source must be request, athlete_evidence, or null"
        )
    if constraints.get("session_minutes") is not None:
        _integer(constraints.get("session_minutes"), "context.constraints.session_minutes", errors, minimum=1)
    red_flags = _mapping(constraints.get("red_flags"), "context.constraints.red_flags", errors)
    red_flag_fields = ("pain", "illness", "chest_pain", "dizziness", "unusual_symptoms")
    _keys(red_flags, "context.constraints.red_flags", red_flag_fields, errors)
    for field in red_flag_fields:
        value = red_flags.get(field)
        # Checked by type, not by `in (True, False, None)`: equality coerces 1 == True
        # and 0 == False, so an integer would read as a valid flag here while the
        # daily safety gate tests `is True` and would not see it as a symptom.
        if not (isinstance(value, bool) or value is None):
            errors.append(f"context.constraints.red_flags.{field} must be true, false, or null")
        elif value is not False:
            warnings.append(f"red flag {field} is not explicitly false")
    for field in ("leg_fatigue", "soreness"):
        _enum(
            constraints.get(field),
            f"context.constraints.{field}",
            {"normal", "elevated", "severe", "unknown"},
            errors,
        )
    for field in ("schedule_changed", "equipment_changed"):
        if constraints.get(field) not in (True, False, None):
            errors.append(f"context.constraints.{field} must be true, false, or null")

    _validate_athlete_baseline(context.get("athlete_baseline"), "context.athlete_baseline", errors)
    _validate_baseline_evidence(
        context.get("baseline_evidence"), "context.baseline_evidence", errors
    )

    actuals = _list(context.get("recent_actuals"), "context.recent_actuals", errors)
    actual_fields = (
        "activity_id",
        "date",
        "sport",
        "planned_session_id",
        "match_confidence",
        "adaptation",
        "body_stress",
        "cost",
        "duration_minutes",
        "completion",
        "elevation_gain_m",
        "subjective_feel",
    )
    actual_optional_fields = (
        "paired_event_id",
        "distance_km",
        "average_pace_sec_per_km",
        "average_hr",
        # The athlete's own name for a strength session, straight from the provider
        # ("chest day"). Optional because only a provider that carries one emits it, and
        # a source without it is not thereby wrong.
        "session_label",
    )
    for index, raw in enumerate(actuals):
        field = f"context.recent_actuals[{index}]"
        actual = _mapping(raw, field, errors)
        _keys(actual, field, actual_fields, errors, optional=actual_optional_fields)
        _nonempty(actual.get("activity_id"), f"{field}.activity_id", errors)
        _date(actual.get("date"), f"{field}.date", errors)
        _enum(actual.get("sport"), f"{field}.sport", SPORTS - {"rest"}, errors)
        if actual.get("paired_event_id") is not None:
            _nonempty(actual.get("paired_event_id"), f"{field}.paired_event_id", errors)
        if actual.get("planned_session_id") is not None:
            _nonempty(actual.get("planned_session_id"), f"{field}.planned_session_id", errors)
        _enum(actual.get("match_confidence"), f"{field}.match_confidence", {"matched", "owned", "probable", "unmatched", "unknown"}, errors)
        # Null is a real value for these three on an actual, not a missing field: the
        # builders classify running by the athlete's own threshold and strength by what
        # a session is, and for any other sport they state nothing rather than run a
        # running-pace heuristic over a swim (AGENTS.md 3 -- unknown stays unknown).
        for name, vocabulary in (
            ("adaptation", ADAPTATIONS),
            ("body_stress", BODY_STRESS),
            ("cost", COSTS),
        ):
            if actual.get(name) is not None:
                _enum(actual.get(name), f"{field}.{name}", vocabulary, errors)
        if actual.get("duration_minutes") is not None:
            _integer(actual.get("duration_minutes"), f"{field}.duration_minutes", errors, minimum=1)
        _number_or_null(actual.get("distance_km"), f"{field}.distance_km", errors, minimum=0)
        _integer_or_null(
            actual.get("average_pace_sec_per_km"),
            f"{field}.average_pace_sec_per_km",
            errors,
            minimum=1,
        )
        _number_or_null(actual.get("average_hr"), f"{field}.average_hr", errors, minimum=1)
        _string_or_null(actual.get("session_label"), f"{field}.session_label", errors)
        _enum(actual.get("completion"), f"{field}.completion", {"completed", "partial", "skipped"}, errors)
        _number_or_null(actual.get("elevation_gain_m"), f"{field}.elevation_gain_m", errors, minimum=0)
        _integer_or_null(
            actual.get("subjective_feel"), f"{field}.subjective_feel", errors, minimum=1, maximum=5
        )

    trends = _mapping(context.get("recovery_trends"), "context.recovery_trends", errors)
    _keys(trends, "context.recovery_trends", ("sleep", "hrv", "resting_hr"), errors)
    for field in ("sleep", "hrv", "resting_hr"):
        _validate_trend(trends.get(field), f"context.recovery_trends.{field}", errors)

    calendar = _list(context.get("current_calendar"), "context.current_calendar", errors)
    calendar_fields = ("session_id", "date", "sport", "cost", "status")
    for index, raw in enumerate(calendar):
        field = f"context.current_calendar[{index}]"
        item = _mapping(raw, field, errors)
        _keys(item, field, calendar_fields, errors)
        _nonempty(item.get("session_id"), f"{field}.session_id", errors)
        _date(item.get("date"), f"{field}.date", errors)
        _enum(item.get("sport"), f"{field}.sport", SPORTS, errors)
        _enum(item.get("cost"), f"{field}.cost", COSTS, errors)
        _enum(item.get("status"), f"{field}.status", {"planned", "completed", "moved", "replaced", "missed"}, errors)

    # One row per session this cycle prescribed whose day has passed: the prescription
    # beside whatever came back for it. `match_status` is the plan's own value verbatim,
    # `partial` included -- a coach writes that, and nothing here derives it from a ratio.
    # Rest is absent by construction (``store.cycle_sessions``): it is not work, so there
    # is no execution to record against it.
    cycle_sessions = _list(context.get("cycle_sessions"), "context.cycle_sessions", errors)
    cycle_session_fields = (
        "session_id",
        "date",
        "week_start",
        "sport",
        "cost",
        "match_status",
        "planned_minutes",
        "prescription",
        "activity",
        "activity_evidence",
    )
    activity_fields = (
        "activity_id",
        "match_confidence",
        "duration_minutes",
        "distance_km",
        "average_pace_sec_per_km",
        "average_hr",
    )
    for index, raw in enumerate(cycle_sessions):
        field = f"context.cycle_sessions[{index}]"
        item = _mapping(raw, field, errors)
        _keys(item, field, cycle_session_fields, errors)
        _nonempty(item.get("session_id"), f"{field}.session_id", errors)
        _date(item.get("date"), f"{field}.date", errors)
        # The natural week this row belongs to, so a cycle reads week by week. Null only
        # when the date itself could not be parsed -- already an error above.
        if item.get("week_start") is not None:
            session_week = _date(item.get("week_start"), f"{field}.week_start", errors)
            if session_week is not None and session_week.weekday() != 0:
                errors.append(f"{field}.week_start must be a Monday")
        _enum(item.get("sport"), f"{field}.sport", SPORTS - {"rest"}, errors)
        _enum(item.get("cost"), f"{field}.cost", COSTS, errors)
        _enum(
            item.get("match_status"),
            f"{field}.match_status",
            {"planned", "completed", "partial", "moved", "replaced", "missed"},
            errors,
        )
        _integer_or_null(item.get("planned_minutes"), f"{field}.planned_minutes", errors, minimum=0)
        if item.get("prescription") is not None:
            _nonempty(item.get("prescription"), f"{field}.prescription", errors)
        # An absent activity means one of three things, and the coach acts on only one of
        # them: "nothing of that sport attached to this session" is evidence about the
        # athlete; "that sport was trained that day but attached elsewhere" and "older
        # than anything this build read" are both evidence about the data.
        _enum(
            item.get("activity_evidence"),
            f"{field}.activity_evidence",
            {"attached", "athlete_reported", "none_found", "other_activity_same_day", "outside_evidence_window"},
            errors,
        )
        activity = item.get("activity")
        if activity is None:
            if item.get("activity_evidence") == "attached":
                errors.append(f"{field}.activity_evidence is attached but carries no activity")
            continue
        if item.get("activity_evidence") != "attached":
            errors.append(f"{field} carries an activity but does not report it as attached")
        activity = _mapping(activity, f"{field}.activity", errors)
        _keys(activity, f"{field}.activity", activity_fields, errors)
        _nonempty(activity.get("activity_id"), f"{field}.activity.activity_id", errors)
        _enum(
            activity.get("match_confidence"),
            f"{field}.activity.match_confidence",
            {"matched", "owned", "probable"},
            errors,
        )
        _integer_or_null(
            activity.get("duration_minutes"), f"{field}.activity.duration_minutes", errors, minimum=1
        )
        _number_or_null(activity.get("distance_km"), f"{field}.activity.distance_km", errors, minimum=0)
        _integer_or_null(
            activity.get("average_pace_sec_per_km"),
            f"{field}.activity.average_pace_sec_per_km",
            errors,
            minimum=1,
        )
        _number_or_null(activity.get("average_hr"), f"{field}.activity.average_hr", errors, minimum=1)

    _validate_strength_execution(context.get("strength_execution"), "context.strength_execution", errors)
    _validate_recovery_signals(context.get("recovery_signals"), "context.recovery_signals", errors)
    _validate_body_measurements(context.get("body_measurements"), "context.body_measurements", errors)
    _validate_reported_activities(
        context.get("reported_activities"), "context.reported_activities", errors
    )
    _validate_subjective_states(
        context.get("subjective_states"), "context.subjective_states", errors
    )
    _validate_segment_execution(
        context.get("segment_execution"), "context.segment_execution", errors
    )
    _validate_movement_history(
        context.get("movement_history"), "context.movement_history", errors
    )
    _validate_training_history(
        context.get("training_history"), "context.training_history", errors
    )

    _string_array(context.get("unknowns"), "context.unknowns", errors)
    privacy = _mapping(context.get("privacy"), "context.privacy", errors)
    privacy_fields = (
        "sanitized",
        "contains_raw_payloads",
        "contains_credentials",
        "contains_gps_tracks",
        "contains_connection_state",
    )
    _keys(privacy, "context.privacy", privacy_fields, errors)
    if privacy.get("sanitized") is not True:
        errors.append("context.privacy.sanitized must be true")
    for field in privacy_fields[1:]:
        if privacy.get(field) is not False:
            errors.append(f"context.privacy.{field} must be false")
    return _report(errors, warnings)


def _validate_workout_step(
    raw: Any,
    field: str,
    errors: list[str],
    *,
    repeat_child: bool = False,
) -> None:
    step = _mapping(raw, field, errors)
    kind = step.get("kind")
    if kind == "repeat":
        if repeat_child:
            errors.append(f"{field} must not contain a nested repeat")
        _keys(step, field, ("kind", "repetitions", "steps"), errors)
        _integer(step.get("repetitions"), f"{field}.repetitions", errors, minimum=1)
        children = _list(step.get("steps"), f"{field}.steps", errors)
        if not children:
            errors.append(f"{field}.steps must contain at least one work step")
        for index, child in enumerate(children):
            _validate_workout_step(
                child,
                f"{field}.steps[{index}]",
                errors,
                repeat_child=True,
            )
        return
    if kind != "work":
        errors.append(f"{field}.kind must be work or repeat")
        return

    _keys(step, field, ("kind", "name", "duration", "target"), errors)
    _nonempty(step.get("name"), f"{field}.name", errors)
    duration = _mapping(step.get("duration"), f"{field}.duration", errors)
    duration_kind = duration.get("kind")
    if duration_kind == "time":
        _keys(duration, f"{field}.duration", ("kind", "seconds"), errors)
        _integer(duration.get("seconds"), f"{field}.duration.seconds", errors, minimum=1)
    elif duration_kind == "distance":
        _keys(duration, f"{field}.duration", ("kind", "meters"), errors)
        _integer(duration.get("meters"), f"{field}.duration.meters", errors, minimum=1)
    else:
        errors.append(f"{field}.duration.kind must be time or distance")

    target = _mapping(step.get("target"), f"{field}.target", errors)
    target_kind = target.get("kind")
    if target_kind == "open":
        _keys(target, f"{field}.target", ("kind",), errors)
    elif target_kind == "pace":
        _keys(
            target,
            f"{field}.target",
            ("kind", "unit", "low_seconds_per_km", "high_seconds_per_km"),
            errors,
        )
        if target.get("unit") != "sec_per_km":
            errors.append(f"{field}.target.unit must be sec_per_km")
        low = target.get("low_seconds_per_km")
        high = target.get("high_seconds_per_km")
        _integer(low, f"{field}.target.low_seconds_per_km", errors, minimum=1)
        _integer(high, f"{field}.target.high_seconds_per_km", errors, minimum=1)
        if (
            isinstance(low, int)
            and not isinstance(low, bool)
            and isinstance(high, int)
            and not isinstance(high, bool)
            and low > high
        ):
            errors.append(
                f"{field}.target.low_seconds_per_km must be <= high_seconds_per_km"
            )
    elif target_kind == "hr_ceiling":
        # No floor/low field on purpose: the provider doc-JSON shape can only carry
        # a ceiling (start pinned to 0), so a lower bound must be unrepresentable
        # here rather than merely undocumented (#38: a floor silently reached the
        # watch through a %hr denominator swap).
        if repeat_child:
            errors.append(f"{field}.target.kind hr_ceiling is not allowed inside a repeat")
        _keys(target, f"{field}.target", ("kind", "unit", "ceiling_bpm"), errors)
        if target.get("unit") != "bpm":
            errors.append(f"{field}.target.unit must be bpm")
        _integer(target.get("ceiling_bpm"), f"{field}.target.ceiling_bpm", errors, minimum=1)
    else:
        errors.append(f"{field}.target.kind must be open, pace, or hr_ceiling")


def _iter_step_targets(steps: Any) -> Iterable[dict[str, Any]]:
    """Yield every work step's target object in a workout, at any nesting depth."""
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("kind") == "work":
            target = step.get("target")
            if isinstance(target, dict):
                yield target
        elif step.get("kind") == "repeat":
            yield from _iter_step_targets(step.get("steps"))


def _validate_time_axis_plan(plan: dict[str, Any], field: str, errors: list[str]) -> None:
    _nonempty(plan.get("name"), f"{field}.name", errors)
    if isinstance(plan.get("name"), str) and len(plan["name"]) > 80:
        errors.append(f"{field}.name must be at most 80 characters")
    steps = _list(plan.get("steps"), f"{field}.steps", errors)
    if not steps:
        errors.append(f"{field}.steps must contain at least one step")
    for index, step in enumerate(steps):
        _validate_workout_step(step, f"{field}.steps[{index}]", errors)
    # One target per session (#38 constraint 4): pace and heart rate must never
    # both bind the device on the same workout.
    target_kinds = {target.get("kind") for target in _iter_step_targets(steps)}
    if {"pace", "hr_ceiling"} <= target_kinds:
        errors.append(f"{field} must not mix pace and hr_ceiling targets; one target per session")


def _validate_strength_movement(raw: Any, field: str, errors: list[str]) -> None:
    """Validate one planned movement's shape.

    Nullable exactly where reality is: a set taken to failure has no rep count, and a
    bodyweight movement has no kg figure. What must not be nullable is *which* of those
    an absent load means -- that is `load_basis`, which the deleted free-text path had to
    recover by looking for 自重 or 待確認 in a sentence.

    The coherence rule below is the one structural block this object adds. A movement that declares
    bodyweight and carries 60 kg contradicts itself inside one object, and the evidence
    gate downstream would have to pick which half to believe -- reinstating the guess
    this field exists to remove. A warning cannot do that job, because the gate has to
    act on one of the two values either way. Both intents stay expressible: a bodyweight
    movement leaves the loads null, and a weighted variant of it is measured_baseline
    with the anchor filled in.
    """
    movement = _mapping(raw, field, errors)
    _keys(movement, field, STRENGTH_MOVEMENT_FIELDS, errors)
    _nonempty(movement.get("exercise"), f"{field}.exercise", errors)
    _nonempty(movement.get("display_name"), f"{field}.display_name", errors)
    _integer(movement.get("sets"), f"{field}.sets", errors, minimum=1)
    if movement.get("reps") is not None:
        _integer(movement.get("reps"), f"{field}.reps", errors, minimum=1)
    _number_or_null(movement.get("load_kg"), f"{field}.load_kg", errors, minimum=0)
    _number_or_null(movement.get("assist_kg"), f"{field}.assist_kg", errors, minimum=0)
    basis = movement.get("load_basis")
    _enum(basis, f"{field}.load_basis", STRENGTH_LOAD_BASES, errors)
    carries_load = any(
        _finite_number(movement.get(key)) is not None for key in ("load_kg", "assist_kg")
    )
    if basis == "measured_baseline" and not carries_load:
        errors.append(f"{field}.load_basis measured_baseline requires a load_kg or assist_kg figure")
    if basis in {"bodyweight", "pending_confirmation"} and carries_load:
        errors.append(f"{field}.load_basis {basis} must leave load_kg and assist_kg null")


def _validate_movement_list_plan(plan: dict[str, Any], field: str, errors: list[str]) -> None:
    movements = _list(plan.get("movements"), f"{field}.movements", errors)
    if not movements:
        errors.append(f"{field}.movements must contain at least one movement")
    for index, movement in enumerate(movements):
        _validate_strength_movement(movement, f"{field}.movements[{index}]", errors)


def _validate_session_plan(raw: Any, field: str, errors: list[str]) -> None:
    """Validate one session's plan against the model it declares.

    `kind` is the discriminator and the only one: which branch runs is decided by how the
    session is executed, never by which sport it belongs to. That is what makes a new
    sport a one-line enum change -- a swim is a time axis with an intensity target, the
    same as a run, and it needs no field and no branch of its own here.

    A session that declares nothing still declares which nothing it is. `unstructured` is
    an explicit answer, not a missing field, which is why `plan` is required: "no
    structure" and "structure the model forgot" were the same state for as long as the
    field was optional, and telling them apart is the entire point.
    """
    plan = _mapping(raw, field, errors)
    kind = plan.get("kind")
    if kind not in SESSION_PLAN_KINDS:
        errors.append(f"{field}.kind must be one of {', '.join(sorted(SESSION_PLAN_KINDS))}")
        return
    _keys(plan, field, SESSION_PLAN_FIELDS[kind], errors)
    if kind == "time_axis":
        _validate_time_axis_plan(plan, field, errors)
    elif kind == "movement_list":
        _validate_movement_list_plan(plan, field, errors)


def _validate_session(raw: Any, field: str, errors: list[str], warnings: list[str]) -> None:
    session = _mapping(raw, field, errors)
    fields = (
        "session_id",
        "sport",
        "scheduled_date",
        "time_window",
        "purpose",
        "adaptation",
        "body_stress",
        "cost",
        "priority",
        "planned_minutes",
        "hard",
        "plan",
        "prescription",
        "fallback",
        "execution",
        "match_status",
    )
    # Every field above is required, `plan` and `prescription` included. No general
    # `optional=` set: a PlanState stored before this shape existed does not open at all,
    # deliberately -- "optional because history lacks it" is what kept the free-text path
    # alive through five repairs, and the athlete regenerates their plan once instead.
    #
    # `measures` is the one exception, and for the opposite reason: it says this ordinary
    # session is also the cycle's measurement point (issue #75), which most sessions are
    # not. Requiring it would mean writing "not the measurement" on every session in every
    # plan, which is how a field stops being read.
    #
    # `coach_note` is the second (issue #56): one sentence from the coach about this
    # session, which travels to Intervals inside the delivered description. Optional for
    # the same reason -- most sessions carry none, and requiring an empty string on every
    # one of them is how a field stops being read.
    _keys(session, field, fields, errors, optional=("measures", "coach_note"))
    _nonempty(session.get("session_id"), f"{field}.session_id", errors)
    if "measures" in session:
        _nonempty(session.get("measures"), f"{field}.measures", errors)
    _enum(session.get("sport"), f"{field}.sport", SPORTS, errors)
    _date(session.get("scheduled_date"), f"{field}.scheduled_date", errors)
    if session.get("time_window") is not None:
        _nonempty(session.get("time_window"), f"{field}.time_window", errors)
    _nonempty(session.get("purpose"), f"{field}.purpose", errors)
    # The intent line may say what the session is for; it may not prescribe (issue #99).
    # A number written here has no anchor for the evidence gate to read -- issue #38's
    # `5x1000m @5:50/km` sat in this field for two days -- and issue #93 closed the same
    # surface on `prescription` by generating it, leaving this the one authored field
    # left. The refusal lives in `intent_text` rather than here because the guard below
    # this module is right that the validator reads no prose; `intent_text` carries that
    # rule with it and its own AST test pins what it may do with the value. AGENTS.md 6 is
    # documented in that module: the invariant, the observed harm, why a warning and an
    # instruction to the model have both already failed, what stays writable, and the
    # false-positive cost.
    prescribed = prescribed_token_in_intent(session)
    if prescribed is not None:
        errors.append(
            f"{field}.purpose states intent only, so move {prescribed!r} into "
            f"{field}.plan, where the evidence gate can read the anchor behind it"
        )
    if "coach_note" in session:
        _nonempty(session.get("coach_note"), f"{field}.coach_note", errors)
        # The same rule, run by the same scan, on the second authored field (issue #56).
        # A note is prose the athlete reads on the day and it rides to the watch inside
        # the delivered description, so a number written here has no more anchor behind it
        # than one written in the intent line -- and one field being checked while the
        # other is not would simply move the 2026-08-13 shape one field over.
        noted = prescribed_token_in_coach_note(session)
        if noted is not None:
            errors.append(
                f"{field}.coach_note is what you want to say about this session, so move "
                f"{noted!r} into {field}.plan, where the evidence gate can read the "
                "anchor behind it"
            )
    _enum(session.get("adaptation"), f"{field}.adaptation", ADAPTATIONS, errors)
    _enum(session.get("body_stress"), f"{field}.body_stress", BODY_STRESS, errors)
    _enum(session.get("cost"), f"{field}.cost", COSTS, errors)
    _enum(session.get("priority"), f"{field}.priority", PRIORITIES, errors)
    if session.get("planned_minutes") is None:
        warnings.append(f"{field}.planned_minutes is unknown")
    else:
        _integer(session.get("planned_minutes"), f"{field}.planned_minutes", errors)
    if not isinstance(session.get("hard"), bool):
        errors.append(f"{field}.hard must be boolean")
    elif session.get("hard") != (session.get("cost") == "hard"):
        errors.append(f"{field}.hard must match cost=hard")
    plan_errors: list[str] = []
    _validate_session_plan(session.get("plan"), f"{field}.plan", plan_errors)
    errors.extend(plan_errors)
    # The prescription is a rendering of the plan, so the only value it may hold is the
    # one the renderer produces. Checked here rather than left to the write paths because
    # this is what makes "generated, never authored" true of the *artifact*: a plan that
    # arrives through the CLI with a hand-written sentence is refused exactly like one
    # that arrives through the gateway. A rendering cannot disagree with its source, and
    # this is the check that keeps it a rendering.
    #
    # The cost is real and accepted: changing the renderer changes every stored plan's
    # prescription, so it is a schema change and needs the same regeneration. That is the
    # price of the sentence and the structure never drifting apart -- which they did, five
    # times, while prose was an input.
    #
    # Any language the renderer speaks is accepted, not only the athlete's current one.
    # The check that matters is that the sentence is a rendering rather than prose
    # somebody wrote, and every rendering of one plan says the same thing. Pinning it to
    # the athlete's current language instead would make changing that language fail the
    # entire commit history at once -- every plan they trained under, rendered in the
    # language they used at the time -- which is the store refusing to open over a
    # cosmetic fact.
    if not plan_errors:
        renderings = [
            render_prescription(session.get("plan"), language)
            for language in PRESCRIPTION_LANGUAGES
        ]
        if session.get("prescription") not in renderings:
            wanted = " or ".join(repr(text) for text in dict.fromkeys(renderings))
            errors.append(
                f"{field}.prescription is generated from {field}.plan and must read {wanted}"
            )
    else:
        _nonempty(session.get("prescription"), f"{field}.prescription", errors)
    fallback = _mapping(session.get("fallback"), f"{field}.fallback", errors)
    _keys(fallback, f"{field}.fallback", ("action", "description"), errors)
    _enum(fallback.get("action"), f"{field}.fallback.action", {"reduce", "move", "replace", "rest"}, errors)
    _nonempty(fallback.get("description"), f"{field}.fallback.description", errors)
    execution = _mapping(session.get("execution"), f"{field}.execution", errors)
    _keys(
        execution,
        f"{field}.execution",
        ("publish_supported", "external_id", "delivery_state"),
        errors,
        optional=("superseded_external_id",),
    )
    if not isinstance(execution.get("publish_supported"), bool):
        errors.append(f"{field}.execution.publish_supported must be boolean")
    if execution.get("external_id") is not None:
        _nonempty(execution.get("external_id"), f"{field}.execution.external_id", errors)
    # A confirmed change can invalidate content Intervals already accepted. The event does
    # not disappear when the plan moves on, so the id it was delivered under is held here
    # until it is either overwritten by the replacement delivery or withdrawn (issue #113).
    superseded = execution.get("superseded_external_id")
    if superseded is not None:
        _nonempty(superseded, f"{field}.execution.superseded_external_id", errors)
        if superseded == execution.get("external_id"):
            errors.append(
                f"{field}.execution.superseded_external_id is the event currently "
                "delivered, so nothing about it is superseded"
            )
    delivery_state = execution.get("delivery_state")
    external_id = execution.get("external_id")
    _enum(
        delivery_state,
        f"{field}.execution.delivery_state",
        {"not_published", "intervals_accepted"},
        errors,
    )
    if delivery_state == "not_published" and external_id is not None:
        errors.append(f"{field}.execution.external_id must be null while not_published")
    if delivery_state == "intervals_accepted":
        if not isinstance(external_id, str) or not external_id.strip():
            errors.append(f"{field}.execution.external_id is required after Intervals acceptance")
        if execution.get("publish_supported") is not True:
            errors.append(f"{field}.execution.publish_supported must be true after Intervals acceptance")
    _enum(session.get("match_status"), f"{field}.match_status", {"planned", "completed", "partial", "moved", "replaced", "missed"}, errors)


_ACTIVITY_EVIDENCE = (
    "attached",
    "other_activity_same_day",
    "athlete_reported",
    "outside_evidence_window",
    "none_found",
)


def _validate_measurement_evidence(
    context: dict[str, Any], goal: dict[str, Any], errors: list[str]
) -> None:
    """Whether the cycle's two readings are in yet -- an observation, never a verdict.

    Kept out of ``goal_context`` on purpose: that block is bound to project the PlanState
    exactly, and whether an activity came back is not something the plan can say. It is
    the same kind of fact ``cycle_sessions`` reports for every other session, in the same
    vocabulary, plus the two states only a measurement has.

    Null exactly when the cycle declared no runnable measurement, so the two can never
    disagree about whether there is one.
    """
    evidence = context.get("measurement_evidence")
    if (evidence is None) != (goal.get("measurement") is None):
        errors.append(
            "context.measurement_evidence must be present exactly when "
            "context.goal_context.measurement is"
        )
        return
    if evidence is None:
        return
    evidence = _mapping(evidence, "context.measurement_evidence", errors)
    _keys(
        evidence,
        "context.measurement_evidence",
        ("comparison_session_id", "reference_result", "comparison_result"),
        errors,
    )
    if evidence.get("comparison_session_id") is not None:
        _nonempty(
            evidence.get("comparison_session_id"),
            "context.measurement_evidence.comparison_session_id",
            errors,
        )
    _enum(
        evidence.get("reference_result"),
        "context.measurement_evidence.reference_result",
        {*_ACTIVITY_EVIDENCE, "not_in_record"},
        errors,
    )
    _enum(
        evidence.get("comparison_result"),
        "context.measurement_evidence.comparison_result",
        {*_ACTIVITY_EVIDENCE, "scheduled", "not_scheduled"},
        errors,
    )


def _validate_measurement(
    goal: dict[str, Any],
    week: dict[str, Any],
    cycle_start: dt.date | None,
    cycle_end: dt.date | None,
    week_start: dt.date | None,
    errors: list[str],
    warnings: list[str],
) -> None:
    """The runnable half of a cycle's protocol (issues #13, #75).

    Structural only, and narrowly: the measurement week has to be a week of this cycle,
    because a measurement scheduled outside the window it judges measures nothing. What
    the comparison *is*, and what the two readings mean, stay entirely with the coach --
    there is no threshold here, no pass, and no number this function reads.

    The one thing it will say out loud is a warning: the measurement week has arrived and
    no session in it repeats the reference. That is the accountability gap #75 named --
    a protocol nobody schedules is indistinguishable from no protocol -- and it is a
    warning rather than an error because the coach may be mid-way through writing the
    week, and blocking a plan to demand a session is the validator prescribing training.
    """
    measurement = goal.get("measurement")
    if measurement is None:
        return
    measurement = _mapping(measurement, "plan.goal.measurement", errors)
    _keys(
        measurement,
        "plan.goal.measurement",
        ("reference_session_id", "measurement_week_start", "compare"),
        errors,
    )
    _nonempty(
        measurement.get("reference_session_id"),
        "plan.goal.measurement.reference_session_id",
        errors,
    )
    _nonempty(measurement.get("compare"), "plan.goal.measurement.compare", errors)
    measurement_week = _date(
        measurement.get("measurement_week_start"),
        "plan.goal.measurement.measurement_week_start",
        errors,
    )
    if measurement_week is None or cycle_start is None or cycle_end is None:
        return
    if not cycle_start <= measurement_week <= cycle_end or (
        (measurement_week - cycle_start).days % 7
    ):
        errors.append(
            "plan.goal.measurement.measurement_week_start must be one of this cycle's "
            "own weeks"
        )
        return
    sessions = week.get("sessions")
    marked = [
        session
        for session in (sessions if isinstance(sessions, list) else [])
        if isinstance(session, dict) and session.get("measures")
    ]
    if week_start != measurement_week:
        if marked:
            # A marker outside the measurement week is inert -- the context only reads
            # one inside it -- so this says so rather than blocking. A coach who moved
            # the comparison meant to move the measurement week with it.
            warnings.append(
                "plan.goal.measurement names the week of "
                f"{measurement_week.isoformat()}, and this week's "
                f"{marked[0].get('session_id')!r} carries measures; a comparison outside "
                "the measurement week is not read as one"
            )
        return
    if not marked:
        warnings.append(
            "plan.goal.measurement names this week and no session in it repeats "
            f"{measurement.get('reference_session_id')!r}; the cycle's own measurement "
            "is not on the calendar"
        )


def _validate_outlook(
    cycle: dict[str, Any],
    week_start: dt.date | None,
    cycle_end: dt.date | None,
    errors: list[str],
    warnings: list[str],
) -> None:
    """The rest of the cycle, as a view rather than as a plan (issue #61).

    What is checked here is only that these entries are the weeks they claim to be:
    each one seven days after the last, following ``plan.week.start``, inside the cycle,
    at most three of them. That is structural, and it is what makes the four-week view a
    view *of this cycle* instead of four paragraphs.

    Deliberately *not* "consecutive Mondays". Nothing requires ``plan.week.start`` to be
    a Monday -- that has always been true and is not this field's to change -- so the
    outlook follows whatever weekday the plan's own week begins on. The Monday-to-Sunday
    frame a review is answered in is a separate coordinate (``review_frame``) and stays
    separate.

    What is deliberately *not* checked is anything about their content. An outlined week
    is prose about shape and magnitude; whether the shape is right is coaching, and a
    validator picking at it would be the shadow coach AGENTS.md 5 forbids. Nor is
    completeness an error: a cycle whose later weeks are genuinely undecided is a real
    state, and blocking on it would force the coach to invent a week to satisfy a
    schema. It warns instead, because an athlete shown two of three remaining weeks
    should be able to tell that the third was left out rather than does not exist.

    Nothing in here can be delivered. The entries carry no session id and no execution
    block, so the delivery, reconciliation and staleness paths -- all of which read
    ``plan.week.sessions`` -- cannot see them at all. That is by construction rather
    than by a rule, which is why there is no rule about it here.
    """
    outlook = _list(cycle.get("outlook"), "plan.cycle.outlook", errors)
    if len(outlook) > 3:
        errors.append("plan.cycle.outlook covers at most the three weeks after this one")
    for index, raw in enumerate(outlook):
        # Each week's target is computed from the current week, not from whatever the
        # entry before it happened to say. Chaining off the previous value makes one
        # wrong date move every message after it, so a coach fixing three errors is
        # reading three dates that are themselves wrong.
        expected = (
            None if week_start is None else week_start + dt.timedelta(days=7 * (index + 1))
        )
        field = f"plan.cycle.outlook[{index}]"
        entry = _mapping(raw, field, errors)
        _keys(
            entry,
            field,
            ("week_start", "intent", "key_sessions", "relation_to_primary"),
            errors,
        )
        _nonempty(entry.get("intent"), f"{field}.intent", errors)
        _nonempty(entry.get("relation_to_primary"), f"{field}.relation_to_primary", errors)
        _string_array(entry.get("key_sessions"), f"{field}.key_sessions", errors, minimum=1)
        start = _date(entry.get("week_start"), f"{field}.week_start", errors)
        if start is None or expected is None:
            continue
        if start != expected:
            errors.append(
                f"{field}.week_start must be {expected.isoformat()}: the outlook is the "
                "weeks that follow this one, in order"
            )
        if cycle_end is not None and start > cycle_end:
            errors.append(f"{field}.week_start falls outside this cycle")
    if week_start is None or cycle_end is None:
        return
    remaining = max((cycle_end - week_start).days // 7, 0)
    if len(outlook) < remaining:
        warnings.append(
            f"plan.cycle.outlook covers {len(outlook)} of the {remaining} week(s) left in "
            "this cycle; the athlete sees a shorter direction than the cycle has"
        )


def validate_plan_state(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate the current 28-day goal and mixed weekly plan."""

    errors: list[str] = []
    warnings: list[str] = []
    _keys(
        plan,
        "plan",
        ("schema_version", "plan_id", "version", "status", "goal", "cycle", "week"),
        errors,
        # athlete_baseline arrived after plans were already stored; see _keys.
        optional=("athlete_baseline",),
    )
    if plan.get("schema_version") != PLAN_STATE_SCHEMA_VERSION:
        errors.append(f"plan.schema_version must be {PLAN_STATE_SCHEMA_VERSION}")
    _nonempty(plan.get("plan_id"), "plan.plan_id", errors)
    _integer(plan.get("version"), "plan.version", errors, minimum=1)
    _enum(plan.get("status"), "plan.status", {"draft", "active", "completed", "stopped"}, errors)
    # A plan predating the baseline field carries no baseline at all, which is different
    # from carrying one whose fields are all unknown -- only validate a baseline present.
    if "athlete_baseline" in plan:
        _validate_athlete_baseline(plan.get("athlete_baseline"), "plan.athlete_baseline", errors)

    goal = _mapping(plan.get("goal"), "plan.goal", errors)
    _keys(
        goal,
        "plan.goal",
        ("outcome", "measurement_protocol"),
        errors,
        # Optional so a cycle already in flight, which declared its protocol in prose
        # before any of this existed, is not invalidated by the change (issue #13).
        optional=("measurement",),
    )
    _nonempty(goal.get("outcome"), "plan.goal.outcome", errors)
    _nonempty(goal.get("measurement_protocol"), "plan.goal.measurement_protocol", errors)

    cycle = _mapping(plan.get("cycle"), "plan.cycle", errors)
    cycle_fields = (
        "start",
        "end",
        "primary_adaptation",
        "maintenance_adaptation",
        "planned_evidence",
        "adjust_conditions",
        "stop_conditions",
        "outlook",
    )
    _keys(cycle, "plan.cycle", cycle_fields, errors)
    cycle_start = _date(cycle.get("start"), "plan.cycle.start", errors)
    cycle_end = _date(cycle.get("end"), "plan.cycle.end", errors)
    if cycle_start and cycle_end and (cycle_end - cycle_start).days != 27:
        errors.append("plan.cycle must span exactly 28 calendar days inclusive")
    _enum(cycle.get("primary_adaptation"), "plan.cycle.primary_adaptation", ADAPTATIONS, errors)
    if cycle.get("maintenance_adaptation") is not None:
        _enum(cycle.get("maintenance_adaptation"), "plan.cycle.maintenance_adaptation", ADAPTATIONS, errors)
    for field in ("planned_evidence", "adjust_conditions", "stop_conditions"):
        _string_array(cycle.get(field), f"plan.cycle.{field}", errors, minimum=1)

    week = _mapping(plan.get("week"), "plan.week", errors)
    _keys(week, "plan.week", ("start", "intent", "sessions"), errors)
    week_start = _date(week.get("start"), "plan.week.start", errors)
    _validate_outlook(cycle, week_start, cycle_end, errors, warnings)
    _validate_measurement(goal, week, cycle_start, cycle_end, week_start, errors, warnings)
    _nonempty(week.get("intent"), "plan.week.intent", errors)
    sessions = _list(week.get("sessions"), "plan.week.sessions", errors)
    if not sessions:
        errors.append("plan.week.sessions must contain at least one session")
    session_ids: list[str] = []
    primary_anchor = False
    for index, raw in enumerate(sessions):
        field = f"plan.week.sessions[{index}]"
        _validate_session(raw, field, errors, warnings)
        if isinstance(raw, dict):
            if isinstance(raw.get("session_id"), str):
                session_ids.append(raw["session_id"])
            scheduled = _date(raw.get("scheduled_date"), f"{field}.scheduled_date", [])
            if week_start and scheduled and not 0 <= (scheduled - week_start).days <= 6:
                errors.append(f"{field}.scheduled_date must fall in the current week")
            if cycle_start and cycle_end and scheduled and not cycle_start <= scheduled <= cycle_end:
                errors.append(f"{field}.scheduled_date must fall in the cycle")
            if raw.get("priority") == "anchor" and raw.get("adaptation") == cycle.get("primary_adaptation"):
                primary_anchor = True
    if len(session_ids) != len(set(session_ids)):
        errors.append("plan.week session_id values must be unique")
    if sessions and not primary_anchor:
        warnings.append("plan.week has no remaining anchor for the primary adaptation")
    return _report(errors, warnings)


def _validate_event_provenance(event: dict[str, Any], errors: list[str]) -> None:
    """Validate who authored this decision and what it overturns.

    Both fields are optional and both answer questions the plan could not answer
    before: which model and skill version produced a decision, and whether a later
    decision corrected it, outgrew it, or was invalidated by a product change.
    Those three are not interchangeable -- an athlete deserves to know when their
    plan changed because the product changed rather than because they did.
    """
    author = event.get("authored_by")
    if author is not None:
        author_map = _mapping(author, "event.authored_by", errors)
        if author_map:
            _keys(author_map, "event.authored_by", ("model",), errors,
                  optional=("skill_version", "harness"))
            _nonempty(author_map.get("model"), "event.authored_by.model", errors)

    supersedes = event.get("supersedes")
    if supersedes is not None:
        sup = _mapping(supersedes, "event.supersedes", errors)
        if sup:
            _keys(sup, "event.supersedes", ("event_id", "kind", "reason"), errors)
            _nonempty(sup.get("event_id"), "event.supersedes.event_id", errors)
            _enum(sup.get("kind"), "event.supersedes.kind", SUPERSEDE_KINDS, errors)
            _nonempty(sup.get("reason"), "event.supersedes.reason", errors)
            if sup.get("event_id") == event.get("event_id"):
                errors.append("event.supersedes.event_id must not be the event itself")

    if event.get("initiative") is not None:
        _enum(event.get("initiative"), "event.initiative", INITIATIVES, errors)


def validate_decision_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate DecisionEvent structure and its fixed vocabulary."""

    errors: list[str] = []
    warnings: list[str] = []
    fields = (
        "schema_version",
        "event_id",
        "mode",
        "plan_id",
        "plan_version_before",
        "plan_version_after",
        "action",
        "session_id",
        "inputs_used",
        "evidence",
        "unknowns",
        "reason_codes",
        "change",
        "goal_effect",
        "next_review_condition",
        "created_at",
    )
    # Provenance arrived after events were already stored; see _keys.
    _keys(event, "event", fields, errors,
          optional=("authored_by", "supersedes", "initiative", "trigger"))
    _validate_event_provenance(event, errors)
    if event.get("schema_version") != DECISION_EVENT_SCHEMA_VERSION:
        errors.append(f"event.schema_version must be {DECISION_EVENT_SCHEMA_VERSION}")
    _nonempty(event.get("event_id"), "event.event_id", errors)
    _nonempty(event.get("plan_id"), "event.plan_id", errors)
    _integer(event.get("plan_version_before"), "event.plan_version_before", errors)
    _integer(event.get("plan_version_after"), "event.plan_version_after", errors, minimum=1)
    mode = event.get("mode")
    _enum(mode, "event.mode", set(MODE_ACTIONS), errors)
    if mode in MODE_ACTIONS:
        _enum(event.get("action"), "event.action", MODE_ACTIONS[mode], errors)
    if event.get("session_id") is not None:
        _nonempty(event.get("session_id"), "event.session_id", errors)
    _string_array(event.get("inputs_used"), "event.inputs_used", errors, minimum=1)
    evidence = _list(event.get("evidence"), "event.evidence", errors)
    if not evidence:
        errors.append("event.evidence must contain at least one item")
    for index, raw in enumerate(evidence):
        field = f"event.evidence[{index}]"
        item = _mapping(raw, field, errors)
        _keys(item, field, ("field", "observation"), errors)
        _nonempty(item.get("field"), f"{field}.field", errors)
        _nonempty(item.get("observation"), f"{field}.observation", errors)
    _string_array(event.get("unknowns"), "event.unknowns", errors)
    _string_array(event.get("reason_codes"), "event.reason_codes", errors, minimum=1, allowed=REASON_CODES)
    change = _mapping(event.get("change"), "event.change", errors)
    _keys(change, "event.change", ("before", "after", "summary"), errors)
    for field in ("before", "after", "summary"):
        _nonempty(change.get(field), f"event.change.{field}", errors)
    effect = _mapping(event.get("goal_effect"), "event.goal_effect", errors)
    _keys(effect, "event.goal_effect", ("week", "cycle"), errors)
    _nonempty(effect.get("week"), "event.goal_effect.week", errors)
    _nonempty(effect.get("cycle"), "event.goal_effect.cycle", errors)
    _nonempty(event.get("next_review_condition"), "event.next_review_condition", errors)
    _timestamp(event.get("created_at"), "event.created_at", errors)
    return _report(errors, warnings)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sessions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    value = plan.get("week", {}).get("sessions", [])
    return value if isinstance(value, list) else []


def _session_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        session["session_id"]: session
        for session in _sessions(plan)
        if isinstance(session, dict) and isinstance(session.get("session_id"), str)
    }


def _check_changed_delivery_content_resets_observation(
    before: dict[str, Any],
    after: dict[str, Any],
    errors: list[str],
) -> None:
    before_sessions = _session_map(before)
    after_sessions = _session_map(after)
    before_week = before.get("week") if isinstance(before.get("week"), dict) else {}
    after_week = after.get("week") if isinstance(after.get("week"), dict) else {}
    if before_week.get("start") == after_week.get("start"):
        for session_id in before_sessions.keys() - after_sessions.keys():
            before_execution = before_sessions[session_id].get("execution") or {}
            if before_execution.get("delivery_state") == "intervals_accepted":
                errors.append(
                    f"same-week plan removed delivered session {session_id}; preserve its "
                    "session_id and reset, move, replace, or redeliver it explicitly"
                )
    for session_id in before_sessions.keys() & after_sessions.keys():
        before_session = before_sessions[session_id]
        before_execution = before_session.get("execution") or {}
        if before_execution.get("delivery_state") != "intervals_accepted":
            continue
        after_session = after_sessions[session_id]
        if delivery_session_content(before_session) == delivery_session_content(after_session):
            continue
        after_execution = after_session.get("execution") or {}
        if (
            after_execution.get("delivery_state") != "not_published"
            or after_execution.get("external_id") is not None
        ):
            errors.append(
                f"session {session_id} changed delivered workout content; reset "
                "execution.delivery_state to not_published and external_id to null"
            )


def _expected_goal_context(plan: dict[str, Any]) -> dict[str, Any]:
    cycle = plan.get("cycle") or {}
    goal = plan.get("goal") or {}
    primary = " ".join(
        f"{cycle.get('primary_adaptation')} — {goal.get('outcome')}".split()
    )
    return {
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("version"),
        "primary_goal": primary,
        "maintenance_goal": cycle.get("maintenance_adaptation"),
        # Bound like the rest: a review that judges the outcome has to be judging it
        # against the protocol this exact plan version declared, not one edited since.
        "measurement_protocol": goal.get("measurement_protocol"),
        # Verbatim, and bound like the rest: a review reading a measurement the plan no
        # longer declares would be judging the outcome against a protocol nobody set.
        "measurement": goal.get("measurement") or None,
    }


def _expected_context_baseline(plan: dict[str, Any]) -> dict[str, Any]:
    baseline = plan.get("athlete_baseline")
    if isinstance(baseline, dict):
        return baseline
    return {
        "threshold_pace_sec_per_km": None,
        "max_hr": None,
        "easy_hr_ceiling": None,
        "longest_recent_run_km": None,
        "weekly_volume_km_4wk_avg": None,
        "max_session_minutes": None,
        "strength_loads": [],
    }


def _expected_current_calendar(plan: dict[str, Any]) -> list[dict[str, Any]]:
    statuses = {
        "planned": "planned",
        "completed": "completed",
        "partial": "completed",
        "moved": "moved",
        "replaced": "replaced",
        "missed": "missed",
    }
    return [
        {
            "session_id": session.get("session_id"),
            "date": session.get("scheduled_date"),
            "sport": session.get("sport"),
            "cost": session.get("cost"),
            "status": statuses.get(session.get("match_status")),
        }
        for session in _sessions(plan)
        if isinstance(session, dict)
    ]


def _check_context_projects_before_plan(
    context: dict[str, Any],
    before: dict[str, Any],
    errors: list[str],
) -> None:
    """Bind decision evidence to the exact PlanState the model actually saw."""
    if _canonical(context.get("goal_context")) != _canonical(_expected_goal_context(before)):
        errors.append("context.goal_context must exactly project the before PlanState")
    if _canonical(context.get("athlete_baseline")) != _canonical(_expected_context_baseline(before)):
        errors.append("context.athlete_baseline must exactly project the before PlanState")
    if _canonical(context.get("current_calendar")) != _canonical(_expected_current_calendar(before)):
        errors.append("context.current_calendar must exactly project the before PlanState")


def _actionable_trained_sessions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every running or strength session in this plan the athlete is still going to do."""
    return [
        session
        for session in _sessions(plan)
        if isinstance(session, dict)
        and session.get("match_status") in ACTIONABLE_MATCH_STATUSES
        and session.get("sport") in {"running", "strength"}
    ]


def _actionable_sessions_for_event(
    after: dict[str, Any],
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    if event.get("reason_codes") == ["planned_actual_reconciled"]:
        return []
    sessions = _actionable_trained_sessions(after)
    if event.get("mode") in {"plan_cycle", "plan_week", "review_cycle", "review_week"}:
        return sessions
    if event.get("mode") == "revisit_today":
        session_id = event.get("session_id")
        return [session for session in sessions if session.get("session_id") == session_id]
    return []


def _session_plan(session: dict[str, Any]) -> dict[str, Any]:
    """This session's plan object, or an empty mapping when it does not carry one.

    The single place that reads the field, so every check downstream asks the same
    question of the same object. A malformed plan is already an error on the same bundle
    from `_validate_session_plan`, so the checks here only have to be safe on it, not to
    re-report it.
    """
    plan = session.get("plan")
    return plan if isinstance(plan, dict) else {}


def _plan_kind(session: dict[str, Any]) -> Any:
    return _session_plan(session).get("kind")


def _plan_steps(session: dict[str, Any]) -> Any:
    return _session_plan(session).get("steps")


def plan_movements(session: dict[str, Any]) -> list[dict[str, Any]]:
    movements = _session_plan(session).get("movements")
    if not isinstance(movements, list):
        return []
    return [movement for movement in movements if isinstance(movement, dict)]


# The execution model each trained sport is planned under. Only the two sports this
# product actually trains are listed: a session it does not train -- mobility, recovery,
# rest, or a sport added later -- is not asked to be executable, which is what keeps
# adding one from touching this file.
REQUIRED_PLAN_KIND_BY_SPORT = {"running": "time_axis", "strength": "movement_list"}


def _check_actionable_sessions_declare_executable_work(
    sessions: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
) -> None:
    """Require every adopted running/strength session to declare what it executes.

    Executability is now a structural fact and only a structural fact. A running session
    is executed along a time axis, so a run that declares `unstructured` has declared
    that there is nothing to execute -- the "go for a run" case the old text patterns
    existed to catch, and the only part of that job that survives them. Everything else
    those patterns tried to reconstruct -- sets, reps, the basis of a load, whether an
    intensity dimension was named -- is a recorded field now, validated where it lives.

    A strength session is executed as a list of movements and normally carries one, but
    declaring `unstructured` instead is the athlete's own decision (2026-08-14), not a
    defect: it adopts with a warning naming what the blank costs -- nothing on that
    session is verified against the baseline, and its record has nothing to reconcile
    against what was lifted. The asymmetry is the delivery boundary. A run's structure
    is what the watch executes; a strength session publishes as a title either way, so
    an absent list crosses nothing and may lower confidence but not block (invariant 5).

    The kind binding itself still runs both ways, which is where issue #100's refusal
    now lives. It used to be a request-shape rule -- `strength_movements` was rejected
    unless the session's sport was strength -- and a request shape can only speak about
    the request in front of it. Here the same claim is checked against the plan being
    adopted, so a run cannot end up prescribing lifted work whether the movements
    arrived by add, replace or reduce, or were left behind by a session that changed
    sport.

    What is not checked here is which target, which movements, or which wording: the
    plan's own validation owns the shape, the evidence gates below own the numbers, and
    the rest is coaching judgment a blocking validator does not own (invariant 5). The
    false-positive cost is bounded to one case, and it has an escape that costs nothing:
    a run genuinely left to the athlete is a time axis with an `open` target, which
    states the duration and prescribes no intensity.
    """
    for session in sessions:
        if not isinstance(session, dict):
            continue
        required = REQUIRED_PLAN_KIND_BY_SPORT.get(session.get("sport"))
        if required is None:
            continue
        session_id = session.get("session_id", "?")
        declared = _plan_kind(session)
        if declared == required:
            pass
        elif declared == "unstructured" and session.get("sport") == "strength":
            warnings.append(
                f"adopted strength session {session_id} declares no quantified "
                "structure; nothing is verified against the athlete's baseline"
            )
        elif declared == "unstructured":
            errors.append(
                f"adopted {session.get('sport')} session {session_id} must carry a "
                f"{required} plan; an unstructured one prescribes nothing to do"
            )
        else:
            # Naming the model it did declare, because the two ways to get this wrong
            # need different repairs: an unstructured session is missing a prescription,
            # a mismatched one is carrying somebody else's.
            errors.append(
                f"adopted {session.get('sport')} session {session_id} must carry a "
                f"{required} plan; a {declared} plan is not how this sport is executed"
            )
        if session.get("sport") == "running":
            planned = session.get("planned_minutes")
            if not isinstance(planned, int) or isinstance(planned, bool) or planned <= 0:
                errors.append(f"adopted running session {session_id} needs known positive planned_minutes")


def _check_rest_days_prescribe_nothing(plan: dict[str, Any], errors: list[str]) -> None:
    """The other end of issue #100's refusal: a rest day may not carry work.

    The check above requires a trained sport to declare the model it is executed under.
    This one denies the one sport defined by executing nothing, and it cannot be said
    through REQUIRED_PLAN_KIND_BY_SPORT because rest sits outside every actionability
    filter this file has -- ``store.cycle_sessions`` drops rest days entirely, since there
    is no execution to record against them, and the load and intensity gates select by
    execution model rather than by sport. So a rest day carrying a movement list is seen
    by nothing else, renders as a set of lifts, and reaches the athlete as work on the day
    the plan told them to stop. Issue #100 refused it as a request-shape rule; a rest day
    now declares `unstructured`, like the model it always meant.

    Every session, not only the actionable ones: a rest day that has already passed is no
    more able to have prescribed lifts than one still ahead, and no legitimate rest day
    has ever carried anything else, so there is no false positive to trade against.
    """
    for session in _sessions(plan):
        if not isinstance(session, dict) or session.get("sport") != "rest":
            continue
        if _plan_kind(session) != "unstructured":
            errors.append(
                f"rest session {session.get('session_id', '?')} must carry an "
                "unstructured plan; a rest day prescribes nothing to do"
            )


def _planned_minutes(plan: dict[str, Any]) -> int | None:
    """The week's total planned minutes, or None when any session's figure is unknown.

    A session that is not even an object counts as unknown rather than raising: the shape
    error is already this plan's own blocking error, and these two totals are now read for
    every bundle carrying an explicit symptom, where a crash would replace a refusal.
    """
    values = [
        session.get("planned_minutes") if isinstance(session, dict) else None
        for session in _sessions(plan)
    ]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    return sum(values)


def _hard_count(plan: dict[str, Any]) -> int:
    return sum(
        session.get("hard") is True for session in _sessions(plan) if isinstance(session, dict)
    )


def _check_structured_intensity_has_measured_anchor(
    after: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    """Require every intensity target a device will enforce to stand on a measured anchor.

    This is the evidence boundary, and it is the whole of it: an exact pace or an exact
    heart rate looks equally precise whether the athlete's own measurements support it or
    not, and the watch obeys it either way. How far a VO2, threshold or repetition pace
    may sit from threshold is coaching judgment and stays that way -- what is refused is
    only the number that stands on nothing.

    It reads targets, not sentences. Three consequences worth stating, because they are
    what the free-text version could never have:

    - punctuation, language and wording cannot change the verdict, so the same session
      written in Chinese and in English is read identically;
    - a target the schema cannot express cannot arrive at all. `%HR` had its own pattern
      and its own anchor rule; the structured vocabulary has no percentage-of-maximum
      target and no heart-rate floor, so both are refused by being unrepresentable
      (#38: a %hr denominator swap put a floor on a recovery run);
    - what is left unanchored is left unanchored deliberately: an `open` target names no
      number, and needs none.

    Sport is not consulted. A time axis is a time axis, so a swim prescribing 1:45/100m
    as a pace target would be held to the same anchor as a run.
    """
    threshold = baseline.get("threshold_pace_sec_per_km")
    has_threshold = (
        isinstance(threshold, int) and not isinstance(threshold, bool) and threshold > 0
    )
    max_hr = baseline.get("max_hr")
    has_measured_max_hr = isinstance(max_hr, int) and not isinstance(max_hr, bool) and max_hr > 0
    easy_hr_ceiling = baseline.get("easy_hr_ceiling")
    has_easy_hr_ceiling = (
        isinstance(easy_hr_ceiling, int)
        and not isinstance(easy_hr_ceiling, bool)
        and easy_hr_ceiling > 0
    )

    for session in _sessions(after):
        if not isinstance(session, dict) or _plan_kind(session) != "time_axis":
            continue
        session_id = session.get("session_id", "?")
        targets = list(_iter_step_targets(_plan_steps(session)))

        paces = [
            target["low_seconds_per_km"]
            for target in targets
            if target.get("kind") == "pace"
            and isinstance(target.get("low_seconds_per_km"), int)
            and not isinstance(target.get("low_seconds_per_km"), bool)
        ]
        if paces and not has_threshold:
            errors.append(
                f"session {session_id} prescribes an exact pace target but "
                "athlete_baseline.threshold_pace_sec_per_km is not measured; without a "
                "measured anchor, prescribe an open or heart-rate target and state how "
                "to calibrate"
            )

        ceilings = [
            target["ceiling_bpm"]
            for target in targets
            if target.get("kind") == "hr_ceiling"
            and isinstance(target.get("ceiling_bpm"), int)
            and not isinstance(target.get("ceiling_bpm"), bool)
        ]
        if not ceilings:
            continue
        # The gate exists to stop invented intensity precision, not to insist on a
        # laboratory max_hr specifically. An athlete's own stated easy-HR ceiling is
        # exactly the evidence it asks for, so it anchors an hr_ceiling target too --
        # taking the max of whichever bounds are actually stated keeps the rule
        # monotonic and judgment-free.
        hr_anchors: list[tuple[str, int]] = []
        if has_measured_max_hr:
            hr_anchors.append(("max_hr", max_hr))
        if has_easy_hr_ceiling:
            hr_anchors.append(("easy_hr_ceiling", easy_hr_ceiling))
        if not hr_anchors:
            errors.append(
                f"session {session_id} prescribes an hr_ceiling target without a "
                "measured athlete_baseline.max_hr or a stated easy_hr_ceiling anchor"
            )
            continue
        anchor_field, anchor_value = max(hr_anchors, key=lambda pair: pair[1])
        for ceiling in ceilings:
            if ceiling > anchor_value:
                errors.append(
                    f"session {session_id} hr_ceiling {ceiling}bpm exceeds "
                    f"athlete_baseline.{anchor_field} {anchor_value}"
                )


def normalize_exercise_name(value: Any) -> str:
    # Keep every word character rather than ASCII only. Baselines carry canonical keys
    # like "split_squat" while a plan may name the same lift in the athlete's own
    # language, so dropping non-ASCII made every Chinese movement silently unmatchable.
    return " ".join(re.findall(r"[^\W_]+", str(value).lower(), re.UNICODE))


def baseline_exercise_aliases(load: dict[str, Any]) -> list[str]:
    names = (load.get("exercise"), load.get("display_name"))
    return [alias for alias in map(normalize_exercise_name, names) if alias]


def _actionable_movement_list_sessions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every session in this plan the athlete is still going to do that prescribes loads.

    Selected by execution model, not by sport: the load gate reads `movements`, so the
    sessions it applies to are exactly the ones that have movements to read.
    """
    return [
        session
        for session in _sessions(plan)
        if isinstance(session, dict)
        and _plan_kind(session) == "movement_list"
        and session.get("match_status") in ACTIONABLE_MATCH_STATUSES
    ]


def _movement_list_sessions_requiring_precision_check(
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    if event.get("reason_codes") == ["planned_actual_reconciled"]:
        return []
    actionable = _actionable_movement_list_sessions(after)
    if event.get("mode") in {"plan_cycle", "plan_week"}:
        return actionable
    before_sessions = _session_map(before)
    if event.get("mode") in {"revisit_today", "review_cycle", "review_week"}:
        return [
            session
            for session in actionable
            if session.get("session_id") == event.get("session_id")
            or _canonical(before_sessions.get(session.get("session_id"))) != _canonical(session)
        ]
    return []


def _measured_anchors(load: dict[str, Any]) -> list[float]:
    """Every figure this baseline entry actually measured.

    Which column holds the measurement is a property of the lift, not of the wording: an
    assisted lift records assist_kg and leaves load_kg empty. Reading both is what lets
    the gate accept an assisted movement without inferring assistance from the word
    "assist" appearing in a sentence.
    """
    return [
        float(anchor)
        for anchor in (load.get("load_kg"), load.get("assist_kg"))
        if isinstance(anchor, (int, float)) and not isinstance(anchor, bool)
    ]


def anchoring_baseline(
    exercise: Any,
    established: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The baseline entry a planned movement names, by canonical key or display_name.

    Field to field, on the normalized names `_baseline_exercise_aliases` already
    produces -- no second naming scheme, and "bench_press" and "bench press" are one
    name because normalization drops the separator either way.

    A name that IS some entry's own key wins outright, before any display_name gets a
    say. Nothing forbids one entry's display_name equalling another entry's key, and
    plain list order would then hand a name that matches an entry verbatim to whichever
    entry happens to sit first -- reading loads against the wrong baseline. With no such
    collision the two passes answer identically, so ordinary data is unaffected.
    """
    name = normalize_exercise_name(exercise)
    if not name:
        return None
    for load in established:
        if name == normalize_exercise_name(load.get("exercise")):
            return load
    for load in established:
        if name in baseline_exercise_aliases(load):
            return load
    return None


def _check_planned_loads_have_matching_baseline(
    sessions: list[dict[str, Any]],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    """Require every exact kg load to name a lift the athlete has actually measured.

    What must not reach the athlete is unchanged and is not new: an exact kg load their
    own measurements do not support, which looks exactly as precise as one they do. What
    changed is where the evidence comes from. `doctor-store` re-runs the whole commit
    history with no conversation present, so a rule that read prose depended on a reader
    that is not there and had to re-derive the plan's numbers identically every time; a
    recorded field is simply read.

    So no clause splitting, no unit vocabulary, no rep-count patterns, and no punctuation
    -- there is no sentence to split. `load_basis` says which of the three things an
    absent load means, so a bodyweight movement and a lift still to be tested are told
    apart by a value rather than by whether someone wrote 自重 or TBD. A load written in
    another unit is not a concern of this gate either: the Coach converts at authoring
    time, and load_kg is the only load it reads.

    The movement is named in the error because the athlete's fix is per movement: measure
    that lift, or say the load is bodyweight or still to be confirmed.
    """
    established = [
        load
        for load in (baseline.get("strength_loads") or [])
        if isinstance(load, dict) and baseline_exercise_aliases(load)
    ]
    for session in sessions:
        for index, movement in enumerate(plan_movements(session)):
            if movement.get("load_basis") != "measured_baseline":
                continue  # bodyweight and pending_confirmation prescribe no kg figure at all
            anchor = anchoring_baseline(movement.get("exercise"), established)
            if anchor is not None and _measured_anchors(anchor):
                continue
            errors.append(
                f"session {session.get('session_id', '?')} prescribes an exact kg load "
                "without a matching established strength baseline for "
                f"plan.movements[{index}] {movement.get('exercise')!r}; use bodyweight "
                "or pending_confirmation, or measure the anchor first"
            )


def _check_max_session_minutes(
    after: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    max_minutes = baseline.get("max_session_minutes")
    if max_minutes is None:
        warnings.append(
            "unknown: athlete_baseline.max_session_minutes is not set; "
            "skipped session duration check"
        )
        return
    for session in _sessions(after):
        if not isinstance(session, dict):
            continue
        # The ceiling describes how long the athlete is willing to *run* in one go: it
        # comes from the time an evening session can take, not from a limit on training
        # in general. Strength work routinely runs longer at a fraction of the systemic
        # cost, so applying the running ceiling to it would block plans that merely
        # record what the athlete already does -- which is how a plan ends up
        # understating real training load.
        if session.get("sport") != "running":
            continue
        planned = session.get("planned_minutes")
        if isinstance(planned, bool) or not isinstance(planned, int):
            continue  # unknown planned_minutes is already flagged by _validate_session
        if planned > max_minutes:
            errors.append(
                f"running session {session.get('session_id', '?')} planned_minutes {planned} "
                f"exceeds athlete_baseline max_session_minutes {max_minutes}"
            )


def validate_adopted_plan(
    plan: dict[str, Any],
    *,
    red_flags: Any = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Ask of one plan alone what ``validate_bundle`` asks of a plan being adopted.

    ``validate_bundle`` needs a before plan, an event and a context; a first plan has
    none of the three, so its whole athlete-fitness half was unreachable on the one path
    where the athlete has no prior plan to fall back on. Every check below is that same
    check, reading the plan's own ``athlete_baseline`` instead of the context's copy of
    it -- PlanState is the authority for that object either way (``context_core``).

    No new rule, then, but two concrete harms it now catches at creation. A first week
    could tell a watch to run 1000 m repeats at 6:00/km, hold 155 bpm, or squat 80 kg for
    an athlete who never gave a threshold pace, a maximum heart rate, or that lift, while
    the identical edit a week later was refused. And a session that declared nothing to
    execute could enter v1 and then block *every* later change, because a week review
    re-checks the whole week: a store the athlete could not move.

    A warning is not enough for either: the pace, the ceiling and the kg figure reach the
    athlete and the device regardless, and the number's only authority is that a coach
    stated it. What stays possible is everything an unmeasured athlete should be doing
    anyway -- open targets, a heart-rate ceiling once a measured maximum or a stated
    easy ceiling exists, and ``load_basis: pending_confirmation`` for a lift still to
    be tested. The
    false-positive cost is bounded by where the anchors come from: the athlete's own
    answers, in the same request that carries the sessions.

    The last check from ``validate_bundle`` is the explicit-symptom boundary (#84), and
    it needs evidence rather than a before plan: the symptoms the athlete stated in the
    turn that authored this one. There is no context on this path to hold them -- an
    account with no PlanState is reported as such and no context is built for it -- so
    the caller passes what the athlete said, with ``today`` naming their own day. Both
    default to nothing, and nothing is unknown: a plan authored without them is validated
    exactly as it was before this existed, never as an all-clear (issue #19).
    """
    errors: list[str] = []
    warnings: list[str] = []
    raw = plan.get("athlete_baseline")
    baseline = raw if isinstance(raw, dict) else {}
    _check_structured_intensity_has_measured_anchor(plan, baseline, errors)
    _check_planned_loads_have_matching_baseline(
        _actionable_movement_list_sessions(plan), baseline, errors
    )
    _check_max_session_minutes(plan, baseline, errors, warnings)
    _check_actionable_sessions_declare_executable_work(
        _actionable_trained_sessions(plan), errors, warnings
    )
    _check_rest_days_prescribe_nothing(plan, errors)
    _check_first_plan_symptom_boundary(plan, red_flags, today, errors)
    return _report(errors, warnings)


def _check_change_is_material(
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
    errors: list[str],
) -> None:
    """Reject a decision that claims to change training but only rewrites prose.

    Every revision costs the athlete something: a new version to read, a workout
    possibly re-pushed to the watch, and one more entry in a history they are
    supposed to be able to trust. A decision that moves no number, no date and no
    target is not worth that cost -- and there is already a vocabulary for saying
    so, `keep` with `plan_kept_no_material_change`.

    This is deliberately a field-class test rather than a magnitude threshold.
    Changing a quality run's target from 5:50/km to 6:00/km moves ten seconds and
    is one of the most consequential edits available; shortening a session by ten
    minutes may be trivial. Which field moved carries the meaning, not how far.
    """
    action = event.get("action")
    if action not in {"reduce", "move", "replace", "rest", "adjust", "create"}:
        return  # keep / human_review / continue are not claiming a change

    before_sessions = _session_map(before)
    after_sessions = _session_map(after)
    if set(before_sessions) != set(after_sessions):
        return  # adding or removing a session is material by definition
    for key in ("goal", "cycle", "athlete_baseline"):
        if before.get(key) != after.get(key):
            return

    for session_id, after_session in after_sessions.items():
        before_session = before_sessions[session_id]
        moved = {
            field for field in set(before_session) | set(after_session)
            if before_session.get(field) != after_session.get(field)
        }
        if moved & MATERIAL_SESSION_FIELDS:
            return

    errors.append(
        f"action {action} claims a change but nothing material moved; "
        "use keep with plan_kept_no_material_change instead"
    )


def _ownership_backed(
    context: dict[str, Any],
    plan: dict[str, Any],
    session: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    """Re-derive the "owned" attachment from the context, rather than trusting its label.

    The matcher writes ``match_confidence`` and this gate must be able to refuse a
    hand-built event that simply claims it. So every condition is recomputed here from
    the same two artifacts the matcher had: the day must hold exactly one activity of
    that sport and exactly one planned session of that sport, the product must have
    delivered that session, the provider must not have paired the activity to some other
    event, and the duration must sit inside the stated band.

    What this does not claim: that the athlete executed the prescription as written.
    Ownership plus an unambiguous day says the session was trained; how well is the
    coach's judgment, read from the actual's own numbers.
    """
    if actual.get("match_confidence") != "owned":
        return False
    paired_event_id = actual.get("paired_event_id")
    if paired_event_id is not None and str(paired_event_id):
        return False
    if not product_delivered(session):
        return False
    date = session.get("scheduled_date")
    sport = session.get("sport")
    if actual.get("date") != date or actual.get("sport") != sport:
        return False
    if not owned_duration_within_band(session.get("planned_minutes"), actual.get("duration_minutes")):
        return False

    actuals = context.get("recent_actuals")
    same_day_actuals = [
        entry
        for entry in (actuals if isinstance(actuals, list) else [])
        if isinstance(entry, dict) and entry.get("date") == date and entry.get("sport") == sport
    ]
    sessions = (plan.get("week") or {}).get("sessions")
    same_day_sessions = [
        entry
        for entry in (sessions if isinstance(sessions, list) else [])
        if isinstance(entry, dict)
        and entry.get("scheduled_date") == date
        and entry.get("sport") == sport
    ]
    return len(same_day_actuals) == 1 and len(same_day_sessions) == 1


def _positive_flags(red_flags: Any) -> list[str]:
    """Every symptom one ``red_flags`` object reports as explicitly present.

    ``is True`` by identity, never truthiness and never membership: null means the flag
    was not assessed and an absent field means nothing was asked, and both are unknown
    (AGENTS.md 3). Neither may trigger the boundaries below, and neither is evidence of
    safety either -- an unassessed athlete's plan stays exactly as free as it was.

    One reading for both routes, because the athlete's report is the same report whether
    it arrives inside a context or beside the request authoring their first plan.
    """
    if not isinstance(red_flags, dict):
        return []
    return sorted(field for field, value in red_flags.items() if value is True)


def _positive_red_flags(context: dict[str, Any]) -> list[str]:
    """Every symptom this context reports as explicitly present."""
    constraints = context.get("constraints")
    return _positive_flags(
        constraints.get("red_flags") if isinstance(constraints, dict) else None
    )


def _context_date(context: dict[str, Any]) -> dt.date | None:
    """The calendar day this context is about: the date its own ``as_of`` carries.

    The same day ``context_core.build_window`` calls ``window_end`` -- the local date as
    written in the timestamp, not a wall clock read at validation time. That is what lets
    the boundary below give one bundle the same verdict every time it is validated,
    whether that is now, on a retried apply, or in a much later re-read.
    """
    value = context.get("as_of")
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _trained_sessions_on(plan: dict[str, Any], date: dt.date) -> list[dict[str, Any]]:
    """Sessions this plan still asks the athlete to train on one exact day.

    Rest is excluded because the store already treats it as not-work: ``store.cycle_sessions``
    leaves rest days out entirely, since there is no execution to record against them.
    Everything else -- running, strength, mobility, recovery -- is work the athlete is
    being asked to do. So is a session moved *onto* this day by the very change being
    validated; ``after`` is read, so the question is always what the plan says now.

    A session whose day has already resolved -- completed, partial, missed -- is not in
    ACTIONABLE_MATCH_STATUSES and is not here: the plan is not asking for it, it is
    recording it, and an evening conversation must not be refused over training that
    already happened.
    """
    return [
        session
        for session in _sessions(plan)
        if isinstance(session, dict)
        and session.get("scheduled_date") == date.isoformat()
        and session.get("match_status") in ACTIONABLE_MATCH_STATUSES
        and session.get("sport") != "rest"
    ]


def _check_explicit_symptom_boundary(
    context: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
    errors: list[str],
) -> None:
    """An explicitly reported symptom limits today to rest or a human decision.

    **The invariant.** When ``context.constraints.red_flags`` carries at least one field
    whose value is exactly ``True``, a decision bundle may be adopted only if the plan it
    produces (a) asks the athlete to train nothing today, and (b) does not add load to the
    week -- neither total planned minutes nor hard sessions. "Today" is the day the
    context itself is about (``as_of``), and the two load figures are the ones the preview
    already shows the athlete. This reads the evidence in the context; it does not consult
    what the event calls itself, because whether a safety rule applies must not be a
    decision the party being regulated gets to make. The rule this replaces triggered on
    ``mode == "revisit_today"``, and since the gateway began deriving mode server-side
    (#71, #83) that mode cannot occur on the hosted path at all: the rule went from
    "bypassable" to "never runs" without a line of it changing (#84).

    **The harm.** The athlete says "今天胸口有點悶". The flag lands honestly in the
    context, produces a warning, and the product then hands back a plan whose today is
    a 50-minute interval session -- because the change was a week review, and the only
    gate that read the symptom was watching a mode that no longer exists on this route.
    That plan is the product telling a symptomatic athlete to train hard today, which is
    exactly the decision AGENTS.md 9 says must fall to a human. Trimming Friday while
    leaving today's session in place is not a reduction of what the athlete is asked to
    do *today*, and today is the day the symptom is about.

    **Why not a warning, and why only here.** Deterministic validation is not a shadow
    coach (AGENTS.md 5), and nearly everything this file could block, it does not: stale
    evidence, an unassessed flag, a missing recovery reading and a partial window all stay
    warnings, because the coach may reasonably decide either way from them. A warning
    works when a reader weighs it. This one has no reader: the plan is committed by the
    same turn that produced it, and the warning arrives in a field beside the week the
    athlete is already being shown. "Conflicts with an explicit positive safety signal" is
    one of the few hard blocks AGENTS.md 5 authorizes, and an explicit positive flag is
    the only input in the whole context that is not an inference -- it is the athlete's
    own report of a symptom, in their own words, with no reading, threshold or model
    judgment between them and the field.

    **What stays possible.** Everything except prescribing training today under a symptom:

    - resting today and reshaping the rest of the week in the same change -- the ordinary
      answer, and the one this rule steers toward;
    - reducing the week: fewer minutes, fewer hard sessions, moving a rest day, changing
      any day that is not today;
    - escalating: ``human_review`` that leaves the plan untouched, unchanged from #43;
    - any change at all on a day whose today is already rest, holds no session, or holds
      one that already happened -- the last is the evening conversation;
    - deterministic reconciliation, exempt below: it records what the athlete already did
      and prescribes nothing, and a symptomatic athlete must not lose the ability to have
      yesterday's run written down;
    - every ordinary decision for an athlete whose flags are false, null, or unasked.

    One edge this deliberately does not cover: publishing a workout that is *already* in
    the plan does not pass through here, so a symptom does not withdraw a delivered
    session. A first plan does not reach this rule either, for want of a context and a
    before plan -- it gets the same boundary from ``_check_first_plan_symptom_boundary``,
    which is the half of this that one plan alone can answer.

    **The false-positive cost.** Any one of the five flags reported true, however mild --
    a sore toe under ``pain`` -- couples a plan change to resting today. The athlete who
    wanted only to shorten Saturday must now also drop today's easy jog, or make no
    change. That cost is real and it is bounded in three ways: the flags are the athlete's
    own answer in that same conversation, so a symptom they call resolved is reported
    false and the boundary does not fire; nothing here prevents the athlete from training
    -- it refuses to *prescribe* it; and the escalation path leaves the existing plan
    exactly as it was. What it cannot defend against is a coach that misreports what the
    athlete said, which is a different failure from a rule that never runs.
    """
    positive = _positive_red_flags(context)
    if not positive:
        return
    reported = ", ".join(positive)

    if event.get("reason_codes") == ["planned_actual_reconciled"]:
        # Mechanical, and the same marker the executability and precision gates already
        # step aside for: this bundle moves match_status to record a fact, and asks the
        # athlete for nothing.
        return

    changed = _canonical(after) != _canonical(before)
    if event.get("action") == "human_review" and not changed:
        # The authorized exit at every level: prescribe nothing, change nothing, and hand
        # the decision to a person. This is what AGENTS.md 9 asks for.
        return

    if event.get("mode") == "revisit_today":
        # Where a single-session vocabulary exists, it still binds (#43): a daily decision
        # under an explicit symptom may only rest or escalate, so moving today's hard
        # session to Friday is refused here rather than merely emptying today.
        if event.get("action") not in {"human_review", "rest"}:
            errors.append(
                f"explicit red flag ({reported}) limits today to rest or human_review"
            )

    today = _context_date(context)
    if today is None:
        # Unreadable as_of with a symptom present: fail closed rather than skip. The
        # context validator refuses this bundle for the same field anyway.
        errors.append(
            f"explicit red flag ({reported}) cannot be applied: context.as_of does not "
            "name the day it is about"
        )
    else:
        trained_today = _trained_sessions_on(after, today)
        if trained_today:
            named = ", ".join(
                f"{session.get('session_id', '?')} {session.get('sport')}"
                for session in trained_today
            )
            errors.append(
                f"explicit red flag ({reported}) limits {today.isoformat()} to rest or a "
                f"human decision; this plan still trains today: {named}"
            )

    before_minutes = _planned_minutes(before)
    after_minutes = _planned_minutes(after)
    if before_minutes is None or after_minutes is None:
        if changed:
            errors.append(
                f"explicit red flag ({reported}) requires known planned minutes to prove "
                "this change adds no volume"
            )
    elif after_minutes > before_minutes:
        errors.append(
            f"explicit red flag ({reported}) forbids adding volume: weekly planned "
            f"minutes {before_minutes} -> {after_minutes}"
        )
    if _hard_count(after) > _hard_count(before):
        errors.append(
            f"explicit red flag ({reported}) forbids adding hard sessions: "
            f"{_hard_count(before)} -> {_hard_count(after)}"
        )


def _check_first_plan_symptom_boundary(
    plan: dict[str, Any],
    red_flags: Any,
    today: dt.date | None,
    errors: list[str],
) -> None:
    """The boundary above, on the one plan that has no context to read the symptom from.

    **The invariant.** When the athlete explicitly reports a symptom in the turn that
    authors their first plan, that plan may not ask them to train today. Same evidence
    (a field whose value is exactly ``True``), same day (theirs, not the server's), same
    refusal, same sentence. What differs is only where the report arrives from: a first
    plan is authored for an account that has no PlanState, so ``start_session`` builds no
    context and there is no ``constraints.red_flags`` to read -- the authoring request
    carries it or nothing does (issue #19).

    **The harm.** The athlete says "今天胸口有點悶，但幫我開始一個 VO2max 計畫". The
    symptom was stated as plainly as it ever is on the change path, and the first week
    then prescribes intervals for that same afternoon -- the exact decision AGENTS.md 9
    reserves for a person, reached because a plan's *first* version was the one version
    no deterministic check of this evidence could see. A week later the identical week,
    arriving as a change, is refused.

    **Parity, and where it stops.** The change-path rule has three refusals: training
    today, adding weekly minutes, and adding hard sessions. Only the first is a property
    of one plan; the other two compare an after plan against a before plan, and a first
    plan has no before. Reading the missing one as zero would make every first plan a
    volume increase and refuse them all, which is a stricter rule than any change gets --
    so those two are scoped to where their second operand exists. The mode-specific
    ``revisit_today`` clause and the reconciliation and ``human_review`` exemptions have
    no analogue either: there is no DecisionEvent here, and a first plan cannot record
    yesterday or leave an existing plan untouched.

    **What stays possible.** A first plan that rests today and trains the rest of the
    week; a first plan whose block starts tomorrow, or any day after; a first plan for
    an athlete who reported nothing, was not asked, or answered no; and -- since this
    refuses to *prescribe*, never to train -- an athlete who trains today anyway and
    reports it afterwards. The escalation the change path spells ``human_review`` is
    spelled here by not authoring a plan today, which costs the athlete a day of a block
    that does not exist yet.

    **The false-positive cost.** An athlete with a sore toe under ``pain`` who wants to
    start today must either start their block tomorrow or make today its rest day. That
    is one day, on the one path where nothing has been committed yet, against a symptom
    they stated themselves in that same conversation.
    """
    positive = _positive_flags(red_flags)
    if not positive:
        return
    reported = ", ".join(positive)

    if today is None:
        # A stated symptom with no day to apply it to: fail closed, exactly as the
        # change path does for a context whose as_of cannot be read.
        errors.append(
            f"explicit red flag ({reported}) cannot be applied: nothing names the day "
            "this plan is being authored on"
        )
        return

    trained_today = _trained_sessions_on(plan, today)
    if trained_today:
        named = ", ".join(
            f"{session.get('session_id', '?')} {session.get('sport')}"
            for session in trained_today
        )
        errors.append(
            f"explicit red flag ({reported}) limits {today.isoformat()} to rest or a "
            f"human decision; this plan still trains today: {named}"
        )


def _check_reconcile_semantics(
    context: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
    errors: list[str],
) -> None:
    """A reconcile event records one fact; it must be able to do nothing else.

    `planned_actual_reconciled` marks the mechanical transition "this actionable session
    was trained, and here is the activity attached to it". Structurally it rides the same review_week /
    adjust shape a human reconciliation already used, which also means -- verified by
    building one -- that without these checks any prescription edit could dress itself
    up as a reconciliation and pass. So the reason code buys deterministic limits: one
    bound session, only its match_status may move, only actionable -> completed, and the
    context must actually contain the attached completed actual being recorded --
    attached by provider identity, or by product ownership re-derived here. Anything
    more is a coaching decision and must say so with a coaching reason code.

    Scope, stated plainly: these fences bind events that carry this reason code -- which
    is every event the automated reconciler emits. A review_week/adjust event under a
    coaching reason code may still move match_status without a backing actual (store
    commit 7 did exactly that, by hand, before any actual was matchable); that is a
    coaching judgment left open on purpose, visible in history through its reason code,
    not a bypass of this fence.
    """
    reason_codes = event.get("reason_codes") or []
    if "planned_actual_reconciled" not in reason_codes:
        return
    if reason_codes != ["planned_actual_reconciled"]:
        errors.append(
            "planned_actual_reconciled stands alone; a mechanical transition has no "
            "second reason"
        )
    if event.get("mode") != "review_week" or event.get("action") != "adjust":
        errors.append("planned_actual_reconciled requires mode review_week with action adjust")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        errors.append("a reconcile event must bind exactly one session_id")
        return

    before_sessions = _session_map(before)
    after_sessions = _session_map(after)
    if set(before_sessions) != set(after_sessions):
        errors.append("a reconcile event must not add or remove sessions")
        return

    changed_ids: list[str] = []
    for sid, after_session in after_sessions.items():
        before_session = before_sessions[sid]
        moved = {
            field for field in set(before_session) | set(after_session)
            if before_session.get(field) != after_session.get(field)
        }
        if not moved:
            continue
        changed_ids.append(sid)
        if moved != {"match_status"}:
            errors.append(
                f"a reconcile event may only move match_status; session {sid} also moves "
                f"{sorted(moved - {'match_status'})}"
            )
    if changed_ids != [session_id]:
        errors.append(
            "a reconcile event must change the bound session's match_status and nothing else"
        )

    # Every top-level field except version and week.sessions must survive untouched --
    # including plan `status`: quietly stamping the plan completed/stopped while
    # "recording a fact" was a verified smuggling path.
    for field in ("plan_id", "schema_version", "status", "goal", "cycle", "athlete_baseline"):
        if before.get(field) != after.get(field):
            errors.append(f"a reconcile event must not change {field}")
    before_week = {k: v for k, v in (before.get("week") or {}).items() if k != "sessions"}
    after_week = {k: v for k, v in (after.get("week") or {}).items() if k != "sessions"}
    if before_week != after_week:
        errors.append("a reconcile event must not change week-level fields")

    before_status = before_sessions.get(session_id, {}).get("match_status")
    after_status = after_sessions.get(session_id, {}).get("match_status")
    if changed_ids == [session_id] and not (
        before_status in ACTIONABLE_MATCH_STATUSES and after_status == "completed"
    ):
        errors.append(
            "a reconcile event records actionable -> completed only; other transitions are "
            "coaching decisions"
        )

    # The backing actual must not just name the session -- it must carry either the
    # provider identity of the product-owned event, or the product's own ownership
    # evidence re-derived here from the context rather than trusted from the label the
    # matcher wrote. Date is not an identity field for the provider path: Intervals may
    # pair a workout completed after it was moved. The matcher is one-to-one, so two
    # attached actuals claiming one session is conflicting data; the proposer refuses to
    # write it, and this gate must refuse a hand-built event in the same situation rather
    # than passing on the one claim that fits.
    session = after_sessions.get(session_id, {})
    external_id = (session.get("execution") or {}).get("external_id")
    actuals = context.get("recent_actuals")
    claiming = [
        actual
        for actual in (actuals if isinstance(actuals, list) else [])
        if isinstance(actual, dict)
        and actual.get("planned_session_id") == session_id
        and actual.get("match_confidence") in ATTACHED_MATCH_CONFIDENCES
    ]
    backing = [
        actual
        for actual in claiming
        if actual.get("completion") == "completed"
        and actual.get("sport") == session.get("sport")
        and (
            (external_id is not None and str(actual.get("paired_event_id")) == str(external_id))
            or _ownership_backed(context, after, session, actual)
        )
    ]
    if len(claiming) > 1:
        errors.append(
            f"more than one attached actual claims session {session_id}; conflicting "
            f"data is reported, never reconciled"
        )
    elif not backing:
        errors.append(
            f"no completed actual in context.recent_actuals backs reconciling session "
            f"{session_id}: it must share the session's sport and carry either the paired "
            f"provider event or product ownership of an unambiguous day"
    )


def validate_bundle(
    context: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Validate cross-artifact links and daily no-upshift safety policy."""

    reports = {
        "context": validate_coach_context(context),
        "before": validate_plan_state(before),
        "after": validate_plan_state(after),
        "event": validate_decision_event(event),
    }
    errors = [f"{name}: {error}" for name, report in reports.items() for error in report["errors"]]
    warnings = [f"{name}: {warning}" for name, report in reports.items() for warning in report["warnings"]]

    plan_id = before.get("plan_id")
    if after.get("plan_id") != plan_id or event.get("plan_id") != plan_id:
        errors.append("context bundle must bind one exact plan_id")
    goal_context = context.get("goal_context", {})
    if goal_context.get("plan_id") != plan_id:
        errors.append("context.goal_context.plan_id must match the current plan")
    if goal_context.get("plan_version") != before.get("version"):
        errors.append("context.goal_context.plan_version must match the before plan")
    if event.get("plan_version_before") != before.get("version"):
        errors.append("event.plan_version_before must match the before plan")
    if event.get("plan_version_after") != after.get("version"):
        errors.append("event.plan_version_after must match the after plan")
    _check_context_projects_before_plan(context, before, errors)
    _check_changed_delivery_content_resets_observation(before, after, errors)

    if event.get("mode") == "record_delivery":
        errors.append(
            "record_delivery is written only by the verified delivery boundary, "
            "not through a model-authored decision bundle"
        )

    if event.get("mode") in {"plan_week", "review_week"}:
        if before.get("goal") != after.get("goal"):
            errors.append("week mode must preserve the current goal")
        # Everything about the cycle except the outlook. The outlook is the *rest* of the
        # cycle, so a week that rolls forward necessarily shortens it -- the week just made
        # precise is the one that leaves (issue #61). Holding it fixed here would mean
        # either a stale outlook that still names the current week, or a week roll that has
        # to be a cycle-mode decision, which would let one slip past the goal check above.
        _CYCLE_KEYS_A_WEEK_MAY_NOT_MOVE = (
            "start",
            "end",
            "primary_adaptation",
            "maintenance_adaptation",
            "planned_evidence",
            "adjust_conditions",
            "stop_conditions",
        )
        before_cycle = before.get("cycle") or {}
        after_cycle = after.get("cycle") or {}
        if any(
            before_cycle.get(key) != after_cycle.get(key)
            for key in _CYCLE_KEYS_A_WEEK_MAY_NOT_MOVE
        ):
            errors.append("week mode must preserve the current 28-day cycle")
        # athlete_baseline is deliberately not preserved here (issue #32). The goal and
        # cycle are the 28-day direction a week-scoped decision must not rewrite; the
        # baseline is the measurement anchor that direction is checked against, and
        # judging whether it still describes the athlete is part of prescribing --
        # which is exactly what week modes do. A week decision may therefore move it,
        # carrying its evidence, as the ordinary material change it is.

    if event.get("mode") == "revisit_today":
        action = event.get("action")
        if action not in DAILY_ACTIONS:
            errors.append("daily event action is outside policy")
        # Evidence quality does not choose the coaching response (#43). Non-fresh
        # optional evidence stays visible through the context freshness warnings and
        # the unknowns-preservation rule below; it may lower the Coach's confidence,
        # but it must not by itself reject keep/reduce/move/replace or force
        # rest/human_review. The one hard safety boundary -- an explicit positive
        # symptom -- is no longer read here: it is evidence in the context, not a
        # property of this mode, and _check_explicit_symptom_boundary applies it to
        # every bundle regardless of what the event calls itself (#84).
        missing_unknowns = set(context.get("unknowns", [])) - set(event.get("unknowns", []))
        if missing_unknowns:
            errors.append("event.unknowns must preserve every context unknown")

        changed_action = action in {"reduce", "move", "replace", "rest"}
        expected_version = before.get("version", 0) + (1 if changed_action else 0)
        if after.get("version") != expected_version:
            errors.append(f"daily action {action} must produce plan version {expected_version}")
        if not changed_action and _canonical(after) != _canonical(before):
            errors.append(f"daily action {action} must leave PlanState unchanged")
        if changed_action and _canonical(after) == _canonical(before):
            errors.append(f"daily action {action} must make an exact plan change")
        if before.get("goal") != after.get("goal") or before.get("cycle") != after.get("cycle"):
            errors.append("daily mode must not change the goal or 28-day cycle")

        before_sessions = _session_map(before)
        after_sessions = _session_map(after)
        before_ids = set(before_sessions)
        after_ids = set(after_sessions)
        if before_ids != after_ids:
            errors.append("daily mode must preserve the exact weekly session_id set")
        if changed_action:
            session_id = event.get("session_id")
            if not session_id or session_id not in before_ids or session_id not in after_ids:
                errors.append("daily changed action must preserve and bind the affected session_id")
            changed_ids = {
                candidate
                for candidate in before_ids & after_ids
                if _canonical(before_sessions[candidate]) != _canonical(after_sessions[candidate])
            }
            if changed_ids != {session_id}:
                errors.append("daily changed action must modify only the bound session_id")
            expected_status = {"move": "moved", "replace": "replaced"}.get(action)
            if (
                expected_status is not None
                and isinstance(session_id, str)
                and after_sessions.get(session_id, {}).get("match_status") != expected_status
            ):
                errors.append(
                    f"daily action {action} must leave its bound session actionable as "
                    f"match_status={expected_status}"
                )

        before_minutes = _planned_minutes(before)
        after_minutes = _planned_minutes(after)
        if before_minutes is None or after_minutes is None:
            if changed_action:
                errors.append("daily change requires known planned minutes to prove no volume increase")
        elif after_minutes > before_minutes:
            errors.append("daily mode must not increase planned weekly minutes")
        if _hard_count(after) > _hard_count(before):
            errors.append("daily mode must not increase hard-session count")

    # Athlete-baseline consistency: does the proposed plan actually fit this athlete?
    # Runs for every mode (not just revisit_today) against the plan being adopted, since
    # an unsafe prescription is unsafe regardless of which mode produced it. `after`
    # alone is sufficient: unchanged daily actions leave after == before, so the
    # previously-adopted plan is re-checked for free, and changed actions validate
    # exactly the new proposal.
    baseline_raw = context.get("athlete_baseline")
    baseline = baseline_raw if isinstance(baseline_raw, dict) else {}
    _check_structured_intensity_has_measured_anchor(after, baseline, errors)
    _check_planned_loads_have_matching_baseline(
        _movement_list_sessions_requiring_precision_check(before, after, event), baseline, errors
    )
    _check_max_session_minutes(after, baseline, errors, warnings)
    _check_actionable_sessions_declare_executable_work(
        _actionable_sessions_for_event(after, event), errors, warnings
    )
    _check_rest_days_prescribe_nothing(after, errors)
    _check_change_is_material(before, after, event, errors)
    _check_reconcile_semantics(context, before, after, event, errors)
    # Triggered by the evidence in the context, not by the mode the event declares, and
    # therefore reached by every route that adopts a plan (#84).
    _check_explicit_symptom_boundary(context, before, after, event, errors)

    return {
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": warnings,
        "artifacts": {name: report["status"] for name, report in reports.items()},
        "policy": "deterministic_coach_boundary",
    }
