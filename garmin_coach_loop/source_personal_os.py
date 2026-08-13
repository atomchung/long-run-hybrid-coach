"""Owner-only local fallback: read the personal-os health.db SQLite snapshot.

THIS IS NOT THE PRODUCT PATH. It is the owner's transitional patch for the window before
their Garmin -> intervals.icu wellness sync is fully flowing; it depends on a local SQLite
file that exists on exactly one machine and will not exist for any other user. The
product path is ``source_intervals`` (the default). This module is never imported at
top level by ``context_builder`` -- it is imported lazily, only inside the
``source == "personal-os"`` branch of ``build_context``, specifically so a machine with
no personal-os installation can import ``context_builder`` and run ``--source
intervals`` end to end without this module ever loading.

Selecting this source is always an explicit, one-off opt-in (``--source personal-os``,
never automatic and never a fallback the code chooses on your behalf), and every
CoachContext it produces carries a standing ``unknowns`` note saying so -- see
``PERSONAL_OS_SOURCE_NOTE`` below.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import statistics
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .context_core import (
    BuildWindow,
    ContextBuildError,
    SourceDomain,
    _classify_running,
    _coverage_entry,
    _dedupe_preserve_order,
    _median_trend,
    _safe_date,
    _safe_float,
)


# Tables this module actually reads. sleep_sessions is part of the documented health.db
# schema but is unused here (sleep signal comes from recovery_daily instead), so its
# absence does not block context building.
REQUIRED_HEALTH_DB_TABLES = ("workouts", "recovery_daily", "daily_metrics")

# The resting-hr signal was verified once against the real health.db schema: daily_metrics
# is an EAV table (date, source, metric, value, ...) and resting heart rate lives at
# metric='resting_hr' (unit bpm). This mapping is hardcoded rather than rediscovered.
RESTING_HR_METRIC = "resting_hr"

SOURCE_NAME = "personal-os-health-db"

# health.db has no subjective-feel column, and its elevation_gain_m is present but
# untrusted (it disagreed with intervals.icu by an order of magnitude on the same
# 2026-08-10 activity: 8 m versus 145 m), so neither is read here. This source is also a
# private patch rather than the product path -- every CoachContext built from it says so
# explicitly in `unknowns`, rather than looking identical to an intervals.icu build.
PERSONAL_OS_SOURCE_NOTE = "personal_os_source_not_product_path"

# strength_execution (issue #37) is a standalone optional evidence group with its own
# entry point (fetch_strength_execution below) -- never merged into fetch_domain's
# SourceDomain, since strength_log is athlete-authored ground truth, not an
# activity/recovery reading.
STRENGTH_LOG_TABLE = "strength_log"
STRENGTH_EXECUTION_SOURCE_NAME = "personal-os:strength_log"

# recovery_signals (issue #37 slice 2) is a second standalone optional evidence group,
# alongside strength_execution above: readiness/HRV-status/acute-load/recovery-time from
# recovery_daily, plus Body Battery and stress from daily_metrics. Both tables are
# already read by fetch_domain (REQUIRED_HEALTH_DB_TABLES above) for an unrelated
# purpose -- sleep/HRV trend inputs, no source filter -- so this entry point reads them
# again independently, filtered to Garmin's own rows and to columns fetch_domain never
# touches. See fetch_recovery_signals for the merge semantics.
RECOVERY_SIGNALS_REQUIRED_TABLES = ("recovery_daily", "daily_metrics")
RECOVERY_SIGNALS_SOURCE_NAME = "personal-os:recovery_daily+daily_metrics"
# Both tables could in principle carry rows from more than one device/account; only
# Garmin populates them today, but the filter is explicit rather than assumed so a
# future non-Garmin row never silently mixes into what is meant to be a Garmin-only
# recovery read.
RECOVERY_SIGNALS_ROW_SOURCE = "garmin"
RECOVERY_SIGNALS_DAILY_METRICS = ("body_battery_high", "body_battery_low", "avg_stress")


# --------------------------------------------------------------------------------------
# Health database location -- CLI/env only, no default (see module docstring)
# --------------------------------------------------------------------------------------


HEALTH_DB_ENV_VARS = ("GARMIN_COACH_LOOP_HEALTH_DB", "HEALTH_DB_PATH")


def resolve_health_db_path(cli_value: Path | None) -> Path | None:
    """Resolve the health database path: ``--db``, then the vars in ``HEALTH_DB_ENV_VARS``.

    ``HEALTH_DB_PATH`` is the name the database's own owner (personal_os) already reads,
    so it is the standard way any consumer locates that file; the repo-specific name wins
    when both are set, for a machine that points this tool somewhere else on purpose.

    Returns ``None`` -- never a guessed path -- when none is given. There is no default
    location: the only one that has ever existed is a single local machine's path, and
    hardcoding it would silently break for every other user while looking like it works
    for the one machine that happens to have it. ``None`` means "this source is
    unavailable"; the caller must turn that into an explicit block, never a silent skip.
    """
    if cli_value is not None:
        return Path(cli_value)
    for name in HEALTH_DB_ENV_VARS:
        env_value = os.environ.get(name)
        if env_value:
            return Path(env_value)
    return None


# --------------------------------------------------------------------------------------
# Small helpers private to this source
# --------------------------------------------------------------------------------------


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()))}?mode=ro"


def _open_health_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ContextBuildError(f"health database not found: {path}")
    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True)
        connection.execute("SELECT 1")
    except sqlite3.Error as exc:
        raise ContextBuildError(f"cannot open health database: {exc}") from exc
    return connection


def _missing_tables(connection: sqlite3.Connection, required: tuple[str, ...]) -> list[str]:
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return sorted(name for name in required if name not in existing)


def _parse_local_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_utc_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _safe_json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _age(now: dt.datetime, moment: dt.datetime | None) -> dt.timedelta | None:
    if moment is None:
        return None
    return now - moment


def _freshness_from_age(age: dt.timedelta | None) -> str:
    """<=36h fresh, <=7d stale; no observation, a read error, or older than 7d is failed."""
    if age is None:
        return "failed"
    if age <= dt.timedelta(hours=36):
        return "fresh"
    if age <= dt.timedelta(days=7):
        return "stale"
    return "failed"


def _max_ingested_at(rows: list[dict[str, Any]]) -> dt.datetime | None:
    moments = [row["ingested_at"] for row in rows if row["ingested_at"] is not None]
    return max(moments) if moments else None


def _hrv_trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest hrv_last_night_ms vs its own Garmin baseline (or +/-10% of the 7d average)."""
    observed = [row for row in rows if row["hrv_last_night_ms"] is not None]
    observed_days = len(observed)
    if observed_days < 3:
        return {"status": "unknown", "observed_days": observed_days, "expected_days": 7}
    latest = max(observed, key=lambda row: row["date"])
    value = latest["hrv_last_night_ms"]
    low = high = None
    baseline = latest["hrv_baseline"]
    if isinstance(baseline, dict):
        low = _safe_float(baseline.get("balancedLow"))
        high = _safe_float(baseline.get("balancedUpper"))
    if low is None or high is None:
        avg = latest["hrv_7d_avg_ms"]
        if avg is None:
            return {"status": "unknown", "observed_days": observed_days, "expected_days": 7}
        low, high = avg * 0.9, avg * 1.1
    if value < low:
        status = "below_baseline"
    elif value > high:
        status = "above_baseline"
    else:
        status = "within_baseline"
    return {"status": status, "observed_days": observed_days, "expected_days": 7}


def _actual_sport(activity_type: Any) -> str | None:
    lowered = str(activity_type or "").lower()
    if "running" in lowered:
        return "running"
    if "strength" in lowered:
        return "strength"
    return None


# --------------------------------------------------------------------------------------
# health.db readers
# --------------------------------------------------------------------------------------


def _fetch_workouts(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, start_time, activity_type, duration_sec, avg_speed_mps, ingested_at FROM workouts"
    ).fetchall()
    parsed: list[dict[str, Any]] = []
    for activity_id, start_time, activity_type, duration_sec, avg_speed_mps, ingested_at in rows:
        moment = _parse_local_datetime(start_time)
        parsed.append(
            {
                "activity_id": activity_id,
                "date": moment.date() if moment is not None else None,
                "activity_type": activity_type,
                "duration_sec": _safe_float(duration_sec),
                "avg_speed_mps": _safe_float(avg_speed_mps),
                "ingested_at": _parse_utc_iso(ingested_at),
            }
        )
    return parsed


def _fetch_recovery(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT date, hrv_last_night_ms, hrv_7d_avg_ms, hrv_baseline_json, "
        "readiness_factors_json, ingested_at FROM recovery_daily"
    ).fetchall()
    parsed: list[dict[str, Any]] = []
    for date_text, hrv_last, hrv_avg, baseline_json, factors_json, ingested_at in rows:
        day = _safe_date(date_text)
        if day is None:
            continue
        factors = _safe_json(factors_json)
        sleep_percent = None
        if isinstance(factors, dict):
            sleep_score = factors.get("sleep_score")
            if isinstance(sleep_score, dict):
                sleep_percent = _safe_float(sleep_score.get("percent"))
        baseline = _safe_json(baseline_json)
        parsed.append(
            {
                "date": day,
                "hrv_last_night_ms": _safe_float(hrv_last),
                "hrv_7d_avg_ms": _safe_float(hrv_avg),
                "hrv_baseline": baseline if isinstance(baseline, dict) else None,
                "sleep_percent": sleep_percent,
                "ingested_at": _parse_utc_iso(ingested_at),
            }
        )
    return parsed


def _fetch_resting_hr(connection: sqlite3.Connection) -> dict[dt.date, float]:
    rows = connection.execute(
        "SELECT date, value FROM daily_metrics WHERE metric = ? AND value IS NOT NULL",
        (RESTING_HR_METRIC,),
    ).fetchall()
    values: dict[dt.date, list[float]] = {}
    for date_text, value in rows:
        day = _safe_date(date_text)
        parsed_value = _safe_float(value)
        if day is not None and parsed_value is not None:
            values.setdefault(day, []).append(parsed_value)
    return {day: statistics.fmean(day_values) for day, day_values in values.items()}


def _fetch_strength_log_rows(connection: sqlite3.Connection, window: BuildWindow) -> list[dict[str, Any]]:
    """Read strength_log rows inside the 42-day cycle-planning window, parsed but not
    yet grouped into sessions. A row whose date or set_number cannot be parsed is
    skipped -- the same defensive stance _fetch_recovery already takes on an
    unparseable date -- rather than raising for a single malformed row.

    ORDER BY set_number matters beyond the final sessions[].sets ordering: per-row
    notes are appended to a session's notes list in the order rows are iterated in
    fetch_strength_execution, so an unordered fetch would make note order (and thus
    de-duplication) depend on SQLite's unspecified default row order.
    """
    rows = connection.execute(
        "SELECT date, category, exercise, set_number, weight_kg, assist_kg, reps, rpe, notes "
        "FROM strength_log WHERE date >= ? AND date <= ? "
        "ORDER BY date, exercise, set_number",
        (window.window42_start.isoformat(), window.window42_end.isoformat()),
    ).fetchall()
    parsed: list[dict[str, Any]] = []
    for date_text, category, exercise, set_number, weight_kg, assist_kg, reps, rpe, notes in rows:
        day = _safe_date(date_text)
        set_index = _safe_int(set_number)
        if day is None or set_index is None:
            continue
        parsed.append(
            {
                "date": day,
                "category": category,
                "exercise": exercise,
                "set": set_index,
                "weight_kg": _safe_float(weight_kg),
                "assist_kg": _safe_float(assist_kg),
                "reps": _safe_int(reps),
                "rpe": _safe_float(rpe),
                "note": notes if isinstance(notes, str) and notes.strip() else None,
            }
        )
    return parsed


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


def fetch_domain(
    db_path: Path,
    window: BuildWindow,
    *,
    threshold_sec_per_km: int | float | None = None,
) -> SourceDomain:
    """Build one CoachContext activity/recovery domain from the local personal-os
    health.db.

    Raises ``ContextBuildError`` when the database cannot be opened or is missing a
    required table -- never fabricates a domain from a broken source, and never falls
    back to anything else; that decision belongs to the caller. Freshness here is
    age-based (``_freshness_from_age`` against each row's ``ingested_at``) because,
    unlike a live API read, a local sync snapshot can silently go stale between syncs.

    Every recent_actuals entry from this source carries ``elevation_gain_m=None`` and
    ``subjective_feel=None`` -- never a guessed value. health.db has no subjective-feel
    column at all, and its ``workouts.elevation_gain_m`` is deliberately not read: the
    values contradict intervals.icu on the same activity (see module docstring), so
    ``None`` is the honest answer until that is resolved.
    """
    connection = _open_health_db(db_path)
    try:
        missing = _missing_tables(connection, REQUIRED_HEALTH_DB_TABLES)
        if missing:
            raise ContextBuildError(
                "health database missing required tables",
                details={"missing_tables": missing},
            )
        workouts = _fetch_workouts(connection)
        recovery = _fetch_recovery(connection)
        resting_hr_by_date = _fetch_resting_hr(connection)
    finally:
        connection.close()

    workout_dates = [row["date"] for row in workouts if row["date"] is not None]
    recovery_dates = [row["date"] for row in recovery]
    all_dates = workout_dates + recovery_dates
    data_through = max(all_dates).isoformat() if all_dates else None

    source_entry = {
        "source": SOURCE_NAME,
        "mode": "local_readonly_sqlite",
        "doctor_status": "passed",
        "observed_at": window.now_iso,
        "data_through": data_through,
        "sanitized": True,
    }

    freshness_activities = _freshness_from_age(_age(window.resolved_now, _max_ingested_at(workouts)))
    # A recovery_daily row with every signal null is a sync artifact, not an
    # observation -- counting its ingested_at would let a value-empty feed read as
    # "fresh", the exact dishonesty the intervals source's signal-value grading
    # rejects. Only rows carrying at least one real signal value participate.
    observed_recovery = [
        row
        for row in recovery
        if row.get("hrv_last_night_ms") is not None or row.get("sleep_percent") is not None
    ]
    freshness_recovery = _freshness_from_age(_age(window.resolved_now, _max_ingested_at(observed_recovery)))

    activity_days = {
        row["date"] for row in workouts
        if row["date"] is not None and window.window_start <= row["date"] <= window.window_end
    }
    sleep_days = {
        row["date"] for row in recovery
        if row["sleep_percent"] is not None and window.window_start <= row["date"] <= window.window_end
    }
    hrv_days = {
        row["date"] for row in recovery
        if row["hrv_last_night_ms"] is not None and window.window_start <= row["date"] <= window.window_end
    }
    resting_days = {day for day in resting_hr_by_date if window.window_start <= day <= window.window_end}

    recovery_window = [row for row in recovery if window.window_start <= row["date"] <= window.window_end]
    sleep_values = {
        row["date"]: row["sleep_percent"] for row in recovery_window if row["sleep_percent"] is not None
    }
    resting_window = {
        day: value for day, value in resting_hr_by_date.items()
        if window.window_start <= day <= window.window_end
    }
    recovery_trends = {
        "sleep": _median_trend(sleep_values, window.window_end, band_points=10.0),
        "hrv": _hrv_trend(recovery_window),
        # Same median logic as sleep, just over resting bpm; a higher recent median is
        # already "above_baseline" under this shared formula, matching the higher-is-worse
        # convention for resting heart rate without any extra inversion.
        "resting_hr": _median_trend(resting_window, window.window_end, band_points=10.0),
    }

    # This source always carries the "not the product path" note, plus one note per
    # activity whose pace could not be classified -- see _classify_running.
    pace_notes: list[str] = [PERSONAL_OS_SOURCE_NOTE]
    candidates: list[dict[str, Any]] = []
    for row in workouts:
        if row["date"] is None or not (window.window14_start <= row["date"] <= window.window14_end):
            continue
        sport = _actual_sport(row["activity_type"])
        if sport is None:
            continue
        if sport == "strength":
            adaptation, body_stress, cost = "strength", "full", "moderate"
        else:
            adaptation, cost = _classify_running(
                row["avg_speed_mps"], row["activity_id"], pace_notes, threshold_sec_per_km
            )
            body_stress = "lower"
        duration_minutes = None
        if row["duration_sec"] is not None:
            duration_minutes = max(1, round(row["duration_sec"] / 60))
        candidates.append(
            {
                "activity_id": row["activity_id"],
                "date": row["date"].isoformat(),
                "sport": sport,
                "planned_session_id": None,
                "match_confidence": "unmatched",
                "adaptation": adaptation,
                "body_stress": body_stress,
                "cost": cost,
                "duration_minutes": duration_minutes,
                "completion": "completed",
                "elevation_gain_m": None,
                "subjective_feel": None,
            }
        )
    # Keep only the 20 most recent, then present them oldest-to-newest.
    candidates.sort(key=lambda item: (item["date"], item["activity_id"]), reverse=True)
    recent_actuals = candidates[:20]
    recent_actuals.sort(key=lambda item: (item["date"], item["activity_id"]))

    # What this source actually looked at: the 14-day window, unless the 20-cap bit.
    # Then the honest edge is the day *after* the newest activity the cap dropped --
    # not the oldest one it kept. The cap is ranked by (date, activity_id), so it can
    # cut a day in half: two sessions that day, one kept and one dropped. Claiming that
    # day was fully read would report the dropped one's session as "nothing came back",
    # which is a missed session as far as the coach is concerned. An athlete training
    # twice most days passes 20 activities inside 14 days, so this is the ordinary case
    # for this source, not a corner of it.
    actuals_window_start = window.window14_start
    if len(candidates) > 20:
        newest_dropped = _safe_date(candidates[20]["date"])
        if newest_dropped is not None:
            actuals_window_start = max(actuals_window_start, newest_dropped + dt.timedelta(days=1))

    return SourceDomain(
        sources=[source_entry],
        freshness_activities=freshness_activities,
        freshness_recovery=freshness_recovery,
        actuals_window_start=actuals_window_start,
        coverage_activities=_coverage_entry(len(activity_days)),
        coverage_sleep=_coverage_entry(len(sleep_days)),
        coverage_hrv=_coverage_entry(len(hrv_days)),
        coverage_resting_hr=_coverage_entry(len(resting_days)),
        recovery_trends=recovery_trends,
        recent_actuals=recent_actuals,
        extra_unknowns=list(pace_notes),
    )


def fetch_strength_execution(db_path: Path, window: BuildWindow) -> dict[str, Any]:
    """Build the standalone strength_execution evidence group from health.db's
    strength_log table (issue #37).

    This is never attached to ``recent_actuals`` and never matched to any activity --
    strength_log is athlete-authored ground truth (per-set weight/reps written by the
    ``/log-strength`` skill), not an activity enrichment, and merging it onto a matched
    activity would reopen exactly the identity-merge problem this standalone group
    exists to sidestep.

    Raises ``ContextBuildError`` when the database cannot be opened or has no
    strength_log table -- a *configured* source that cannot be read must fail loud,
    the same stance ``fetch_domain`` takes on its own required tables. The
    *unconfigured* case (no ``--health-db`` and no env var) never reaches this
    function; it is handled by the caller (``context_builder.build_context``).

    Carries no judgment: every set is a raw value verbatim, never a "completed" flag,
    a max/best aggregation, or a comparison against ``athlete_baseline.strength_loads``
    -- the coach judges, the product does not score (issue #3 direction). Zero rows in
    the window is a valid, distinct-from-unknown read: ``"sessions": []`` means "looked,
    nothing there".
    """
    connection = _open_health_db(db_path)
    try:
        missing = _missing_tables(connection, (STRENGTH_LOG_TABLE,))
        if missing:
            raise ContextBuildError(
                "health database missing required table for strength execution",
                details={"missing_tables": missing},
            )
        rows = _fetch_strength_log_rows(connection, window)
    finally:
        connection.close()

    sessions_by_key: dict[tuple[dt.date, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["date"], row["exercise"])
        session = sessions_by_key.get(key)
        if session is None:
            session = {
                "date": row["date"],
                "exercise": row["exercise"],
                "category": row["category"],
                "sets": [],
                "notes": [],
            }
            sessions_by_key[key] = session
        session["sets"].append(
            {
                "set": row["set"],
                "weight_kg": row["weight_kg"],
                "assist_kg": row["assist_kg"],
                "reps": row["reps"],
                "rpe": row["rpe"],
            }
        )
        if row["note"] is not None:
            session["notes"].append(row["note"])

    # Sessions: date descending, exercise ascending within a date. Sets: set_number
    # ascending within a session.
    ordered_keys = sorted(sessions_by_key, key=lambda item: (-item[0].toordinal(), item[1]))
    sessions: list[dict[str, Any]] = []
    for key in ordered_keys:
        session = sessions_by_key[key]
        session["sets"].sort(key=lambda item: item["set"])
        sessions.append(
            {
                "date": session["date"].isoformat(),
                "exercise": session["exercise"],
                "category": session["category"],
                "sets": session["sets"],
                "notes": _dedupe_preserve_order(session["notes"]),
            }
        )

    return {
        "source": STRENGTH_EXECUTION_SOURCE_NAME,
        "window_start": window.window42_start.isoformat(),
        "window_end": window.window42_end.isoformat(),
        "sessions": sessions,
    }


def _fetch_recovery_signals_rows(connection: sqlite3.Connection, window: BuildWindow) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT date, readiness_score, readiness_level, hrv_status, hrv_7d_avg_ms, "
        "acute_load, recovery_time_sec FROM recovery_daily "
        "WHERE source = ? AND date >= ? AND date <= ?",
        (RECOVERY_SIGNALS_ROW_SOURCE, window.window_start.isoformat(), window.window_end.isoformat()),
    ).fetchall()
    parsed: list[dict[str, Any]] = []
    for date_text, readiness_score, readiness_level, hrv_status, hrv_7d_avg_ms, acute_load, recovery_time_sec in rows:
        day = _safe_date(date_text)
        if day is None:
            continue
        parsed.append(
            {
                "date": day,
                "readiness_score": _safe_float(readiness_score),
                "readiness_level": readiness_level,
                "hrv_status": hrv_status,
                "hrv_7d_avg_ms": _safe_float(hrv_7d_avg_ms),
                "acute_load": _safe_float(acute_load),
                "recovery_time_sec": _safe_float(recovery_time_sec),
            }
        )
    return parsed


def _fetch_recovery_daily_metrics_rows(connection: sqlite3.Connection, window: BuildWindow) -> list[dict[str, Any]]:
    # The three ?'s in the IN clause are bound positionally to RECOVERY_SIGNALS_DAILY_METRICS
    # below -- keep both in sync if that tuple's length ever changes.
    rows = connection.execute(
        "SELECT date, metric, value FROM daily_metrics "
        "WHERE source = ? AND metric IN (?, ?, ?) AND date >= ? AND date <= ?",
        (
            RECOVERY_SIGNALS_ROW_SOURCE,
            *RECOVERY_SIGNALS_DAILY_METRICS,
            window.window_start.isoformat(),
            window.window_end.isoformat(),
        ),
    ).fetchall()
    parsed: list[dict[str, Any]] = []
    for date_text, metric, value in rows:
        day = _safe_date(date_text)
        if day is None:
            continue
        parsed.append({"date": day, "metric": metric, "value": _safe_float(value)})
    return parsed


def fetch_recovery_signals(db_path: Path, window: BuildWindow) -> dict[str, Any]:
    """Build the standalone recovery_signals evidence group from health.db's
    recovery_daily + daily_metrics tables (issue #37 slice 2).

    Window is the 7-day trends window (``window.window_start``..``window.window_end``),
    the same window fetch_domain's own recovery_trends uses -- recovery is judged on
    the recent arc, not the 14- or 42-day activity windows the other groups read.

    The two tables are merged per date: this is a natural-key join of two tables
    written from the same Garmin daily snapshot in one file, not a cross-provider
    identity merge like the planned/actual matching in context_core. A date present in
    either table gets exactly one day row; whichever fields that date's row(s) did not
    carry stay null -- never dropped, never guessed from the other table.

    Raises ``ContextBuildError`` when the database cannot be opened or is missing
    either required table -- a *configured* source that cannot be read must fail
    loud, the same stance ``fetch_strength_execution`` takes on its own required
    table. The *unconfigured* case (no ``--health-db`` and no env var) never reaches
    this function; it is handled by the caller (``context_builder.build_context``).

    Every value is read verbatim: floats stay floats, SQL NULL stays ``None``, and
    the string ``"NONE"`` for hrv_status arrives unchanged -- it is Garmin's own
    "still learning this athlete's baseline" reading, real information rather than
    an absent one, so it is never coerced to null. No trend, threshold, or score is
    computed here; the coach judges (issue #3 direction).

    Deliberately not carried, and why: ``vo2_max`` (it contradicts this same db's own
    ``workouts`` data on this account -- a recorded trap, see issue #37/#15 -- so it
    is never surfaced as a trustworthy reading); ``training_status`` (empty for this
    account, no demonstrated use yet); ``hrv_baseline_json`` and
    ``readiness_factors_json`` (opaque blobs, deferred until a #39 arm shows a
    decision actually needs one); ``hrv_last_night_ms`` and sleep
    respiration/SpO2 (the base source's own recovery_trends already carries
    sleep/HRV trend evidence). An empty window, or a window with no matching rows,
    returns ``"days": []`` -- looked, nothing there, distinct from this group being
    ``None`` (never configured, never looked).
    """
    connection = _open_health_db(db_path)
    try:
        missing = _missing_tables(connection, RECOVERY_SIGNALS_REQUIRED_TABLES)
        if missing:
            raise ContextBuildError(
                "health database missing required tables for recovery signals",
                details={"missing_tables": missing},
            )
        recovery_rows = _fetch_recovery_signals_rows(connection, window)
        metric_rows = _fetch_recovery_daily_metrics_rows(connection, window)
    finally:
        connection.close()

    days_by_date: dict[dt.date, dict[str, Any]] = {}

    def _day(day: dt.date) -> dict[str, Any]:
        return days_by_date.setdefault(
            day,
            {
                "date": day,
                "readiness_score": None,
                "readiness_level": None,
                "hrv_status": None,
                "hrv_7d_avg_ms": None,
                "acute_load": None,
                "recovery_time_sec": None,
                "body_battery_high": None,
                "body_battery_low": None,
                "avg_stress": None,
            },
        )

    for row in recovery_rows:
        entry = _day(row["date"])
        entry["readiness_score"] = row["readiness_score"]
        entry["readiness_level"] = row["readiness_level"]
        entry["hrv_status"] = row["hrv_status"]
        entry["hrv_7d_avg_ms"] = row["hrv_7d_avg_ms"]
        entry["acute_load"] = row["acute_load"]
        entry["recovery_time_sec"] = row["recovery_time_sec"]

    for row in metric_rows:
        # row["metric"] is always one of RECOVERY_SIGNALS_DAILY_METRICS (the SQL IN
        # clause above admits nothing else), and those three names are exactly the
        # matching keys _day() initializes, so this is a plain field write, never a
        # KeyError risk.
        entry = _day(row["date"])
        entry[row["metric"]] = row["value"]

    ordered_days = sorted(days_by_date, reverse=True)
    days = [dict(days_by_date[day], date=day.isoformat()) for day in ordered_days]

    return {
        "source": RECOVERY_SIGNALS_SOURCE_NAME,
        "window_start": window.window_start.isoformat(),
        "window_end": window.window_end.isoformat(),
        "days": days,
    }
