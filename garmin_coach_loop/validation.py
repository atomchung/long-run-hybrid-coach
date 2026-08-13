"""Dependency-free structural and semantic validation for Coach Loop V1.

The language model may propose the three public artifacts. This module owns the stable
safety boundary. It does not authenticate, read a live provider, persist state, or publish.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Iterable


COACH_CONTEXT_SCHEMA_VERSION = "1.0"
PLAN_STATE_SCHEMA_VERSION = "1.0"
DECISION_EVENT_SCHEMA_VERSION = "1.0"

FRESHNESS = {"fresh", "partial", "stale", "failed", "unknown"}
COVERAGE_STATUS = {"complete", "partial", "missing"}
SPORTS = {"running", "strength", "mobility", "recovery", "rest"}
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
# Fields whose movement changes what the athlete actually does. Everything else on a
# session -- purpose, fallback wording -- is how the plan explains itself, and rewording
# an explanation is not a training decision.
MATERIAL_SESSION_FIELDS = frozenset({
    "scheduled_date", "sport", "adaptation", "planned_minutes", "priority",
    "cost", "body_stress", "hard", "prescription", "structured_workout",
    "strength_movements", "time_window", "execution", "match_status",
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
STRENGTH_LOAD_FIELDS = ("exercise", "load_kg", "assist_kg", "scheme")
# The athlete writes prescriptions in their own language; display_name is how that
# wording binds back to this measured anchor. Optional: an English prescription matches
# the canonical exercise key on its own.
STRENGTH_LOAD_OPTIONAL_FIELDS = ("display_name",)

# session.strength_movements (issue #52): the plan-side counterpart of a strength_load,
# named the same way so the two compare field to field instead of a pattern re-deriving
# the plan's own numbers out of the sentence that reported them.
STRENGTH_MOVEMENT_FIELDS = ("exercise", "sets", "reps", "load_kg", "assist_kg", "load_basis")
# Why a load is what it is. The two loadless bases are the point of the field: an absent
# load_kg is either a bodyweight movement or a number the athlete still owes, and prose
# said which by carrying 自重 / 待確認 / bodyweight / TBD for a pattern to find again.
# An RPE-only prescription has no basis here on purpose -- there is no structured field
# to hold the RPE number, so such a session keeps using the free-text path whole.
STRENGTH_LOAD_BASES = {"measured_baseline", "bodyweight", "pending_confirmation"}

# strength_execution (issue #37): the standalone optional evidence group described in
# source_personal_os.fetch_strength_execution. Exact keys throughout -- unlike
# athlete_baseline, nothing here predates the field existing, so there is no
# backward-compatibility reason to allow an `optional=` set.
STRENGTH_EXECUTION_FIELDS = ("source", "window_start", "window_end", "sessions")
STRENGTH_EXECUTION_SESSION_FIELDS = ("date", "exercise", "category", "sets", "notes")
STRENGTH_EXECUTION_SET_FIELDS = ("set", "weight_kg", "assist_kg", "reps", "rpe")

# recovery_signals (issue #37 slice 2): the standalone optional evidence group
# described in source_personal_os.fetch_recovery_signals. Exact keys throughout, same
# rationale as STRENGTH_EXECUTION_FIELDS above -- nothing here predates the field
# existing.
RECOVERY_SIGNALS_FIELDS = ("source", "window_start", "window_end", "days")
RECOVERY_SIGNALS_DAY_FIELDS = (
    "date",
    "readiness_score",
    "readiness_level",
    "hrv_status",
    "hrv_7d_avg_ms",
    "acute_load",
    "recovery_time_sec",
    "body_battery_high",
    "body_battery_low",
    "avg_stress",
)

# Quality-pace check: match an explicit "M:SS/km" pace target inside free-text prescription.
_PACE_PATTERN = re.compile(r"(\d{1,2}):([0-5]\d)\s*/\s*km")
# Distance check: match an explicit "5x1000m" / "5×1000m" interval-repeat distance.
_INTERVAL_METERS_PATTERN = re.compile(r"(\d+)\s*[x×X]\s*(\d+(?:\.\d+)?)\s*m(?![a-zA-Z])")
# Free-text executability fallback for a running session with no structured_workout
# (see _check_actionable_sessions_have_executable_prescriptions). Matched by intensity
# *dimension* -- pace, heart rate, zone, perceived effort, breathing -- rather than by
# approved phrase: "配速隨意" and "全程用鼻子呼吸" carry no number and are still explicit
# instructions for how to run the session, while "go for a run" names no dimension at
# all. Which dimension a session should use is coaching judgment, not this pattern's.
_RUN_TARGET_PATTERN = re.compile(
    r"(?:/\s*km|\bpace\b|配速|\bbpm\b|\bhr\b|heart[ -]?rate|心率|"
    r"\bzone\s*\d|\bz\d\b|\brpe\b|effort|體感|強度|"
    r"conversational|talk test|breath|呼吸|\beasy\b|輕鬆)",
    re.IGNORECASE,
)
# One set's worth of work: an explicit rep count in whichever vocabulary the athlete
# writes in (reps / 次 / 下), or an explicit stop rule that stands in for the count.
# A set taken to failure has no rep number by design, and demanding one only forces a
# fabricated target into an otherwise executable prescription.
_STRENGTH_REPS = r"(?:\d+\s*(?:reps?|次|下)|力竭|failure|\bamrap\b|max(?:imum)?\s*reps?)"
_STRENGTH_SCHEME_PATTERN = re.compile(
    r"(?:\d+\s*[x×X]\s*\d+|\d+\s*(?:sets?|組)\D{0,12}" + _STRENGTH_REPS + r")",
    re.IGNORECASE,
)
_STRENGTH_LOAD_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:kg|公斤)|\brpe\s*\d|bodyweight|自重|待確認|"
    r"confirm(?:ation)?|\btbd\b|baseline)",
    re.IGNORECASE,
)
_BPM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*bpm\b", re.IGNORECASE)
# Bounded `hr`, not bare `hr`: under IGNORECASE the unanchored form read the "hr" in a
# duration ("1hr 30 min", "2hr 45 分鐘") as a heart-rate label and the following number
# as a bpm target, which with no measured max_hr/easy_hr_ceiling anchor blocked the
# whole plan over a duration. Excluded is the number-prefixed duration form only, spaced
# or not, never every unanchored "hr": "maxHR 150" and "LTHR 165" are heart-rate labels
# this gate still has to read, and losing them would quietly narrow the evidence gate.
# The boundary is on the ASCII alternative only -- 心率 is a word character to `\b`, so
# anchoring it would instead demand punctuation around it.
_HR_ABSOLUTE_PATTERN = re.compile(
    r"(?:heart[ -]?rate|(?<!\d)(?<!\d )hr\b|心率)\s*(?:<=|<|under|below|ceiling|at|@|:)?\s*"
    r"(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_HR_RANGE_PATTERN = re.compile(
    r"(?:heart[ -]?rate|(?<!\d)(?<!\d )hr\b|心率)\s*:?[ ]*(\d+(?:\.\d+)?)[ ]*[-–—][ ]*"
    r"(\d+(?:\.\d+)?)(?:\s*bpm\b)?",
    re.IGNORECASE,
)
_HR_PERCENT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s*)?(?:max(?:imum)?\s*)?(?:heart[ -]?rate|hr)\b",
    re.IGNORECASE,
)
# Reads the same load vocabulary as _STRENGTH_LOAD_PATTERN on purpose: this is the
# evidence gate, so any unit the executability check accepts must also be a unit the
# baseline check can see. A load written "60 公斤" that only one of the two recognises
# is exact precision reaching the athlete unanchored.
_KG_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg\b|公斤)", re.IGNORECASE)
# The sets-and-reps shape that says a new movement starts here: "5x5", "5×5", "4 組 8 下".
# It is what tells a prescribed load apart from a number the coach wrote while reasoning
# about a past session, and it -- not punctuation -- decides how far back a load may look
# for the exercise that vouches for it (#49).
_SET_SCHEME_PATTERN = re.compile(r"\d+\s*(?:[x×]\s*\d+|組\s*\d+\s*下)", re.IGNORECASE)


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
    _keys(coverage, field, ("observed_days", "expected_days", "status"), errors)
    _integer(coverage.get("observed_days"), f"{field}.observed_days", errors)
    _integer(coverage.get("expected_days"), f"{field}.expected_days", errors, minimum=1)
    _enum(coverage.get("status"), f"{field}.status", COVERAGE_STATUS, errors)
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
    _nonempty(session.get("category"), f"{field}.category", errors)
    sets = _list(session.get("sets"), f"{field}.sets", errors)
    for index, raw in enumerate(sets):
        _validate_strength_execution_set(raw, f"{field}.sets[{index}]", errors)
    _string_array(session.get("notes"), f"{field}.notes", errors)


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
    day = _mapping(value, field, errors)
    _keys(day, field, RECOVERY_SIGNALS_DAY_FIELDS, errors)
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
        "constraints",
        "athlete_baseline",
        "recent_actuals",
        "recovery_trends",
        "current_calendar",
        "cycle_sessions",
        "strength_execution",
        "recovery_signals",
        "unknowns",
        "privacy",
    )
    _keys(context, "context", required, errors)
    if context.get("schema_version") != COACH_CONTEXT_SCHEMA_VERSION:
        errors.append(f"context.schema_version must be {COACH_CONTEXT_SCHEMA_VERSION}")
    _nonempty(context.get("context_id"), "context.context_id", errors)
    _timestamp(context.get("as_of"), "context.as_of", errors)
    _nonempty(context.get("timezone"), "context.timezone", errors)

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
    coverage_fields = ("activities", "sleep", "hrv", "resting_hr", "calendar")
    _keys(coverage, "context.coverage", coverage_fields, errors)
    for field in coverage_fields:
        _validate_coverage(coverage.get(field), f"context.coverage.{field}", errors)

    goal = _mapping(context.get("goal_context"), "context.goal_context", errors)
    _keys(goal, "context.goal_context", ("plan_id", "plan_version", "primary_goal", "maintenance_goal"), errors)
    _nonempty(goal.get("plan_id"), "context.goal_context.plan_id", errors)
    _integer(goal.get("plan_version"), "context.goal_context.plan_version", errors, minimum=1)
    _nonempty(goal.get("primary_goal"), "context.goal_context.primary_goal", errors)
    if goal.get("maintenance_goal") is not None:
        _nonempty(goal.get("maintenance_goal"), "context.goal_context.maintenance_goal", errors)

    constraints = _mapping(context.get("constraints"), "context.constraints", errors)
    constraint_fields = (
        "available_days",
        "session_minutes",
        "red_flags",
        "leg_fatigue",
        "soreness",
        "schedule_changed",
        "equipment_changed",
    )
    _keys(constraints, "context.constraints", constraint_fields, errors)
    _string_array(
        constraints.get("available_days"),
        "context.constraints.available_days",
        errors,
        allowed={"mon", "tue", "wed", "thu", "fri", "sat", "sun"},
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
        _enum(actual.get("adaptation"), f"{field}.adaptation", ADAPTATIONS, errors)
        _enum(actual.get("body_stress"), f"{field}.body_stress", BODY_STRESS, errors)
        _enum(actual.get("cost"), f"{field}.cost", COSTS, errors)
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
            {"attached", "none_found", "other_activity_same_day", "outside_evidence_window"},
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


def _validate_structured_workout(raw: Any, field: str, errors: list[str]) -> None:
    workout = _mapping(raw, field, errors)
    _keys(workout, field, ("name", "steps"), errors)
    _nonempty(workout.get("name"), f"{field}.name", errors)
    if isinstance(workout.get("name"), str) and len(workout["name"]) > 80:
        errors.append(f"{field}.name must be at most 80 characters")
    steps = _list(workout.get("steps"), f"{field}.steps", errors)
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
    an absent load means -- that is `load_basis`, and the free-text path had to recover
    it by looking for 自重 or 待確認 in a sentence.

    The coherence rule below is the one new structural block. A movement that declares
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


def _validate_strength_movements(raw: Any, field: str, errors: list[str]) -> None:
    movements = _list(raw, field, errors)
    if not movements:
        errors.append(f"{field} must contain at least one movement")
    for index, movement in enumerate(movements):
        _validate_strength_movement(movement, f"{field}[{index}]", errors)


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
        "fallback",
        "execution",
        "match_status",
    )
    # prescription, structured_workout and strength_movements arrived after plans were
    # already stored; see _keys. Historical append-only commits remain valid, but
    # delivery refuses a current session that has no canonical structured_workout.
    _keys(
        session,
        field,
        fields,
        errors,
        optional=("prescription", "structured_workout", "strength_movements"),
    )
    _nonempty(session.get("session_id"), f"{field}.session_id", errors)
    _enum(session.get("sport"), f"{field}.sport", SPORTS, errors)
    _date(session.get("scheduled_date"), f"{field}.scheduled_date", errors)
    if session.get("time_window") is not None:
        _nonempty(session.get("time_window"), f"{field}.time_window", errors)
    _nonempty(session.get("purpose"), f"{field}.purpose", errors)
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
    if session.get("prescription") is not None:
        _nonempty(session.get("prescription"), f"{field}.prescription", errors)
    if "structured_workout" in session:
        if session.get("sport") != "running":
            errors.append(f"{field}.structured_workout is only allowed for running")
        _validate_structured_workout(
            session.get("structured_workout"),
            f"{field}.structured_workout",
            errors,
        )
    if "strength_movements" in session:
        # Bound to the sport whose checks read it, for the same reason structured_workout
        # is bound to running: only a strength session's baseline gate ever looks at this
        # field, so carrying it anywhere else is a second prescription nothing validates.
        if session.get("sport") != "strength":
            errors.append(f"{field}.strength_movements is only allowed for strength")
        _validate_strength_movements(
            session.get("strength_movements"),
            f"{field}.strength_movements",
            errors,
        )
    fallback = _mapping(session.get("fallback"), f"{field}.fallback", errors)
    _keys(fallback, f"{field}.fallback", ("action", "description"), errors)
    _enum(fallback.get("action"), f"{field}.fallback.action", {"reduce", "move", "replace", "rest"}, errors)
    _nonempty(fallback.get("description"), f"{field}.fallback.description", errors)
    execution = _mapping(session.get("execution"), f"{field}.execution", errors)
    _keys(execution, f"{field}.execution", ("publish_supported", "external_id", "delivery_state"), errors)
    if not isinstance(execution.get("publish_supported"), bool):
        errors.append(f"{field}.execution.publish_supported must be boolean")
    if execution.get("external_id") is not None:
        _nonempty(execution.get("external_id"), f"{field}.execution.external_id", errors)
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
    _keys(goal, "plan.goal", ("outcome", "measurement_protocol"), errors)
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


def delivery_session_content(session: dict[str, Any]) -> dict[str, Any]:
    """Project only the session content that makes a delivered workout stale.

    Match/completion state, coaching labels and provider observations do not alter what
    was sent. Date and executable training content do. This projection is public to the
    store so validation and proposal/read-back identity cannot drift into two rules.
    """
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    return {
        field: session.get(field)
        for field in (
            "session_id",
            "sport",
            "scheduled_date",
            "adaptation",
            "planned_minutes",
            "hard",
            "prescription",
            "structured_workout",
        )
    } | {"publish_supported": execution.get("publish_supported")}


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


def _actionable_sessions_for_event(
    after: dict[str, Any],
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    if event.get("reason_codes") == ["planned_actual_reconciled"]:
        return []
    sessions = [
        session
        for session in _sessions(after)
        if isinstance(session, dict)
        and session.get("match_status") in ACTIONABLE_MATCH_STATUSES
        and session.get("sport") in {"running", "strength"}
    ]
    if event.get("mode") in {"plan_cycle", "plan_week", "review_cycle", "review_week"}:
        return sessions
    if event.get("mode") == "revisit_today":
        session_id = event.get("session_id")
        return [session for session in sessions if session.get("session_id") == session_id]
    return []


def _has_executable_structure(session: dict[str, Any]) -> bool:
    """True when the session carries a structured target the device can execute.

    _validate_workout_step owns the open/pace/hr_ceiling vocabulary and blocks anything
    else on the same bundle, so this only asks whether a work step's target is there to
    read at all. An `open` target counts: a run deliberately left without an intensity
    target still executes as its structured duration or distance.
    """
    structured = session.get("structured_workout")
    if not isinstance(structured, dict):
        return False
    return any(
        target.get("kind") in {"open", "pace", "hr_ceiling"}
        for target in _iter_step_targets(structured.get("steps"))
    )


def _structured_movements(session: dict[str, Any]) -> list[dict[str, Any]] | None:
    """This strength session's structured movements, or None when it carries none.

    The single place that decides "the structure exists here", so the executability gate
    and the baseline gate can never disagree about which session gets read from prose.
    Like `_has_executable_structure`, it only asks whether there is something to read:
    `_validate_strength_movement` owns the shape and blocks a malformed list on the same
    bundle, so nothing can slip through by being structured badly.
    """
    movements = session.get("strength_movements")
    if not isinstance(movements, list) or not movements:
        return None
    if not all(isinstance(movement, dict) for movement in movements):
        return None
    return movements


def _check_actionable_sessions_have_executable_prescriptions(
    after: dict[str, Any],
    event: dict[str, Any],
    errors: list[str],
) -> None:
    """Require every adopted running/strength session to be executable.

    Executability is a structural fact, and for a running session the structure that
    holds it is `structured_workout` -- the only executable source at delivery. Where
    one exists it decides, and the prescription is free to be what README says it is: a
    human-readable summary, in the athlete's own language, in any wording. Whether
    "Zone 2 有氧跑 50 分鐘" is the right prescription is coaching judgment, and a
    blocking validator does not own that (invariant 5); it used to reject exactly that
    wording while the session carried the hr_ceiling step the watch enforces.

    Strength works the same way through its own structure. A strength session carrying
    `strength_movements` states its sets, its reps or its stop rule, and the basis of
    every load as recorded fields, which is all the text patterns were trying to
    reconstruct; the prescription beside them is then the same human summary a run's is,
    in whatever wording the athlete uses. Strength still delivers as a titled calendar
    entry with no executable structure, so this changes what the validator reads, not
    what the watch receives.

    Free text is read only where no structure exists to read: strength sessions with no
    `strength_movements`, and running sessions on historical PlanStates, both predate
    their field. There the text check survives, because without it nothing separates a
    session from "go for a run".
    """
    for session in _actionable_sessions_for_event(after, event):
        if (
            not isinstance(session, dict)
            or session.get("sport") not in {"running", "strength"}
        ):
            continue
        prescription = session.get("prescription")
        if not isinstance(prescription, str) or not prescription.strip():
            errors.append(
                f"adopted {session.get('sport')} session "
                f"{session.get('session_id', '?')} requires a non-empty prescription"
            )
            continue
        session_id = session.get("session_id", "?")
        if session.get("sport") == "running":
            if not _has_executable_structure(session) and not _RUN_TARGET_PATTERN.search(
                prescription
            ):
                errors.append(
                    f"adopted running session {session_id} needs a structured_workout "
                    "target, or a pace, heart-rate, or explicit effort target in its "
                    "prescription"
                )
            planned = session.get("planned_minutes")
            if not isinstance(planned, int) or isinstance(planned, bool) or planned <= 0:
                errors.append(f"adopted running session {session_id} needs known positive planned_minutes")
        elif _structured_movements(session) is None:
            if not _STRENGTH_SCHEME_PATTERN.search(prescription):
                errors.append(
                    f"adopted strength session {session_id} needs explicit sets and reps"
                )
            if not _STRENGTH_LOAD_PATTERN.search(prescription):
                errors.append(
                    f"adopted strength session {session_id} needs a supported load, "
                    "RPE/bodyweight target, or one explicit pending confirmation"
                )


def _planned_minutes(plan: dict[str, Any]) -> int | None:
    values = [session.get("planned_minutes") for session in _sessions(plan)]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    return sum(values)


def _hard_count(plan: dict[str, Any]) -> int:
    return sum(session.get("hard") is True for session in _sessions(plan))


def _format_pace(total_seconds: int) -> str:
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _parse_prescribed_paces_sec_per_km(prescription: str) -> list[int]:
    """Extract every explicit "M:SS/km" pace target from free-text prescription.

    Returns an empty list when nothing matches; callers must treat that as unknown,
    never as "no pace constraint exists".
    """
    return [int(minutes) * 60 + int(seconds) for minutes, seconds in _PACE_PATTERN.findall(prescription)]



def _check_prescribed_pace_against_threshold(
    after: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    """Require every precise pace prescription, in prescription or purpose, to stand
    on a measured anchor.

    How far a VO2, threshold, or short repetition may sit from threshold is a coaching
    judgement, not a universal deterministic cap. The validator only enforces the
    evidence boundary: without a measured anchor, an exact pace is invented precision.

    Heart-rate and effort targets are untouched. They stay readable without a
    calibrated pace anchor, which is exactly what a plan should fall back to while
    the anchor is still an estimate.
    """
    threshold = baseline.get("threshold_pace_sec_per_km")
    for session in _sessions(after):
        if not isinstance(session, dict) or session.get("sport") != "running":
            continue
        session_id = session.get("session_id", "?")
        prescription = session.get("prescription")
        prescription = prescription if isinstance(prescription, str) else ""
        purpose = session.get("purpose")
        purpose = purpose if isinstance(purpose, str) else ""
        # purpose is scanned too: a precise pace written there is exactly as
        # invented without a measured anchor as one written in prescription (#38 --
        # a too-fast interval pace sat in purpose undetected for two days).
        paces = _parse_prescribed_paces_sec_per_km(prescription) + _parse_prescribed_paces_sec_per_km(purpose)

        if paces and threshold is None:
            errors.append(
                f"session {session_id} prescribes {_format_pace(min(paces))}/km but "
                "athlete_baseline.threshold_pace_sec_per_km is not measured; without a "
                "measured anchor, prescribe heart rate or effort and state how to "
                "calibrate"
            )
            continue


def _check_exact_heart_rate_has_measured_anchor(
    after: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    anchors = [
        float(value)
        for value in (baseline.get("max_hr"), baseline.get("easy_hr_ceiling"))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    ]
    max_hr = baseline.get("max_hr")
    for session in _sessions(after):
        if not isinstance(session, dict) or session.get("sport") != "running":
            continue
        prescription = session.get("prescription")
        if not isinstance(prescription, str):
            continue
        bpm_targets = sorted(
            {
                float(value)
                for pattern in (_BPM_PATTERN, _HR_ABSOLUTE_PATTERN)
                for value in pattern.findall(prescription)
            }
            | {
                float(value)
                for endpoints in _HR_RANGE_PATTERN.findall(prescription)
                for value in endpoints
            }
        )
        session_id = session.get("session_id", "?")
        if bpm_targets and not anchors:
            errors.append(
                f"session {session_id} prescribes exact BPM without a measured max_hr "
                "or easy_hr_ceiling anchor; use effort until HR is established"
            )
        elif bpm_targets and (min(bpm_targets) <= 0 or max(bpm_targets) > max(anchors)):
            errors.append(
                f"session {session_id} prescribes BPM outside its established HR anchors"
            )

        percent_targets = [float(value) for value in _HR_PERCENT_PATTERN.findall(prescription)]
        if percent_targets and not (
            isinstance(max_hr, (int, float)) and not isinstance(max_hr, bool) and max_hr > 0
        ):
            errors.append(
                f"session {session_id} prescribes %HR without a measured max_hr anchor"
            )
        elif percent_targets and (min(percent_targets) <= 0 or max(percent_targets) > 100):
            errors.append(f"session {session_id} prescribes an invalid HR percentage")


def _check_structured_hr_ceiling_against_max_hr(
    after: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    """Require every structured hr_ceiling target to stand on a measured max_hr anchor.

    Mirrors _check_exact_heart_rate_has_measured_anchor's free-text guard, but for the
    structured target the device will actually enforce. An hr_ceiling with no measured
    max_hr, or one set above it, is an invented number the watch nonetheless obeys.
    """
    max_hr = baseline.get("max_hr")
    has_measured_max_hr = isinstance(max_hr, int) and not isinstance(max_hr, bool) and max_hr > 0
    for session in _sessions(after):
        if not isinstance(session, dict) or session.get("sport") != "running":
            continue
        structured = session.get("structured_workout")
        steps = structured.get("steps") if isinstance(structured, dict) else None
        ceilings = [
            target["ceiling_bpm"]
            for target in _iter_step_targets(steps)
            if target.get("kind") == "hr_ceiling"
            and isinstance(target.get("ceiling_bpm"), int)
            and not isinstance(target.get("ceiling_bpm"), bool)
        ]
        if not ceilings:
            continue
        session_id = session.get("session_id", "?")
        if not has_measured_max_hr:
            errors.append(
                f"session {session_id} prescribes an hr_ceiling target without a "
                "measured athlete_baseline.max_hr anchor"
            )
            continue
        for ceiling in ceilings:
            if ceiling > max_hr:
                errors.append(
                    f"session {session_id} hr_ceiling {ceiling}bpm exceeds "
                    f"athlete_baseline.max_hr {max_hr}"
                )


def _normalize_exercise_name(value: Any) -> str:
    # Keep every word character rather than ASCII only. Baselines carry canonical keys
    # like "split_squat" while prescriptions are written in the athlete's own language,
    # so dropping non-ASCII made every Chinese prescription silently unmatchable.
    return " ".join(re.findall(r"[^\W_]+", str(value).lower(), re.UNICODE))


def _baseline_exercise_aliases(load: dict[str, Any]) -> list[str]:
    names = (load.get("exercise"), load.get("display_name"))
    return [alias for alias in map(_normalize_exercise_name, names) if alias]


def _normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    """`_normalize_exercise_name` applied to a whole prescription, keeping a map from
    each normalized character back to its offset in the original.

    Exercise names have to be matched on normalized text -- the athlete writes them in
    their own language, spaced or not -- while loads and set schemes are located in the
    original. Binding one to the other needs both coordinate systems at once.
    """
    pieces: list[str] = []
    offsets: list[int] = []
    for match in re.finditer(r"[^\W_]+", text, re.UNICODE):
        if pieces:
            pieces.append(" ")
            offsets.append(match.start())
        token = match.group().lower()
        pieces.append(token)
        if len(token) == len(match.group()):
            offsets.extend(range(match.start(), match.start() + len(token)))
        else:  # a lowercase form that changes length: bind the token to its own start
            offsets.extend([match.start()] * len(token))
    return "".join(pieces), offsets


def _established_mentions(
    prescription: str,
    established: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """Every established exercise named in the prescription, as (offset, baseline entry)."""
    normalized, offsets = _normalized_with_offsets(prescription)
    mentions: list[tuple[int, dict[str, Any]]] = []
    for load in established:
        for alias in _baseline_exercise_aliases(load):
            start = normalized.find(alias)
            while start != -1:
                mentions.append((offsets[start], load))
                start = normalized.find(alias, start + 1)
    return sorted(mentions, key=lambda mention: mention[0])


def _exercise_vouching_for_load(
    load_at: int,
    mentions: list[tuple[int, dict[str, Any]]],
    scheme_starts: list[int],
    load_starts: list[int],
) -> dict[str, Any] | None:
    """The established exercise that vouches for one exact kg load, or None.

    A load is vouched for by the last established exercise named before it. The one
    exception is a load that its own set scheme introduces -- "臥推 4 組 8 下 50 公斤" --
    where the exercise must be the one named for *that* scheme: sets and reps say a new
    movement starts, so the load may not reach back past them and borrow the previous
    movement's anchor. Punctuation is never consulted, which is the whole point: the
    same sentence written with a half-width or a full-width comma reads the same way.

    A number written later in the prose with no scheme of its own -- "8/11 的 65kg 做不完
    五組" -- is the coach reasoning about a past session rather than prescribing a second
    one, so it stays bound to the movement the prescription is about.
    """
    governing = None
    for start in scheme_starts:
        if start < load_at and not any(start < other < load_at for other in load_starts):
            governing = start
    window_start, limit = 0, load_at
    if governing is not None:
        limit = governing
        window_start = max((start for start in scheme_starts if start < governing), default=0)
    vouching = [load for offset, load in mentions if window_start <= offset < limit]
    return vouching[-1] if vouching else None


def _strength_sessions_requiring_precision_check(
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    if event.get("reason_codes") == ["planned_actual_reconciled"]:
        return []
    actionable = [
        session
        for session in _sessions(after)
        if isinstance(session, dict)
        and session.get("sport") == "strength"
        and session.get("match_status") in ACTIONABLE_MATCH_STATUSES
    ]
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


def _anchoring_baseline(
    exercise: Any,
    established: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The baseline entry a planned movement names, by canonical key or display_name.

    Field to field, on the normalized names `_baseline_exercise_aliases` already
    produces -- no second naming scheme, and "bench_press" and "bench press" are one
    name because normalization drops the separator either way.
    """
    name = _normalize_exercise_name(exercise)
    if not name:
        return None
    for load in established:
        if name in _baseline_exercise_aliases(load):
            return load
    return None


def _check_structured_strength_loads_have_matching_baseline(
    session: dict[str, Any],
    movements: list[dict[str, Any]],
    established: list[dict[str, Any]],
    errors: list[str],
) -> None:
    """The same invariant as the free-text gate, read from the record instead of re-derived.

    What must not reach the athlete is unchanged and is not new here: an exact kg load
    the athlete's own measurements do not support, which looks exactly as precise as one
    they do. What changes is where the evidence comes from. `doctor-store` re-runs the
    whole commit history with no conversation present, so a rule that reads prose depends
    on a reader that is not there and must re-derive the plan's numbers identically every
    time; a recorded field is simply read.

    So no clause splitting, no unit vocabulary, no rep-count patterns are involved on
    this path, and punctuation and language cannot change the verdict -- there is no
    sentence to split. A load written in another unit is not a concern of this gate
    either: the Coach converts at authoring time, and load_kg is the only load it reads.
    """
    for index, movement in enumerate(movements):
        if movement.get("load_basis") != "measured_baseline":
            continue  # bodyweight and pending_confirmation prescribe no kg figure at all
        anchor = _anchoring_baseline(movement.get("exercise"), established)
        if anchor is not None and _measured_anchors(anchor):
            continue
        errors.append(
            f"session {session.get('session_id', '?')} prescribes an exact kg load "
            "without a matching established strength baseline for "
            f"strength_movements[{index}] {movement.get('exercise')!r}; use bodyweight "
            "or pending_confirmation, or measure the anchor first"
        )


def _check_exact_strength_loads_have_matching_baseline(
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    established = [
        load
        for load in (baseline.get("strength_loads") or [])
        if isinstance(load, dict) and _baseline_exercise_aliases(load)
    ]
    for session in _strength_sessions_requiring_precision_check(before, after, event):
        movements = _structured_movements(session)
        if movements is not None:
            # Where the structure exists it decides, and the patterns below are not
            # consulted for this session at all. Running the text path as well would put
            # the guess back in the one place the structure was added to remove it.
            _check_structured_strength_loads_have_matching_baseline(
                session, movements, established, errors
            )
            continue
        prescription = session.get("prescription")
        if not isinstance(prescription, str):
            continue
        mentions = _established_mentions(prescription, established)
        scheme_starts = [match.start() for match in _SET_SCHEME_PATTERN.finditer(prescription)]
        loads = list(_KG_PATTERN.finditer(prescription))
        load_starts = [match.start() for match in loads]
        previous_end = 0
        for match in loads:
            vouching = _exercise_vouching_for_load(
                match.start(), mentions, scheme_starts, load_starts
            ) or {}
            if not _measured_anchors(vouching):
                phrase = prescription[previous_end:match.end()].strip().lstrip(",;、，；。 ").strip()
                errors.append(
                    f"session {session.get('session_id', '?')} prescribes an exact kg load "
                    f"without a matching established strength baseline for {phrase!r}; "
                    "use RPE/bodyweight or an explicit pending confirmation"
                )
            previous_end = match.end()

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
        if before.get("cycle") != after.get("cycle"):
            errors.append("week mode must preserve the current 28-day cycle")
        if before.get("athlete_baseline") != after.get("athlete_baseline"):
            errors.append("week mode must preserve athlete_baseline")

    if event.get("mode") == "revisit_today":
        action = event.get("action")
        if action not in DAILY_ACTIONS:
            errors.append("daily event action is outside policy")
        # Evidence quality does not choose the coaching response (#43). Non-fresh
        # optional evidence stays visible through the context freshness warnings and
        # the unknowns-preservation rule below; it may lower the Coach's confidence,
        # but it must not by itself reject keep/reduce/move/replace or force
        # rest/human_review. Only an explicit positive symptom is the hard safety
        # boundary: null means unassessed, not present, so demanding `is False`
        # here required a blanket all-clear before every ordinary daily decision.
        red_flags = context.get("constraints", {}).get("red_flags", {})
        positive_red_flags = [field for field, value in red_flags.items() if value is True]
        if positive_red_flags and action not in {"human_review", "rest"}:
            errors.append(
                "explicit red flag ("
                + ", ".join(sorted(positive_red_flags))
                + ") limits today to rest or human_review"
            )
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
    _check_prescribed_pace_against_threshold(after, baseline, errors)
    _check_exact_heart_rate_has_measured_anchor(after, baseline, errors)
    _check_structured_hr_ceiling_against_max_hr(after, baseline, errors)
    _check_exact_strength_loads_have_matching_baseline(
        before, after, event, baseline, errors
    )
    _check_max_session_minutes(after, baseline, errors, warnings)
    _check_actionable_sessions_have_executable_prescriptions(after, event, errors)
    _check_change_is_material(before, after, event, errors)
    _check_reconcile_semantics(context, before, after, event, errors)

    return {
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": warnings,
        "artifacts": {name: report["status"] for name, report in reports.items()},
        "policy": "deterministic_coach_boundary",
    }
