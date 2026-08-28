"""Source-agnostic core for building a CoachContext.

Holds the shared build window, the ``SourceDomain`` shape a provider must fill in, every
derivation formula that does not depend on which provider produced a reading, and the
final CoachContext assembly. This module has no idea intervals.icu or personal-os
health.db exist -- it does not import either. Both ``source_intervals`` and
``source_personal_os`` import from here (never from each other), and ``context_builder``
imports from here too when it dispatches between them, so the two providers can never
silently drift apart on how a trend, a coverage bucket, or the final CoachContext shape
gets computed.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .validation import (
    COACH_CONTEXT_SCHEMA_VERSION,
    RECONCILIATION_ACTUAL_FIELDS,
    anchoring_baseline,
    normalize_exercise_name,
    owned_duration_within_band,
    plan_movements,
    product_delivered,
    validate_coach_context,
)


# The athlete-local day every date-boundary calculation answers "today"/"next" with when
# nobody has said otherwise. The athlete's own stored profile
# (``athlete_evidence.resolve_settings``) comes first, and a request may override it for
# one call; this is the last resort, kept because it is what every store written before a
# profile could be stated was answered with. Never inferred from the server/host location.
DEFAULT_TIMEZONE = "Asia/Taipei"
DEFAULT_SESSION_MINUTES: int | None = None
ALL_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RED_FLAG_FIELDS = ("pain", "illness", "chest_pain", "dizziness", "unusual_symptoms")

MATCH_STATUS_TO_CALENDAR_STATUS = {
    "planned": "planned",
    "completed": "completed",
    "partial": "completed",
    "moved": "moved",
    "replaced": "replaced",
    "missed": "missed",
}

# athlete_baseline shape used when PlanState carries none -- every field explicitly
# unknown, never a guessed number. Mirrors contracts/coach-context.schema.json and
# contracts/plan-state.schema.json's athlete_baseline $defs exactly.
ATHLETE_BASELINE_UNKNOWN: dict[str, Any] = {
    "threshold_pace_sec_per_km": None,
    "max_hr": None,
    "easy_hr_ceiling": None,
    "longest_recent_run_km": None,
    "weekly_volume_km_4wk_avg": None,
    "max_session_minutes": None,
    "strength_loads": [],
}


class ContextBuildError(RuntimeError):
    """A deterministic CoachContext build step was blocked.

    ``upstream_status`` is the provider's own HTTP status when one caused this, and
    ``None`` for every other blocked step. It exists so a caller can tell "the provider
    refused this credential" from "the provider had a bad minute": one is fixed by
    authorizing again, the other by retrying, and a transport that cannot tell them apart
    reports both as an outage.
    """

    def __init__(
        self,
        message: str,
        *,
        details: Any | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details
        self.upstream_status = upstream_status


@dataclass(frozen=True)
class ContextRequest:
    """The athlete-input fields of a CoachContext build, independent of data source.

    Bundled per the project rule (functions with more than three parameters take a
    dataclass) -- ``build_context`` otherwise sat at roughly a dozen keyword params.
    """

    as_of_raw: str | None
    timezone_name: str
    available_days: list[str]
    session_minutes: int | None
    red_flags: dict[str, bool | None]
    leg_fatigue: str
    soreness: str
    schedule_changed: bool | None
    equipment_changed: bool | None
    extra_unknowns: list[str]


@dataclass(frozen=True)
class BuildWindow:
    """The single temporal frame a build runs against, shared by every data source."""

    as_of: dt.datetime
    resolved_now: dt.datetime
    now_iso: str
    window_start: dt.date  # 7-day coverage/trends window start
    window_end: dt.date  # == as_of.date()
    window14_start: dt.date  # 14-day recent_actuals window start
    window14_end: dt.date  # == as_of.date()
    window28_start: dt.date  # 28-day cycle window start -- one cycle is at most 28 days
    window28_end: dt.date  # == as_of.date()
    window42_start: dt.date  # 42-day cycle-planning activity window start
    window42_end: dt.date  # == as_of.date()


@dataclass(frozen=True)
class SourceDomain:
    """The activity/recovery slice of a CoachContext that varies by data source.

    Everything else (goal_context, constraints, athlete_baseline, current_calendar,
    freshness.calendar, privacy) comes from ``ContextRequest`` and the local PlanState,
    which are identical regardless of where activities and recovery signals came from. A
    provider builds one of these in a single shot and hands it to ``assemble_context`` --
    nothing mutates it afterward.
    """

    sources: list[dict[str, Any]]
    freshness_activities: str
    freshness_recovery: str
    # The earliest date ``recent_actuals`` could hold, stated by the provider that built
    # it -- the two do not read the same span (intervals reads the full 42-day cycle
    # window, the local health.db snapshot reads 14 days). A cycle session older than this
    # was never searched for an attachment, which is a different fact from searching and
    # finding nothing, and ``assemble_context`` is the only place that can tell the coach
    # which of the two it is looking at.
    actuals_window_start: dt.date
    # The dates the provider holds any activity for, inside the 7-day coverage window.
    # A set of dates and not a bare count: multiple activities on the same day must still
    # dedupe to one, which only set semantics guarantee.
    activity_days: frozenset[dt.date]
    coverage_sleep: dict[str, Any]
    coverage_hrv: dict[str, Any]
    coverage_resting_hr: dict[str, Any]
    recovery_trends: dict[str, Any]
    recent_actuals: list[dict[str, Any]]
    # Per-segment execution for recent runs, or ``None`` when this source cannot
    # produce it. Unlike ``strength_execution`` and ``recovery_signals``, which are
    # standalone groups fed by a separate local file, this one is the base source's own
    # activity evidence read one level finer, so it belongs to the domain and carries
    # the same source identity ``recent_actuals`` does.
    segment_execution: dict[str, Any] | None
    # The max HR configured on the provider's own Run sport settings, when this source
    # can reach it and a Run entry exists there -- ``None`` otherwise, for any reason:
    # no such setting, the read failed, this source has no such concept at all, or the
    # plan carries no measured max HR for it to be compared against, which is the one
    # case where the read is not worth making. All of those land on ``None`` because
    # this field is one side of a comparison rather than evidence in its own right:
    # ``_max_hr_divergence_note`` is its only reader and reports nothing unless both
    # sides are measured numbers, so no reader can be misled about what a ``None`` here
    # was. Kept apart from ``athlete_baseline.max_hr`` (PlanState, the coach's own
    # written figure) so ``assemble_context`` can compare the two without either
    # provider having to know the other value exists.
    sport_settings_max_hr: int | float | None
    extra_unknowns: list[str]


# --------------------------------------------------------------------------------------
# CLI argument parsing helpers (pure, testable independent of argparse)
# --------------------------------------------------------------------------------------


def parse_available_days(raw: str | None) -> list[str]:
    """Parse confirmed availability; omission stays unknown instead of meaning every day."""
    if raw is None:
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def parse_optional_bool(raw: str | None) -> bool | None:
    """Parse a tri-state true/false/null CLI value."""
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    raise ValueError(f"cannot parse boolean value: {raw!r}")


def parse_red_flag_overrides(pairs: list[str], *, all_clear: bool) -> dict[str, bool | None]:
    """Build the red_flags mapping: default null, ``--all-clear`` sets all false, then overrides."""
    flags: dict[str, bool | None] = {field: (False if all_clear else None) for field in RED_FLAG_FIELDS}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--red-flag must be key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        key = key.strip()
        if key not in RED_FLAG_FIELDS:
            raise ValueError(f"unknown red flag: {key!r}")
        flags[key] = parse_optional_bool(value)
    return flags


# --------------------------------------------------------------------------------------
# Small pure helpers shared by both providers
# --------------------------------------------------------------------------------------


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContextBuildError(f"unknown timezone: {name!r}") from exc


def _resolve_as_of(raw: str | None, tz: ZoneInfo, now: dt.datetime) -> dt.datetime:
    if raw is None:
        return now.astimezone(tz)
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ContextBuildError(f"--as-of must be an ISO-8601 timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _format_utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# The provenance an athlete-reported record carries. Defined here rather than in
# ``athlete_evidence`` because ``assemble_context`` has to recognise it too, and
# ``athlete_evidence`` already imports from this module -- the other direction would be a
# cycle. ``athlete_evidence`` re-exports it for readers of that module.
ATHLETE_REPORTED_SOURCE = "athlete_reported"

# The other two provenances a ``training_history`` bucket's rows can carry (issue #101).
# Same literal values ``athlete_evidence`` defines for its own writers -- this module
# never imports them from there, for the identical cycle reason ``ATHLETE_REPORTED_SOURCE``
# above already states -- kept here only because ``_build_training_history`` has to name
# a fixed, stable join order for a bucket's ``source`` summary and cannot import it.
ATHLETE_IMPORTED_SOURCE = "athlete_imported"
PRESCRIBED_CONFIRMED_SOURCE = "prescribed_confirmed"


def _reported_training_dates(strength_execution: dict[str, Any] | None) -> set[dt.date]:
    """The dates the athlete says they trained strength, from statements rather than devices.

    Only ever from sessions marked ``athlete_reported``. A local strength log's rows are
    measurements sitting beside the provider read that already counted their day, and
    counting them again here would double it.

    Strength only -- not because a strength report is the only statement of this kind the
    product accepts (``record_activity_summary`` now takes a run, swim or ride the watch
    missed the same way), but because strength is the one sport whose report shares a
    container with a measured, non-statement source, which is what the guard above exists
    for. Every other sport's report lives in ``reported_activities`` instead, with no such
    row to be confused with, and is folded in separately by ``_reported_training_days``
    (issue #30).
    """
    dates: set[dt.date] = set()
    for session in (strength_execution or {}).get("sessions") or []:
        if not isinstance(session, dict) or session.get("source") != ATHLETE_REPORTED_SOURCE:
            continue
        day = _safe_date(session.get("date"))
        if day is not None:
            dates.add(day)
    return dates


def _measurement_evidence(
    plan: dict[str, Any],
    plan_sessions: list[dict[str, Any]],
    elapsed_sessions: list[dict[str, Any]],
    cycle_session_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """What the cycle's own measurement can be read from right now (issues #13, #75).

    A review has always been able to say "progress is unproven", and until now that was
    the only thing it could ever say: the protocol was prose, so the product could not
    tell whether the measurement had been run, scheduled, or forgotten. This names the two
    sessions the comparison is between and reports, for each, whether a result came back
    -- and nothing else. There is no verdict here, no difference computed, and no
    threshold: the two readings live in ``cycle_sessions`` beside every other session, and
    what they mean is the coach's answer.

    ``None`` when the cycle declared no structured measurement, which is a real state and
    the honest one for a cycle whose protocol is prose alone. A review reading ``None``
    says this cycle scheduled no measurement, rather than saying progress is unproven for
    the twenty-eighth day running.
    """
    measurement = (plan.get("goal") or {}).get("measurement")
    if not isinstance(measurement, dict):
        return None
    reference_id = measurement.get("reference_session_id")
    evidence = {
        record.get("session_id"): record.get("activity_evidence")
        for record in cycle_session_records
    }
    week_start = _safe_date(measurement.get("measurement_week_start"))

    def marks_the_comparison(session: dict[str, Any]) -> bool:
        """A `measures` marker counts only inside the week the cycle named.

        Without the week test, a marker left on any session -- the reference itself, a
        session in an earlier week, one the coach moved out of the measurement week --
        becomes "the comparison", and the review compares two readings that were never
        the comparison. Being wrong here is worse than reporting nothing: the answer
        looks like the measurement and is not.
        """
        if session.get("measures") != reference_id or week_start is None:
            return False
        scheduled = _safe_date(session.get("scheduled_date"))
        return scheduled is not None and 0 <= (scheduled - week_start).days <= 6

    comparison = next(
        (
            session.get("session_id")
            for session in plan_sessions
            if marks_the_comparison(session)
        ),
        None,
    ) or next(
        # The elapsed sessions as the store wrote them, not the records built from them:
        # `measures` is a fact about the session, and the record carries the evidence.
        (
            session.get("session_id")
            for session in elapsed_sessions
            if marks_the_comparison(session)
        ),
        None,
    )
    return {
        "comparison_session_id": comparison,
        # The same vocabulary every other session's evidence is reported in, plus the two
        # states only a measurement has: a session the cycle's record does not hold yet
        # because its day has not passed, and no such session at all.
        "reference_result": evidence.get(reference_id, "not_in_record"),
        "comparison_result": (
            "not_scheduled"
            if comparison is None
            else evidence.get(comparison, "scheduled")
        ),
    }


def _reported_training_days(
    strength_execution: dict[str, Any] | None,
    reported_activities: dict[str, Any] | None = None,
) -> set[tuple[str, str]]:
    """The same statements as ``(date, sport)``, for matching against planned sessions.

    Strength comes from ``strength_execution`` via ``_reported_training_dates``, tagged
    with the one sport a strength report can mean. Every other sport comes straight off
    ``reported_activities`` (issue #30): each row already names its own sport, stated once
    through ``record_activity_summary`` (or carried in from an upload) and validated
    against the same sport vocabulary a planned session uses, so there is no mapping to
    invent here -- just the same field read from both sides.
    """
    pairs = {
        (day.isoformat(), "strength")
        for day in _reported_training_dates(strength_execution)
    }
    for row in (reported_activities or {}).get("activities") or []:
        if not isinstance(row, dict):
            continue
        date_value = row.get("date")
        sport = row.get("sport")
        if isinstance(date_value, str) and date_value and isinstance(sport, str) and sport:
            pairs.add((date_value, sport))
    return pairs


def coverage_entry(observed_days: int, expected_days: int = 7) -> dict[str, Any]:
    if observed_days == 0:
        status = "missing"
    elif observed_days == expected_days:
        status = "complete"
    else:
        status = "partial"
    return {"observed_days": observed_days, "expected_days": expected_days, "status": status}


def _median_trend(
    window_values: dict[dt.date, float],
    as_of_date: dt.date,
    *,
    band_points: float = 0.0,
    band_fraction: float = 0.0,
) -> dict[str, Any]:
    """Median of the last 3 days vs the prior 4 days, +/-band -> within_baseline.

    The band is whichever is larger of a fixed ``band_points`` offset and
    ``band_fraction`` of the prior-window median's magnitude. Sleep score and resting
    heart rate use a fixed point band (``band_points=10.0``); HRV has no single absolute
    band that makes sense across athletes, so it uses a relative ``band_fraction``
    instead. Passing only one of the two keeps the other inert (default 0.0).
    """
    observed_days = len(window_values)
    if observed_days < 3:
        return {"status": "unknown", "observed_days": observed_days, "expected_days": 7}
    recent_dates = {as_of_date - dt.timedelta(days=offset) for offset in range(0, 3)}
    prior_dates = {as_of_date - dt.timedelta(days=offset) for offset in range(3, 7)}
    recent_values = [value for day, value in window_values.items() if day in recent_dates]
    prior_values = [value for day, value in window_values.items() if day in prior_dates]
    if not recent_values or not prior_values:
        return {"status": "unknown", "observed_days": observed_days, "expected_days": 7}
    recent_median = statistics.median(recent_values)
    prior_median = statistics.median(prior_values)
    band = max(band_points, abs(prior_median) * band_fraction)
    low = prior_median - band
    high = prior_median + band
    if recent_median < low:
        status = "below_baseline"
    elif recent_median > high:
        status = "above_baseline"
    else:
        status = "within_baseline"
    return {"status": status, "observed_days": observed_days, "expected_days": 7}


def _classify_running(
    avg_speed_mps: float | None,
    activity_id: str,
    notes: list[str],
    threshold_sec_per_km: int | float | None,
) -> tuple[str, str]:
    """Return (adaptation, cost) for a running actual; body_stress is always 'lower'.

    This is a fallback only, used when an actual cannot be linked to a planned session
    (see ``_match_actuals_to_plan`` below). Average pace is a poor proxy for what a
    session actually was -- a threshold test's warmup and cooldown pull its average pace
    toward "easy" -- so this classification is replaced outright the moment a match is
    found; it exists only for the genuinely unplanned/unmatched case.

    Intensity is relative to the athlete's own threshold pace, never an absolute pace: a
    6:10/km threshold runner working at 6:05/km is doing hard threshold work, while the
    same pace is an easy jog for a 5:00/km athlete. Within 5% of threshold (or faster)
    reads as threshold work; the 5-12% band covers steady/moderate aerobic running;
    anything slower is easy. With no threshold on the baseline there is nothing to be
    relative to, so the activity stays at the floor (aerobic_base/easy) with a note
    rather than a guess from someone else's pace bands.
    """
    if avg_speed_mps is None or avg_speed_mps <= 0:
        notes.append(f"run_pace_unavailable:{activity_id}")
        return "aerobic_base", "easy"
    if threshold_sec_per_km is None or threshold_sec_per_km <= 0:
        notes.append(f"run_pace_unclassified_no_baseline:{activity_id}")
        return "aerobic_base", "easy"
    pace_sec_km = 1000.0 / avg_speed_mps
    if pace_sec_km <= threshold_sec_per_km * 1.05:
        return "threshold", "hard"
    if pace_sec_km <= threshold_sec_per_km * 1.12:
        return "aerobic_base", "moderate"
    return "aerobic_base", "easy"


# --------------------------------------------------------------------------------------
# Deterministic planned <-> actual matching
#
# This lives in context_core (not in either source module) because assemble_context is
# the one place that has both the current PlanState's sessions and the domain's actuals
# in hand at once -- see the module docstring. Every rule below is structural (same date
# + same sport, product ownership of the calendar item) or a plain numeric comparison
# (closest planned_minutes to duration_minutes, a stated duration band); there is no
# probability model and no learned heuristic anywhere in it.
# --------------------------------------------------------------------------------------

def _duration_gap_minutes(planned_minutes: Any, actual_minutes: Any) -> float:
    """Absolute planned-vs-actual duration gap, for ranking candidates. Unknown duration
    on either side sorts last (``math.inf``) rather than winning a ranking by default."""
    planned = _safe_float(planned_minutes)
    actual = _safe_float(actual_minutes)
    if planned is None or actual is None:
        return math.inf
    return abs(planned - actual)

def _apply_planned_classification(actual: dict[str, Any], session: dict[str, Any]) -> None:
    """Overwrite an actual's adaptation/body_stress/cost with the matched planned
    session's own values. Once an actual is linked to a specific session, the plan
    already says exactly what that session was for -- there is nothing left to infer
    from average pace, and inferring anyway is exactly what mislabels a full-effort
    threshold test as an easy aerobic run once its warmup/cooldown pull the average
    down (the real 2026-08-10 case this function exists to fix)."""
    actual["adaptation"] = session["adaptation"]
    actual["body_stress"] = session["body_stress"]
    actual["cost"] = session["cost"]


def _match_actuals_to_plan(
    recent_actuals: list[dict[str, Any]],
    plan_sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Link each actual to at most one planned session without turning a calendar
    coincidence into a completion fact.

    Rules, all structural or duration-ranked -- never a probability model or a learned
    heuristic:

      - a provider identity match exists only when the actual's ``paired_event_id``
        equals a session's ``execution.external_id`` and sport agrees. That is the only
        path to ``"matched"``;
      - ``"owned"`` is the product's own evidence, for the athlete who trained the
        session without entering it from the calendar item -- which is most of them, and
        nearly all strength work. It requires that the product delivered this session,
        that the provider has not already paired the activity to something else, and
        that the day admits exactly one reading: one activity of that sport, one planned
        session of that sport, and a duration inside ``owned_duration_within_band``.
        ``"matched"`` and ``"owned"`` are the two attachments deterministic
        reconciliation may auto-complete;
      - otherwise, same-date/same-sport is only ``"probable"``. When several sessions
        qualify, closest planned-vs-actual duration wins, with ``session_id`` as the
        deterministic tie-breaker;
      - zero candidates stays ``"unmatched"`` with ``planned_session_id=None``;
      - a planned session is claimed by at most one actual. When several actuals could
        claim the same session, the actual whose own best candidate is the closest
        duration match resolves first -- an explicit, duration-ranked "first come" order,
        never accidental input-list order;
    Mutates nothing in place -- returns new actual dicts in the same order as
    ``recent_actuals`` (oldest-to-newest), so callers never see matching reorder them.
    """
    pool: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    by_external_id: dict[str, list[dict[str, Any]]] = {}
    for session in plan_sessions:
        date = session.get("scheduled_date")
        sport = session.get("sport")
        session_id = session.get("session_id")
        if not date or not sport or not session_id:
            continue
        pool.setdefault((date, sport), []).append(session)
        external_id = (session.get("execution") or {}).get("external_id")
        if external_id is not None and str(external_id):
            by_external_id.setdefault(str(external_id), []).append(session)

    paired_claim_counts: dict[str, int] = {}
    for actual in recent_actuals:
        paired_event_id = actual.get("paired_event_id")
        if paired_event_id is not None and str(paired_event_id):
            key = str(paired_event_id)
            paired_claim_counts[key] = paired_claim_counts.get(key, 0) + 1

    def identity_candidate(actual: dict[str, Any]) -> dict[str, Any] | None:
        paired_event_id = actual.get("paired_event_id")
        if paired_event_id is None or not str(paired_event_id):
            return None
        key = str(paired_event_id)
        candidates = by_external_id.get(key, [])
        # Duplicate ids on either side are conflicting evidence, not a tie that may
        # be broken into an automatic completion by a duration heuristic.
        if (
            paired_claim_counts.get(key) != 1
            or len(candidates) != 1
            or candidates[0].get("sport") != actual.get("sport")
        ):
            return None
        return candidates[0]

    actuals_per_day_sport: dict[tuple[Any, Any], int] = {}
    for actual in recent_actuals:
        key = (actual.get("date"), actual.get("sport"))
        actuals_per_day_sport[key] = actuals_per_day_sport.get(key, 0) + 1

    def ownership_candidate(actual: dict[str, Any], claimed: set[str]) -> dict[str, Any] | None:
        paired_event_id = actual.get("paired_event_id")
        if paired_event_id is not None and str(paired_event_id):
            # The provider already named an event for this activity. If that event were
            # ours, identity resolution would have claimed it; that it did not means the
            # pairing points elsewhere, and contrary evidence never becomes ownership.
            return None
        key = (actual.get("date"), actual.get("sport"))
        if actuals_per_day_sport.get(key) != 1:
            return None
        candidates = pool.get(key, [])
        if len(candidates) != 1:
            return None
        session = candidates[0]
        if session["session_id"] in claimed or not product_delivered(session):
            return None
        if not owned_duration_within_band(session.get("planned_minutes"), actual.get("duration_minutes")):
            return None
        return session

    def unclaimed_candidates(actual: dict[str, Any], claimed: set[str]) -> list[dict[str, Any]]:
        key = (actual.get("date"), actual.get("sport"))
        return [session for session in pool.get(key, []) if session["session_id"] not in claimed]

    def best_gap(actual: dict[str, Any]) -> float:
        key = (actual.get("date"), actual.get("sport"))
        candidates = pool.get(key, [])
        if not candidates:
            return math.inf
        return min(
            _duration_gap_minutes(session.get("planned_minutes"), actual.get("duration_minutes"))
            for session in candidates
        )

    # Exact provider identities resolve before calendar candidates; otherwise a moved
    # completion could lose its session to an unrelated same-day activity.
    processing_order = sorted(
        range(len(recent_actuals)),
        key=lambda i: (
            0 if identity_candidate(recent_actuals[i]) is not None else 1,
            best_gap(recent_actuals[i]),
            recent_actuals[i].get("activity_id") or "",
        ),
    )

    results = [dict(actual) for actual in recent_actuals]
    claimed: set[str] = set()
    for i in processing_order:
        actual = results[i]
        actual_minutes = actual.get("duration_minutes")
        identity_session = identity_candidate(actual)
        if (
            identity_session is not None
            and identity_session["session_id"] not in claimed
            and identity_session.get("sport") == actual.get("sport")
        ):
            claimed.add(identity_session["session_id"])
            actual["planned_session_id"] = identity_session["session_id"]
            actual["match_confidence"] = "matched"
            _apply_planned_classification(actual, identity_session)
            continue

        owned_session = ownership_candidate(actual, claimed)
        if owned_session is not None:
            claimed.add(owned_session["session_id"])
            actual["planned_session_id"] = owned_session["session_id"]
            actual["match_confidence"] = "owned"
            _apply_planned_classification(actual, owned_session)
            continue

        candidates = unclaimed_candidates(actual, claimed)
        if not candidates:
            continue  # no plan session left to claim -- stays "unmatched" as the domain set it

        if len(candidates) == 1:
            session = candidates[0]
        else:
            session = min(
                candidates,
                key=lambda s: (_duration_gap_minutes(s.get("planned_minutes"), actual_minutes), s["session_id"]),
            )

        claimed.add(session["session_id"])
        actual["planned_session_id"] = session["session_id"]
        actual["match_confidence"] = "probable"
        _apply_planned_classification(actual, session)

    return results


# --------------------------------------------------------------------------------------
# Shared time window
# --------------------------------------------------------------------------------------


def review_horizon_start(
    plan: dict[str, Any], as_of_date: dt.date, read_window_start: dt.date | None = None
) -> dt.date:
    """The earliest day a review still reads session by session.

    Three windows are under review at once and each names its own start: a week review
    starts at the previous Monday, a cycle review at the cycle's declared start, and
    reconciliation at whatever day the plan's current week begins on -- which is
    wherever the plan put it, not necessarily either of the others. The earliest of
    the three is the horizon, so one window covers all of them and the context states
    one number instead of leaving each evidence group to pick its own span.

    Reconciliation is the reason the plan's own week is in that minimum rather than
    assumed to sit inside the other two: it matches this week's sessions against
    ``recent_actuals`` by date, so a stale week whose start predates the previous
    Monday must still find its actuals there.

    Everything before the horizon is not dropped -- it is read at the grain that
    answers questions about it. Months of training answer "how has my volume moved",
    which is ``training_history``'s monthly rollup, and what six weeks of provider
    evidence says about a baseline is ``baseline_evidence``, which states its own
    window. Neither is answered session by session, and carrying those rows spends
    every later turn of the conversation on evidence no review reads (issue #233,
    AGENTS.md 13).

    ``read_window_start`` is the earliest day the provider was actually read on, and
    the horizon is never earlier than it. A cycle is allowed to overrun its declared
    length -- ``cycle_day`` is uncapped precisely so that running out is visible -- so
    a long-overdue cycle can declare a start before the six weeks anybody looked at.
    Reporting that day as the horizon would say the rows begin where nothing was read,
    which is the one thing a stated window must not do.
    """
    week_start = as_of_date - dt.timedelta(days=as_of_date.weekday())
    candidates = [week_start - dt.timedelta(days=7)]
    cycle = plan.get("cycle") if isinstance(plan.get("cycle"), dict) else {}
    cycle_start = _safe_date(cycle.get("start"))
    if cycle_start is not None:
        candidates.append(cycle_start)
    plan_week = plan.get("week") if isinstance(plan.get("week"), dict) else {}
    plan_week_start = _safe_date(plan_week.get("start"))
    if plan_week_start is not None:
        candidates.append(plan_week_start)
    horizon = min(candidates)
    if read_window_start is not None:
        return max(horizon, read_window_start)
    return horizon


def prescribed_reps_dates(sessions: list[dict[str, Any]]) -> frozenset[dt.date]:
    """The days these sessions prescribed more than one step on.

    The discriminator ``segment_execution`` is read through (issue #233). It is
    structural rather than a reading of the numbers: a session whose plan is one step
    -- "easy 40 minutes under 140 bpm" -- is judged by the average pace and average
    heart rate ``recent_actuals`` already carries, and a session that prescribed a
    warm-up, four repeats and a cool-down is the case an average cannot answer.
    Nothing here inspects a target, a pace, or the prescription text.
    """
    days: set[dt.date] = set()
    for session in sessions:
        if not isinstance(session, dict):
            continue
        plan = session.get("plan")
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list) or len(steps) < 2:
            continue
        day = _safe_date(session.get("scheduled_date"))
        if day is not None:
            days.add(day)
    return frozenset(days)


def build_window(request: ContextRequest, resolved_now: dt.datetime) -> BuildWindow:
    """Resolve the single temporal frame (as_of, coverage/trend windows) a build runs
    against, from the athlete's requested timezone/as_of and the injected wall clock.
    Pure and source-agnostic: every provider reads the same window.
    """
    tz = _zone(request.timezone_name)
    as_of = _resolve_as_of(request.as_of_raw, tz, resolved_now)
    return BuildWindow(
        as_of=as_of,
        resolved_now=resolved_now,
        now_iso=_format_utc(resolved_now),
        window_start=as_of.date() - dt.timedelta(days=6),
        window_end=as_of.date(),
        window14_start=as_of.date() - dt.timedelta(days=13),
        window14_end=as_of.date(),
        # One cycle, which the product caps at 28 days. It is the span a cycle review
        # asks about -- "the 5x1000m in week one" on day 26 -- and the reason the
        # segment read stops here rather than at 42: six weeks of quality sessions do
        # not fit the context budget even in the compact shape (issue #290).
        window28_start=as_of.date() - dt.timedelta(days=27),
        window28_end=as_of.date(),
        window42_start=as_of.date() - dt.timedelta(days=41),
        window42_end=as_of.date(),
    )


def _movement_group_identity(
    exercise: Any, established: list[dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """The key one strength row groups under, and the baseline entry that names it.

    ``anchoring_baseline`` already answers "which baseline entry is this movement" by
    canonical key or display_name; grouping by the raw spelling while that answer sits
    two lines away is what filed one lift under two keys -- ``bench_press`` confirmed
    from the plan and 臥推 in the athlete's own words never met (issue #238). A movement
    no baseline names keeps its own normalized spelling: widening the match with string
    similarity would invent the athlete's meaning (AGENTS.md 5).

    Projection only. The stored identity, ``athlete_evidence.exercise_key``, stays the
    raw spelling on purpose: a correcting upsert or a retraction must find the record
    under the name it was stored with, and widening *that* key would let two same-day
    records of differently-named movements displace each other.
    """
    anchor = anchoring_baseline(exercise, established)
    if anchor is not None:
        key = normalize_exercise_name(anchor.get("exercise"))
        if key:
            return key, anchor
    # No anchor -- or one whose own key normalizes to nothing, which the caller could
    # not tell apart from a mismatched group name. Either way the row stands alone.
    return normalize_exercise_name(exercise), None


def _prescribed_movements_by_date(
    cycle_sessions: list[dict[str, Any]] | None,
    plan: dict[str, Any],
    established: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Index every prescribed movement by (date, baseline-resolved exercise key).

    Both the elapsed cycle and the week the plan still holds: today's session lives in
    the plan and has not reached the cycle record yet, and today is exactly the session
    a coach is most likely to be reading against.

    Keys resolve through ``_movement_group_identity``, same as the performed side --
    a prescription written under the plan's canonical key and a report in the athlete's
    own word must land on the same key, or the occurrence reads as trained off-plan.

    A list per key, not one entry: a day can prescribe the same movement twice on
    purpose -- four sets at one load and a fifth at another is one prescription in two
    parts, and collapsing it would erase the part that says where the load gave way.
    """
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    week_sessions = (plan.get("week") or {}).get("sessions") or []
    for session in list(cycle_sessions or []) + list(week_sessions):
        date = session.get("scheduled_date")
        if not isinstance(date, str):
            continue
        for movement in plan_movements(session):
            key, _anchor = _movement_group_identity(movement.get("exercise"), established)
            if not key:
                continue
            entry = {
                "sets": movement.get("sets"),
                "reps": movement.get("reps"),
                "load_kg": movement.get("load_kg"),
                "assist_kg": movement.get("assist_kg"),
                "load_basis": movement.get("load_basis"),
            }
            bucket = index.setdefault((date, key), [])
            if entry not in bucket:
                bucket.append(entry)
    return index


def _load_key(item: dict[str, Any]) -> tuple[Any, Any]:
    """The identity one performed set's load carries, for grouping alongside others.

    ``(weight_kg, assist_kg)`` together, not ``weight_kg`` alone: an assisted movement's
    load lives in ``assist_kg``, and a rollup keyed on weight only would fold every
    assisted set into one "no weight" bucket regardless of how much help it carried.
    Unmeasured stays unmeasured -- a non-numeric or absent field becomes ``None``, never
    a guessed ``0``, so a bodyweight set and an unrecorded one still group correctly by
    what is actually equal.
    """
    weight = item.get("weight_kg")
    assist = item.get("assist_kg")
    return (
        weight if _measured_number(weight) else None,
        assist if _measured_number(assist) else None,
    )


def _load_rollup(sets: list[dict[str, Any]]) -> dict[str, Any]:
    """The arithmetic one occurrence's own ``performed_sets`` already answers, done once
    here instead of by whoever reads it next.

    Three additions, all over the same array ``performed_sets`` carries beside this:
    reps at each distinct load, the session's total reps, and which load was heaviest
    and whether every set held it. Nothing here weighs a session, compares it to
    another, or concludes a direction -- that reading stays the coach's (AGENTS.md 1),
    matching ``_build_movement_history``'s own stance one level up.

    A group's ``reps`` -- and the session ``total_reps`` -- is ``null`` the moment any
    set contributing to it has no recorded rep count. Summing only the sets that do
    have one and calling the result complete would read a missing rep count as zero,
    which is exactly what AGENTS.md 3 forbids: the honest answer to "how many reps" is
    "not fully known", not a number that looks exact but silently skipped one.
    """
    groups: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []
    total_reps: int | None = 0
    for item in sets:
        if not isinstance(item, dict):
            continue
        key = _load_key(item)
        bucket = groups.get(key)
        if bucket is None:
            bucket = {"weight_kg": key[0], "assist_kg": key[1], "reps": 0, "complete": True}
            groups[key] = bucket
            order.append(key)
        reps = item.get("reps")
        if isinstance(reps, int) and not isinstance(reps, bool):
            bucket["reps"] += reps
            if total_reps is not None:
                total_reps += reps
        else:
            bucket["complete"] = False
            total_reps = None
    by_load = [
        {
            "weight_kg": groups[key]["weight_kg"],
            "assist_kg": groups[key]["assist_kg"],
            "reps": groups[key]["reps"] if groups[key]["complete"] else None,
        }
        for key in order
    ]
    # The top load: a weight comparison wins outright when any group carries one, since
    # a weighted and an assisted set are never the same movement's two working loads.
    # Otherwise the least assistance is the heavier direction -- same rule
    # ``_build_baseline_evidence`` already uses one level up, kept in one place so the
    # two never learn to disagree about which load in a session was hardest.
    weighted = [key for key in order if key[0] is not None]
    assisted = [key for key in order if key[0] is None and key[1] is not None]
    if weighted:
        top_key = max(weighted, key=lambda key: key[0])
    elif assisted:
        top_key = min(assisted, key=lambda key: key[1])
    else:
        top_key = None
    top_load = None
    if top_key is not None:
        top_load = {
            "weight_kg": top_key[0],
            "assist_kg": top_key[1],
            "held_every_set": all(
                _load_key(item) == top_key for item in sets if isinstance(item, dict)
            ),
        }
    return {"by_load": by_load, "total_reps": total_reps, "top_load": top_load}


def _build_movement_history(
    cycle_sessions: list[dict[str, Any]] | None,
    plan: dict[str, Any],
    strength_execution: dict[str, Any] | None,
    baseline: dict[str, Any],
) -> dict[str, Any] | None:
    """Group strength evidence by movement, with what was prescribed beside it.

    ``strength_execution`` answers "what was lifted on this date"; this answers "how has
    this movement been going", which is the question a next prescription actually turns
    on. The same figures, pivoted -- no new source, no second strength store.

    Reading two occurrences side by side is the point. Four sets of five at 65 kg with
    the fifth dropped to 60, then five sets of four at 65, are the same load conceding
    in two different directions; either one alone reads like a simple pass or fail.

    Nothing here is a verdict. No completion rate, no progression score, no "improving"
    flag: which way a movement is going is a coaching read of these rows (AGENTS.md 1).
    """
    if not strength_execution:
        return None
    performed = strength_execution.get("sessions") or []
    if not performed:
        return None

    established = [
        load for load in (baseline.get("strength_loads") or []) if isinstance(load, dict)
    ]
    prescribed = _prescribed_movements_by_date(cycle_sessions, plan, established)

    grouped: dict[str, dict[str, Any]] = {}
    for entry in performed:
        date = entry.get("date")
        exercise = entry.get("exercise")
        # Skip, never coerce: normalize would happily stringify a non-string into a
        # truthy key ("none"), and the resulting nameless group fails context
        # validation -- taking the whole build down for one damaged row instead of
        # dropping the row, the same stance _strength_report_rows takes at ingest.
        if not isinstance(exercise, str) or not isinstance(date, str):
            continue
        key, anchor = _movement_group_identity(exercise, established)
        if not key:
            continue
        group = grouped.setdefault(
            key,
            {
                # The baseline's own key when one anchors, so a group merging two
                # spellings has a stable name; the reported spelling otherwise.
                "exercise": anchor.get("exercise") if anchor is not None else exercise,
                # The athlete's own word for it, when a baseline entry carries one.
                # Null rather than the canonical key: that key is an internal handle,
                # and showing it would put it in front of the athlete.
                "display_name": (anchor or {}).get("display_name"),
                "baseline": (
                    {
                        "load_kg": anchor.get("load_kg"),
                        "assist_kg": anchor.get("assist_kg"),
                        "scheme": anchor.get("scheme"),
                    }
                    if anchor is not None
                    else None
                ),
                "occurrences": [],
            },
        )
        occurrence: dict[str, Any] = {
            "date": date,
            # Null means this date prescribed no such movement -- trained off-plan,
            # or on a day older than the plan record. Not the same as prescribed
            # and missed, which shows as a prescription with no performed sets.
            "prescribed": prescribed.get((date, key)),
            "performed_sets": entry.get("sets") or [],
            # Derived from that same array, never stored independently of it: the
            # arithmetic a reader was doing by hand, and getting wrong.
            "load_rollup": _load_rollup(entry.get("sets") or []),
            "notes": entry.get("notes") or [],
            # Per occurrence, because a movement's rows can now come from two places
            # at once: a local strength log writes what was measured, and the athlete
            # reports what they remember. Reading two occurrences side by side is the
            # point of this group, and 65 kg measured followed by 70 kg recalled is
            # not the same evidence as two measured figures -- without this the coach
            # would read a provenance change as a load change.
            "source": entry.get("source"),
        }
        # The name this row is stored under, said only when it differs from the
        # group's. Storage stays keyed on the raw spelling (athlete_evidence.
        # exercise_key), so a correction or retraction aimed at this row must use
        # this name -- sent under the group's merged name it would miss the record,
        # and a correction would open a second same-day entry instead.
        if normalize_exercise_name(exercise) != key:
            occurrence["reported_as"] = exercise
        group["occurrences"].append(occurrence)

    if not grouped:
        return None
    for group in grouped.values():
        group["occurrences"].sort(key=lambda item: item["date"])
    return {
        "source": strength_execution.get("source"),
        "window_start": strength_execution.get("window_start"),
        "window_end": strength_execution.get("window_end"),
        "movements": [grouped[key] for key in sorted(grouped)],
    }


# The most recent populated calendar months ``training_history`` keeps (issue #101). A
# single global cap across every sport combined, not per sport: an athlete training three
# sports in one month must not cost three times the budget of one who trains one sport,
# and every kept month is paid for by every later turn (AGENTS.md 13).
TRAINING_HISTORY_MAX_MONTHS = 24

# The most movements ``movement_longevity`` keeps. Unlike the month cap, nothing here
# is a calendar unit to sort by -- an exercise vocabulary can grow without bound (a
# typo, a rename, a genuinely new lift), so it needs its own cap and its own priority
# rule. See ``_training_history_movement_longevity`` for what "most recently observed"
# and its tiebreak mean.
TRAINING_HISTORY_MAX_MOVEMENTS = 15

# The fixed order a bucket's ``source`` field joins whichever provenances its rows
# actually carry in, mirroring ``_strength_execution_source``'s own "+"-joined style one
# level up in ``context_builder``. Rarely all three at once -- that needs a bucket
# mixing a spoken report, an upload, and a confirmed prescription.
_TRAINING_HISTORY_PROVENANCE_ORDER = (
    ATHLETE_REPORTED_SOURCE,
    ATHLETE_IMPORTED_SOURCE,
    PRESCRIBED_CONFIRMED_SOURCE,
)


def _training_history_month(date_str: Any) -> str | None:
    """The calendar month one dated row belongs to (``YYYY-MM``), or ``None`` for a row
    too damaged to place. A fixed calendar unit, never a rolling 30-day slice -- "June"
    is a span every later reader agrees on without recomputing it against ``as_of``."""
    day = _safe_date(date_str)
    if day is None:
        return None
    return f"{day.year:04d}-{day.month:02d}"


def _training_history_bucket() -> dict[str, Any]:
    return {
        "minutes": [],
        "km": [],
        "activity_row_count": 0,
        "activity_dates": set(),
        "strength_dates": set(),
        "counts": {name: 0 for name in _TRAINING_HISTORY_PROVENANCE_ORDER},
    }


def _training_history_session_count(sport: str, bucket: dict[str, Any]) -> int:
    """One (month, sport) bucket's session count.

    Every other sport's rows are one row per session by construction -- an upload is the
    one case that can leave two real sessions on one day, and it does so as two distinct
    rows (``athlete_evidence``'s own same-session predicate only collapses a *spoken*
    restatement), so counting rows is counting sessions. Strength is the exception: one
    gym visit can be described twice over -- once as a coarse ``reported_activities``
    summary, once as one or more per-exercise ``strength_reports`` entries -- and
    counting rows there would read one workout as several. So strength counts distinct
    training days across both containers instead, the same union
    ``_reported_training_days`` already uses one level up for the identical
    two-containers-one-sport problem.
    """
    if sport == "strength":
        return len(bucket["activity_dates"] | bucket["strength_dates"])
    return bucket["activity_row_count"]


def _training_history_heaviness_rank(observation: dict[str, Any] | None) -> tuple[int, float]:
    """A sortable "how heavy" proxy for one load observation -- a larger tuple is
    heavier, comparable with plain ``>``. Weighted beats assisted outright (tier 2 vs
    1), the same precedence ``_load_rollup``'s own top-load comparator uses; a
    bodyweight-only or absent observation ranks lowest (tier 0). Restated as a plain
    sortable key here because ``movement_longevity``'s truncation tiebreak compares
    across *different* movements, not within one occurrence's sets, so it cannot reuse
    ``_load_rollup``'s within-session grouping directly.
    """
    if observation is None:
        return (0, 0.0)
    if observation.get("weight_kg") is not None:
        return (2, float(observation["weight_kg"]))
    if observation.get("assist_kg") is not None:
        return (1, -float(observation["assist_kg"]))
    return (0, 0.0)


def _training_history_movement_longevity(
    strength_reports: list[dict[str, Any]], baseline: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Per movement, the earliest and the heaviest observation across the athlete's
    complete strength-report history (issue #101) -- never windowed, because "how long
    has this movement been going, and what is the most it has ever carried" is exactly
    the question six weeks of evidence cannot answer.

    Reuses ``_load_rollup``'s own top-load reading -- the heaviest working set within one
    occurrence -- rather than a second comparator, so the two can never learn to disagree
    about what counts as heavier within one day. Weighted beats assisted outright, same
    as every other load comparison in this module; among assisted occurrences, less
    assistance is the heavier direction (``_load_rollup``'s own rule, reused via
    ``_latest_extreme`` for the newest-date tiebreak). ``heaviest`` is ``None`` only when
    no occurrence of the movement ever carried a measured weight or assist figure at all
    -- a bodyweight movement with nothing to compare. ``earliest`` is never ``None``: the
    movement's own key existing already means at least one dated report does.

    Nothing here is a verdict, matching ``_build_movement_history``'s own stance: no
    progression score, no "improving" flag. Two numbers and their dates are the coaching
    evidence; which way they point is the coach's reading (AGENTS.md 1).

    At most ``TRAINING_HISTORY_MAX_MOVEMENTS`` survive, unlike ``months`` there is no
    calendar to sort a cut by -- an exercise vocabulary can grow without bound, so the
    kept movements are the most recently observed ones: each movement's own latest
    occurrence date, newest first. A tie on that date is broken by
    ``_training_history_heaviness_rank`` -- the objectively heavier historical best
    wins -- and a further tie (both bodyweight-only, same last-observed day) by the
    normalized exercise key, so the order never depends on dict or input iteration
    order. The returned bool is ``True`` exactly when this cut dropped anything, the
    same fact ``months``' own ``truncated`` states one level up.
    """
    established = [
        load for load in (baseline.get("strength_loads") or []) if isinstance(load, dict)
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for entry in strength_reports:
        exercise = entry.get("exercise")
        date = entry.get("date")
        if not isinstance(exercise, str):
            continue
        key, anchor = _movement_group_identity(exercise, established)
        if not key or not isinstance(date, str):
            continue
        top_load = _load_rollup(entry.get("sets") or []).get("top_load") or {}
        observation = {
            "date": date,
            "weight_kg": top_load.get("weight_kg"),
            "assist_kg": top_load.get("assist_kg"),
            # Not applicable rather than a claim: an occurrence with no measured weight
            # or assistance never had a load to hold, so True here would assert a
            # load-consistency question that never arose.
            "held_every_set": bool(top_load.get("held_every_set", False)),
        }
        # Same rule as a movement_history occurrence: when the stored spelling is not
        # the merged group's name, the row says which name a correction or retraction
        # must use -- here there is no windowed sibling field left to recover it from.
        if normalize_exercise_name(exercise) != key:
            observation["reported_as"] = exercise
        group = grouped.setdefault(
            key,
            {
                # Same rule as _build_movement_history: the anchoring baseline's own
                # key names a merged group; the reported spelling stands otherwise.
                "exercise": anchor.get("exercise") if anchor is not None else exercise,
                "observations": [],
            },
        )
        group["observations"].append(observation)

    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for key in sorted(grouped):
        exercise = grouped[key]["exercise"]
        observations = grouped[key]["observations"]
        # Re-resolved from the group's own name rather than carried in the group dict:
        # a merged group's name is the anchor's own key, which resolves straight back
        # to that anchor, and an unmerged name resolves to nothing -- same answer, one
        # less field whose meaning depends on which occurrence created the group.
        anchor = anchoring_baseline(exercise, established)
        earliest = min(observations, key=lambda item: item["date"])
        weighted = [item for item in observations if item["weight_kg"] is not None]
        assisted = [item for item in observations if item["assist_kg"] is not None]
        if weighted:
            heaviest = _latest_extreme(weighted, "weight_kg", pick_max=True)
        elif assisted:
            heaviest = _latest_extreme(assisted, "assist_kg", pick_max=False)
        else:
            heaviest = None
        movement = {
            "exercise": exercise,
            "display_name": (anchor or {}).get("display_name"),
            "earliest": earliest,
            "heaviest": heaviest,
        }
        latest_date = max(item["date"] for item in observations)
        tier, magnitude = _training_history_heaviness_rank(heaviest)
        # Ascending-sortable priority for "most recently observed first, heavier-best
        # breaks a tie, exercise key breaks what's left" -- every numeric component is
        # negated so a single plain ascending sort produces the wanted order in one
        # pass, with no separate reverse=True per field.
        priority = (-dt.date.fromisoformat(latest_date).toordinal(), -tier, -magnitude, key)
        ranked.append((priority, movement))

    ranked.sort(key=lambda item: item[0])
    truncated = len(ranked) > TRAINING_HISTORY_MAX_MOVEMENTS
    kept = [movement for _priority, movement in ranked[:TRAINING_HISTORY_MAX_MOVEMENTS]]
    return kept, truncated


def _build_training_history(
    reported_activities: list[dict[str, Any]] | None,
    strength_reports: list[dict[str, Any]] | None,
    baseline: dict[str, Any],
) -> dict[str, Any] | None:
    """Store-held athlete evidence, rolled up to the span six weeks of ``recent_actuals``
    can never show (issue #101's hosted half).

    Two ingredients, both already the athlete's own evidence and neither windowed to the
    42-day cycle-planning span: ``reported_activities`` is
    ``athlete_evidence.all_reported_activity_summaries`` (spoken plus imported sessions,
    every sport including a coarse strength summary); ``strength_reports`` is
    ``athlete_evidence.all_reported_strength_sessions`` (per-exercise, per-date detail).
    Nothing measured ever reaches this function -- a Garmin-connected provider's own
    pre-connection history is the other, still-open half of issue #101, structurally
    unavailable to a hosted build and out of scope for this group.

    Bucketed by calendar month x sport, never a rolling window, and only a month holding
    at least one row appears at all. This is a coarse rollup and stays shaped like one --
    no activity_id, no match_confidence, no per-session rows -- so it can never be
    misread as ``recent_actuals``'s per-session truth (AGENTS.md 3 cuts both ways: an
    evidence gap must not read as zero, and a summary must not read as more precision
    than it carries).

    Session counting differs by sport; see ``_training_history_session_count``'s own
    docstring for why. ``total_minutes`` and ``total_km`` are honest partial sums -- over
    rows that actually stated the figure, ``None`` rather than 0 when none did (AGENTS.md
    3) -- never a claim that every session in the bucket is accounted for.
    ``strength_reports`` rows never contribute minutes or distance at all; that source
    carries neither.

    At most ``TRAINING_HISTORY_MAX_MONTHS`` populated calendar months survive, oldest
    first -- a coach reading a trend reads it left to right. ``truncated`` and
    ``earliest_observed_month`` say, honestly, whether older populated months exist
    beyond what is kept and how far back they go, rather than letting a dropped month
    silently read as a month with nothing in it.

    ``movement_longevity`` carries its own, separate cap and its own honest truncation
    flag -- see ``_training_history_movement_longevity`` for the full reasoning. A
    calendar month is a bounded, self-limiting axis; an exercise vocabulary is not, so
    it needs its own rule rather than inheriting the month cap's.

    ``None`` -- the whole group, never an empty one -- only when neither ingredient holds
    a single row: the ordinary starting state for an athlete who has reported nothing
    long-range yet, the same precedent every other conversational-evidence group here
    already sets. ``assemble_context`` pairs a ``None`` result with an ``unknowns`` entry,
    because a bare null here is exactly the shape "no long-range evidence" and "never
    looked" would otherwise share -- and reading the first as the second, across a gap
    read as zero, is the exact failure issue #101 opened on.
    """
    activities = [row for row in (reported_activities or []) if isinstance(row, dict)]
    strength = [row for row in (strength_reports or []) if isinstance(row, dict)]
    if not activities and not strength:
        return None

    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for row in activities:
        month = _training_history_month(row.get("date"))
        sport = row.get("sport")
        if month is None or not isinstance(sport, str) or not sport:
            continue
        bucket = buckets.setdefault((month, sport), _training_history_bucket())
        bucket["activity_row_count"] += 1
        if sport == "strength":
            bucket["activity_dates"].add(row.get("date"))
        minutes = row.get("duration_minutes")
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            bucket["minutes"].append(minutes)
        km = row.get("distance_km")
        if isinstance(km, (int, float)) and not isinstance(km, bool):
            bucket["km"].append(km)
        source = row.get("source")
        if source in bucket["counts"]:
            bucket["counts"][source] += 1

    for row in strength:
        month = _training_history_month(row.get("date"))
        if month is None:
            continue
        bucket = buckets.setdefault((month, "strength"), _training_history_bucket())
        bucket["strength_dates"].add(row.get("date"))
        source = row.get("source")
        if source in bucket["counts"]:
            bucket["counts"][source] += 1

    if not buckets:
        return None

    all_months = sorted({month for month, _sport in buckets})
    earliest_observed_month = all_months[0]
    kept_months = all_months[-TRAINING_HISTORY_MAX_MONTHS:]
    kept = set(kept_months)
    truncated = len(all_months) > len(kept_months)

    months_out: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for (month, sport), bucket in buckets.items():
        if month not in kept:
            continue
        seen_sources.update(name for name, count in bucket["counts"].items() if count)
        months_out.append(
            {
                "month": month,
                "sport": sport,
                "session_count": _training_history_session_count(sport, bucket),
                "total_minutes": sum(bucket["minutes"]) if bucket["minutes"] else None,
                "total_km": round(sum(bucket["km"]), 2) if bucket["km"] else None,
                "provenance_counts": dict(bucket["counts"]),
            }
        )
    months_out.sort(key=lambda item: (item["month"], item["sport"]))
    movement_longevity, movement_longevity_truncated = _training_history_movement_longevity(
        strength, baseline
    )

    return {
        "source": "+".join(
            name for name in _TRAINING_HISTORY_PROVENANCE_ORDER if name in seen_sources
        ),
        "months": months_out,
        "truncated": truncated,
        "earliest_observed_month": earliest_observed_month,
        "movement_longevity": movement_longevity,
        "movement_longevity_truncated": movement_longevity_truncated,
    }


def _dated_observations(
    rows: list[dict[str, Any]] | None, sources: set[str] | None = None
) -> list[dt.date]:
    """Every day one stream of evidence produced something, one entry per row -- so a
    stream that produced twice on one day appears twice, which is what ``observations``
    counts.

    ``sources`` keeps a stream to the provenances that stream is made of; ``None`` takes
    every row. A row too damaged to place on a date is not an observation of anything and
    is dropped, the same way every reader upstream of this already drops it.
    """
    dates: list[dt.date] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if sources is not None and row.get("source") not in sources:
            continue
        day = _safe_date(row.get("date"))
        if day is not None:
            dates.append(day)
    return dates


def _evidence_expectation_stream(
    stream: str,
    dates: list[dt.date],
    *,
    as_of: dt.date,
    window_start: dt.date | None = None,
) -> dict[str, Any] | None:
    """One stream's row, or ``None`` when it has never produced a dated observation.

    ``window_start`` is present on exactly the rows whose evidence is a read rather than
    a record, and it is what stops ``first_observed`` from being read as the athlete's
    first ever session: on a provider row it is only the first one inside the span this
    build asked for, and a longer span could hold an earlier one.

    ``observations`` counts rows, not distinct days: two lifts logged on one day are two
    observations of a stream that is producing. ``days_since_last`` is the count from the
    last observed day to ``as_of`` and never goes below zero -- an observation dated later
    than ``as_of`` (a provider row on the far side of a timezone boundary) is zero days of
    silence, and blocking a whole coaching turn over one would be a high price for a group
    nothing deterministic reads.
    """
    if not dates:
        return None
    row: dict[str, Any] = {
        "stream": stream,
        "basis": "read_window" if window_start is not None else "stored_record",
    }
    if window_start is not None:
        row["window_start"] = window_start.isoformat()
    last = max(dates)
    row["first_observed"] = min(dates).isoformat()
    row["last_observed"] = last.isoformat()
    row["observations"] = len(dates)
    row["days_since_last"] = max((as_of - last).days, 0)
    return row


def _build_evidence_expectations(
    provider_actuals: list[dict[str, Any]],
    reported_activities: list[dict[str, Any]] | None,
    strength_reports: list[dict[str, Any]] | None,
    body_measurements: list[dict[str, Any]] | None,
    *,
    actuals_window_start: dt.date,
    as_of: dt.date,
) -> dict[str, Any] | None:
    """Which streams of evidence this athlete has ever produced, and when each one last
    did (issue #28, the derived half).

    A stream that supplied evidence for months and then stopped used to be invisible. The
    group it fed reads ``null`` beside an ``unknowns`` line that said the same thing on
    the first day the product was ever run, so nothing separated "this stopped five weeks
    ago" from "this has never been here" -- and the first is a fact worth acting on while
    the second is not. One dated row per stream is what tells them apart.

    **No row is the false-positive control, and it is structural rather than a rule.** A
    stream appears only once it has produced a dated observation, so an athlete who has
    never claimed a recovery device has no recovery row to be missing -- never seen is not
    expected, and not expected is not reported. There is no list of streams an athlete
    ought to have anywhere in this function, which is what keeps the group from nagging
    somebody about equipment they do not own.

    **Nothing here is a verdict** (AGENTS.md 5). No status, no ``expected`` flag, no
    severity, no score, no boolean for broken: a row is dates and counts, and what a gap
    means is the coach's reading. Nothing deterministic reads the group either -- no
    validator branches on it, no ``unknowns`` entry comes from it, and no
    ``activity_evidence`` value moves because of it.

    The axis is ``date``, the day the evidence is about, never ``recorded_at``. Recording
    time was the rejected alternative: a provider row has none at all, so the two kinds of
    stream would be measured on different clocks and be incomparable, and a bulk import of
    a year of training would collapse ``first_observed`` onto the day it was uploaded.

    Both athlete-written streams take *spoken* records only, and the imported half of
    each container is not a stream of its own either. An upload is an event, not a supply:
    a file holding a year of sessions, or a year of weigh-ins, arrives on one day, and
    letting its rows set a stream's dates would report a supply that ran for a year and
    then stopped -- when nothing about what the athlete does changed at all, and one file
    simply arrived. ``training_history`` is where an upload's own rows are read, at the
    grain that question needs.

    ``basis`` says which kind of evidence a row rests on, because the two answer different
    questions. ``stored_record`` is a file this product keeps: its ``first_observed`` is
    the first day on record, full stop. ``read_window`` is a span this build asked a
    provider about: its ``first_observed`` is bounded by ``window_start`` beside it and
    says nothing about what came before.

    This is about the record and never about the read. Nothing here looks at
    ``freshness``, ``coverage`` or any ``unknowns`` string: a wellness read that failed
    this morning is this turn's news and is already reported as such, while a stream that
    stopped in June is a different fact that no single read can see.

    ``None`` -- the whole group, never an empty one -- when no stream has ever produced a
    dated observation, and it is silent: no ``unknowns`` line pairs with it. A context
    with nothing in any stream is a context whose every evidence group is already null and
    already says so, and a line here would only spend budget restating them (AGENTS.md 13).
    """
    rows = [
        row
        for row in (
            _evidence_expectation_stream(
                "provider_activities",
                _dated_observations(provider_actuals),
                as_of=as_of,
                window_start=actuals_window_start,
            ),
            # Spoken sessions only -- see the docstring's own paragraph on why an upload
            # is not a supply.
            _evidence_expectation_stream(
                "athlete_reported_activities",
                _dated_observations(reported_activities, {ATHLETE_REPORTED_SOURCE}),
                as_of=as_of,
            ),
            # Both ways the athlete states a lift: describing the sets, and confirming the
            # prescription they were given. They are different claims (which is why
            # ``training_history`` counts them separately) but one supply -- a coach asking
            # whether strength is still being reported is asking about both.
            _evidence_expectation_stream(
                "athlete_reported_strength",
                _dated_observations(
                    strength_reports, {ATHLETE_REPORTED_SOURCE, PRESCRIBED_CONFIRMED_SOURCE}
                ),
                as_of=as_of,
            ),
            # Stated weigh-ins only, by the same rule: an Apple Health export carries a
            # weight for every day it covers, and it is one upload.
            _evidence_expectation_stream(
                "athlete_body_measurements",
                _dated_observations(body_measurements, {ATHLETE_REPORTED_SOURCE}),
                as_of=as_of,
            ),
        )
        if row is not None
    ]
    if not rows:
        return None
    return {"streams": rows}


def _measured_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _max_hr_divergence_note(baseline_max_hr: Any, sport_settings_max_hr: Any) -> str | None:
    """Report, never resolve: two records of one athlete's max HR that disagree.

    ``athlete_baseline.max_hr`` is PlanState's own written figure; ``sport_settings_max_hr``
    is read independently from the provider's Run sport settings. Neither is preferred,
    averaged, or written back here -- picking one would be the product deciding a fact
    about the athlete's body it cannot verify, and the whole point is to surface the
    disagreement for the coach to weigh instead.

    Both values present and unequal is the only case that returns a note. A single value
    present is an ordinary known fact, not a disagreement, and reporting it as one would
    manufacture a warning about evidence that is simply one-sided -- the false-positive
    cost a blocking or warning check must justify before it exists.
    """
    if not _measured_number(baseline_max_hr) or not _measured_number(sport_settings_max_hr):
        return None
    if baseline_max_hr == sport_settings_max_hr:
        return None
    return (
        "athlete_baseline.max_hr diverges from the Intervals Run sport settings max HR: "
        f"athlete_baseline.max_hr={baseline_max_hr}, "
        f"intervals_run_sport_settings.max_hr={sport_settings_max_hr}"
    )


def _latest_extreme(
    candidates: list[dict[str, Any]], key: str, *, pick_max: bool
) -> dict[str, Any]:
    """The candidate holding the extreme value of ``key``; the newest date wins a tie."""
    values = [item[key] for item in candidates]
    target = max(values) if pick_max else min(values)
    tied = [item for item in candidates if item[key] == target]
    return max(tied, key=lambda item: str(item.get("date") or ""))


def _baseline_evidence_scalar_row(
    field: str,
    baseline: dict[str, Any],
    observed: dict[str, Any] | None,
    observations: int,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    claim = baseline.get(field)
    return {
        "field": field,
        "baseline": claim if _measured_number(claim) else None,
        "observed": observed,
        "observations": observations,
        "window_start": window_start,
        "window_end": window_end,
    }


def _build_baseline_evidence(
    baseline: dict[str, Any],
    recent_actuals: list[dict[str, Any]],
    movement_history: dict[str, Any] | None,
    strength_execution: dict[str, Any] | None,
    *,
    actuals_window_start: dt.date,
    window: BuildWindow,
) -> list[dict[str, Any]]:
    """Each ``athlete_baseline`` field's claim beside what the evidence shows (issue #32).

    The baseline is written by hand and nothing notices when it stops describing the
    athlete. This states, per field, what the baseline claims, what the recent evidence
    shows, and how many observations back it -- so "once" and "four weeks running" are
    distinguishable without re-deriving them from the raw rows. One row shape for
    running and strength; the readers differ only because the fields measure different
    things, and each reading mirrors its field's own definition -- the evidence for
    "longest recent run" is the longest recent run, never a second formula.

    Read entirely from evidence already in this context (``recent_actuals``,
    ``movement_history``): no new source, no new store. Average pace and average heart
    rate are named as averages -- a whole-run average spans warm-up and recoveries, and
    the label is what keeps it from being read as a threshold measurement.

    Never a verdict. No stale flag, no confidence, no suggested value, no
    once-vs-established boundary: which side is right -- and whether an anchor should
    move -- is the coaching judgment this group exists to inform (AGENTS.md 4). A field
    with nothing observed reports ``observed: null`` with zero observations, and a null
    window on a strength row means no strength source was read at all, which is not the
    same fact as reading one and finding nothing.
    """
    as_of_date = window.window_end
    run_window_start = actuals_window_start.isoformat()
    run_window_end = as_of_date.isoformat()
    runs = [item for item in recent_actuals if item.get("sport") == "running"]
    rows: list[dict[str, Any]] = []

    paced = [run for run in runs if _measured_number(run.get("average_pace_sec_per_km"))]
    observed: dict[str, Any] | None = None
    if paced:
        fastest = _latest_extreme(paced, "average_pace_sec_per_km", pick_max=False)
        distance = fastest.get("distance_km")
        observed = {
            "fastest_average_pace_sec_per_km": fastest["average_pace_sec_per_km"],
            "date": fastest.get("date"),
            "distance_km": distance if _measured_number(distance) else None,
        }
    rows.append(
        _baseline_evidence_scalar_row(
            "threshold_pace_sec_per_km", baseline, observed, len(paced),
            run_window_start, run_window_end,
        )
    )

    # Any sport: a heart rate observed on a strength session still bounds max_hr, and
    # the row says which sport carried it.
    with_hr = [item for item in recent_actuals if _measured_number(item.get("average_hr"))]
    observed = None
    if with_hr:
        highest = _latest_extreme(with_hr, "average_hr", pick_max=True)
        observed = {
            "highest_average_hr": highest["average_hr"],
            "date": highest.get("date"),
            "sport": highest.get("sport"),
        }
    rows.append(
        _baseline_evidence_scalar_row(
            "max_hr", baseline, observed, len(with_hr), run_window_start, run_window_end
        )
    )

    # Runs this context already classified easy -- the ceiling is a claim about exactly
    # those runs. The range is reported, not the excess: how the ceiling relates to it
    # is the judgment.
    easy = [
        run
        for run in runs
        if run.get("cost") == "easy" and _measured_number(run.get("average_hr"))
    ]
    observed = None
    if easy:
        values = [run["average_hr"] for run in easy]
        observed = {"average_hr_low": min(values), "average_hr_high": max(values)}
    rows.append(
        _baseline_evidence_scalar_row(
            "easy_hr_ceiling", baseline, observed, len(easy), run_window_start, run_window_end
        )
    )

    with_distance = [run for run in runs if _measured_number(run.get("distance_km"))]
    observed = None
    if with_distance:
        longest = _latest_extreme(with_distance, "distance_km", pick_max=True)
        observed = {"longest_run_km": longest["distance_km"], "date": longest.get("date")}
    rows.append(
        _baseline_evidence_scalar_row(
            "longest_recent_run_km", baseline, observed, len(with_distance),
            run_window_start, run_window_end,
        )
    )

    # Natural Monday-to-Sunday weeks, newest first, only weeks the actuals window holds
    # in full -- a week clipped at the window's edge would undercount and read as a down
    # week that never happened. The running week is included and says how far it has
    # run: ``through`` before the week's Sunday is the fact that the week is still
    # open, not a status. A week with no runs observed reads zero observed, which is a
    # statement about this feed's window, not a claim the athlete trained nothing --
    # coverage and freshness sit beside it.
    week_rows: list[dict[str, Any]] = []
    week_start = as_of_date - dt.timedelta(days=as_of_date.weekday())
    while week_start >= actuals_window_start:
        through = min(week_start + dt.timedelta(days=6), as_of_date)
        in_week = []
        for run in runs:
            day = _safe_date(run.get("date"))
            if day is not None and week_start <= day <= through:
                in_week.append(run)
        distances = [run.get("distance_km") for run in in_week]
        km: float | None
        if any(not _measured_number(value) for value in distances):
            # A run with no recorded distance makes the week's total unknown, never zero.
            km = None
        else:
            km = round(sum(distances), 2)
        week_rows.append(
            {
                "week_start": week_start.isoformat(),
                "through": through.isoformat(),
                "km": km,
                "runs": len(in_week),
            }
        )
        week_start -= dt.timedelta(days=7)
    rows.append(
        _baseline_evidence_scalar_row(
            "weekly_volume_km_4wk_avg", baseline, {"weeks": week_rows}, len(week_rows),
            run_window_start, run_window_end,
        )
    )

    with_duration = [
        item for item in recent_actuals if _measured_number(item.get("duration_minutes"))
    ]
    observed = None
    if with_duration:
        longest = _latest_extreme(with_duration, "duration_minutes", pick_max=True)
        observed = {
            "longest_session_minutes": longest["duration_minutes"],
            "date": longest.get("date"),
            "sport": longest.get("sport"),
        }
    rows.append(
        _baseline_evidence_scalar_row(
            "max_session_minutes", baseline, observed, len(with_duration),
            run_window_start, run_window_end,
        )
    )

    # Strength, through the same row shape. The evidence is movement_history's own
    # occurrences -- the same grouping and the same baseline anchoring, pivoted once
    # more: one line per load the movement was actually worked at, so "reached 60 on
    # 7/25" and "working at 65 since 8/11" sit beside the written figure as dated
    # counts rather than a recomputation.
    established = [
        load for load in (baseline.get("strength_loads") or []) if isinstance(load, dict)
    ]
    strength_source = movement_history if movement_history is not None else strength_execution
    strength_window_start = strength_window_end = None
    if isinstance(strength_source, dict):
        strength_window_start = strength_source.get("window_start")
        strength_window_end = strength_source.get("window_end")

    anchored: set[int] = set()
    for movement in (movement_history or {}).get("movements") or []:
        anchor = anchoring_baseline(movement.get("exercise"), established)
        if anchor is not None:
            anchored.add(id(anchor))
        occurrences = [
            item for item in movement.get("occurrences") or [] if isinstance(item, dict)
        ]
        buckets: dict[tuple[Any, Any], dict[str, Any]] = {}
        for occurrence in occurrences:
            sets = [
                item
                for item in occurrence.get("performed_sets") or []
                if isinstance(item, dict)
            ]
            weights = [item["weight_kg"] for item in sets if _measured_number(item.get("weight_kg"))]
            assists = [item["assist_kg"] for item in sets if _measured_number(item.get("assist_kg"))]
            if weights:
                # The day's top working weight. An assisted movement records the least
                # assistance instead -- less help is the heavier direction.
                key = (max(weights), None)
            elif assists:
                key = (None, min(assists))
            else:
                key = (None, None)
            date = str(occurrence.get("date") or "")
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = {
                    "load_kg": key[0],
                    "assist_kg": key[1],
                    "sessions": 1,
                    "first": date,
                    "last": date,
                }
            else:
                bucket["sessions"] += 1
                bucket["first"] = min(bucket["first"], date)
                bucket["last"] = max(bucket["last"], date)
        loads = sorted(
            buckets.values(),
            key=lambda item: (
                item["last"],
                item["first"],
                item["load_kg"] if item["load_kg"] is not None else float("-inf"),
                -item["assist_kg"] if item["assist_kg"] is not None else float("-inf"),
            ),
            reverse=True,
        )
        rows.append(
            {
                "field": "strength_loads",
                "exercise": movement.get("exercise"),
                "display_name": movement.get("display_name"),
                "baseline": copy.deepcopy(movement.get("baseline")),
                "observed": {"loads": loads},
                "observations": len(occurrences),
                "window_start": strength_window_start,
                "window_end": strength_window_end,
            }
        )

    # Baseline entries no recent occurrence anchored to: the claim is on record and the
    # window holds nothing for it, which is itself one of the facts this group states.
    leftover = [load for load in established if id(load) not in anchored]
    for load in sorted(leftover, key=lambda item: normalize_exercise_name(item.get("exercise"))):
        rows.append(
            {
                "field": "strength_loads",
                "exercise": load.get("exercise"),
                "display_name": load.get("display_name"),
                "baseline": {
                    "load_kg": load.get("load_kg"),
                    "assist_kg": load.get("assist_kg"),
                    "scheme": load.get("scheme"),
                },
                "observed": None,
                "observations": 0,
                "window_start": strength_window_start,
                "window_end": strength_window_end,
            }
        )

    return rows


# --------------------------------------------------------------------------------------
# Shared assembly: identical CoachContext shape regardless of source
# --------------------------------------------------------------------------------------


def _reconciliation_row(actual: dict[str, Any]) -> dict[str, Any]:
    """One recent_actuals row reduced to its reconciliation identity.

    Applied only to a row whose settled (matched or owned) attachment put its whole
    reading -- pace, heart rate, distance, elevation, feel, label -- on a
    ``cycle_sessions`` record's ``activity``, so repeating it here bought nothing and
    grew with every session (issue #240 §1). What stays is exactly what the
    deterministic readers consume: ownership re-derivation counts rows by date and
    sport and checks match_confidence, paired_event_id and the duration band;
    reconciliation groups by planned_session_id and reads completion; activity_id is
    how anything names this row. Everything else keeps every field: today's session
    has not rolled into the cycle record, an unmatched second run has no session to
    hang from, a pre-cycle activity's reading exists nowhere else, and a *probable*
    attachment is still being judged by exactly the figures a reduction would strip.

    ``session_label`` survives when the provider carries one: the served instructions
    tell the coach to read a strength actual's own label instead of asking what was
    trained, and that sentence points at this row.
    """
    row = {key: actual.get(key) for key in RECONCILIATION_ACTUAL_FIELDS}
    if actual.get("session_label") is not None:
        row["session_label"] = actual.get("session_label")
    return row


# The keys one sport structurally does not have (issue #240 §3): a null there states
# that nothing was missing, which is noise, unlike a null the device simply failed to
# record. A measured value always survives -- the cut is by applicability AND absence,
# never by nullness alone.
_SPORT_INAPPLICABLE_KEYS: dict[str, tuple[str, ...]] = {
    "strength": ("distance_km", "average_pace_sec_per_km", "elevation_gain_m"),
}


def _without_inapplicable_nulls(actual: dict[str, Any]) -> dict[str, Any]:
    """A full recent_actuals row with its sport's structurally-inapplicable null keys
    omitted. The row object itself is never mutated -- builders upstream read it."""
    dropped = [
        key
        for key in _SPORT_INAPPLICABLE_KEYS.get(actual.get("sport"), ())
        if key in actual and actual[key] is None
    ]
    # The provider names strength sessions only; a null label on any other sport is
    # the same inapplicable statement in the other direction.
    if (
        actual.get("sport") != "strength"
        and "session_label" in actual
        and actual["session_label"] is None
    ):
        dropped.append("session_label")
    # And the same statement for where a run was recorded: a lift has no indoors to be
    # recorded in, and a null there would be the product reporting that it looked and
    # found nothing about a question nobody asked.
    if (
        actual.get("sport") != "running"
        and "recorded_indoors" in actual
        and actual["recorded_indoors"] is None
    ):
        dropped.append("recorded_indoors")
    if not dropped:
        return actual
    return {key: value for key, value in actual.items() if key not in dropped}


def _actual_day_sports(recent_actuals: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
    """The ``(date, sport)`` pairs the provider's actuals cover.

    One implementation on purpose: cycle-session evidence ("another activity trained
    that day") and the reported-session overlap flag answer with the same pairs, and
    two copies of this comprehension would eventually disagree about a damaged row --
    leaving one context claiming a day is covered and, three keys later, that it is not.
    """
    return {
        (actual.get("date"), actual.get("sport"))
        for actual in recent_actuals
        if isinstance(actual, dict)
    }


def flag_provider_overlap(
    reported_activities: dict[str, Any] | None,
    recent_actuals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """State, per reported session, whether the provider also holds its day and sport.

    The common life of a reported session: the device failed, the athlete said the
    numbers, and later the device synced after all -- or the athlete reported a session
    the watch had already recorded. Either way one session now stands in this context
    twice, once as the athlete's word and once as a provider actual, and a coach reading
    the two lists as disjoint would count a week's training half again. This writes the
    one fact deterministic code can see -- same day, same sport, at least one provider
    activity -- onto the reported row, and stops there. Duration is deliberately not
    compared and nothing is merged or hidden: whether 40 reported minutes and a
    43-minute actual are one session is exactly the reading the coach owns, and a row
    this code suppressed would be a statement the athlete believes the coach still has
    (AGENTS.md 3, 4).

    The flag is written on every row, ``False`` included, so "checked, nothing there"
    and "never checked" cannot be confused. Rows too damaged to carry a date or sport
    keep ``False`` -- there is nothing to look up, and the row itself already reads as
    what it is.
    """
    if reported_activities is None:
        return None
    held = _actual_day_sports(recent_actuals)
    activities = [
        {
            **row,
            "provider_actual_same_day": (row.get("date"), row.get("sport")) in held,
        }
        for row in reported_activities.get("activities") or []
        if isinstance(row, dict)
    ]
    return {**reported_activities, "activities": activities}


def assemble_context(
    request: ContextRequest,
    plan: dict[str, Any],
    window: BuildWindow,
    domain: SourceDomain,
    *,
    strength_execution: dict[str, Any] | None = None,
    strength_execution_unknown: str | None = None,
    recovery_signals: dict[str, Any] | None = None,
    recovery_signals_unknown: str | None = None,
    cycle_sessions: list[dict[str, Any]] | None = None,
    athlete_availability: dict[str, Any] | None = None,
    athlete_profile: dict[str, Any] | None = None,
    body_measurements: dict[str, Any] | None = None,
    reported_activities: dict[str, Any] | None = None,
    subjective_states: dict[str, Any] | None = None,
    long_term_goals: dict[str, Any] | None = None,
    training_preferences: dict[str, Any] | None = None,
    training_history_activities: list[dict[str, Any]] | None = None,
    training_history_strength_reports: list[dict[str, Any]] | None = None,
    body_measurement_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge a source-specific ``SourceDomain`` with the request and plan into one
    CoachContext, then self-validate it. Every provider funnels through this exact
    function, so their outputs can never structurally drift apart.

    ``strength_execution`` (issue #37) and ``recovery_signals`` (issue #37 slice 2)
    are standalone optional evidence groups, never derived from ``domain``: neither is
    attached to any activity and neither carries base-source identity. Both are
    ``None`` (unconfigured or not requested) by default so intervals-only behavior
    stays byte-identical when a caller does not pass them -- only
    ``context_builder.build_context`` supplies real values. When one is ``None``, pass
    its matching ``..._unknown`` string to record why (e.g. "not configured") as one
    more entry in ``unknowns``; the context always carries both the
    ``strength_execution`` and ``recovery_signals`` keys, dict or ``None``, never
    omitted.

    ``cycle_sessions`` is this cycle's elapsed sessions rebuilt from the commit chain
    (``store.cycle_sessions``); the plan itself holds one week, so it is the only place
    an earlier week's prescription still exists.

    ``athlete_availability`` (issue #28) is what the athlete previously told the coach
    about the week ``as_of`` sits in, resolved by
    ``athlete_evidence.effective_availability``. It never overrides this request: an
    athlete who names their days in the request is speaking now, and a stored default is
    a standing statement made earlier. Left ``None`` by default, so a caller that does
    not read stored evidence produces exactly the constraints it always did.

    ``athlete_profile`` is the timezone and language the athlete has stated, or ``None``
    when they have stated neither. It is carried verbatim rather than merged with the
    defaults standing in for it: ``timezone`` above already says which day this build
    answered about, and the coach needs the separate fact of whether anybody ever said
    so -- an athlete who has not is the one to ask, and an athlete who has must not be
    asked again.

    ``body_measurements`` and ``reported_activities`` are the athlete's own conversational
    evidence: what they weighed, and the sessions no device recorded. Both are ``None``
    when nothing was stated, which is the ordinary starting state and never an unknown to
    flag. ``body_measurements`` touches nothing below it. ``reported_activities`` touches
    exactly one thing, read early rather than at final placement: a same-day, same-sport
    row lets ``_reported_training_days`` (issue #30) turn a cycle session's
    ``activity_evidence`` from ``none_found`` into ``athlete_reported``, the same door
    ``strength_execution`` already opens for a reported lift. That is all it does. Both
    groups are still placed in the context after matching, after ``recent_actuals``, and
    after the cycle record are built, so a reported session still carries no activity id,
    still cannot attach to a planned one, still completes none, and still moves no
    coverage or freshness row -- it is evidence beside the provider's, never inside it;
    only the review vocabulary for an absent activity gained one more honest reading.
    One observation flows the *other* way: each reported session states whether the
    provider also holds an activity of its sport on its day
    (``provider_actual_same_day``), because the report usually exists precisely because
    the device failed -- and when the device then syncs late, the same session stands on
    both sides of the context looking like two. The flag is the fact only; whether the
    two are one session is the coach's reading, and nothing is merged, suppressed or
    scored here (AGENTS.md 4).

    ``subjective_states`` is the last fortnight of what the athlete said about how they
    felt, dated and in their own words (issue #188). It exists because a subjective
    statement used to survive only as the plan change it caused, so a coach could never see
    that this was the third week of it -- and that run is the reading with the most value
    in it. Nothing here scores one, counts a run, or lines them up against
    ``recovery_signals``: a number standing in for "很累" is exactly what this product
    refuses to store, and the sentence is stored instead. Nothing deterministic reads the
    group at all. Symptoms are not here and must not be: pain, illness, chest pain,
    dizziness and unusual symptoms are ``red_flags`` on the request, where an explicit true
    limits the day rather than waiting to be read (AGENTS.md 9).

    ``long_term_goals`` and ``training_preferences`` are what the athlete is training for
    past this cycle, and how they say they like to train. Both outlive the 28-day cycle
    and neither belongs to it: ``goal_context`` above is this cycle's milestone, chosen by
    the coach as one step toward these, and a cycle that ended would take a long-term
    target with it if the target lived there.

    Neither constrains anything. No validator reads a preference, nothing here compares
    one against ``recent_actuals`` or ``cycle_sessions``, and no divergence is computed:
    a stated five strength sessions beside three weeks of three is a comparison the coach
    can make from evidence already in this context, and a number computed here would be
    that judgment made in the wrong layer (AGENTS.md 4, 5). What the coach owes a
    preference it plans against is the reason, and the athlete owns whether the habit
    itself changes -- nothing infers that it lapsed.

    ``training_history_activities`` and ``training_history_strength_reports`` are the
    athlete's complete, unwindowed evidence history -- every row
    ``athlete_evidence.all_reported_activity_summaries`` and
    ``all_reported_strength_sessions`` can produce, not the 42-day slice
    ``reported_activities``/``strength_execution`` above read. They feed exactly one
    group, ``training_history`` (issue #101's hosted half): a monthly rollup that answers
    "how has this changed over the past year", which six weeks of evidence structurally
    cannot. See ``_build_training_history`` for the shape and the reasoning behind it.
    Both default to ``None`` for the same byte-identical-by-default reason every other
    optional evidence parameter here does; only ``context_builder.build_context``
    supplies real values.

    ``body_measurement_history`` is the third unwindowed list, and it feeds one group
    only: ``evidence_expectations`` (issue #28), which reports per stream the first and
    last day evidence arrived, how many observations there were, and how long the silence
    since has run. ``body_measurements`` above is the same evidence clipped to the
    42-day window, and a stream that stopped seven weeks ago is empty inside it -- the
    same shape as a stream that never existed, which is the confusion the group exists to
    end. The two provider-fed and store-fed streams beside it are read from ``domain``
    and from the two ``training_history_*`` lists rather than from a fourth parameter.
    See ``_build_evidence_expectations`` for what a row does and does not say.
    """
    plan_sessions = plan.get("week", {}).get("sessions", [])

    # Three rows only, each measuring data completeness against a fixed seven-day
    # window. "Days trained" and "days planned" are real counts but not completeness
    # signals -- a rest day is not missing data and a full week of planned sessions is
    # not a data gap, so counting either one here would wear a data-quality label it
    # does not deserve. Both counts stay available where they actually belong: trained
    # days in recent_actuals, planned days in current_calendar.
    coverage = {
        "sleep": domain.coverage_sleep,
        "hrv": domain.coverage_hrv,
        "resting_hr": domain.coverage_resting_hr,
    }
    freshness = {
        "activities": domain.freshness_activities,
        "recovery": domain.freshness_recovery,
        # The state store already passed its own doctor check before this is called
        # (status_store raises otherwise), and the plan is the local source of truth for
        # the calendar domain regardless of where activities/recovery came from.
        "calendar": "fresh",
    }

    current_calendar = [
        {
            "session_id": session["session_id"],
            "date": session["scheduled_date"],
            "sport": session["sport"],
            "cost": session["cost"],
            "status": MATCH_STATUS_TO_CALENDAR_STATUS[session["match_status"]],
        }
        for session in plan_sessions
    ]

    goal_context = {
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "primary_goal": _one_line(f"{plan['cycle']['primary_adaptation']} — {plan['goal']['outcome']}"),
        "maintenance_goal": plan["cycle"].get("maintenance_adaptation"),
        # The yardstick this cycle declared for itself, carried verbatim. A week trained
        # exactly as prescribed still proves nothing about the outcome, so asking whether
        # the athlete improved needs the protocol beside the training record rather than
        # the training record alone -- and when the protocol was never run, the honest
        # answer is that progress is unproven, not a wearable number standing in for it.
        "measurement_protocol": plan["goal"]["measurement_protocol"],
        # The runnable half of that protocol, verbatim from the plan or null (issue #13).
        # Only what the cycle declared: goal_context is bound to project the PlanState
        # exactly, so whether either reading is in yet is an observation and lives in
        # `measurement_evidence` beside the other observations.
        "measurement": (plan["goal"].get("measurement") or None),
    }

    # The athlete's week runs Monday to Sunday. No other window in this context does:
    # coverage and recovery trends are rolling spans ending at as_of, and the plan's own
    # week.start is wherever the plan put it. A review framed on those answers "the last
    # seven days", which is not the week the athlete trained. Both weeks are stated
    # because both get reviewed -- one run on Monday is about the week that just ended,
    # one run mid-week is about the week still in progress.
    as_of_date = window.as_of.date()
    week_start = as_of_date - dt.timedelta(days=as_of_date.weekday())
    previous_week_start = week_start - dt.timedelta(days=7)
    cycle = plan.get("cycle") if isinstance(plan.get("cycle"), dict) else {}
    cycle_start_date = _safe_date(cycle.get("start"))
    # ``review_horizon_start`` is the earlier of the two starts below, and it is what
    # bounds the per-session evidence groups. It is derived from the same two numbers
    # rather than stated a third time, so a frame and a window cannot disagree.
    review_frame = {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + dt.timedelta(days=6)).isoformat(),
        "previous_week_start": previous_week_start.isoformat(),
        "previous_week_end": (previous_week_start + dt.timedelta(days=6)).isoformat(),
        "cycle_start": cycle.get("start"),
        "cycle_end": cycle.get("end"),
        # 1-based, and deliberately not capped: a cycle_day past its length is the fact
        # that the declared window has run out, which is exactly when the measurement
        # protocol comes due. Null before the cycle opens -- a day that has not arrived
        # is unknown, not day zero.
        "cycle_day": (
            (as_of_date - cycle_start_date).days + 1
            if cycle_start_date is not None and as_of_date >= cycle_start_date
            else None
        ),
        # The earliest day this context reports session by session, and the one span
        # `recent_actuals` and `reported_activities` are both cut to. Stated here rather
        # than left to be inferred from the three starts above, because a coach reading
        # "nothing since" needs the date nobody looked past, not three dates to take a
        # minimum of. Older evidence is in `training_history` by month and in
        # `baseline_evidence` by claim, each over its own stated window (issue #233).
        "detail_horizon_start": review_horizon_start(
            plan, as_of_date, domain.actuals_window_start
        ).isoformat(),
    }

    # Availability has two possible authors and the context says which one spoke. The
    # request wins whenever it names a day: it is this turn's statement, and stored
    # evidence is a standing one. ``unavailable_days`` only ever comes from stored
    # evidence -- a request that lists Monday and Thursday says nothing about Wednesday,
    # so inferring the complement of ``available_days`` would invent a constraint the
    # athlete never gave.
    stored_days_stated = (
        athlete_availability is not None and athlete_availability.get("basis") is not None
    )
    if request.available_days:
        available_days = list(request.available_days)
        unavailable_days: list[str] = []
        availability_source: str | None = "request"
    elif stored_days_stated:
        available_days = list(athlete_availability.get("available_days") or [])
        unavailable_days = list(athlete_availability.get("unavailable_days") or [])
        availability_source = "athlete_evidence"
    else:
        # Including the case where the athlete said something about this week without
        # naming a day -- "I'm travelling". That is a real constraint and it is carried
        # below, but it confirms no training day, so availability stays unstated.
        available_days = []
        unavailable_days = []
        availability_source = None

    constraints = {
        "available_days": available_days,
        "unavailable_days": unavailable_days,
        "availability_source": availability_source,
        # What the athlete said about *this* week that names no weekday: a trip, a hotel
        # gym, a work week that will run late. Verbatim and uninterpreted, and gone when
        # the week is -- which is what keeps it from ever reading as a standing habit.
        "week_constraints": list(
            (athlete_availability or {}).get("week_constraints") or []
        ),
        "session_minutes": request.session_minutes,
        "red_flags": request.red_flags,
        "leg_fatigue": request.leg_fatigue,
        "soreness": request.soreness,
        "schedule_changed": request.schedule_changed,
        "equipment_changed": request.equipment_changed,
    }

    unknowns: list[str] = []
    # Still keyed on the available list alone. Knowing Wednesday is out does not confirm
    # any other day is in, so evidence that names only unavailable days leaves
    # availability exactly as unconfirmed as it was.
    if not available_days:
        unknowns.append("available_days_not_confirmed")
    if request.session_minutes is None:
        unknowns.append("session_minutes_not_confirmed")
    if any(value is None for value in request.red_flags.values()):
        unknowns.append("red_flags_not_confirmed")
    if coverage["sleep"]["status"] == "missing":
        unknowns.append("sleep_data_unavailable")
    if coverage["hrv"]["status"] == "missing":
        unknowns.append("hrv_data_unavailable")
    if coverage["resting_hr"]["status"] == "missing":
        unknowns.append("resting_hr_unavailable")
    unknowns.extend(domain.extra_unknowns)
    unknowns.extend(request.extra_unknowns)
    if strength_execution_unknown is not None:
        unknowns.append(strength_execution_unknown)
    if recovery_signals_unknown is not None:
        unknowns.append(recovery_signals_unknown)
    movement_history = _build_movement_history(
        cycle_sessions, plan, strength_execution, plan.get("athlete_baseline") or {}
    )
    if movement_history is None and strength_execution is not None:
        # Said only when there was strength evidence to pivot and none of it resolved
        # to a movement -- when strength_execution itself is absent, its own unknown
        # already covers it and a second line would say the same thing twice.
        unknowns.append(
            "movement_history: recent strength evidence carries no identifiable "
            "movement; per-movement history unavailable"
        )
    training_history = _build_training_history(
        training_history_activities,
        training_history_strength_reports,
        plan.get("athlete_baseline") or {},
    )
    if training_history is None:
        # The athlete-evidence file is always readable (same stance ``reported_activities``
        # and ``body_measurements`` take), so a null result only ever means nothing
        # long-range has been reported yet -- never "never looked". Said explicitly rather
        # than left to a bare null, because a silent gap here is exactly what issue #101
        # opened on: a coach reading an evidence gap as a training restart instead of as
        # unknown.
        unknowns.append(
            "training_history: no long-range athlete-reported training history "
            "recorded; a multi-month or year-over-year trend is not observable -- do not "
            "infer one from the last six weeks alone"
        )
    # Deliberately no unknowns line of its own, whichever way it comes out. A null group
    # means no stream has ever produced anything, which every other group in this context
    # is already null and already saying; and a present group is evidence rather than a
    # gap. See ``_build_evidence_expectations``.
    evidence_expectations = _build_evidence_expectations(
        domain.recent_actuals,
        training_history_activities,
        training_history_strength_reports,
        body_measurement_history,
        actuals_window_start=domain.actuals_window_start,
        as_of=as_of_date,
    )
    if domain.segment_execution is None:
        # Said once, whichever way it came about -- a source that cannot produce
        # segments at all, or one that could and found none in the window. Both leave
        # the coach reading whole-session averages, and for a quality session that
        # average spans the warm-up and the recoveries too.
        unknowns.append(
            "segment_execution: no per-segment execution available; recent quality "
            "sessions readable only as whole-session averages"
        )

    # Deterministic planned <-> actual matching: the one thing that turns this from "a
    # new plan generated every day" into an actual loop that reads back what happened.
    #
    # The pool is the whole cycle, not just the week the plan still holds: an activity
    # from last Friday has a session to attach to only if that session is in it, and
    # without the attachment the prescription and the actual sit in different weeks with
    # nothing joining them. Sessions still in the week are authoritative -- they carry
    # any status written since -- so the chain only contributes the ones that rolled out.
    week_session_ids = {
        session.get("session_id")
        for session in plan_sessions
        if isinstance(session, dict)
    }
    match_pool = [
        *plan_sessions,
        *(
            session
            for session in (cycle_sessions or [])
            if session.get("session_id") not in week_session_ids
        ),
    ]
    recent_actuals = _match_actuals_to_plan(domain.recent_actuals, match_pool)

    # What this cycle prescribed, day by day, beside what came back for it. The plan holds
    # one week, so without the commit chain behind this the record resets every Monday:
    # neither "too many missed" nor "this load keeps not being finished" can be asked at
    # all. Both questions read these same rows -- an absent activity is the first, an
    # activity that fell short of the prescription is the second -- which is why there is
    # one record here and not one field per reading.
    #
    # It is a record and not a verdict. Nothing computes a completion ratio, and no status
    # is downgraded to "partial" by arithmetic: three sets instead of five, or a last set
    # 5 kg lighter, is a fact the coach weighs against sleep, load and the goal (AGENTS.md
    # 4, 5). A strength session's per-set truth is not copied in either -- it stays in
    # ``strength_execution``, joined by date.
    attached_actuals: dict[str, dict[str, Any]] = {}
    for actual in recent_actuals:
        attached_id = actual.get("planned_session_id")
        # One-to-one by construction (_match_actuals_to_plan claims each session once).
        if isinstance(attached_id, str) and attached_id:
            attached_actuals[attached_id] = actual
    trained_day_sports = _actual_day_sports(recent_actuals)
    # Days the athlete said they trained, which no provider recorded (issue #66, #30). The
    # athlete's word is taken as fact, not weighed as a clue: a watch that was off, flat
    # or failed to sync is the ordinary reason a session is missing, and treating the
    # athlete as unreliable to protect against the rare alternative gets the common case
    # wrong every time. What this does *not* do is invent an activity -- there is no
    # activity_id, nothing enters recent_actuals, and automatic reconciliation still sees
    # only what the provider holds. Strength and every other sport both feed this from
    # their own container -- strength_execution and reported_activities -- so a lift and a
    # run the watch missed are believed the same way.
    reported_day_sports = (
        _reported_training_days(strength_execution, reported_activities) - trained_day_sports
    )
    cycle_session_records: list[dict[str, Any]] = []
    # The exact recent_actuals row objects whose reading a cycle_sessions record now
    # carries -- collected by object identity, not by activity_id value, so a
    # duplicate row sharing an id (a provider retry, an import overlap) is never
    # reduced along with the one actually attached: its reading exists nowhere else.
    reduced_row_ids: set[int] = set()
    # The Monday of the week BEFORE the one as_of sits in. It bounds one thing only --
    # whether a row names the activity attached to it (issue #240 §3) -- and is named
    # for that thing: it governed the prescription too until the A/B eval took that
    # half back, and a window still called `prose` would send the next reader looking
    # for a rule that is not there.
    as_of_date = window.as_of.date()
    activity_id_horizon = as_of_date - dt.timedelta(days=as_of_date.weekday() + 7)
    for session in cycle_sessions or []:
        scheduled_date = session.get("scheduled_date")
        parsed_date = _safe_date(scheduled_date)
        past_week = parsed_date is not None and parsed_date < activity_id_horizon
        actual = attached_actuals.get(session.get("session_id"))
        activity: dict[str, Any] | None = None
        if actual is not None:
            # The activity's whole reading, not a teaser: the matching recent_actuals
            # row is reduced to its reconciliation identity (see the projection below),
            # so any measurement missing here would be missing from the context.
            activity = {
                "match_confidence": actual.get("match_confidence"),
                "duration_minutes": actual.get("duration_minutes"),
                "average_hr": actual.get("average_hr"),
                "subjective_feel": actual.get("subjective_feel"),
            }
            # A past week's activity keeps its numbers and drops its id (issue #240
            # §3): naming things at a review runs on session_id -- the join a reduced
            # recent_actuals row also carries via planned_session_id -- and the id of
            # an activity nothing will re-deliver buys nothing per turn. This week's
            # id stays: today's ambiguity questions are asked about concrete
            # activities.
            if not past_week:
                activity["activity_id"] = actual.get("activity_id")
            # By applicability, not by nullness (#240's own rule), through the same
            # table the recent_actuals projection uses -- the same activity must not
            # say "the concept does not apply" in one container and "looked, found
            # nothing" in the other. A measured value always survives, whatever the
            # sport says about it; a session label is the provider's name for a
            # strength session only.
            sport = session.get("sport")
            inapplicable = _SPORT_INAPPLICABLE_KEYS.get(sport, ())
            for key in ("distance_km", "average_pace_sec_per_km", "elevation_gain_m"):
                if key not in inapplicable or actual.get(key) is not None:
                    activity[key] = actual.get(key)
            if sport == "strength" or actual.get("session_label") is not None:
                activity["session_label"] = actual.get("session_label")
            # The one field here that qualifies another field here. Once the match is
            # settled this record *is* the reading -- the recent_actuals row beside it
            # is reduced to its reconciliation identity -- so a pace that arrived from
            # a treadmill and a pace that was measured would otherwise be the same
            # number by the time a review reads them, which is the whole failure
            # `recorded_indoors` exists to prevent. It travels with the pace, or it
            # does not travel at all.
            if sport == "running" or actual.get("recorded_indoors") is not None:
                activity["recorded_indoors"] = actual.get("recorded_indoors")
            # The reduced row keeps its reconciliation identity only when the match is
            # settled. A probable attachment is a same-day candidate a human still
            # judges -- by reading its pace and heart rate against the prescription --
            # so its row keeps the full reading it is judged by.
            if actual.get("match_confidence") in ("matched", "owned"):
                reduced_row_ids.add(id(actual))
            activity_evidence = "attached"
        else:
            # An absent activity has three quite different causes and only the last one is
            # evidence about the athlete. The day held that sport but it landed on another
            # session (or on none) -- that sport was trained, and a matching question must
            # not be reported as a missed session. The day is older than anything this
            # build read -- nothing was looked at, so nothing was found. Otherwise this
            # build did read that day and nothing of that sport came back.
            if (scheduled_date, session.get("sport")) in trained_day_sports:
                activity_evidence = "other_activity_same_day"
            elif (scheduled_date, session.get("sport")) in reported_day_sports:
                # The athlete says they trained this sport that day and no device
                # recorded it. That is training, and reporting it as a missed session
                # would feed the coach a false signal -- one it acts on by easing the
                # load of somebody who is in fact training. It is deliberately its own
                # value rather than folded into the line above: how the product knows
                # stays visible, because the prescription's own completion is still a
                # separate question this does not answer.
                activity_evidence = "athlete_reported"
            elif parsed_date is not None and parsed_date < domain.actuals_window_start:
                activity_evidence = "outside_evidence_window"
            else:
                activity_evidence = "none_found"
        record: dict[str, Any] = {
            "session_id": session.get("session_id"),
            "date": scheduled_date,
            # The Monday of the natural week this session sat in, so the cycle groups
            # into the weeks the athlete actually trained rather than into arbitrary
            # seven-day slices counted back from today.
            "week_start": (
                (parsed_date - dt.timedelta(days=parsed_date.weekday())).isoformat()
                if parsed_date is not None
                else None
            ),
            "sport": session.get("sport"),
            "cost": session.get("cost"),
            "match_status": session.get("match_status"),
            "planned_minutes": session.get("planned_minutes"),
            # What this session asked for, on every row, however old the week is, and
            # beside `planned_minutes` rather than behind the activity: the planned
            # half of planned-versus-actual reads first.
            #
            # Issue #240 §3 dropped it past a two-week window and the A/B eval in
            # `evals/ab` measured what that cost: asked what week one's long run
            # prescribed, the coach went from stating all three of its figures to
            # stating none, and asked what a fourth-week comparison meant, it read a
            # whole-session average -- twelve minutes of warm-up and eight of cool-down
            # included -- against the athlete's threshold pace and proposed re-anchoring
            # the next cycle faster than they can run. The arm whose row carried this
            # line said instead that the average spans the warm-up and cannot be
            # compared. Both readings had the same evidence elsewhere in the payload;
            # only one had it here, where the comparison is made.
            #
            # It is cheap: 595 characters across the nineteen rows of that cycle, 5% of
            # the response. `planned_minutes` and `cost` are what a row says without it,
            # and "50 minutes, hard" does not distinguish an interval session from a
            # tempo run.
            "prescription": session.get("prescription"),
            "activity": activity,
            "activity_evidence": activity_evidence,
        }
        cycle_session_records.append(record)

    measurement_evidence = _measurement_evidence(
        plan, plan_sessions, list(cycle_sessions or []), cycle_session_records
    )

    # athlete_baseline: PlanState is the sole authority. Defensive on purpose -- the
    # store's own doctor check should already guarantee this field is present and
    # well-formed, but a missing/malformed value here must still degrade to an honest
    # all-null baseline plus an unknowns note, never crash and never a guessed number.
    baseline_raw = plan.get("athlete_baseline")
    if isinstance(baseline_raw, dict):
        athlete_baseline = copy.deepcopy(baseline_raw)
    else:
        athlete_baseline = copy.deepcopy(ATHLETE_BASELINE_UNKNOWN)
        unknowns.append("athlete_baseline_unavailable")

    # Two independent records of one physiological fact, reported beside each other
    # rather than reconciled -- see _max_hr_divergence_note for why neither is preferred.
    max_hr_divergence = _max_hr_divergence_note(
        athlete_baseline.get("max_hr"), domain.sport_settings_max_hr
    )
    if max_hr_divergence is not None:
        unknowns.append(max_hr_divergence)

    baseline_evidence = _build_baseline_evidence(
        athlete_baseline,
        recent_actuals,
        movement_history,
        strength_execution,
        actuals_window_start=domain.actuals_window_start,
        window=window,
    )

    # Issue #238's second layer: a reported movement that anchored to no baseline
    # entry, beside baseline entries the window holds nothing for. Without a line the
    # mismatch is silent -- the movement reads as baseline-less and the baseline reads
    # as never trained, in the same response. Both lists are stated, nothing is
    # paired: which of them, if any, are one lift under two words is the athlete's
    # answer, never a string comparison's (AGENTS.md 5) -- a rotated-out lift beside
    # an unrelated accessory movement is the ordinary week, not a naming problem.
    # Read from baseline_evidence's own rows rather than re-derived, so this line and
    # those rows cannot learn to disagree about which baselines went unobserved.
    unanchored = [
        str(movement.get("exercise"))
        for movement in (movement_history or {}).get("movements") or []
        if movement.get("baseline") is None
    ]
    unobserved = [
        str(row.get("display_name") or row.get("exercise"))
        for row in baseline_evidence
        if row.get("field") == "strength_loads" and row.get("observations") == 0
    ]
    if unanchored and unobserved:
        unknowns.append(
            "movement_history: "
            + ", ".join(unanchored)
            + " matched no baseline entry by name; baselines with no observations "
            "in this window: "
            + ", ".join(unobserved)
            + " -- if any pair names one lift, only the athlete can join them"
        )

    unknowns = _dedupe_preserve_order(unknowns)

    sources = [
        *domain.sources,
        {
            "source": "coach-loop-state-store",
            "mode": "local_store",
            "doctor_status": "passed",
            "observed_at": window.now_iso,
            "data_through": None,
            "sanitized": True,
        },
    ]

    context: dict[str, Any] = {
        "schema_version": COACH_CONTEXT_SCHEMA_VERSION,
        "context_id": f"ctx-{window.as_of.strftime('%Y%m%d-%H%M%S')}",
        "as_of": window.as_of.isoformat(),
        "timezone": request.timezone_name,
        "athlete_profile": athlete_profile,
        "sources": sources,
        "freshness": freshness,
        "coverage": coverage,
        "goal_context": goal_context,
        "review_frame": review_frame,
        "constraints": constraints,
        "athlete_baseline": athlete_baseline,
        "baseline_evidence": baseline_evidence,
        # Reported from the horizon onward, not over the whole span the provider was
        # read on. Everything above this line -- matching, the cycle record, the
        # baseline claims, the provider-overlap flag -- ran against all six weeks, so
        # narrowing what is reported changes no attachment and no reconciliation:
        # every actual a session claimed sits on that session's own day, and no
        # session in the plan or the cycle predates the horizon. What it drops is the
        # unmatched middle of the window -- activities from before the cycle that no
        # review reads session by session, and that `baseline_evidence` and
        # `training_history` already report at the grain their questions need
        # (issue #233). A row whose settled attachment put its reading on a
        # cycle_sessions record is then reduced to its reconciliation identity, and a
        # full row drops the null keys its sport structurally does not have -- this
        # projection is the only place either happens: every builder above read the
        # full rows.
        "recent_actuals": [
            (
                _reconciliation_row(actual)
                if id(actual) in reduced_row_ids
                else _without_inapplicable_nulls(actual)
            )
            for actual in recent_actuals
            if not isinstance(actual.get("date"), str)
            or actual["date"] >= review_frame["detail_horizon_start"]
        ],
        "recovery_trends": domain.recovery_trends,
        "current_calendar": current_calendar,
        "cycle_sessions": cycle_session_records,
        "measurement_evidence": measurement_evidence,
        "strength_execution": strength_execution,
        "recovery_signals": recovery_signals,
        "segment_execution": domain.segment_execution,
        "movement_history": movement_history,
        "body_measurements": body_measurements,
        "reported_activities": flag_provider_overlap(reported_activities, recent_actuals),
        "subjective_states": subjective_states,
        "long_term_goals": long_term_goals,
        "training_preferences": training_preferences,
        "training_history": training_history,
        "evidence_expectations": evidence_expectations,
        "unknowns": unknowns,
        "privacy": {
            "sanitized": True,
            "contains_raw_payloads": False,
            "contains_credentials": False,
            "contains_gps_tracks": False,
            "contains_connection_state": False,
        },
    }

    validation = validate_coach_context(context)
    if validation["status"] != "passed":
        # Bug guard: the builder above is expected to always produce a valid context, but
        # never print an artifact that fails its own contract.
        return {"status": "blocked", "validation": validation}
    return {"status": "passed", "validation": validation, "context": context}
