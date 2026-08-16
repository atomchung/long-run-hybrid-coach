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
    anchoring_baseline,
    normalize_exercise_name,
    owned_duration_within_band,
    plan_movements,
    product_delivered,
    validate_coach_context,
)


# The athlete-local day every date-boundary calculation answers "today"/"next" with when
# no explicit timezone is given. Every entry point that needs one -- CLI `status`, the CLI
# context-building commands, and the hosted `startCoachSession` -- accepts an explicit
# IANA timezone instead; this is only the backward-compatible default for existing
# Asia/Taipei owner state (issue #112), never inferred from the server/host location.
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
    """A deterministic CoachContext build step was blocked."""

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.details = details


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


def _reported_training_dates(strength_execution: dict[str, Any] | None) -> set[dt.date]:
    """The dates the athlete says they trained, from statements rather than devices.

    Only ever from sessions marked ``athlete_reported``. A local strength log's rows are
    measurements sitting beside the provider read that already counted their day, and
    counting them again here would double it.

    Strength only, because a strength report is the only statement of this kind the
    product accepts. A run the watch missed is a different problem with a different
    answer -- the athlete still knows its distance and duration, so it belongs in the
    provider as a manual activity carrying real figures, not here as a bare assertion
    that it happened.
    """
    dates: set[dt.date] = set()
    for session in (strength_execution or {}).get("sessions") or []:
        if not isinstance(session, dict) or session.get("source") != ATHLETE_REPORTED_SOURCE:
            continue
        day = _safe_date(session.get("date"))
        if day is not None:
            dates.add(day)
    return dates


def _reported_training_days(
    strength_execution: dict[str, Any] | None,
) -> set[tuple[str, str]]:
    """The same statements as ``(date, sport)``, for matching against planned sessions."""
    return {
        (day.isoformat(), "strength")
        for day in _reported_training_dates(strength_execution)
    }


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
        window42_start=as_of.date() - dt.timedelta(days=41),
        window42_end=as_of.date(),
    )


def _prescribed_movements_by_date(
    cycle_sessions: list[dict[str, Any]] | None, plan: dict[str, Any]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Index every prescribed movement by (date, normalized exercise).

    Both the elapsed cycle and the week the plan still holds: today's session lives in
    the plan and has not reached the cycle record yet, and today is exactly the session
    a coach is most likely to be reading against.

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
            key = normalize_exercise_name(movement.get("exercise"))
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

    prescribed = _prescribed_movements_by_date(cycle_sessions, plan)
    established = [
        load for load in (baseline.get("strength_loads") or []) if isinstance(load, dict)
    ]

    grouped: dict[str, dict[str, Any]] = {}
    for entry in performed:
        date = entry.get("date")
        exercise = entry.get("exercise")
        key = normalize_exercise_name(exercise)
        if not key or not isinstance(date, str):
            continue
        anchor = anchoring_baseline(exercise, established)
        group = grouped.setdefault(
            key,
            {
                "exercise": exercise,
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
        group["occurrences"].append(
            {
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
        )

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


def _measured_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    }

    # Availability has two possible authors and the context says which one spoke. The
    # request wins whenever it names a day: it is this turn's statement, and stored
    # evidence is a standing one. ``unavailable_days`` only ever comes from stored
    # evidence -- a request that lists Monday and Thursday says nothing about Wednesday,
    # so inferring the complement of ``available_days`` would invent a constraint the
    # athlete never gave.
    if request.available_days:
        available_days = list(request.available_days)
        unavailable_days: list[str] = []
        availability_source: str | None = "request"
    elif athlete_availability is not None:
        available_days = list(athlete_availability.get("available_days") or [])
        unavailable_days = list(athlete_availability.get("unavailable_days") or [])
        availability_source = "athlete_evidence"
    else:
        available_days = []
        unavailable_days = []
        availability_source = None

    constraints = {
        "available_days": available_days,
        "unavailable_days": unavailable_days,
        "availability_source": availability_source,
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
    trained_day_sports = {
        (actual.get("date"), actual.get("sport"))
        for actual in recent_actuals
        if isinstance(actual, dict)
    }
    # Days the athlete said they trained, which no provider recorded (issue #66). The
    # athlete's word is taken as fact, not weighed as a clue: a watch that was off, flat
    # or failed to sync is the ordinary reason a session is missing, and treating the
    # athlete as unreliable to protect against the rare alternative gets the common case
    # wrong every time. What this does *not* do is invent an activity -- there is no
    # activity_id, nothing enters recent_actuals, and automatic reconciliation still sees
    # only what the provider holds.
    reported_day_sports = _reported_training_days(strength_execution) - trained_day_sports
    cycle_session_records: list[dict[str, Any]] = []
    for session in cycle_sessions or []:
        scheduled_date = session.get("scheduled_date")
        parsed_date = _safe_date(scheduled_date)
        actual = attached_actuals.get(session.get("session_id"))
        activity: dict[str, Any] | None = None
        if actual is not None:
            activity = {
                "activity_id": actual.get("activity_id"),
                "match_confidence": actual.get("match_confidence"),
                "duration_minutes": actual.get("duration_minutes"),
                "distance_km": actual.get("distance_km"),
                "average_pace_sec_per_km": actual.get("average_pace_sec_per_km"),
                "average_hr": actual.get("average_hr"),
            }
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
        cycle_session_records.append(
            {
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
                "prescription": session.get("prescription"),
                "activity": activity,
                "activity_evidence": activity_evidence,
            }
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

    baseline_evidence = _build_baseline_evidence(
        athlete_baseline,
        recent_actuals,
        movement_history,
        strength_execution,
        actuals_window_start=domain.actuals_window_start,
        window=window,
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
        "sources": sources,
        "freshness": freshness,
        "coverage": coverage,
        "goal_context": goal_context,
        "review_frame": review_frame,
        "constraints": constraints,
        "athlete_baseline": athlete_baseline,
        "baseline_evidence": baseline_evidence,
        "recent_actuals": recent_actuals,
        "recovery_trends": domain.recovery_trends,
        "current_calendar": current_calendar,
        "cycle_sessions": cycle_session_records,
        "strength_execution": strength_execution,
        "recovery_signals": recovery_signals,
        "segment_execution": domain.segment_execution,
        "movement_history": movement_history,
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
