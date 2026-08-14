from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import context_core, source_personal_os
from garmin_coach_loop.prescription import render_prescription
from garmin_coach_loop.cli import main
from garmin_coach_loop.context_builder import (
    ALL_DAYS,
    DEFAULT_SESSION_MINUTES,
    DEFAULT_SOURCE,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    VALID_SOURCES,
    ContextBuildError,
    ContextRequest,
    build_context,
    parse_red_flag_overrides,
)
from garmin_coach_loop.source_personal_os import PERSONAL_OS_SOURCE_NOTE
from garmin_coach_loop.store import cycle_sessions as store_cycle_sessions, init_store, status_store


# Fields that belong to ContextRequest (the athlete-input side of a build), as opposed to
# build_context's own orchestration kwargs (state_dir/source/db_path/now). Keeping this
# list next to _build below makes the split between the two explicit and easy to audit.
REQUEST_FIELDS = (
    "as_of_raw",
    "timezone_name",
    "available_days",
    "session_minutes",
    "red_flags",
    "leg_fatigue",
    "soreness",
    "schedule_changed",
    "equipment_changed",
    "extra_unknowns",
)


ROOT = Path(__file__).resolve().parents[1]

# A fixed "wall clock" and "as of" moment shared by most tests, so freshness (measured
# against real now) and coverage/trend windows (measured against as_of) are both fully
# deterministic regardless of when the suite actually runs.
NOW = dt.datetime(2026, 1, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
AS_OF_RAW = "2026-01-08T20:00:00+08:00"

ATHLETE_BASELINE_FIXTURE: dict[str, Any] = {
    "threshold_pace_sec_per_km": 370,
    "max_hr": 188,
    "easy_hr_ceiling": 150,
    "longest_recent_run_km": 12.0,
    "weekly_volume_km_4wk_avg": 32.0,
    "max_session_minutes": 75,
    "strength_loads": [
        {"exercise": "back squat", "load_kg": 70.0, "assist_kg": None, "scheme": "4x6"},
        {"exercise": "pull-up", "load_kg": None, "assist_kg": 15.0, "scheme": "3x8"},
    ],
}

PLAN_FIXTURE: dict[str, Any] = {
    "schema_version": "1.0",
    "plan_id": "test-plan-001",
    "version": 1,
    "status": "active",
    "goal": {
        "outcome": "improve repeatable 5K performance while maintaining lower-body strength",
        "measurement_protocol": "Repeat the same controlled 5K route in comparable conditions at Day 0 and Day 28",
    },
    "cycle": {
        "start": "2026-01-05",
        "end": "2026-02-01",
        "primary_adaptation": "threshold",
        "maintenance_adaptation": "strength",
        "planned_evidence": ["Complete one controlled threshold anchor per planned week"],
        "adjust_conditions": ["Two consecutive weeks miss the primary stimulus"],
        "stop_conditions": ["Pain, illness, chest pain, dizziness, or unusual symptoms require a human decision"],
    },
    "week": {
        "start": "2026-01-05",
        "intent": "Protect Thursday quality while maintaining two strength exposures",
        "sessions": [
            {
                "session_id": "strength-full-01",
                "sport": "strength",
                "scheduled_date": "2026-01-05",
                "time_window": "evening",
                "purpose": "Maintain full-body strength without lower-body failure",
                "adaptation": "strength",
                "body_stress": "full",
                "cost": "moderate",
                "priority": "anchor",
                "planned_minutes": 55,
                "hard": False,
                "fallback": {"action": "reduce", "description": "Reduce lower-body accessory volume"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "completed",
            },
            {
                "session_id": "run-easy-01",
                "sport": "running",
                "scheduled_date": "2026-01-06",
                "time_window": "morning",
                "purpose": "Support aerobic base at conversational effort",
                "adaptation": "aerobic_base",
                "body_stress": "lower",
                "cost": "easy",
                "priority": "flexible",
                "planned_minutes": 35,
                "hard": False,
                "fallback": {"action": "reduce", "description": "Shorten while retaining easy effort"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "completed",
            },
            {
                "session_id": "mobility-01",
                "sport": "mobility",
                "scheduled_date": "2026-01-07",
                "time_window": None,
                "purpose": "Preserve recovery before the quality anchor",
                "adaptation": "recovery",
                "body_stress": "full",
                "cost": "easy",
                "priority": "optional",
                "planned_minutes": 20,
                "hard": False,
                "fallback": {"action": "rest", "description": "Rest if mobility is not useful"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "completed",
            },
            {
                "session_id": "run-quality-01",
                "sport": "running",
                "scheduled_date": "2026-01-08",
                "time_window": "morning",
                "purpose": "Accumulate controlled threshold work",
                "adaptation": "threshold",
                "body_stress": "lower",
                "cost": "hard",
                "priority": "anchor",
                "planned_minutes": 50,
                "hard": True,
                "fallback": {"action": "replace", "description": "Replace with 30 minutes easy"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "planned",
            },
            {
                "session_id": "strength-upper-01",
                "sport": "strength",
                "scheduled_date": "2026-01-09",
                "time_window": "evening",
                "purpose": "Maintain upper-body strength with low-volume lower accessory work",
                "adaptation": "strength",
                "body_stress": "upper",
                "cost": "moderate",
                "priority": "anchor",
                "planned_minutes": 50,
                "hard": False,
                "fallback": {"action": "reduce", "description": "Remove lower-body accessory work"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "planned",
            },
            {
                "session_id": "rest-01",
                "sport": "rest",
                "scheduled_date": "2026-01-10",
                "time_window": None,
                "purpose": "Protect recovery before the long aerobic run",
                "adaptation": "recovery",
                "body_stress": "systemic",
                "cost": "easy",
                "priority": "anchor",
                "planned_minutes": 0,
                "hard": False,
                "fallback": {"action": "rest", "description": "Keep the day as rest"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "planned",
            },
            {
                "session_id": "run-long-01",
                "sport": "running",
                "scheduled_date": "2026-01-11",
                "time_window": "morning",
                "purpose": "Build aerobic endurance at easy effort",
                "adaptation": "aerobic_base",
                "body_stress": "lower",
                "cost": "moderate",
                "priority": "anchor",
                "planned_minutes": 55,
                "hard": False,
                "fallback": {"action": "reduce", "description": "Shorten to 40 minutes at easy effort"},
                "execution": {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
                "match_status": "planned",
            },
        ],
    },
    "athlete_baseline": ATHLETE_BASELINE_FIXTURE,
}


def _default_plan(sport: str) -> dict[str, Any]:
    """The execution model each fixture sport is planned under (issue #93).

    Applied to the fixture below rather than written into every session literal: these
    tests are about context building, not about what any one session prescribes. An
    `open` target and a bodyweight movement are the shapes that need no measured anchor,
    so the fixture stays valid whatever baseline a test hands it.
    """
    if sport == "running":
        return {
            "kind": "time_axis",
            "name": "Fixture run",
            "steps": [{
                "kind": "work", "name": "Run",
                "duration": {"kind": "time", "seconds": 1800},
                "target": {"kind": "open"},
            }],
        }
    if sport == "strength":
        return {
            "kind": "movement_list",
            "movements": [{
                "exercise": "back squat", "display_name": "深蹲", "sets": 4, "reps": 6, "load_kg": None,
                "assist_kg": None, "load_basis": "bodyweight",
            }],
        }
    return {"kind": "unstructured"}


for _session in PLAN_FIXTURE["week"]["sessions"]:
    _session["plan"] = _default_plan(_session["sport"])
    _session["prescription"] = render_prescription(_session["plan"])


def _make_plan() -> dict[str, Any]:
    return copy.deepcopy(PLAN_FIXTURE)


def _create_health_db(
    path: Path,
    *,
    workouts: list[dict[str, Any]] = (),
    recovery: list[dict[str, Any]] = (),
    resting_hr: list[dict[str, Any]] = (),
    strength_log: list[dict[str, Any]] = (),
    recovery_daily_garmin: list[dict[str, Any]] = (),
    daily_metrics_garmin: list[dict[str, Any]] = (),
) -> None:
    """Create a synthetic health.db fixture with the same shape as the real schema.

    strength_log is always created (matching the real health.db, where it lives
    alongside the other tables in one file) even when no rows are given -- a test
    that resolves --health-db from the same shared env var as --db (see
    source_personal_os.HEALTH_DB_ENV_VARS) must find a real, empty table rather than
    a missing one. recovery_daily and daily_metrics likewise always carry the full
    real-schema columns (see source_personal_os.fetch_recovery_signals), whether or
    not any row is given.

    recovery_daily_garmin/daily_metrics_garmin insert source='garmin' rows carrying
    the recovery_signals columns (issue #37 slice 2) -- kept separate from
    recovery/resting_hr above, which insert source='fixture' rows read by the
    unrelated fetch_domain trend calculation. Different source values on the same
    (date, source) primary key mean a date can appear in both without collision.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE workouts ("
            "id TEXT PRIMARY KEY, source TEXT NOT NULL, start_time TEXT NOT NULL, "
            "activity_type TEXT, duration_sec REAL, avg_speed_mps REAL, ingested_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE recovery_daily ("
            "date TEXT NOT NULL, source TEXT NOT NULL, hrv_last_night_ms REAL, hrv_7d_avg_ms REAL, "
            "hrv_status TEXT, hrv_baseline_json TEXT, sleep_respiration_bpm REAL, sleep_spo2_pct REAL, "
            "readiness_score REAL, readiness_level TEXT, readiness_factors_json TEXT, "
            "acute_load REAL, recovery_time_sec REAL, "
            "vo2_max REAL, vo2_observed_date TEXT, training_status TEXT, "
            "ingested_at TEXT NOT NULL, PRIMARY KEY (date, source))"
        )
        connection.execute(
            "CREATE TABLE daily_metrics ("
            "date TEXT NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL, value REAL, unit TEXT, "
            "ingested_at TEXT NOT NULL, PRIMARY KEY (date, source, metric))"
        )
        connection.execute(
            "CREATE TABLE strength_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, category TEXT NOT NULL, "
            "exercise TEXT NOT NULL, set_number INTEGER NOT NULL, weight_kg REAL, assist_kg REAL, "
            "reps INTEGER, rpe REAL, slow_negative INTEGER, notes TEXT, created_at TEXT NOT NULL)"
        )
        for row in workouts:
            connection.execute(
                "INSERT INTO workouts (id, source, start_time, activity_type, duration_sec, "
                "avg_speed_mps, ingested_at) VALUES (?, 'fixture', ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["start_time"],
                    row.get("activity_type"),
                    row.get("duration_sec"),
                    row.get("avg_speed_mps"),
                    row["ingested_at"],
                ),
            )
        for row in recovery:
            baseline = row.get("hrv_baseline")
            sleep_percent = row.get("sleep_percent")
            connection.execute(
                "INSERT INTO recovery_daily (date, source, hrv_last_night_ms, hrv_7d_avg_ms, "
                "hrv_baseline_json, readiness_factors_json, ingested_at) VALUES (?, 'fixture', ?, ?, ?, ?, ?)",
                (
                    row["date"],
                    row.get("hrv_last_night_ms"),
                    row.get("hrv_7d_avg_ms"),
                    json.dumps(baseline) if baseline is not None else None,
                    json.dumps({"sleep_score": {"percent": sleep_percent}}) if sleep_percent is not None else None,
                    row["ingested_at"],
                ),
            )
        for row in resting_hr:
            connection.execute(
                "INSERT INTO daily_metrics (date, source, metric, value, unit, ingested_at) "
                "VALUES (?, 'fixture', 'resting_hr', ?, 'bpm', ?)",
                (row["date"], row["value"], row["ingested_at"]),
            )
        for row in strength_log:
            connection.execute(
                "INSERT INTO strength_log (date, category, exercise, set_number, weight_kg, "
                "assist_kg, reps, rpe, slow_negative, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["date"],
                    row["category"],
                    row["exercise"],
                    row["set_number"],
                    row.get("weight_kg"),
                    row.get("assist_kg"),
                    row.get("reps"),
                    row.get("rpe"),
                    row.get("slow_negative"),
                    row.get("notes"),
                    row.get("created_at", "2026-01-01T00:00:00"),
                ),
            )
        for row in recovery_daily_garmin:
            connection.execute(
                "INSERT INTO recovery_daily (date, source, readiness_score, readiness_level, "
                "hrv_status, hrv_7d_avg_ms, acute_load, recovery_time_sec, ingested_at) "
                "VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["date"],
                    row.get("readiness_score"),
                    row.get("readiness_level"),
                    row.get("hrv_status"),
                    row.get("hrv_7d_avg_ms"),
                    row.get("acute_load"),
                    row.get("recovery_time_sec"),
                    row.get("ingested_at", "2026-01-01T00:00:00"),
                ),
            )
        for row in daily_metrics_garmin:
            connection.execute(
                "INSERT INTO daily_metrics (date, source, metric, value, unit, ingested_at) "
                "VALUES (?, 'garmin', ?, ?, ?, ?)",
                (
                    row["date"],
                    row["metric"],
                    row.get("value"),
                    row.get("unit"),
                    row.get("ingested_at", "2026-01-01T00:00:00"),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _build(**overrides: Any) -> dict[str, Any]:
    """Call build_context with sensible defaults, overridable per test.

    Defaults ``source`` to "personal-os" explicitly (never relying on DEFAULT_SOURCE):
    this file exercises the personal-os path only, and the repo-root .env in this
    worktree carries real intervals.icu credentials, so falling through to
    DEFAULT_SOURCE ("intervals") would make these tests attempt a real network call
    instead of staying deterministic and offline.
    """
    request_kwargs: dict[str, Any] = {
        "as_of_raw": AS_OF_RAW,
        "timezone_name": DEFAULT_TIMEZONE,
        "available_days": list(ALL_DAYS),
        "session_minutes": DEFAULT_SESSION_MINUTES,
        "red_flags": {field: None for field in RED_FLAG_FIELDS},
        "leg_fatigue": "unknown",
        "soreness": "unknown",
        "schedule_changed": None,
        "equipment_changed": None,
        "extra_unknowns": [],
    }
    build_kwargs: dict[str, Any] = {"now": NOW, "source": "personal-os"}
    for key, value in overrides.items():
        if key in REQUEST_FIELDS:
            request_kwargs[key] = value
        else:
            build_kwargs[key] = value
    return build_context(ContextRequest(**request_kwargs), **build_kwargs)


class ContextBuilderTests(unittest.TestCase):
    def test_happy_path_passes_validation_and_links_goal_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            _create_health_db(
                db_path,
                workouts=[
                    {
                        "id": "fx-strength-01",
                        "start_time": "2026-01-08T06:00:00",
                        "activity_type": "strength_training",
                        "duration_sec": 3300,
                        "avg_speed_mps": None,
                        "ingested_at": (NOW - dt.timedelta(hours=1)).isoformat(),
                    }
                ],
            )

            report = _build(db_path=db_path, state_dir=state_dir, extra_unknowns=["fixture-manual-note"])

            self.assertEqual("passed", report["status"])
            self.assertEqual([], report["validation"]["errors"])
            context = report["context"]

            self.assertEqual(
                {
                    "plan_id": "test-plan-001",
                    "plan_version": 1,
                    "primary_goal": "threshold — improve repeatable 5K performance while maintaining lower-body strength",
                    "maintenance_goal": "strength",
                    "measurement_protocol": (
                        "Repeat the same controlled 5K route in comparable conditions at "
                        "Day 0 and Day 28"
                    ),
                },
                context["goal_context"],
            )
            self.assertEqual(ATHLETE_BASELINE_FIXTURE, context["athlete_baseline"])
            self.assertEqual("passed", context["sources"][0]["doctor_status"])
            self.assertEqual("passed", context["sources"][1]["doctor_status"])
            self.assertEqual(
                {"observed_days": 1, "expected_days": 7, "status": "partial"},
                context["coverage"]["activities"],
            )
            self.assertEqual(7, len(context["current_calendar"]))
            self.assertEqual(1, len(context["recent_actuals"]))
            actual = context["recent_actuals"][0]
            self.assertEqual("strength", actual["sport"])
            self.assertEqual("strength", actual["adaptation"])
            self.assertEqual("full", actual["body_stress"])
            self.assertEqual("moderate", actual["cost"])
            self.assertEqual(55, actual["duration_minutes"])
            self.assertEqual("unmatched", actual["match_confidence"])
            self.assertIsNone(actual["planned_session_id"])
            # health.db has no elevation or subjective-feel columns -- never fabricated.
            self.assertIsNone(actual["elevation_gain_m"])
            self.assertIsNone(actual["subjective_feel"])
            self.assertIn("sleep_data_unavailable", context["unknowns"])
            self.assertIn("resting_hr_unavailable", context["unknowns"])
            self.assertIn("red_flags_not_confirmed", context["unknowns"])
            self.assertIn("fixture-manual-note", context["unknowns"])
            # This source is an owner-only patch, not the product path -- every build
            # from it says so explicitly.
            self.assertIn(PERSONAL_OS_SOURCE_NOTE, context["unknowns"])

    def test_health_db_configures_both_optional_evidence_groups_together(self):
        """The product path: --health-db (one flag, resolved once) feeds both
        standalone groups at once. Neither fetcher's wiring in build_context
        accidentally drops or shadows the other."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            _create_health_db(
                db_path,
                strength_log=[
                    {"date": "2026-01-08", "category": "chest", "exercise": "bench_press",
                     "set_number": 1, "weight_kg": 60.0, "reps": 5, "created_at": "2026-01-08T19:00:00"},
                ],
                recovery_daily_garmin=[
                    {"date": "2026-01-08", "readiness_score": 56.0, "readiness_level": "MODERATE",
                     "hrv_status": "NONE", "acute_load": 409.0, "recovery_time_sec": 682.0},
                ],
                daily_metrics_garmin=[
                    {"date": "2026-01-08", "metric": "body_battery_low", "value": 55.0},
                ],
            )

            report = _build(db_path=db_path, health_db=db_path, state_dir=state_dir)

            self.assertEqual("passed", report["status"], report)
            self.assertEqual([], report["validation"]["errors"])
            context = report["context"]
            self.assertEqual(1, len(context["strength_execution"]["sessions"]))
            self.assertEqual(1, len(context["recovery_signals"]["days"]))
            self.assertEqual(56.0, context["recovery_signals"]["days"][0]["readiness_score"])
            self.assertEqual(55.0, context["recovery_signals"]["days"][0]["body_battery_low"])

    def test_coverage_status_derivation_matches_observed_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"

            window_dates = [f"2026-01-0{day}" for day in range(2, 9)]  # 2026-01-02 .. 2026-01-08
            fresh_ingested = (NOW - dt.timedelta(hours=1)).isoformat()
            recovery_rows = []
            for index, date_text in enumerate(window_dates):
                recovery_rows.append(
                    {
                        "date": date_text,
                        "sleep_percent": 65.0,
                        # Only the last 3 days (index 4, 5, 6 -> Jan6, Jan7, Jan8) get an hrv reading.
                        "hrv_last_night_ms": 45.0 if index >= 4 else None,
                        "ingested_at": fresh_ingested,
                    }
                )
            _create_health_db(
                db_path,
                workouts=[],  # activities must come out "missing"
                recovery=recovery_rows,
                resting_hr=[
                    {"date": "2026-01-07", "value": 55.0, "ingested_at": fresh_ingested},
                    {"date": "2026-01-08", "value": 56.0, "ingested_at": fresh_ingested},
                ],
            )

            report = _build(db_path=db_path, state_dir=state_dir)
            self.assertEqual("passed", report["status"])
            self.assertEqual([], report["validation"]["errors"])
            coverage = report["context"]["coverage"]

            self.assertEqual({"observed_days": 0, "expected_days": 7, "status": "missing"}, coverage["activities"])
            self.assertEqual({"observed_days": 7, "expected_days": 7, "status": "complete"}, coverage["sleep"])
            self.assertEqual({"observed_days": 3, "expected_days": 7, "status": "partial"}, coverage["hrv"])
            self.assertEqual({"observed_days": 2, "expected_days": 7, "status": "partial"}, coverage["resting_hr"])
            self.assertEqual({"observed_days": 4, "expected_days": 7, "status": "partial"}, coverage["calendar"])

    def test_stale_ingested_at_yields_stale_freshness_and_still_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            stale_ingested = (NOW - dt.timedelta(days=3)).isoformat()  # between 36h and 7d old
            _create_health_db(
                db_path,
                workouts=[
                    {
                        "id": "fx-stale-01",
                        "start_time": "2026-01-06T07:00:00",
                        "activity_type": "running",
                        "duration_sec": 1800,
                        "avg_speed_mps": 2.5,
                        "ingested_at": stale_ingested,
                    }
                ],
                # The row must carry a real signal value: freshness only counts observed
                # rows, and this test is about ingested_at age, not signal absence.
                recovery=[{"date": "2026-01-06", "sleep_percent": 75.0, "ingested_at": stale_ingested}],
            )

            report = _build(db_path=db_path, state_dir=state_dir)

            self.assertEqual("passed", report["status"])
            self.assertEqual([], report["validation"]["errors"])
            self.assertEqual("stale", report["context"]["freshness"]["activities"])
            self.assertEqual("stale", report["context"]["freshness"]["recovery"])

    def test_recovery_rows_with_no_signal_values_read_as_failed_not_fresh(self):
        # A recovery_daily row with every signal null is a sync artifact. Before this
        # rule, a freshly-synced but value-empty row made recovery "fresh" -- the same
        # dishonesty the intervals source's signal-value grading rejects, surfaced on
        # this source by that PR's review.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            fresh_ingested = (NOW - dt.timedelta(hours=2)).isoformat()
            _create_health_db(
                db_path,
                workouts=[
                    {
                        "id": "fx-nullrow-01",
                        "start_time": "2026-01-08T07:00:00",
                        "activity_type": "running",
                        "duration_sec": 1800,
                        "avg_speed_mps": 2.5,
                        "ingested_at": fresh_ingested,
                    }
                ],
                recovery=[
                    {"date": "2026-01-07", "sleep_percent": None, "ingested_at": fresh_ingested},
                    {"date": "2026-01-08", "sleep_percent": None, "ingested_at": fresh_ingested},
                ],
            )

            report = _build(db_path=db_path, state_dir=state_dir)

            self.assertEqual("passed", report["status"])
            self.assertEqual("fresh", report["context"]["freshness"]["activities"])
            self.assertEqual("failed", report["context"]["freshness"]["recovery"])

    def test_missing_db_file_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            missing_db = tmp_path / "does-not-exist.db"

            with self.assertRaisesRegex(ContextBuildError, "not found"):
                _build(db_path=missing_db, state_dir=state_dir)

    def test_red_flags_default_null_then_all_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            _create_health_db(db_path, workouts=[], recovery=[], resting_hr=[])

            unresolved = parse_red_flag_overrides([], all_clear=False)
            self.assertEqual({field: None for field in RED_FLAG_FIELDS}, unresolved)
            report_unresolved = _build(db_path=db_path, state_dir=state_dir, red_flags=unresolved)
            self.assertEqual("passed", report_unresolved["status"])
            self.assertIn("red_flags_not_confirmed", report_unresolved["context"]["unknowns"])
            self.assertEqual(unresolved, report_unresolved["context"]["constraints"]["red_flags"])

            all_clear = parse_red_flag_overrides([], all_clear=True)
            self.assertEqual({field: False for field in RED_FLAG_FIELDS}, all_clear)
            report_clear = _build(db_path=db_path, state_dir=state_dir, red_flags=all_clear)
            self.assertEqual("passed", report_clear["status"])
            self.assertNotIn("red_flags_not_confirmed", report_clear["context"]["unknowns"])
            self.assertEqual(all_clear, report_clear["context"]["constraints"]["red_flags"])

    def test_plan_predating_athlete_baseline_field_still_builds_with_null_baseline(self):
        """athlete_baseline is optional on a stored PlanState (validation.py's _keys
        ``optional=`` param) so an append-only history commit made before the field
        existed stays valid -- this is the real end-to-end path for that case, through
        the actual store rather than a direct assemble_context call."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            plan_without_baseline = _make_plan()
            del plan_without_baseline["athlete_baseline"]
            init_store(state_dir, plan_without_baseline)
            db_path = tmp_path / "health.db"
            _create_health_db(db_path, workouts=[], recovery=[], resting_hr=[])

            report = _build(db_path=db_path, state_dir=state_dir)

            self.assertEqual("passed", report["status"], report)
            self.assertEqual(context_core.ATHLETE_BASELINE_UNKNOWN, report["context"]["athlete_baseline"])
            self.assertIn("athlete_baseline_unavailable", report["context"]["unknowns"])

    def test_build_context_help_flag_exits_zero(self):
        with self.assertRaises(SystemExit) as cm:
            main(["build-context", "--help"])
        self.assertEqual(0, cm.exception.code)

    def test_cli_build_context_round_trip_writes_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            _create_health_db(
                db_path,
                workouts=[
                    {
                        "id": "fx-cli-strength-01",
                        "start_time": "2026-01-08T18:00:00",
                        "activity_type": "strength_training",
                        "duration_sec": 3000,
                        "avg_speed_mps": None,
                        "ingested_at": (NOW - dt.timedelta(hours=1)).isoformat(),
                    }
                ],
            )
            out_path = tmp_path / "context.json"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "garmin_coach_loop.cli",
                    "build-context",
                    "--state-dir",
                    str(state_dir),
                    "--db",
                    str(db_path),
                    "--as-of",
                    AS_OF_RAW,
                    "--source",
                    "personal-os",
                    "--all-clear",
                    "--out",
                    str(out_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("passed", payload["status"])
            self.assertTrue(out_path.exists())
            written = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["context"], written)
            self.assertEqual("test-plan-001", written["goal_context"]["plan_id"])


class SourceSelectionPolicyTests(unittest.TestCase):
    """--source has exactly two values, "intervals" is the default, and degrading to
    "personal-os" is always an explicit, non-default choice with no guessed path."""

    def test_valid_sources_has_no_auto(self):
        self.assertEqual(("intervals", "personal-os"), VALID_SOURCES)
        self.assertEqual("intervals", DEFAULT_SOURCE)

    def test_unknown_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with self.assertRaisesRegex(ContextBuildError, "unknown --source"):
                _build(state_dir=state_dir, source="auto")

    def test_personal_os_source_without_db_or_env_var_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch.dict(os.environ, {}, clear=False):
                for name in source_personal_os.HEALTH_DB_ENV_VARS:
                    os.environ.pop(name, None)
                with self.assertRaisesRegex(ContextBuildError, "personal-os source unavailable"):
                    # No db_path override -> db_path stays None; source="personal-os" is
                    # _build's own test default.
                    _build(state_dir=state_dir)

    def test_health_db_path_resolution_order(self):
        """--db wins, then the repo-specific var, then personal_os's own HEALTH_DB_PATH."""
        resolve = source_personal_os.resolve_health_db_path
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in source_personal_os.HEALTH_DB_ENV_VARS:
                os.environ.pop(name, None)
            self.assertIsNone(resolve(None))

            # The standard name any consumer of the database can set.
            os.environ["HEALTH_DB_PATH"] = "/tmp/standard.db"
            self.assertEqual(resolve(None), Path("/tmp/standard.db"))

            # A machine pointing this tool somewhere else on purpose.
            os.environ["GARMIN_COACH_LOOP_HEALTH_DB"] = "/tmp/override.db"
            self.assertEqual(resolve(None), Path("/tmp/override.db"))

            self.assertEqual(resolve(Path("/tmp/flag.db")), Path("/tmp/flag.db"))

    def test_personal_os_source_resolves_db_path_from_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            _create_health_db(db_path, workouts=[], recovery=[], resting_hr=[])

            with mock.patch.dict(os.environ, {"GARMIN_COACH_LOOP_HEALTH_DB": str(db_path)}):
                request = ContextRequest(
                    as_of_raw=AS_OF_RAW,
                    timezone_name=DEFAULT_TIMEZONE,
                    available_days=list(ALL_DAYS),
                    session_minutes=DEFAULT_SESSION_MINUTES,
                    red_flags={field: None for field in RED_FLAG_FIELDS},
                    leg_fatigue="unknown",
                    soreness="unknown",
                    schedule_changed=None,
                    equipment_changed=None,
                    extra_unknowns=[],
                )
                # db_path intentionally omitted here -- must come from the env var alone.
                report = build_context(request, state_dir=state_dir, source="personal-os", now=NOW)

            self.assertEqual("passed", report["status"], report)


class BuildWindowTimezoneTests(unittest.TestCase):
    """`build_window`'s athlete-local date boundary (issue #112).

    Every context-building command (CLI `build-context`/`refresh-context`, and the
    hosted `startCoachSession`) threads `ContextRequest.timezone_name` through here, so
    the two instants below exercise the same UTC/local midnight boundary `status_store`
    is tested against directly in ``tests/test_state_store.py`` -- proving both layers
    agree on "today" at the same instant, not merely that each is internally consistent.
    """

    def _request(self, timezone_name: str) -> context_core.ContextRequest:
        return context_core.ContextRequest(
            as_of_raw=None,
            timezone_name=timezone_name,
            available_days=list(ALL_DAYS),
            session_minutes=DEFAULT_SESSION_MINUTES,
            red_flags={field: False for field in RED_FLAG_FIELDS},
            leg_fatigue="unknown",
            soreness="unknown",
            schedule_changed=None,
            equipment_changed=None,
            extra_unknowns=[],
        )

    def test_taipei_and_utc_disagree_on_as_of_date_at_the_same_instant(self):
        # 2026-08-13T18:00:00Z is already 2026-08-14 in Taipei (UTC+8) but still
        # 2026-08-13 in UTC. as_of_raw is omitted, so as_of comes entirely from "now"
        # resolved into each request's own timezone -- exactly what a caller near a
        # Taipei midnight and a caller in UTC would each see for "today".
        now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)

        taipei = context_core.build_window(self._request("Asia/Taipei"), now)
        utc = context_core.build_window(self._request("UTC"), now)

        self.assertEqual(dt.date(2026, 8, 14), taipei.as_of.date())
        self.assertEqual(dt.date(2026, 8, 13), utc.as_of.date())
        # window_end mirrors as_of.date() everywhere context_core derives it; asserting
        # it here is the same invariant from the caller's own side.
        self.assertEqual(taipei.as_of.date(), taipei.window_end)
        self.assertEqual(utc.as_of.date(), utc.window_end)

    def test_utc_and_new_york_disagree_across_the_utc_midnight_boundary(self):
        # 2026-08-14T00:30:00Z has just crossed UTC midnight; America/New_York
        # (UTC-4 in August) is still 2026-08-13 20:30 -- the other side of the same
        # boundary: here UTC has rolled over and a zone behind it has not.
        now = dt.datetime(2026, 8, 14, 0, 30, tzinfo=dt.timezone.utc)

        utc = context_core.build_window(self._request("UTC"), now)
        new_york = context_core.build_window(self._request("America/New_York"), now)

        self.assertEqual(dt.date(2026, 8, 14), utc.as_of.date())
        self.assertEqual(dt.date(2026, 8, 13), new_york.as_of.date())

    def test_an_explicit_as_of_still_needs_a_valid_timezone_to_interpret_it(self):
        # Unlike status_store's `today` override, as_of_raw may be a naive timestamp --
        # the timezone is what makes it unambiguous, so build_window resolves the zone
        # unconditionally, even when as_of is given explicitly.
        request = context_core.ContextRequest(
            as_of_raw="2026-08-13T20:00:00",
            timezone_name="Nowhere/Nothing",
            available_days=list(ALL_DAYS),
            session_minutes=DEFAULT_SESSION_MINUTES,
            red_flags={field: False for field in RED_FLAG_FIELDS},
            leg_fatigue="unknown",
            soreness="unknown",
            schedule_changed=None,
            equipment_changed=None,
            extra_unknowns=[],
        )
        with self.assertRaisesRegex(ContextBuildError, "unknown timezone: 'Nowhere/Nothing'"):
            context_core.build_window(request, NOW)


class ContextCoreAssemblyTests(unittest.TestCase):
    """assemble_context is the one seam every source funnels through; exercise its
    defensive athlete_baseline handling directly rather than through a full source."""

    def _window(self) -> context_core.BuildWindow:
        return context_core.build_window(
            context_core.ContextRequest(
                as_of_raw=AS_OF_RAW,
                timezone_name=DEFAULT_TIMEZONE,
                available_days=list(ALL_DAYS),
                session_minutes=DEFAULT_SESSION_MINUTES,
                red_flags={field: False for field in RED_FLAG_FIELDS},
                leg_fatigue="unknown",
                soreness="unknown",
                schedule_changed=None,
                equipment_changed=None,
                extra_unknowns=[],
            ),
            NOW,
        )

    def _empty_domain(self) -> context_core.SourceDomain:
        empty_coverage = context_core._coverage_entry(0)
        empty_trend = {"status": "unknown", "observed_days": 0, "expected_days": 7}
        return context_core.SourceDomain(
            sources=[
                {
                    "source": "fixture-source",
                    "mode": "offline",
                    "doctor_status": "passed",
                    "observed_at": "2026-01-08T12:00:00+00:00",
                    "data_through": None,
                    "sanitized": True,
                }
            ],
            freshness_activities="unknown",
            freshness_recovery="unknown",
            # Same 14-day span source_personal_os reads, so a fixture dated before it
            # exercises the "never searched" case rather than "searched, found nothing".
            actuals_window_start=NOW.date() - dt.timedelta(days=13),
            coverage_activities=empty_coverage,
            coverage_sleep=empty_coverage,
            coverage_hrv=empty_coverage,
            coverage_resting_hr=empty_coverage,
            recovery_trends={"sleep": empty_trend, "hrv": empty_trend, "resting_hr": empty_trend},
            recent_actuals=[],
            extra_unknowns=[],
        )

    def test_missing_athlete_baseline_in_plan_fills_null_structure_and_records_unknown(self):
        plan = _make_plan()
        del plan["athlete_baseline"]
        request = context_core.ContextRequest(
            as_of_raw=AS_OF_RAW,
            timezone_name=DEFAULT_TIMEZONE,
            available_days=list(ALL_DAYS),
            session_minutes=DEFAULT_SESSION_MINUTES,
            red_flags={field: False for field in RED_FLAG_FIELDS},
            leg_fatigue="unknown",
            soreness="unknown",
            schedule_changed=None,
            equipment_changed=None,
            extra_unknowns=[],
        )

        report = context_core.assemble_context(request, plan, self._window(), self._empty_domain())

        self.assertEqual("passed", report["status"], report)
        self.assertEqual(context_core.ATHLETE_BASELINE_UNKNOWN, report["context"]["athlete_baseline"])
        self.assertIn("athlete_baseline_unavailable", report["context"]["unknowns"])

    def _request(self) -> context_core.ContextRequest:
        return context_core.ContextRequest(
            as_of_raw=AS_OF_RAW,
            timezone_name=DEFAULT_TIMEZONE,
            available_days=list(ALL_DAYS),
            session_minutes=DEFAULT_SESSION_MINUTES,
            red_flags={field: False for field in RED_FLAG_FIELDS},
            leg_fatigue="unknown",
            soreness="unknown",
            schedule_changed=None,
            equipment_changed=None,
            extra_unknowns=[],
        )

    @staticmethod
    def _elapsed_session(**overrides: Any) -> dict[str, Any]:
        """One session of an earlier week, in the shape store.cycle_sessions returns."""
        session = {
            "session_id": "strength-mon-01",
            "scheduled_date": "2026-01-02",
            "sport": "strength",
            "adaptation": "strength",
            "body_stress": "upper",
            "cost": "moderate",
            "match_status": "planned",
            "planned_minutes": 60,
            "prescription": "臥推 5×5 @65kg",
        }
        session.update(overrides)
        return session

    @staticmethod
    def _actual(**overrides: Any) -> dict[str, Any]:
        actual = {
            "activity_id": "act-1",
            "date": "2026-01-02",
            "sport": "strength",
            "planned_session_id": None,
            "match_confidence": "unmatched",
            "adaptation": "strength",
            "body_stress": "full",
            "cost": "moderate",
            "duration_minutes": 55,
            "average_hr": 108.0,
            "completion": "completed",
            "elevation_gain_m": None,
            "subjective_feel": None,
        }
        actual.update(overrides)
        return actual

    def test_an_earlier_weeks_session_reaches_the_coach_beside_what_came_back_for_it(self):
        # The point of the record (issue #78): the prescription left the week the moment
        # it rolled over, and the activity is in a different group of the context. Without
        # this the coach retypes "did four sets of five, last one at 60kg" by hand.
        domain = self._empty_domain()
        domain.recent_actuals.append(self._actual())

        report = context_core.assemble_context(
            self._request(),
            _make_plan(),
            self._window(),
            domain,
            cycle_sessions=[self._elapsed_session()],
        )

        self.assertEqual("passed", report["status"], report)
        self.assertEqual(
            [
                {
                    "session_id": "strength-mon-01",
                    "date": "2026-01-02",
                    "week_start": "2025-12-29",
                    "sport": "strength",
                    "cost": "moderate",
                    "match_status": "planned",
                    "planned_minutes": 60,
                    "prescription": "臥推 5×5 @65kg",
                    "activity": {
                        "activity_id": "act-1",
                        "match_confidence": "probable",
                        "duration_minutes": 55,
                        "distance_km": None,
                        "average_pace_sec_per_km": None,
                        "average_hr": 108.0,
                    },
                    "activity_evidence": "attached",
                }
            ],
            report["context"]["cycle_sessions"],
        )
        # Nothing anywhere says how much of it got done: three sets against a five-set
        # prescription is a fact for the coach to weigh, not a ratio for code to write.
        self.assertNotIn("completion", report["context"]["cycle_sessions"][0])

    def test_a_session_with_nothing_recorded_that_day_says_exactly_that(self):
        report = context_core.assemble_context(
            self._request(),
            _make_plan(),
            self._window(),
            self._empty_domain(),
            cycle_sessions=[self._elapsed_session()],
        )

        self.assertEqual("passed", report["status"], report)
        record = report["context"]["cycle_sessions"][0]
        self.assertIsNone(record["activity"])
        self.assertEqual("none_found", record["activity_evidence"])
        self.assertEqual("planned", record["match_status"])

    def test_a_day_that_holds_that_sport_is_never_reported_as_not_done(self):
        # Two strength sessions that day, one activity: it attaches to one of them, and
        # the other must not read as "never trained". Something of that sport was
        # recorded; which session it belongs to is a question about data, and reading it
        # as behaviour would tell the coach to lower a load the athlete is carrying.
        domain = self._empty_domain()
        domain.recent_actuals.append(self._actual())
        elapsed = [
            self._elapsed_session(),
            self._elapsed_session(session_id="strength-mon-02", planned_minutes=55),
        ]

        report = context_core.assemble_context(
            self._request(), _make_plan(), self._window(), domain, cycle_sessions=elapsed
        )

        self.assertEqual("passed", report["status"], report)
        evidence = {
            record["session_id"]: record["activity_evidence"]
            for record in report["context"]["cycle_sessions"]
        }
        # Closest planned-vs-actual duration wins the attachment; the loser reports why
        # it has none rather than claiming the day was empty.
        self.assertEqual(
            {"strength-mon-01": "other_activity_same_day", "strength-mon-02": "attached"},
            evidence,
        )

    def test_a_session_older_than_the_activities_read_is_not_a_missed_session(self):
        # The provider read 14 days; this session is 20 days old. "Nothing attached"
        # here says nothing about the athlete -- the day was never fetched.
        report = context_core.assemble_context(
            self._request(),
            _make_plan(),
            self._window(),
            self._empty_domain(),
            cycle_sessions=[self._elapsed_session(scheduled_date="2025-12-19")],
        )

        self.assertEqual("passed", report["status"], report)
        record = report["context"]["cycle_sessions"][0]
        self.assertIsNone(record["activity"])
        self.assertEqual("outside_evidence_window", record["activity_evidence"])

    def test_the_review_frame_is_the_athletes_calendar_week_not_a_rolling_seven_days(self):
        # The frame a review is read on (issue #89). Every other window in the context ends
        # at as_of and counts backwards; the athlete's week ends on Sunday. Both weeks are
        # stated because a review run on Monday is about the one that just ended.
        window = self._window()

        report = context_core.assemble_context(
            self._request(), _make_plan(), window, self._empty_domain()
        )

        self.assertEqual("passed", report["status"], report)
        self.assertEqual(
            {
                # as_of is Thursday 2026-01-08.
                "week_start": "2026-01-05",
                "week_end": "2026-01-11",
                "previous_week_start": "2025-12-29",
                "previous_week_end": "2026-01-04",
                "cycle_start": "2026-01-05",
                "cycle_end": "2026-02-01",
                "cycle_day": 4,
            },
            report["context"]["review_frame"],
        )
        # The rolling coverage window starts two days earlier and ends today: reading a
        # week off that would answer about the last seven days, not about this week.
        self.assertEqual(dt.date(2026, 1, 2), window.window_start)
        self.assertEqual(dt.date(2026, 1, 8), window.window_end)

    def test_a_cycle_day_is_unknown_before_the_cycle_opens_and_uncapped_after_it_closes(self):
        # Null rather than zero or one: a day that has not arrived is unknown (AGENTS.md 3).
        # Past the end it keeps counting, because "day 34 of a 28-day cycle" is exactly the
        # fact that says the declared window ran out and the measurement is overdue.
        not_started = _make_plan()
        not_started["cycle"]["start"] = "2026-02-02"
        not_started["cycle"]["end"] = "2026-03-01"
        overrun = _make_plan()
        overrun["cycle"]["start"] = "2025-12-06"
        overrun["cycle"]["end"] = "2026-01-02"

        for plan, expected in ((not_started, None), (overrun, 34)):
            with self.subTest(cycle_start=plan["cycle"]["start"]):
                report = context_core.assemble_context(
                    self._request(), plan, self._window(), self._empty_domain()
                )
                self.assertEqual("passed", report["status"], report)
                self.assertEqual(expected, report["context"]["review_frame"]["cycle_day"])

    def test_the_measurement_protocol_travels_with_the_goal_it_belongs_to(self):
        # Outcome is judged against the protocol the cycle declared, so the protocol has to
        # be in the same reading as the goal -- not left in a file the review may not open.
        report = context_core.assemble_context(
            self._request(), _make_plan(), self._window(), self._empty_domain()
        )

        self.assertEqual("passed", report["status"], report)
        self.assertEqual(
            PLAN_FIXTURE["goal"]["measurement_protocol"],
            report["context"]["goal_context"]["measurement_protocol"],
        )

    def test_a_session_still_in_the_week_does_not_compete_with_its_own_chain_copy(self):
        # store.cycle_sessions rebuilds from the commit chain, so a session whose day has
        # passed but which is still in the current week arrives twice. Left undeduped it
        # would put two candidates on that day, ownership would refuse the ambiguity, and
        # a delivered session the product already knows about would drop to "probable" --
        # putting the athlete back in front of a question the plan answers (#22).
        plan = _make_plan()
        week_session = next(
            session for session in plan["week"]["sessions"]
            if session["session_id"] == "strength-full-01"
        )
        week_session["execution"] = {
            "publish_supported": True,
            "external_id": "evt-1",
            "delivery_state": "intervals_accepted",
        }
        domain = self._empty_domain()
        domain.recent_actuals.append(
            self._actual(activity_id="act-week", date="2026-01-05", duration_minutes=55)
        )

        report = context_core.assemble_context(
            self._request(),
            plan,
            self._window(),
            domain,
            cycle_sessions=[
                {
                    "session_id": "strength-full-01",
                    "scheduled_date": "2026-01-05",
                    "sport": "strength",
                    "adaptation": "strength",
                    "body_stress": "full",
                    "cost": "moderate",
                    "match_status": "completed",
                    "planned_minutes": 55,
                    "prescription": "Back squat 4x6 @70kg",
                }
            ],
        )

        self.assertEqual("passed", report["status"], report)
        self.assertEqual(
            ["owned"], [a["match_confidence"] for a in report["context"]["recent_actuals"]]
        )
        self.assertEqual(1, len(report["context"]["cycle_sessions"]))
        self.assertEqual("owned", report["context"]["cycle_sessions"][0]["activity"]["match_confidence"])

    def test_present_athlete_baseline_is_copied_through_unchanged(self):
        plan = _make_plan()
        request = context_core.ContextRequest(
            as_of_raw=AS_OF_RAW,
            timezone_name=DEFAULT_TIMEZONE,
            available_days=list(ALL_DAYS),
            session_minutes=DEFAULT_SESSION_MINUTES,
            red_flags={field: False for field in RED_FLAG_FIELDS},
            leg_fatigue="unknown",
            soreness="unknown",
            schedule_changed=None,
            equipment_changed=None,
            extra_unknowns=[],
        )

        report = context_core.assemble_context(request, plan, self._window(), self._empty_domain())

        self.assertEqual("passed", report["status"], report)
        self.assertEqual(ATHLETE_BASELINE_FIXTURE, report["context"]["athlete_baseline"])
        self.assertNotIn("athlete_baseline_unavailable", report["context"]["unknowns"])


def _strength_window(window42_start: dt.date, window42_end: dt.date) -> context_core.BuildWindow:
    """Minimal BuildWindow for exercising fetch_strength_execution directly --  it
    only reads window42_start/window42_end; the other fields are plausible
    placeholders mirroring build_window's own derivation, unused by that function."""
    return context_core.BuildWindow(
        as_of=dt.datetime(window42_end.year, window42_end.month, window42_end.day, 20, 0, tzinfo=dt.timezone.utc),
        resolved_now=NOW,
        now_iso=NOW.isoformat(),
        window_start=window42_end - dt.timedelta(days=6),
        window_end=window42_end,
        window14_start=window42_end - dt.timedelta(days=13),
        window14_end=window42_end,
        window42_start=window42_start,
        window42_end=window42_end,
    )


def _recovery_window(window_start: dt.date, window_end: dt.date) -> context_core.BuildWindow:
    """Minimal BuildWindow for exercising fetch_recovery_signals directly -- it only
    reads window.window_start/window.window_end (the 7-day trends window); the other
    fields are plausible placeholders mirroring build_window's own derivation, unused
    by that function."""
    return context_core.BuildWindow(
        as_of=dt.datetime(window_end.year, window_end.month, window_end.day, 20, 0, tzinfo=dt.timezone.utc),
        resolved_now=NOW,
        now_iso=NOW.isoformat(),
        window_start=window_start,
        window_end=window_end,
        window14_start=window_end - dt.timedelta(days=13),
        window14_end=window_end,
        window42_start=window_end - dt.timedelta(days=41),
        window42_end=window_end,
    )


class ActualsWindowStartTests(unittest.TestCase):
    """What each provider says about the span its recent_actuals could hold. A cycle
    session older than it was never searched for an attachment, and assemble_context
    reports that as `outside_evidence_window` instead of "nothing came back"."""

    def test_a_day_the_twenty_cap_cut_in_half_is_not_reported_as_fully_read(self):
        # This source keeps the 20 most recent activities inside 14 days, ranked by
        # (date, activity_id) -- so a day with two sessions can have one kept and one
        # dropped. Calling that day fully read turns the dropped one's planned session
        # into "nothing came back", which the coach reads as a session not trained. An
        # athlete training twice most days passes 20 activities inside 14 days as a
        # matter of course.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            # 21 activities over 7 days: the cap keeps 20, so the oldest day loses one
            # of its three and is read only in part.
            workouts = []
            for offset in range(7):
                day = (NOW.date() - dt.timedelta(days=offset)).isoformat()
                for slot in ("a", "b", "c"):
                    workouts.append(
                        {
                            "id": f"w-{day}-{slot}",
                            "start_time": f"{day}T07:00:00+00:00",
                            "activity_type": "running",
                            "duration_sec": 1800,
                            "avg_speed_mps": 2.7,
                            "ingested_at": (NOW - dt.timedelta(hours=1)).isoformat(),
                        }
                    )
            _create_health_db(db_path, workouts=workouts)
            request = ContextRequest(
                as_of_raw=AS_OF_RAW,
                timezone_name=DEFAULT_TIMEZONE,
                available_days=list(ALL_DAYS),
                session_minutes=DEFAULT_SESSION_MINUTES,
                red_flags={field: False for field in RED_FLAG_FIELDS},
                leg_fatigue="unknown",
                soreness="unknown",
                schedule_changed=None,
                equipment_changed=None,
                extra_unknowns=[],
            )
            window = context_core.build_window(request, NOW)

            domain = source_personal_os.fetch_domain(db_path, window)

            kept = [actual["date"] for actual in domain.recent_actuals]
            oldest_day = min(kept)
            self.assertEqual(20, len(kept))
            # The oldest day it kept is the day it also dropped from, so the edge of
            # what was fully read is the day after it -- never that day itself.
            self.assertEqual(2, kept.count(oldest_day))
            self.assertEqual(
                dt.date.fromisoformat(oldest_day) + dt.timedelta(days=1),
                domain.actuals_window_start,
            )

    def test_an_uncapped_read_reports_its_whole_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(
                db_path,
                workouts=[
                    {
                        "id": "w-1",
                        "start_time": f"{NOW.date().isoformat()}T07:00:00+00:00",
                        "activity_type": "running",
                        "duration_sec": 1800,
                        "avg_speed_mps": 2.7,
                        "ingested_at": (NOW - dt.timedelta(hours=1)).isoformat(),
                    }
                ],
            )
            request = ContextRequest(
                as_of_raw=AS_OF_RAW,
                timezone_name=DEFAULT_TIMEZONE,
                available_days=list(ALL_DAYS),
                session_minutes=DEFAULT_SESSION_MINUTES,
                red_flags={field: False for field in RED_FLAG_FIELDS},
                leg_fatigue="unknown",
                soreness="unknown",
                schedule_changed=None,
                equipment_changed=None,
                extra_unknowns=[],
            )
            window = context_core.build_window(request, NOW)

            domain = source_personal_os.fetch_domain(db_path, window)

            self.assertEqual(window.window14_start, domain.actuals_window_start)


class StrengthExecutionEvidenceGroupTests(unittest.TestCase):
    """source_personal_os.fetch_strength_execution and its build_context wiring
    (issue #37): a standalone optional evidence group, never attached to
    recent_actuals and never compared against athlete_baseline.strength_loads."""

    def test_fixture_a_regression_carries_every_session_with_per_set_truth(self):
        # #39: a hand-written 62.5kg bench baseline the athlete had never completed
        # for five sets, and nothing could contradict it. Anonymized-in-shape replay
        # of the real incident data -- this is the deterministic precondition for the
        # coach to see the baseline was never earned: every session's actually
        # completed sets must survive verbatim, never a rollup or a "completed" flag.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            rows: list[dict[str, Any]] = []
            for set_number in range(1, 6):
                rows.append(
                    {
                        "date": "2026-08-01", "category": "chest", "exercise": "bench_press",
                        "set_number": set_number, "weight_kg": 60.0, "reps": 5,
                        "created_at": "2026-08-01T19:00:00",
                    }
                )
            for set_number in range(1, 4):
                rows.append(
                    {
                        "date": "2026-08-08", "category": "chest", "exercise": "bench_press",
                        "set_number": set_number, "weight_kg": 62.5, "reps": 5,
                        "notes": "沒做完，後面減重了", "created_at": "2026-08-08T19:00:00",
                    }
                )
            for set_number, weight in enumerate([65.0, 65.0, 65.0, 65.0, 60.0], start=1):
                rows.append(
                    {
                        "date": "2026-08-11", "category": "chest", "exercise": "bench_press",
                        "set_number": set_number, "weight_kg": weight, "reps": 5,
                        "notes": "做不完五組65kg，第五組 60kg 5下", "created_at": "2026-08-11T19:00:00",
                    }
                )
            _create_health_db(db_path, strength_log=rows)
            window = _strength_window(dt.date(2026, 7, 2), dt.date(2026, 8, 12))

            group = source_personal_os.fetch_strength_execution(db_path, window)

            self.assertEqual("personal-os:strength_log", group["source"])
            self.assertEqual(["2026-08-11", "2026-08-08", "2026-08-01"], [s["date"] for s in group["sessions"]])

            by_date = {session["date"]: session for session in group["sessions"]}
            self.assertEqual(3, len(by_date["2026-08-08"]["sets"]))
            self.assertEqual([62.5, 62.5, 62.5], [s["weight_kg"] for s in by_date["2026-08-08"]["sets"]])
            self.assertEqual(["沒做完，後面減重了"], by_date["2026-08-08"]["notes"])

            fifth_set = by_date["2026-08-11"]["sets"][4]
            self.assertEqual(5, fifth_set["set"])
            self.assertEqual(60.0, fifth_set["weight_kg"])
            self.assertEqual(["做不完五組65kg，第五組 60kg 5下"], by_date["2026-08-11"]["notes"])

            self.assertEqual(5, len(by_date["2026-08-01"]["sets"]))
            self.assertEqual([], by_date["2026-08-01"]["notes"])

    def test_multiple_exercises_same_day_are_separate_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            rows = [
                {"date": "2026-08-05", "category": "chest", "exercise": "bench_press",
                 "set_number": 1, "weight_kg": 60.0, "reps": 5, "created_at": "2026-08-05T19:00:00"},
                {"date": "2026-08-05", "category": "back", "exercise": "pull_up_assisted",
                 "set_number": 1, "weight_kg": None, "assist_kg": 15.0, "reps": 6,
                 "created_at": "2026-08-05T19:10:00"},
            ]
            _create_health_db(db_path, strength_log=rows)
            window = _strength_window(dt.date(2026, 7, 1), dt.date(2026, 8, 12))

            group = source_personal_os.fetch_strength_execution(db_path, window)

            self.assertEqual(2, len(group["sessions"]))
            exercises = {session["exercise"] for session in group["sessions"]}
            self.assertEqual({"bench_press", "pull_up_assisted"}, exercises)

    def test_sets_sorted_by_set_number_notes_deduplicated_assisted_load_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            rows = [
                # Inserted out of set_number order on purpose -- output must still be
                # set_number-ascending regardless of insertion/row order.
                {"date": "2026-08-05", "category": "chest", "exercise": "bench_press",
                 "set_number": 3, "weight_kg": 60.0, "reps": 5, "notes": "steady",
                 "created_at": "2026-08-05T19:00:02"},
                {"date": "2026-08-05", "category": "chest", "exercise": "bench_press",
                 "set_number": 1, "weight_kg": 60.0, "reps": 5, "notes": "steady",
                 "created_at": "2026-08-05T19:00:00"},
                {"date": "2026-08-05", "category": "chest", "exercise": "bench_press",
                 "set_number": 2, "weight_kg": 60.0, "reps": 5, "notes": "grinding",
                 "created_at": "2026-08-05T19:00:01"},
            ]
            _create_health_db(db_path, strength_log=rows)
            window = _strength_window(dt.date(2026, 7, 1), dt.date(2026, 8, 12))

            group = source_personal_os.fetch_strength_execution(db_path, window)

            bench = next(s for s in group["sessions"] if s["exercise"] == "bench_press")
            self.assertEqual([1, 2, 3], [s["set"] for s in bench["sets"]])
            # "steady" appears on sets 1 and 3 -- deduplicated but order-preserving:
            # first occurrence (set 1) wins the position, "grinding" (set 2) follows.
            self.assertEqual(["steady", "grinding"], bench["notes"])

    def test_assisted_movement_keeps_weight_and_assist_kg_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            rows = [
                {"date": "2026-08-05", "category": "back", "exercise": "pull_up_assisted",
                 "set_number": 1, "weight_kg": None, "assist_kg": 15.0, "reps": 6,
                 "created_at": "2026-08-05T19:10:00"},
                {"date": "2026-08-05", "category": "back", "exercise": "pull_up_assisted",
                 "set_number": 2, "weight_kg": None, "assist_kg": 18.0, "reps": 5,
                 "created_at": "2026-08-05T19:11:00"},
            ]
            _create_health_db(db_path, strength_log=rows)
            window = _strength_window(dt.date(2026, 7, 1), dt.date(2026, 8, 12))

            group = source_personal_os.fetch_strength_execution(db_path, window)

            pull_up = group["sessions"][0]
            self.assertEqual("pull_up_assisted", pull_up["exercise"])
            self.assertIsNone(pull_up["sets"][0]["weight_kg"])
            self.assertEqual(15.0, pull_up["sets"][0]["assist_kg"])
            self.assertEqual(18.0, pull_up["sets"][1]["assist_kg"])

    def test_row_before_window42_start_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            rows = [
                {"date": "2026-06-01", "category": "chest", "exercise": "bench_press",
                 "set_number": 1, "weight_kg": 60.0, "reps": 5, "created_at": "2026-06-01T19:00:00"},
            ]
            _create_health_db(db_path, strength_log=rows)
            window = _strength_window(dt.date(2026, 7, 2), dt.date(2026, 8, 12))

            group = source_personal_os.fetch_strength_execution(db_path, window)

            self.assertEqual([], group["sessions"])

    def test_empty_window_yields_empty_sessions_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(db_path)  # strength_log table exists; zero rows

            window = _strength_window(dt.date(2026, 7, 2), dt.date(2026, 8, 12))
            group = source_personal_os.fetch_strength_execution(db_path, window)

            self.assertEqual([], group["sessions"])
            self.assertEqual("2026-07-02", group["window_start"])
            self.assertEqual("2026-08-12", group["window_end"])

    def test_missing_file_raises_context_build_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.db"
            window = _strength_window(dt.date(2026, 7, 2), dt.date(2026, 8, 12))
            with self.assertRaisesRegex(ContextBuildError, "not found"):
                source_personal_os.fetch_strength_execution(missing, window)

    def test_db_without_strength_log_table_raises_context_build_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "no-strength-log.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()

            window = _strength_window(dt.date(2026, 7, 2), dt.date(2026, 8, 12))
            with self.assertRaisesRegex(ContextBuildError, "missing required table"):
                source_personal_os.fetch_strength_execution(db_path, window)

    def test_unconfigured_leaves_rest_of_context_unchanged_vs_a_build_without_the_feature(self):
        """Control: with no --health-db and no env var, BOTH standalone groups --
        strength_execution and recovery_signals (issue #37 slice 2), since they now
        share one resolved path -- must degrade to their own explicit unknown without
        perturbing any other field. Proven by comparing the full build_context
        pipeline (which now always resolves both groups, regardless of --source)
        against a direct context_core.assemble_context call over the same
        plan/window/domain -- the pre-issue-#37 call shape, which defaults both
        groups to None and appends no extra unknown.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            init_store(state_dir, _make_plan())
            db_path = tmp_path / "health.db"
            _create_health_db(
                db_path,
                workouts=[
                    {
                        "id": "fx-control-01",
                        "start_time": "2026-01-08T06:00:00",
                        "activity_type": "strength_training",
                        "duration_sec": 3300,
                        "avg_speed_mps": None,
                        "ingested_at": (NOW - dt.timedelta(hours=1)).isoformat(),
                    }
                ],
            )

            with mock.patch.dict(os.environ, {}, clear=False):
                for name in source_personal_os.HEALTH_DB_ENV_VARS:
                    os.environ.pop(name, None)
                new_report = _build(db_path=db_path, state_dir=state_dir)

            self.assertEqual("passed", new_report["status"], new_report)
            new_context = dict(new_report["context"])
            self.assertIsNone(new_context.pop("strength_execution"))
            self.assertIsNone(new_context.pop("recovery_signals"))
            new_unknowns = list(new_context.pop("unknowns"))
            expected_strength_unknown = (
                "strength_execution: no local strength log configured; recent lift "
                "execution unverified"
            )
            expected_recovery_unknown = (
                "recovery_signals: no local health db configured; recent recovery "
                "state unverified"
            )
            self.assertIn(expected_strength_unknown, new_unknowns)
            self.assertIn(expected_recovery_unknown, new_unknowns)
            new_unknowns.remove(expected_strength_unknown)
            new_unknowns.remove(expected_recovery_unknown)

            request = ContextRequest(
                as_of_raw=AS_OF_RAW,
                timezone_name=DEFAULT_TIMEZONE,
                available_days=list(ALL_DAYS),
                session_minutes=DEFAULT_SESSION_MINUTES,
                red_flags={field: None for field in RED_FLAG_FIELDS},
                leg_fatigue="unknown",
                soreness="unknown",
                schedule_changed=None,
                equipment_changed=None,
                extra_unknowns=[],
            )
            plan = status_store(state_dir)["current_plan"]
            window = context_core.build_window(request, NOW)
            domain = source_personal_os.fetch_domain(db_path, window)
            old_report = context_core.assemble_context(
                request,
                plan,
                window,
                domain,
                # The control differs from the real build in exactly one thing -- the two
                # optional evidence groups -- so it reads the same cycle record the
                # dispatch layer does rather than an empty one.
                cycle_sessions=store_cycle_sessions(
                    state_dir,
                    since=plan["cycle"]["start"],
                    before=window.as_of.date().isoformat(),
                ),
            )

            self.assertEqual("passed", old_report["status"], old_report)
            old_context = dict(old_report["context"])
            self.assertIsNone(old_context.pop("strength_execution"))
            self.assertIsNone(old_context.pop("recovery_signals"))
            old_unknowns = list(old_context.pop("unknowns"))

            self.assertEqual(old_unknowns, new_unknowns)
            self.assertEqual(old_context, new_context)


class RecoverySignalsEvidenceGroupTests(unittest.TestCase):
    """source_personal_os.fetch_recovery_signals (issue #37 slice 2): a second
    standalone optional evidence group sharing --health-db with strength_execution,
    merging recovery_daily + daily_metrics per date. See
    StrengthExecutionEvidenceGroupTests.test_unconfigured_leaves_rest_of_context_unchanged_vs_a_build_without_the_feature
    for the shared unconfigured-control coverage spanning both groups."""

    def test_fixture_b_regression_carries_per_day_readiness_and_load_truth(self):
        # #39 Fixture B: 2026-08-08 dogfood -- a bench-press session failed (3 of 5
        # sets) on the week's Body Battery low and stress high, readiness 56, the day
        # after acute_load peaked at 478 on 2026-08-07. Anonymized-in-shape replay of
        # the real incident data -- this is the deterministic precondition for the
        # coach to read that failure against recovery state instead of capacity.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(
                db_path,
                recovery_daily_garmin=[
                    {"date": "2026-08-05", "hrv_status": "NONE", "acute_load": 300.0},
                    {"date": "2026-08-06", "hrv_status": "NONE", "acute_load": 350.0},
                    {
                        "date": "2026-08-07", "readiness_score": 50.0, "readiness_level": "LOW",
                        "hrv_status": "NONE", "hrv_7d_avg_ms": 78.0, "acute_load": 478.0,
                        "recovery_time_sec": 720.0,
                    },
                    {
                        "date": "2026-08-08", "readiness_score": 56.0, "readiness_level": "MODERATE",
                        "hrv_status": "NONE", "hrv_7d_avg_ms": 80.0, "acute_load": 409.0,
                        "recovery_time_sec": 682.0,
                    },
                ],
                daily_metrics_garmin=[
                    {"date": "2026-08-07", "metric": "body_battery_high", "value": 92.0},
                    {"date": "2026-08-07", "metric": "body_battery_low", "value": 71.0},
                    {"date": "2026-08-07", "metric": "avg_stress", "value": 15.0},
                    {"date": "2026-08-08", "metric": "body_battery_high", "value": 100.0},
                    {"date": "2026-08-08", "metric": "body_battery_low", "value": 55.0},
                    {"date": "2026-08-08", "metric": "avg_stress", "value": 18.0},
                ],
            )
            window = _recovery_window(dt.date(2026, 8, 2), dt.date(2026, 8, 8))

            group = source_personal_os.fetch_recovery_signals(db_path, window)

            self.assertEqual("personal-os:recovery_daily+daily_metrics", group["source"])
            self.assertEqual(
                ["2026-08-08", "2026-08-07", "2026-08-06", "2026-08-05"],
                [day["date"] for day in group["days"]],
            )
            by_date = {day["date"]: day for day in group["days"]}
            # The 2026-08-08 failed lift: readiness, Body Battery low, and stress all
            # land on the same day row as the session that failed.
            self.assertEqual(56.0, by_date["2026-08-08"]["readiness_score"])
            self.assertEqual(55.0, by_date["2026-08-08"]["body_battery_low"])
            self.assertEqual(18.0, by_date["2026-08-08"]["avg_stress"])
            # The day before: acute_load at its week peak.
            self.assertEqual(478.0, by_date["2026-08-07"]["acute_load"])

    def test_date_present_in_only_one_table_still_yields_a_day_with_nulls_for_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(
                db_path,
                recovery_daily_garmin=[
                    {"date": "2026-08-03", "readiness_score": 60.0, "readiness_level": "HIGH"},
                ],
                daily_metrics_garmin=[
                    {"date": "2026-08-05", "metric": "avg_stress", "value": 20.0},
                ],
            )
            window = _recovery_window(dt.date(2026, 8, 1), dt.date(2026, 8, 7))

            group = source_personal_os.fetch_recovery_signals(db_path, window)

            self.assertEqual(["2026-08-05", "2026-08-03"], [day["date"] for day in group["days"]])
            by_date = {day["date"]: day for day in group["days"]}

            recovery_only = by_date["2026-08-03"]
            self.assertEqual(60.0, recovery_only["readiness_score"])
            self.assertIsNone(recovery_only["avg_stress"])
            self.assertIsNone(recovery_only["body_battery_high"])
            self.assertIsNone(recovery_only["body_battery_low"])

            metrics_only = by_date["2026-08-05"]
            self.assertEqual(20.0, metrics_only["avg_stress"])
            self.assertIsNone(metrics_only["readiness_score"])
            self.assertIsNone(metrics_only["acute_load"])

    def test_null_columns_stay_null_and_hrv_status_none_string_is_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(
                db_path,
                recovery_daily_garmin=[{"date": "2026-08-04", "hrv_status": "NONE"}],
            )
            window = _recovery_window(dt.date(2026, 8, 1), dt.date(2026, 8, 7))

            group = source_personal_os.fetch_recovery_signals(db_path, window)

            self.assertEqual(1, len(group["days"]))
            day = group["days"][0]
            # 'NONE' is Garmin's own "still learning this athlete's baseline" reading
            # -- real information, never coerced to null.
            self.assertEqual("NONE", day["hrv_status"])
            self.assertIsNone(day["readiness_score"])
            self.assertIsNone(day["readiness_level"])
            self.assertIsNone(day["hrv_7d_avg_ms"])
            self.assertIsNone(day["acute_load"])
            self.assertIsNone(day["recovery_time_sec"])
            self.assertIsNone(day["body_battery_high"])
            self.assertIsNone(day["body_battery_low"])
            self.assertIsNone(day["avg_stress"])

    def test_row_before_window_start_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(
                db_path,
                # window_start below is 2026-08-02, so this row sits one day earlier.
                recovery_daily_garmin=[{"date": "2026-08-01", "readiness_score": 70.0}],
            )
            window = _recovery_window(dt.date(2026, 8, 2), dt.date(2026, 8, 8))

            group = source_personal_os.fetch_recovery_signals(db_path, window)

            self.assertEqual([], group["days"])

    def test_empty_window_yields_empty_days_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "health.db"
            _create_health_db(db_path)  # recovery_daily/daily_metrics exist; zero matching rows

            window = _recovery_window(dt.date(2026, 8, 2), dt.date(2026, 8, 8))
            group = source_personal_os.fetch_recovery_signals(db_path, window)

            self.assertEqual([], group["days"])
            self.assertEqual("2026-08-02", group["window_start"])
            self.assertEqual("2026-08-08", group["window_end"])

    def test_missing_file_raises_context_build_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.db"
            window = _recovery_window(dt.date(2026, 8, 2), dt.date(2026, 8, 8))
            with self.assertRaisesRegex(ContextBuildError, "not found"):
                source_personal_os.fetch_recovery_signals(missing, window)

    def test_configured_but_recovery_daily_table_missing_raises_context_build_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "no-recovery-daily.db"
            connection = sqlite3.connect(db_path)
            # daily_metrics alone exists -- recovery_daily is the one missing table,
            # proving the "OR" in "missing recovery_daily OR daily_metrics" fails loud
            # on either table alone, not only when both are absent.
            connection.execute(
                "CREATE TABLE daily_metrics (date TEXT, source TEXT, metric TEXT, "
                "value REAL, unit TEXT, ingested_at TEXT)"
            )
            connection.commit()
            connection.close()

            window = _recovery_window(dt.date(2026, 8, 2), dt.date(2026, 8, 8))
            with self.assertRaisesRegex(ContextBuildError, "missing required tables"):
                source_personal_os.fetch_recovery_signals(db_path, window)


def _plan_session(
    session_id: str,
    *,
    sport: str = "running",
    scheduled_date: str = "2026-08-10",
    adaptation: str = "threshold",
    body_stress: str = "lower",
    cost: str = "hard",
    planned_minutes: int | None = 42,
    external_id: str | None = None,
    delivery_state: str = "not_published",
) -> dict[str, Any]:
    """The subset of a PlanState session's fields that ``_match_actuals_to_plan``
    actually reads (session_id, scheduled_date, sport, planned_minutes, adaptation,
    body_stress, cost, execution) -- deliberately not a full
    contracts/plan-state.schema.json session, since these fixtures exercise the matcher
    directly rather than round-tripping through ``validate_plan_state``."""
    return {
        "session_id": session_id,
        "sport": sport,
        "scheduled_date": scheduled_date,
        "adaptation": adaptation,
        "body_stress": body_stress,
        "cost": cost,
        "planned_minutes": planned_minutes,
        "execution": {"external_id": external_id, "delivery_state": delivery_state},
    }


def _delivered_session(session_id: str, **kwargs: Any) -> dict[str, Any]:
    """A session the product itself delivered: the ownership evidence the "owned"
    attachment tier rests on."""
    kwargs.setdefault("external_id", f"event-{session_id}")
    return _plan_session(session_id, delivery_state="intervals_accepted", **kwargs)


def _actual_fixture(
    activity_id: str,
    *,
    date: str = "2026-08-10",
    sport: str = "running",
    duration_minutes: int | None = 30,
    adaptation: str = "aerobic_base",
    body_stress: str = "lower",
    cost: str = "easy",
    paired_event_id: str | None = None,
) -> dict[str, Any]:
    """The subset of a ``recent_actuals`` entry that ``_match_actuals_to_plan`` reads or
    overwrites, in the exact pre-matching shape a source module hands to
    ``assemble_context``: unmatched, carrying whatever classification the domain already
    derived from average pace (see ``context_core._classify_running``)."""
    return {
        "activity_id": activity_id,
        "date": date,
        "sport": sport,
        "paired_event_id": paired_event_id,
        "planned_session_id": None,
        "match_confidence": "unmatched",
        "adaptation": adaptation,
        "body_stress": body_stress,
        "cost": cost,
        "duration_minutes": duration_minutes,
    }


class PlannedActualMatchingTests(unittest.TestCase):
    """Provider identity is matched; calendar coincidence is only probable."""

    def test_unique_same_day_candidate_is_probable_not_matched(self):
        plan_sessions = [
            _plan_session(
                "run-test-01",
                scheduled_date="2026-08-10",
                sport="running",
                adaptation="threshold",
                body_stress="lower",
                cost="hard",
                planned_minutes=42,
            )
        ]
        actuals = [
            _actual_fixture(
                "act-1",
                date="2026-08-10",
                sport="running",
                duration_minutes=42,
                adaptation="aerobic_base",
                body_stress="lower",
                cost="easy",
            )
        ]
        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        self.assertEqual(1, len(result))
        matched = result[0]
        self.assertEqual("run-test-01", matched["planned_session_id"])
        self.assertEqual("probable", matched["match_confidence"])
        self.assertEqual("threshold", matched["adaptation"])
        self.assertEqual("hard", matched["cost"])
        self.assertEqual("lower", matched["body_stress"])
        # The input list itself must never be mutated in place.
        self.assertEqual("unmatched", actuals[0]["match_confidence"])
        self.assertIsNone(actuals[0]["planned_session_id"])

    def test_paired_event_identity_matches_even_on_a_moved_date(self):
        plan_sessions = [
            _plan_session(
                "run-test-01",
                scheduled_date="2026-08-10",
                adaptation="threshold",
                cost="hard",
                external_id="event-123",
            )
        ]
        actuals = [
            _actual_fixture(
                "act-1",
                date="2026-08-11",
                paired_event_id="event-123",
            )
        ]

        matched = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("run-test-01", matched["planned_session_id"])
        self.assertEqual("matched", matched["match_confidence"])
        self.assertEqual("threshold", matched["adaptation"])
        self.assertEqual("hard", matched["cost"])

    def test_moved_identity_claim_resolves_before_same_day_probable_candidate(self):
        plan_sessions = [
            _plan_session(
                "run-test-01",
                scheduled_date="2026-08-10",
                external_id="event-123",
            )
        ]
        actuals = [
            _actual_fixture("calendar-only", date="2026-08-10", duration_minutes=42),
            _actual_fixture(
                "identity",
                date="2026-08-11",
                duration_minutes=60,
                paired_event_id="event-123",
            ),
        ]

        result = context_core._match_actuals_to_plan(actuals, plan_sessions)
        by_id = {actual["activity_id"]: actual for actual in result}

        self.assertEqual("matched", by_id["identity"]["match_confidence"])
        self.assertEqual("run-test-01", by_id["identity"]["planned_session_id"])
        self.assertEqual("unmatched", by_id["calendar-only"]["match_confidence"])

    def test_duplicate_provider_identity_is_never_auto_matched(self):
        plan_sessions = [
            _plan_session(
                "run-test-01",
                scheduled_date="2026-08-10",
                external_id="event-123",
            )
        ]
        actuals = [
            _actual_fixture("identity-a", date="2026-08-11", paired_event_id="event-123"),
            _actual_fixture("identity-b", date="2026-08-12", paired_event_id="event-123"),
        ]

        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        self.assertTrue(all(actual["match_confidence"] != "matched" for actual in result))

    def test_multiple_candidates_pick_closest_duration_as_probable(self):
        plan_sessions = [
            _plan_session(
                "run-a", scheduled_date="2026-08-10", planned_minutes=30, adaptation="aerobic_base", cost="easy"
            ),
            _plan_session(
                "run-b", scheduled_date="2026-08-10", planned_minutes=50, adaptation="threshold", cost="hard"
            ),
        ]
        # 48 minutes is 18 away from run-a's 30 but only 2 away from run-b's 50.
        actuals = [_actual_fixture("act-1", date="2026-08-10", duration_minutes=48)]
        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        matched = result[0]
        self.assertEqual("run-b", matched["planned_session_id"])
        self.assertEqual("probable", matched["match_confidence"])
        self.assertEqual("threshold", matched["adaptation"])
        self.assertEqual("hard", matched["cost"])

    def test_no_candidate_stays_unmatched_and_keeps_derived_classification(self):
        # Only a strength session exists that day -- the running actual has zero
        # same-date/same-sport candidates.
        plan_sessions = [_plan_session("strength-x", scheduled_date="2026-08-10", sport="strength", planned_minutes=60)]
        actuals = [
            _actual_fixture(
                "act-1",
                date="2026-08-10",
                sport="running",
                duration_minutes=30,
                adaptation="aerobic_base",
                body_stress="lower",
                cost="easy",
            )
        ]
        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        unmatched = result[0]
        self.assertIsNone(unmatched["planned_session_id"])
        self.assertEqual("unmatched", unmatched["match_confidence"])
        # Never overwritten -- with no plan session to link to, the only classification
        # left is the speed-derived guess the domain already computed.
        self.assertEqual("aerobic_base", unmatched["adaptation"])
        self.assertEqual("easy", unmatched["cost"])

    def test_planned_session_claimed_by_at_most_one_actual(self):
        plan_sessions = [
            _plan_session("run-only", scheduled_date="2026-08-10", planned_minutes=40, adaptation="threshold", cost="hard")
        ]
        # Listed farther-duration-match first on purpose: the winner must be decided by
        # duration closeness, never by input-list order.
        actuals = [
            _actual_fixture("act-far", date="2026-08-10", duration_minutes=70, adaptation="aerobic_base", cost="easy"),
            _actual_fixture("act-close", date="2026-08-10", duration_minutes=42, adaptation="aerobic_base", cost="easy"),
        ]
        result = context_core._match_actuals_to_plan(actuals, plan_sessions)
        by_id = {a["activity_id"]: a for a in result}

        self.assertEqual("run-only", by_id["act-close"]["planned_session_id"])
        self.assertEqual("probable", by_id["act-close"]["match_confidence"])
        self.assertEqual("threshold", by_id["act-close"]["adaptation"])

        self.assertIsNone(by_id["act-far"]["planned_session_id"])
        self.assertEqual("unmatched", by_id["act-far"]["match_confidence"])
        self.assertEqual("aerobic_base", by_id["act-far"]["adaptation"])  # untouched, never double-claimed

        # Output order must still mirror input (oldest-to-newest) regardless of which
        # one won the claim.
        self.assertEqual(["act-far", "act-close"], [a["activity_id"] for a in result])

    def test_large_duration_gap_does_not_become_an_identity_claim(self):
        plan_sessions = [
            _plan_session("run-test-01", scheduled_date="2026-08-10", planned_minutes=42, adaptation="threshold", cost="hard")
        ]
        # 150 actual minutes against a 42-minute plan is a 3.6x ratio -- past anything a
        # warmup/cooldown or a cut-short session could explain.
        actuals = [
            _actual_fixture("act-1", date="2026-08-10", duration_minutes=150, adaptation="aerobic_base", cost="easy")
        ]
        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        probable = result[0]
        self.assertEqual("run-test-01", probable["planned_session_id"])
        self.assertEqual("probable", probable["match_confidence"])
        self.assertEqual("threshold", probable["adaptation"])

    def test_large_duration_gap_among_many_still_only_selects_a_probable_candidate(self):
        plan_sessions = [
            _plan_session("run-a", scheduled_date="2026-08-10", planned_minutes=20),
            _plan_session("run-b", scheduled_date="2026-08-10", planned_minutes=25),
        ]
        # Duration chooses which candidate to show a human; it never upgrades identity.
        actuals = [
            _actual_fixture("act-1", date="2026-08-10", duration_minutes=120, adaptation="aerobic_base", cost="easy")
        ]
        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        probable = result[0]
        self.assertEqual("run-b", probable["planned_session_id"])
        self.assertEqual("probable", probable["match_confidence"])


class OwnershipBackedAttachmentTests(unittest.TestCase):
    """The "owned" tier: the athlete trained the session the product delivered, without
    entering it from the calendar item.

    Every case here is one the provider left unpaired. What the tier may conclude is
    exactly "this session was trained, and this is the activity" -- never how well, which
    the coach reads from the activity's own numbers.
    """

    def test_delivered_session_on_an_unambiguous_day_is_attached(self):
        # The real 2026-08-10 case: the product delivered the test, the watch executed
        # it, and Intervals recorded no pairing at all.
        plan_sessions = [
            _delivered_session(
                "run-test-01",
                scheduled_date="2026-08-10",
                adaptation="threshold",
                cost="hard",
                planned_minutes=42,
            )
        ]
        actuals = [
            _actual_fixture(
                "act-1",
                date="2026-08-10",
                duration_minutes=42,
                adaptation="aerobic_base",
                cost="easy",
            )
        ]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("run-test-01", attached["planned_session_id"])
        self.assertEqual("owned", attached["match_confidence"])
        # Attachment carries the plan's own classification for the same reason a provider
        # identity does: average pace mislabels a test whose warmup and cooldown drag it
        # toward "easy", and that mislabel is what the coach would otherwise read.
        self.assertEqual("threshold", attached["adaptation"])
        self.assertEqual("hard", attached["cost"])

    def test_strength_session_started_outside_the_calendar_item_is_attached(self):
        # The real 2026-08-12 case: strength reaches the calendar as a description, so
        # there is no structured workout for the watch to pair against, ever.
        plan_sessions = [
            _delivered_session(
                "strength-wed-01",
                sport="strength",
                scheduled_date="2026-08-12",
                adaptation="strength",
                body_stress="full",
                cost="hard",
                planned_minutes=60,
            )
        ]
        actuals = [
            _actual_fixture(
                "act-1",
                date="2026-08-12",
                sport="strength",
                duration_minutes=55,
                adaptation="strength",
                body_stress="full",
                cost="moderate",
            )
        ]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("strength-wed-01", attached["planned_session_id"])
        self.assertEqual("owned", attached["match_confidence"])

    def test_training_past_the_prescription_is_still_that_session(self):
        plan_sessions = [_delivered_session("run-easy-01", planned_minutes=30, adaptation="aerobic_base", cost="easy")]
        actuals = [_actual_fixture("act-1", duration_minutes=44, adaptation="aerobic_base", cost="easy")]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("owned", attached["match_confidence"])

    def test_second_activity_of_that_sport_that_day_keeps_both_probable(self):
        # The harmful case this tier must refuse: an extra run on the day of a planned
        # run makes "which one was the session" a question ownership cannot answer.
        plan_sessions = [_delivered_session("run-quality-01", planned_minutes=55)]
        actuals = [
            _actual_fixture("planned-ish", duration_minutes=52),
            _actual_fixture("commute", duration_minutes=25),
        ]

        result = context_core._match_actuals_to_plan(actuals, plan_sessions)

        self.assertTrue(all(actual["match_confidence"] != "owned" for actual in result))
        self.assertEqual({"probable", "unmatched"}, {actual["match_confidence"] for actual in result})

    def test_second_planned_session_of_that_sport_that_day_stays_probable(self):
        plan_sessions = [
            _delivered_session("run-am-01", planned_minutes=40),
            _delivered_session("run-pm-01", planned_minutes=40),
        ]
        actuals = [_actual_fixture("act-1", duration_minutes=40)]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("probable", attached["match_confidence"])

    def test_session_the_product_never_delivered_stays_probable(self):
        # The real 2026-08-11 strength case: nothing was pushed, so there is no ownership
        # to reason from and no relaxation can rescue it.
        plan_sessions = [
            _plan_session("strength-tue-01", sport="strength", planned_minutes=65, adaptation="strength")
        ]
        actuals = [_actual_fixture("act-1", sport="strength", duration_minutes=60, adaptation="strength")]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("probable", attached["match_confidence"])

    def test_duration_far_short_of_the_prescription_stays_probable(self):
        plan_sessions = [_delivered_session("run-quality-01", planned_minutes=55)]
        actuals = [_actual_fixture("act-1", duration_minutes=15)]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("probable", attached["match_confidence"])

    def test_duration_far_beyond_the_prescription_stays_probable(self):
        plan_sessions = [_delivered_session("run-easy-01", planned_minutes=30)]
        actuals = [_actual_fixture("act-1", duration_minutes=130)]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("probable", attached["match_confidence"])

    def test_unreported_duration_stays_probable(self):
        plan_sessions = [_delivered_session("run-easy-01", planned_minutes=30)]
        actuals = [_actual_fixture("act-1", duration_minutes=None)]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("probable", attached["match_confidence"])

    def test_activity_the_provider_paired_elsewhere_stays_probable(self):
        # A pairing that does not name our event is contrary evidence, not silence.
        plan_sessions = [_delivered_session("run-easy-01", planned_minutes=30)]
        actuals = [_actual_fixture("act-1", duration_minutes=30, paired_event_id="event-someone-elses")]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("probable", attached["match_confidence"])

    def test_a_different_sport_never_attaches_to_the_planned_session(self):
        # Planned a run, lifted instead: a plan conflict the coach resolves, not a match.
        plan_sessions = [_delivered_session("run-easy-01", planned_minutes=30)]
        actuals = [_actual_fixture("act-1", sport="strength", duration_minutes=30, adaptation="strength")]

        attached = context_core._match_actuals_to_plan(actuals, plan_sessions)[0]

        self.assertEqual("unmatched", attached["match_confidence"])
        self.assertIsNone(attached["planned_session_id"])
        self.assertEqual("strength", attached["adaptation"])

    def test_provider_identity_still_wins_over_ownership(self):
        plan_sessions = [
            _delivered_session("run-a", scheduled_date="2026-08-10", external_id="event-123", planned_minutes=40),
            _delivered_session("run-b", scheduled_date="2026-08-11", external_id="event-456", planned_minutes=40),
        ]
        actuals = [
            _actual_fixture("act-1", date="2026-08-10", duration_minutes=40),
            _actual_fixture("act-2", date="2026-08-11", duration_minutes=40, paired_event_id="event-456"),
        ]

        by_id = {
            actual["activity_id"]: actual
            for actual in context_core._match_actuals_to_plan(actuals, plan_sessions)
        }

        self.assertEqual("matched", by_id["act-2"]["match_confidence"])
        self.assertEqual("run-b", by_id["act-2"]["planned_session_id"])
        self.assertEqual("owned", by_id["act-1"]["match_confidence"])
        self.assertEqual("run-a", by_id["act-1"]["planned_session_id"])


if __name__ == "__main__":
    unittest.main()
