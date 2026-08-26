from __future__ import annotations

import base64
import contextlib
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import context_core, source_intervals
from garmin_coach_loop.context_core import _measured_number
from garmin_coach_loop.prescription import render_prescription
from garmin_coach_loop.context_builder import (
    ALL_DAYS,
    DEFAULT_SESSION_MINUTES,
    DEFAULT_SOURCE,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    BuildWindow,
    ContextBuildError,
    ContextRequest,
    build_context,
    build_context_with_domain,
)
from garmin_coach_loop.source_intervals import (
    USER_AGENT,
    IntervalsCredentials,
    fetch_domain,
    fetch_recent_activity,
    resolve_credentials,
)
from garmin_coach_loop.reconcile import apply_reconciliation
from garmin_coach_loop.store import init_store


# Fixed "wall clock" and "as of" moment, mirroring test_context_builder.py's convention,
# so freshness/coverage/trend windows are fully deterministic regardless of real time.
NOW = dt.datetime(2026, 1, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
AS_OF_RAW = "2026-01-08T20:00:00+08:00"

# Synthetic credentials only -- never real key material. Used to simulate "a key is
# present" without touching process env or the repo-root .env (which, in this worktree,
# holds real credentials). Positional, not keyword, so this line can't look like a
# "key = value" secret assignment to naive scanners (see scripts/check_repo_safety.py).
FAKE_CREDENTIALS = IntervalsCredentials("synthetic-test-key-not-real", "i0")  # (api_key, athlete_id)

ATHLETE_BASELINE_FIXTURE: dict[str, Any] = {
    "threshold_pace_sec_per_km": 370,
    "max_hr": 188,
    "easy_hr_ceiling": 150,
    "longest_recent_run_km": 12.0,
    "weekly_volume_km_4wk_avg": 32.0,
    "max_session_minutes": 75,
    "strength_loads": [
        {"exercise": "back squat", "load_kg": 70.0, "assist_kg": None, "scheme": "4x6"},
    ],
}

PLAN_FIXTURE: dict[str, Any] = {
    "schema_version": "1.0",
    "plan_id": "intervals-test-plan-001",
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
        "outlook": [
            {
                "week_start": "2026-01-12",
                "intent": "Week of 2026-01-12: one quality exposure more than the week before.",
                "key_sessions": ["One quality run", "One long easy run", "Two strength sessions"],
                "relation_to_primary": "Builds the primary adaptation.",
            },
            {
                "week_start": "2026-01-19",
                "intent": "Week of 2026-01-19: the same shape, volume unchanged.",
                "key_sessions": ["One quality run", "One long easy run", "Two strength sessions"],
                "relation_to_primary": "Holds the primary adaptation.",
            },
            {
                "week_start": "2026-01-26",
                "intent": "Week of 2026-01-26: reduced volume and the cycle's own measurement.",
                "key_sessions": ["One quality run", "One long easy run", "Two strength sessions"],
                "relation_to_primary": "Measures the primary adaptation.",
            },
        ],
    },
    "week": {
        "start": "2026-01-05",
        "intent": "Protect Thursday quality while maintaining two strength exposures",
        "sessions": [
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
                "execution": {
                    "publish_supported": True,
                    "external_id": "event-quality-2002",
                    "delivery_state": "intervals_accepted",
                },
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

# The one running session in the fixture is the cycle's quality anchor, so it prescribes
# reps rather than the single open block `_default_plan` gives an ordinary easy run. That
# is what makes it the session per-segment execution is read for (issue #233): a warm-up,
# four repeats and a cool-down is precisely the shape a whole-activity average cannot
# report, and a run planned as one continuous effort is fully reported by the average
# `recent_actuals` already carries.
for _session in PLAN_FIXTURE["week"]["sessions"]:
    if _session["session_id"] != "run-quality-01":
        continue
    _session["plan"] = {
        "kind": "time_axis",
        "name": "Fixture quality run",
        "steps": [
            {"kind": "work", "name": "Warm-up",
             "duration": {"kind": "time", "seconds": 600},
             "target": {"kind": "open"}},
            {"kind": "repeat", "repetitions": 4, "steps": [
                {"kind": "work", "name": "Rep",
                 "duration": {"kind": "time", "seconds": 180},
                 "target": {"kind": "open"}},
                {"kind": "work", "name": "Recovery",
                 "duration": {"kind": "time", "seconds": 180},
                 "target": {"kind": "open"}},
            ]},
            {"kind": "work", "name": "Cool-down",
             "duration": {"kind": "time", "seconds": 600},
             "target": {"kind": "open"}},
        ],
    }
    _session["prescription"] = render_prescription(_session["plan"])


def _make_plan() -> dict[str, Any]:
    return copy.deepcopy(PLAN_FIXTURE)


def _make_request(**overrides: Any) -> ContextRequest:
    fields: dict[str, Any] = {
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
    fields.update(overrides)
    return ContextRequest(**fields)


# Three activities inside the 42-day window (2025-11-28..2026-01-08), including one
# older than 14 days, plus one far outside it to prove both sides of date filtering.
# match the live 2026-08-10 verification GET against the real account (id, type,
# start_date_local, moving_time, distance, average_speed, total_elevation_gain, feel).
# The Run's 145.0m total_elevation_gain mirrors a real dogfood gap: a 6.15km time trial
# with 145m of climb had its threshold pace systematically underestimated because this
# field was not read at all before -- see source_intervals._fetch_activities.
# Says "remove this key entirely", which is a different instruction from "set it to
# null" -- the two are different provider answers and RecordedIndoorsTests holds them
# apart.
_ABSENT = object()


ACTIVITIES_PAYLOAD = [
    {
        "id": "i2001",
        "type": "WeightTraining",
        "start_date_local": "2026-01-05T18:00:00",
        "moving_time": 3300,
        "distance": None,
        "average_speed": 0.0,
        "average_heartrate": 118,
        "feel": 4,
        # total_elevation_gain intentionally absent -- strength has no elevation; a
        # missing key must map to None, never a fabricated 0.
    },
    {
        "id": "i2002",
        "type": "Run",
        "start_date_local": "2026-01-08T07:00:00",
        "moving_time": 1800,
        "distance": 4870.0,
        "average_speed": 2.7,
        "average_heartrate": 151,
        "paired_event_id": "event-quality-2002",
        "total_elevation_gain": 145.0,
        "feel": 3,
    },
    {
        "id": "i1999",
        "type": "WeightTraining",
        "start_date_local": "2025-12-01T18:00:00",
        "moving_time": 3000,
        "distance": None,
        "average_speed": 0.0,
        "average_heartrate": 110,
    },
    {
        "id": "i1998",
        "type": "WeightTraining",
        "start_date_local": "2025-11-20T18:00:00",
        "moving_time": 3000,
        "distance": None,
        "average_speed": 0.0,
        "average_heartrate": 110,
    },
]

# Six of seven days present in the 7-day window (2026-01-02..2026-01-08), matching the
# confirmed live field names: id (the date), sleepScore, hrv, restingHR.
WELLNESS_PAYLOAD = [
    {"id": "2026-01-02", "sleepScore": 70, "hrv": 45, "restingHR": 52},
    {"id": "2026-01-03", "sleepScore": 72, "hrv": 46, "restingHR": 51},
    {"id": "2026-01-04", "sleepScore": 68, "hrv": 44, "restingHR": 53},
    {"id": "2026-01-06", "sleepScore": 75, "hrv": 50, "restingHR": 50},
    {"id": "2026-01-07", "sleepScore": 78, "hrv": 52, "restingHR": 49},
    {"id": "2026-01-08", "sleepScore": 80, "hrv": 55, "restingHR": 48},
]

# What this account's wellness rows actually look like right now (verified live
# 2026-08-10): Garmin's health feed is not flowing into intervals.icu yet, so every
# recovery field but restingHR (and that, on only one of two rows) is null.
EMPTY_WELLNESS_PAYLOAD = [
    {"id": "2026-01-07", "sleepScore": None, "hrv": None, "restingHR": None, "sleepSecs": None},
    {"id": "2026-01-08", "sleepScore": None, "hrv": None, "restingHR": None, "sleepSecs": None},
]


def _fake_fetch(
    activities_payload: list[dict[str, Any]],
    wellness_payload: list[dict[str, Any]],
    segments_payload: dict[str, Any] | None = None,
    sport_settings_payload: list[dict[str, Any]] | None = None,
):
    """Fake the four reads a build makes. ``segments_payload`` defaults to an
    unanalyzed activity -- no segments, which is what the provider returns for most
    activities and keeps every pre-existing test's expectations unchanged.
    ``sport_settings_payload`` defaults to an empty list -- read successfully, no Run
    entry -- so a fixture that never mentions it stays a single-source, no-divergence
    build for every pre-existing test."""

    def fetch(request: urllib.request.Request) -> bytes:
        # Matched on the suffix, not a substring: the host itself is intervals.icu,
        # so "/intervals" appears in the "https://intervals.icu" of every URL.
        if request.full_url.endswith("/intervals"):
            return json.dumps(segments_payload or {"icu_intervals": []}).encode("utf-8")
        if "/activities" in request.full_url:
            return json.dumps(activities_payload).encode("utf-8")
        if "/wellness" in request.full_url:
            return json.dumps(wellness_payload).encode("utf-8")
        if request.full_url.endswith("/sport-settings"):
            return json.dumps(sport_settings_payload or []).encode("utf-8")
        raise AssertionError(f"unexpected intervals.icu URL in test: {request.full_url}")

    return fetch


class IntervalsSourceHappyPathTests(unittest.TestCase):
    def test_happy_path_maps_activities_and_wellness_into_a_valid_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())

            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )

            self.assertEqual("passed", report["status"], report)
            self.assertEqual([], report["validation"]["errors"])
            context = report["context"]

            # Exactly the intervals source plus the always-present state-store source.
            source_names = {entry["source"] for entry in context["sources"]}
            self.assertEqual({"intervals-icu-api", "coach-loop-state-store"}, source_names)
            intervals_entry = next(e for e in context["sources"] if e["source"] == "intervals-icu-api")
            self.assertEqual("direct_rest_readonly", intervals_entry["mode"])
            self.assertEqual("passed", intervals_entry["doctor_status"])
            self.assertEqual("2026-01-08", intervals_entry["data_through"])

            # A live API read is "fresh" on success regardless of record age.
            self.assertEqual("fresh", context["freshness"]["activities"])
            self.assertEqual("fresh", context["freshness"]["recovery"])

            self.assertEqual(ATHLETE_BASELINE_FIXTURE, context["athlete_baseline"])

            # The provider is still read over 42 days, and the context still says so.
            # What it *reports* session by session starts at the review horizon (issue
            # #233), so the 2025-12-01 activity is inside the read and outside the
            # rows; the 2025-11-20 one is outside both.
            self.assertEqual(
                "2025-12-29", context["review_frame"]["detail_horizon_start"]
            )
            self.assertEqual(2, len(context["recent_actuals"]))
            by_id = {a["activity_id"]: a for a in context["recent_actuals"]}
            self.assertIn("intervals:i2001", by_id)
            self.assertIn("intervals:i2002", by_id)
            self.assertNotIn("intervals:i1999", by_id)
            self.assertNotIn("intervals:i1998", by_id)
            # The 42-day read is what baseline_evidence was computed over, and it names
            # the span rather than leaving it to be inferred from the rows above.
            windows = {
                (row["window_start"], row["window_end"])
                for row in context["baseline_evidence"]
                if row.get("window_start")
            }
            self.assertEqual({("2025-11-28", "2026-01-08")}, windows)

            # PLAN_FIXTURE's only session is "run-quality-01" (running, 2026-01-08) --
            # no strength session exists anywhere in the plan, so the strength activity
            # has zero same-day/same-sport candidates and must stay unmatched.
            strength = by_id["intervals:i2001"]
            self.assertEqual("strength", strength["sport"])
            self.assertEqual("strength", strength["adaptation"])
            # The sport is stated; region and cost are not, and are not guessed from the
            # record (issue #256). What the coach reads instead is on this same row:
            # duration, average heart rate, the stated feel and the session_label.
            self.assertIsNone(strength["body_stress"])
            self.assertIsNone(strength["cost"])
            self.assertEqual(55, strength["duration_minutes"])
            # No elevation concept on a strength session at all, so the key is
            # omitted rather than null (issue #240 §3) -- and never a fabricated 0.
            self.assertNotIn("elevation_gain_m", strength)
            self.assertEqual(4, strength["subjective_feel"])
            self.assertEqual("unmatched", strength["match_confidence"])
            self.assertIsNone(strength["planned_session_id"])

            # The running activity carries the external identity of run-quality-01,
            # so it must match and adopt the plan's own classification
            # (threshold/hard) instead of the speed-derived guess (30 minutes at
            # average_speed=2.7 m/s would otherwise classify as aerobic_base/moderate).
            running = by_id["intervals:i2002"]
            self.assertEqual("running", running["sport"])
            self.assertEqual("threshold", running["adaptation"])
            self.assertEqual("lower", running["body_stress"])
            self.assertEqual("hard", running["cost"])
            self.assertEqual(30, running["duration_minutes"])
            self.assertEqual(145.0, running["elevation_gain_m"])
            self.assertEqual(3, running["subjective_feel"])
            self.assertEqual("matched", running["match_confidence"])
            self.assertEqual("run-quality-01", running["planned_session_id"])
            self.assertEqual("event-quality-2002", running["paired_event_id"])
            self.assertEqual(4.87, running["distance_km"])
            self.assertEqual(370, running["average_pace_sec_per_km"])
            self.assertEqual(151.0, running["average_hr"])

            # last_observed is the newest date each field carried a real value inside
            # the window -- 2026-01-08 for all three here, since WELLNESS_PAYLOAD's
            # last row (only 2026-01-05 is missing from the window) fills every field.
            self.assertEqual(
                {"observed_days": 6, "expected_days": 7, "status": "partial", "last_observed": "2026-01-08"},
                context["coverage"]["sleep"],
            )
            self.assertEqual(
                {"observed_days": 6, "expected_days": 7, "status": "partial", "last_observed": "2026-01-08"},
                context["coverage"]["hrv"],
            )
            self.assertEqual(
                {"observed_days": 6, "expected_days": 7, "status": "partial", "last_observed": "2026-01-08"},
                context["coverage"]["resting_hr"],
            )

            trends = context["recovery_trends"]
            self.assertEqual("within_baseline", trends["sleep"]["status"])
            self.assertEqual("above_baseline", trends["hrv"]["status"])
            self.assertEqual("within_baseline", trends["resting_hr"]["status"])
            for domain in ("sleep", "hrv", "resting_hr"):
                self.assertEqual(6, trends[domain]["observed_days"])

    def test_wellness_empty_account_yields_unknown_trends_and_still_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())

            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch([], EMPTY_WELLNESS_PAYLOAD),
            ):
                report = build_context(
                    _make_request(extra_unknowns=["fixture-manual-note"]),
                    state_dir=state_dir,
                    source="intervals",
                    now=NOW,
                )

            self.assertEqual("passed", report["status"], report)
            self.assertEqual([], report["validation"]["errors"])
            context = report["context"]

            # Rows exist but every value is null: the feed is silent, and claiming
            # "fresh" here is exactly the bug this grading replaced. Activities stay
            # fresh (read success is meaningful there); recovery must say failed.
            self.assertEqual("fresh", context["freshness"]["activities"])
            self.assertEqual("failed", context["freshness"]["recovery"])

            self.assertEqual([], context["recent_actuals"])
            for domain in ("sleep", "hrv", "resting_hr"):
                self.assertEqual({"status": "unknown", "observed_days": 0, "expected_days": 7}, context["recovery_trends"][domain])
            # No field ever carried a real value, so last_observed is null everywhere --
            # not the two rows' dates, which would claim an observation that never happened.
            empty_coverage = {"observed_days": 0, "expected_days": 7, "status": "missing", "last_observed": None}
            self.assertEqual(empty_coverage, context["coverage"]["sleep"])
            self.assertEqual(empty_coverage, context["coverage"]["hrv"])
            self.assertEqual(empty_coverage, context["coverage"]["resting_hr"])

            # Never fabricated: honest "missing" unknowns, never silently treated as zero
            # or as evidence of recovery. All three signals are missing here, and all
            # three must say so (issue #95: hrv used to be silently left out).
            self.assertIn("sleep_data_unavailable", context["unknowns"])
            self.assertIn("hrv_data_unavailable", context["unknowns"])
            self.assertIn("resting_hr_unavailable", context["unknowns"])
            self.assertIn("red_flags_not_confirmed", context["unknowns"])
            self.assertIn("fixture-manual-note", context["unknowns"])
            self.assertNotIn("intervals_source_failed", context["unknowns"])


class RecoveryFreshnessGradingTests(unittest.TestCase):
    """freshness.recovery is a mechanical recency grade, never a sufficiency judgment.

    Ladder under test (see source_intervals._recovery_freshness): no field with any
    real value anywhere in the window -> failed; some field's latest real value <=1
    day old -> fresh; some field has a real value but none of them that recent ->
    stale. Before issue #95 a single current signal graded "partial" instead of
    "fresh" -- that tier was this deterministic layer deciding whether one signal is
    *enough* to lean on, a training judgment now left to the coach, who reads it from
    coverage's observed_days and last_observed instead.
    """

    def _context_for(self, wellness_payload: list[dict[str, Any]]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch([], wellness_payload),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )
        self.assertEqual("passed", report["status"], report)
        return report["context"]

    def test_single_current_signal_is_fresh(self):
        # Only restingHR ever carries a value -- the real account's shape on
        # 2026-08-10 -- and it is current. Before issue #95 this graded "partial":
        # one signal was never considered enough for a multi-signal read. Whether one
        # signal is *enough* to lean on is the coach's judgment now, made from
        # coverage; freshness only reports how current the newest signal is.
        payload = [
            {"id": "2026-01-08", "sleepScore": None, "hrv": None, "restingHR": 48},
        ]
        context = self._context_for(payload)
        self.assertEqual("fresh", context["freshness"]["recovery"])
        # The mechanical fact behind the grade: sleep and hrv never carried a real
        # value in the window (null last_observed), restingHR's latest real value is
        # exactly the day this build reports as of.
        self.assertIsNone(context["coverage"]["sleep"]["last_observed"])
        self.assertIsNone(context["coverage"]["hrv"]["last_observed"])
        self.assertEqual("2026-01-08", context["coverage"]["resting_hr"]["last_observed"])

    def test_multi_signal_but_days_old_is_stale(self):
        # All three signals exist but the newest value is 4 days before as-of:
        # evidence exists, it just is not current enough to grade fresh.
        payload = [
            {"id": "2026-01-04", "sleepScore": 70, "hrv": 45, "restingHR": 52},
        ]
        self.assertEqual("stale", self._context_for(payload)["freshness"]["recovery"])

    def test_two_current_signals_are_fresh(self):
        # hrv and restingHR carry yesterday's values; sleep never reports. One current
        # signal is already enough to grade fresh (issue #95); a second one changes
        # nothing about the grade, only about what coverage separately shows for each.
        payload = [
            {"id": "2026-01-07", "sleepScore": None, "hrv": 52, "restingHR": 49},
        ]
        self.assertEqual("fresh", self._context_for(payload)["freshness"]["recovery"])

    def test_one_current_and_one_old_signal_is_fresh(self):
        # hrv is current (today); restingHR's only value is five days old. Before
        # issue #95 this graded "stale": a multi-signal *current* read needed two
        # fresh fields, and having only one counted the same as having none. Freshness
        # no longer counts signals against each other -- one current signal is enough
        # -- and the older restingHR reading stays visible through its own
        # coverage.last_observed rather than dragging the grade down.
        payload = [
            {"id": "2026-01-03", "sleepScore": None, "hrv": None, "restingHR": 52},
            {"id": "2026-01-08", "sleepScore": None, "hrv": 55, "restingHR": None},
        ]
        context = self._context_for(payload)
        self.assertEqual("fresh", context["freshness"]["recovery"])
        self.assertIsNone(context["coverage"]["sleep"]["last_observed"])
        self.assertEqual("2026-01-08", context["coverage"]["hrv"]["last_observed"])
        self.assertEqual("2026-01-03", context["coverage"]["resting_hr"]["last_observed"])

    def test_rows_outside_the_seven_day_window_do_not_count(self):
        # The only row with values sits before the 7-day window (2026-01-02..08):
        # inside the window the feed is silent -> failed.
        payload = [
            {"id": "2025-12-20", "sleepScore": 80, "hrv": 55, "restingHR": 48},
        ]
        self.assertEqual("failed", self._context_for(payload)["freshness"]["recovery"])

    def test_zero_values_are_sentinels_not_signals(self):
        # A 0 restingHR/hrv/sleepScore is a not-worn artifact, not a measurement.
        # It must count nowhere: not freshness, not coverage (including
        # last_observed), not trends.
        payload = [
            {"id": "2026-01-08", "sleepScore": 0, "hrv": 0, "restingHR": 0},
        ]
        context = self._context_for(payload)
        self.assertEqual("failed", context["freshness"]["recovery"])
        for domain in ("sleep", "hrv", "resting_hr"):
            self.assertIsNone(context["coverage"][domain]["last_observed"])


class UnmatchedRunClassificationTests(unittest.TestCase):
    """Unmatched-run intensity is relative to the athlete's own threshold pace.

    PLAN_FIXTURE's baseline threshold is 370 sec/km (6:10). Its only session sits on
    2026-01-08, so a run on any other date has zero same-day candidates and stays
    unmatched -- exactly the case where the pace-derived fallback classification is
    the one that survives into the context.
    """

    def _unmatched_run_for(
        self, average_speed: float, plan: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], list[str]]:
        payload = [
            {
                "id": "i3001",
                "type": "Run",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 1800,
                "distance": average_speed * 1800,
                "average_speed": average_speed,
                "average_heartrate": 150,
                "feel": 3,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, plan if plan is not None else _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(payload, WELLNESS_PAYLOAD),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )
        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        run = next(a for a in context["recent_actuals"] if a["activity_id"] == "intervals:i3001")
        self.assertEqual("unmatched", run["match_confidence"])
        return run, context["unknowns"]

    def test_faster_than_threshold_reads_as_threshold_hard(self):
        # 2.74 m/s = 365 sec/km, faster than the 370 threshold. Under the old
        # absolute bands (hard only at <=360) this athlete's threshold work was
        # misread as moderate.
        run, _ = self._unmatched_run_for(2.74)
        self.assertEqual("threshold", run["adaptation"])
        self.assertEqual("hard", run["cost"])

    def test_within_twelve_percent_of_threshold_is_moderate(self):
        # 2.50 m/s = 400 sec/km: between 105% (388.5) and 112% (414.4) of threshold.
        run, _ = self._unmatched_run_for(2.50)
        self.assertEqual("aerobic_base", run["adaptation"])
        self.assertEqual("moderate", run["cost"])

    def test_slower_than_twelve_percent_over_threshold_is_easy(self):
        # 2.20 m/s = 455 sec/km, well past the 112% band.
        run, _ = self._unmatched_run_for(2.20)
        self.assertEqual("aerobic_base", run["adaptation"])
        self.assertEqual("easy", run["cost"])

    def test_no_threshold_baseline_stays_at_easy_floor_with_note(self):
        # A null threshold means there is nothing to be relative to: the run stays
        # at the floor with an explicit note instead of borrowing absolute bands.
        plan = _make_plan()
        plan["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        run, unknowns = self._unmatched_run_for(2.74, plan=plan)
        self.assertEqual("aerobic_base", run["adaptation"])
        self.assertEqual("easy", run["cost"])
        self.assertIn("run_pace_unclassified_no_baseline:intervals:i3001", unknowns)


class ClassifyRunningBoundaryTests(unittest.TestCase):
    """Band edges of the relative classification, directly against the shared helper."""

    def _classify(self, pace_sec_per_km: float, threshold: int | float | None = 370):
        from garmin_coach_loop.context_core import _classify_running

        notes: list[str] = []
        result = _classify_running(1000.0 / pace_sec_per_km, "test-activity", notes, threshold)
        return result, notes

    def test_just_inside_the_105_percent_band_is_hard(self):
        # 370 * 1.05 = 388.5; 388.4 sits inside the threshold band.
        (adaptation, cost), _ = self._classify(388.4)
        self.assertEqual(("threshold", "hard"), (adaptation, cost))

    def test_just_past_105_percent_is_moderate(self):
        (adaptation, cost), _ = self._classify(388.6)
        self.assertEqual(("aerobic_base", "moderate"), (adaptation, cost))

    def test_just_inside_the_112_percent_band_is_moderate(self):
        # 370 * 1.12 = 414.4; 414.3 sits inside the moderate band.
        (adaptation, cost), _ = self._classify(414.3)
        self.assertEqual(("aerobic_base", "moderate"), (adaptation, cost))

    def test_just_past_112_percent_is_easy(self):
        (adaptation, cost), _ = self._classify(414.5)
        self.assertEqual(("aerobic_base", "easy"), (adaptation, cost))

    def test_non_positive_threshold_stays_unclassified(self):
        (adaptation, cost), notes = self._classify(388.4, threshold=0)
        self.assertEqual(("aerobic_base", "easy"), (adaptation, cost))
        self.assertEqual(["run_pace_unclassified_no_baseline:test-activity"], notes)


class IntervalsSourceRequestShapeTests(unittest.TestCase):
    def test_outgoing_requests_carry_custom_user_agent_and_basic_auth(self):
        captured: list[urllib.request.Request] = []

        def capturing_fetch(request: urllib.request.Request) -> bytes:
            captured.append(request)
            if "/activities" in request.full_url:
                return json.dumps([]).encode("utf-8")
            return json.dumps([]).encode("utf-8")

        window = BuildWindow(
            as_of=dt.datetime(2026, 1, 8, 20, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            resolved_now=NOW,
            now_iso="2026-01-08T12:00:00+00:00",
            window_start=dt.date(2026, 1, 2),
            window_end=dt.date(2026, 1, 8),
            window14_start=dt.date(2025, 12, 26),
            window14_end=dt.date(2026, 1, 8),
            window42_start=dt.date(2025, 11, 28),
            window42_end=dt.date(2026, 1, 8),
        )

        fetch_domain(FAKE_CREDENTIALS, window, fetch=capturing_fetch, baseline_max_hr=188)

        # activities, wellness, and sport-settings -- no running activities in this
        # empty fixture, so no per-activity segment reads join them. The sport-settings
        # read is here because a baseline max HR was stated: without one it is not
        # requested at all, since nothing would read its answer (see
        # RunSportSettingsMaxHrTests).
        self.assertEqual(3, len(captured))
        activities_request = next(request for request in captured if "/activities" in request.full_url)
        self.assertIn("oldest=2025-11-28", activities_request.full_url)
        self.assertIn("newest=2026-01-08", activities_request.full_url)
        for request in captured:
            self.assertEqual("GET", request.get_method())
            self.assertEqual(USER_AGENT, request.get_header("User-agent"))
            auth_header = request.get_header("Authorization")
            self.assertIsNotNone(auth_header)
            self.assertTrue(auth_header.startswith("Basic "))
            decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode("ascii")
            self.assertEqual(f"API_KEY:{FAKE_CREDENTIALS.api_key}", decoded)
            self.assertTrue(request.full_url.startswith(f"https://intervals.icu/api/v1/athlete/{FAKE_CREDENTIALS.athlete_id}"))


class SourceSelectionPolicyTests(unittest.TestCase):
    """No "auto": a failure on the selected source is always a block, never a fallback,
    and "intervals" is the default -- proven by actually omitting --source."""

    def test_default_source_is_intervals_not_personal_os(self):
        self.assertEqual("intervals", DEFAULT_SOURCE)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch("garmin_coach_loop.source_intervals.resolve_credentials", return_value=None):
                # source= is omitted entirely -- must fall through to DEFAULT_SOURCE and
                # therefore fail on the *intervals* credential check, not silently
                # succeed via personal-os.
                with self.assertRaisesRegex(ContextBuildError, "intervals credentials not configured"):
                    build_context(_make_request(), state_dir=state_dir, now=NOW)

    def test_default_source_runs_the_full_intervals_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch([], EMPTY_WELLNESS_PAYLOAD),
            ):
                report = build_context(_make_request(), state_dir=state_dir, now=NOW)  # source= omitted
            self.assertEqual("passed", report["status"], report)
            source_names = {entry["source"] for entry in report["context"]["sources"]}
            self.assertIn("intervals-icu-api", source_names)

    def test_source_intervals_network_error_is_blocked_with_no_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())

            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals.fetch_domain",
                side_effect=ContextBuildError("simulated network failure"),
            ):
                with self.assertRaisesRegex(ContextBuildError, "simulated network failure"):
                    build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)

    def test_source_intervals_with_no_key_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())

            with mock.patch("garmin_coach_loop.source_intervals.resolve_credentials", return_value=None):
                with self.assertRaisesRegex(ContextBuildError, "credentials not configured"):
                    build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)

    def test_source_intervals_never_reads_personal_os_activity_domain(self):
        """A machine with no health.db / personal-os installation must still be able to
        run --source intervals end to end (the acceptance bar for this refactor).
        source_personal_os's activity/recovery domain (fetch_domain) is imported and
        called lazily by context_builder, only inside the "personal-os" branch, so it
        is never touched here -- proven behaviorally: if the module happens to already
        be imported in this test process (e.g. by test_context_builder.py running
        first in the same `unittest discover` process), poison fetch_domain so any
        accidental call blows up loudly instead of quietly reading a real local path.

        resolve_health_db_path is deliberately NOT poisoned (issue #37): the two
        standalone evidence groups -- strength_execution and recovery_signals (slice
        2), which now share this one resolved path -- probe it on every build
        regardless of --source, since local evidence is meant to layer on top of
        intervals as the required base source, not only on top of
        --source personal-os. It is pure (env/CLI lookup, no file I/O), so calling it
        does not violate the "no personal-os installation required" bar. With none of
        HEALTH_DB_ENV_VARS set (cleared below), it resolves to None here regardless,
        so fetch_strength_execution and fetch_recovery_signals -- both poisoned
        alongside fetch_domain -- are still never actually called, and both groups
        stay unconfigured.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())

            def _forbidden(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("source_personal_os must never be read by --source intervals")

            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch(
                        "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "garmin_coach_loop.source_intervals._default_fetch",
                        new=_fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD),
                    )
                )
                try:
                    import garmin_coach_loop.source_personal_os as personal_os_module
                except ImportError:
                    personal_os_module = None
                if personal_os_module is not None:
                    stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
                    for name in personal_os_module.HEALTH_DB_ENV_VARS:
                        os.environ.pop(name, None)
                    stack.enter_context(
                        mock.patch.object(personal_os_module, "fetch_domain", side_effect=_forbidden)
                    )
                    stack.enter_context(
                        mock.patch.object(personal_os_module, "fetch_strength_execution", side_effect=_forbidden)
                    )
                    stack.enter_context(
                        mock.patch.object(personal_os_module, "fetch_recovery_signals", side_effect=_forbidden)
                    )

                report = build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)

            self.assertEqual("passed", report["status"], report)
            self.assertIsNone(report["context"]["strength_execution"])
            self.assertIsNone(report["context"]["recovery_signals"])


class ResolveCredentialsTests(unittest.TestCase):
    """resolve_credentials's precedence: process env, then per-user config
    (~/.config/garmin-coach-loop/.env), then the repo-root .env (compatibility only).
    Every test pins both file paths explicitly to a throwaway temp location -- this
    worktree's real repo-root .env carries live credentials, and letting any of these
    tests fall through to the real default path would make them non-deterministic.
    """

    def test_process_env_takes_precedence_over_user_config_and_repo_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_config = Path(tmp) / "user-config.env"
            user_config.write_text(
                "INTERVALS_ICU_API_KEY=from-user-config-not-real\nINTERVALS_ICU_ATHLETE_ID=i-user-config\n",
                encoding="utf-8",
            )
            repo_env = Path(tmp) / "repo-root.env"
            repo_env.write_text(
                "INTERVALS_ICU_API_KEY=from-repo-env-not-real\nINTERVALS_ICU_ATHLETE_ID=i-repo-env\n",
                encoding="utf-8",
            )
            credentials = resolve_credentials(
                env={"INTERVALS_ICU_API_KEY": "from-process-env-not-real", "INTERVALS_ICU_ATHLETE_ID": "i-process-env"},
                user_config_env_file=user_config,
                repo_env_file=repo_env,
            )
            self.assertEqual(IntervalsCredentials("from-process-env-not-real", "i-process-env"), credentials)

    def test_user_config_takes_precedence_over_repo_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_config = Path(tmp) / "user-config.env"
            user_config.write_text(
                "# comment\nINTERVALS_ICU_API_KEY=from-user-config-not-real\nINTERVALS_ICU_ATHLETE_ID=i-user-config\n",
                encoding="utf-8",
            )
            repo_env = Path(tmp) / "repo-root.env"
            repo_env.write_text(
                "INTERVALS_ICU_API_KEY=from-repo-env-not-real\nINTERVALS_ICU_ATHLETE_ID=i-repo-env\n",
                encoding="utf-8",
            )
            credentials = resolve_credentials(env={}, user_config_env_file=user_config, repo_env_file=repo_env)
            self.assertEqual(IntervalsCredentials("from-user-config-not-real", "i-user-config"), credentials)

    def test_falls_back_to_repo_env_when_nothing_else_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_config = Path(tmp) / "user-config.env"  # does not exist
            repo_env = Path(tmp) / "repo-root.env"
            repo_env.write_text(
                "INTERVALS_ICU_API_KEY=from-repo-env-not-real\nINTERVALS_ICU_ATHLETE_ID=i-repo-env\n",
                encoding="utf-8",
            )
            credentials = resolve_credentials(env={}, user_config_env_file=user_config, repo_env_file=repo_env)
            self.assertEqual(IntervalsCredentials("from-repo-env-not-real", "i-repo-env"), credentials)

    def test_missing_credentials_everywhere_resolves_to_none_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_config = Path(tmp) / "user-config.env"  # does not exist
            repo_env = Path(tmp) / "repo-root.env"  # does not exist
            self.assertIsNone(
                resolve_credentials(env={}, user_config_env_file=user_config, repo_env_file=repo_env)
            )

    def test_per_key_resolution_can_mix_tiers(self):
        """One credential can legitimately come from a different tier than the other --
        precedence is evaluated per key, not "all or nothing" per file."""
        with tempfile.TemporaryDirectory() as tmp:
            user_config = Path(tmp) / "user-config.env"
            user_config.write_text("INTERVALS_ICU_ATHLETE_ID=i-user-config\n", encoding="utf-8")
            repo_env = Path(tmp) / "repo-root.env"
            repo_env.write_text("INTERVALS_ICU_API_KEY=from-repo-env-not-real\n", encoding="utf-8")
            credentials = resolve_credentials(env={}, user_config_env_file=user_config, repo_env_file=repo_env)
            self.assertEqual(IntervalsCredentials("from-repo-env-not-real", "i-user-config"), credentials)


# --------------------------------------------------------------------------------------
# Issue #111: deliberate activity-type vocabulary, fail-closed provider-shape guard,
# malformed-row counting, and cross-auth-scheme parity for the shared adapter.
# --------------------------------------------------------------------------------------


class ActivityTypeVocabularyTests(unittest.TestCase):
    """_map_activity_sport is a membership test against explicit, documented
    vocabularies -- never a substring or prefix test. The old code matched with
    ``str(activity_type).lower().startswith("run")``, which silently excluded
    "TrailRun" (it starts with "t", not "run") from recent_actuals; a completed trail
    run disappeared from training history with no trace. These first exercise the pure
    function directly, then prove the fix end to end: a previously-dropped type reaches
    recent_actuals, an unrelated sport stays excluded, and every exclusion becomes
    observable instead of a silent drop.
    """

    def _map(self, activity_type: Any) -> str | None:
        from garmin_coach_loop.source_intervals import _map_activity_sport

        return _map_activity_sport(activity_type)

    def test_run_maps_to_running(self):
        self.assertEqual("running", self._map("Run"))

    def test_trailrun_maps_to_running(self):
        self.assertEqual("running", self._map("TrailRun"))

    def test_virtualrun_maps_to_running(self):
        # The third running-family member of the documented vocabulary (Strava API v3
        # SportType enum, which intervals.icu's `type` field mirrors -- see
        # source_intervals._RUNNING_ACTIVITY_TYPES) -- tested explicitly per the
        # acceptance criteria, not merely implied by Run/TrailRun passing.
        self.assertEqual("running", self._map("VirtualRun"))

    def test_weighttraining_still_maps_to_strength(self):
        self.assertEqual("strength", self._map("WeightTraining"))

    def test_mapping_is_case_and_whitespace_insensitive(self):
        self.assertEqual("running", self._map(" TRAILRUN "))
        self.assertEqual("strength", self._map("weightTRAINING"))

    def test_an_unrelated_sport_is_excluded(self):
        # A real, documented Strava/intervals.icu type this product simply does not
        # act on -- must stay excluded, never guessed into any vocabulary member.
        self.assertIsNone(self._map("AlpineSki"))

    def test_each_cross_training_family_maps_to_its_own_sport(self):
        for raw, sport in (
            ("Ride", "cycling"),
            ("VirtualRide", "cycling"),
            ("MountainBikeRide", "cycling"),
            ("GravelRide", "cycling"),
            ("Swim", "swimming"),
            ("OpenWaterSwim", "swimming"),
            ("Hike", "hiking"),
            ("Rowing", "rowing"),
            ("VirtualRow", "rowing"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(sport, self._map(raw))

    def test_the_deliberate_exclusions_hold(self):
        # Named in _CYCLING_ACTIVITY_TYPES' comment: a motor changes what an e-bike
        # ride's duration means, and a Walk is not a Hike. Both stay observable through
        # the activity_type_excluded note rather than silently mapped.
        self.assertIsNone(self._map("EBikeRide"))
        self.assertIsNone(self._map("Walk"))

    def test_unknown_or_malformed_type_is_excluded_not_guessed(self):
        self.assertIsNone(self._map("SomeFutureProviderType"))
        self.assertIsNone(self._map(None))
        self.assertIsNone(self._map(123))

    def _context_for(self, activities_payload: list[Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(activities_payload, WELLNESS_PAYLOAD),
            ):
                report = build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)
        self.assertEqual("passed", report["status"], report)
        return report["context"]

    def test_trailrun_paired_to_a_session_reaches_matched_confidence(self):
        # Acceptance: "A TrailRun paired to a current session must participate in
        # matching/reconciliation." Before the fix this activity's mapped sport was
        # None, so _build_recent_actuals dropped the row before it ever reached
        # context_core._match_actuals_to_plan -- the pairing logic never even saw it.
        # event-quality-2002 is PLAN_FIXTURE's run-quality-01 external_id (see the
        # module-level PLAN_FIXTURE above).
        payload = [
            {
                "id": "i6001",
                "type": "TrailRun",
                "start_date_local": "2026-01-08T07:00:00",
                "moving_time": 1800,
                "distance": 4870.0,
                "average_speed": 2.7,
                "average_heartrate": 151,
                "paired_event_id": "event-quality-2002",
                "feel": 3,
            }
        ]
        context = self._context_for(payload)
        self.assertEqual(1, len(context["recent_actuals"]))
        trail_run = context["recent_actuals"][0]
        self.assertEqual("running", trail_run["sport"])
        self.assertEqual("matched", trail_run["match_confidence"])
        self.assertEqual("run-quality-01", trail_run["planned_session_id"])

    def test_virtualrun_appears_in_recent_actuals(self):
        payload = [
            {
                "id": "i6002",
                "type": "VirtualRun",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 1800,
                "distance": 4500.0,
                "average_speed": 2.5,
                "average_heartrate": 148,
            }
        ]
        context = self._context_for(payload)
        self.assertEqual(1, len(context["recent_actuals"]))
        self.assertEqual("running", context["recent_actuals"][0]["sport"])

    def test_unrelated_sport_is_excluded_and_observable_in_unknowns(self):
        payload = [
            {
                "id": "i6003",
                "type": "AlpineSki",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 3600,
                "distance": 30000.0,
                "average_speed": 8.3,
                "average_heartrate": 140,
            }
        ]
        context = self._context_for(payload)
        # Still excluded from training history -- this product has nothing to say
        # about a ski day -- but no longer a silent drop: the exclusion itself is
        # now a fact the coach can see.
        self.assertEqual([], context["recent_actuals"])
        self.assertIn("activity_type_excluded:AlpineSki", context["unknowns"])

    def test_a_cross_training_actual_arrives_real_and_unclassified(self):
        # A Ride is a real actual now: it reaches recent_actuals as cycling, pairs by
        # date and sport like anything else, and states nothing the builder did not
        # measure -- the running-pace bands read against a run threshold say nothing
        # about a ride, so its classification fields are null rather than borrowed.
        # The passed status inside _context_for is the whole-schema proof: a context
        # carrying a null-classified actual validates end to end.
        payload = [
            {
                "id": "i6005",
                "type": "Ride",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 3600,
                "distance": 30000.0,
                "average_speed": 8.3,
                "average_heartrate": 140,
                "feel": 2,
            }
        ]
        context = self._context_for(payload)
        self.assertEqual(1, len(context["recent_actuals"]))
        ride = context["recent_actuals"][0]
        self.assertEqual("cycling", ride["sport"])
        self.assertIsNone(ride["adaptation"])
        self.assertIsNone(ride["body_stress"])
        self.assertIsNone(ride["cost"])
        self.assertEqual(60, ride["duration_minutes"])
        self.assertEqual(30.0, ride["distance_km"])
        self.assertEqual(140, ride["average_hr"])
        self.assertNotIn("activity_type_excluded:Ride", context["unknowns"])
        # And no pace note either: nothing tried to classify it.
        self.assertFalse(
            any(note.startswith("run_pace_") and "i6005" in note for note in context["unknowns"])
        )

    def test_unrecognized_type_is_excluded_and_observable_the_same_way(self):
        # A genuinely unknown/changed type (a future provider addition, a typo) is
        # observable through the identical mechanism as a known-but-unrelated sport --
        # _map_activity_sport deliberately does not need to tell the two apart.
        payload = [
            {
                "id": "i6004",
                "type": "SomeFutureProviderType",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 1800,
                "distance": None,
                "average_speed": 0.0,
                "average_heartrate": 120,
            }
        ]
        context = self._context_for(payload)
        self.assertEqual([], context["recent_actuals"])
        self.assertIn("activity_type_excluded:SomeFutureProviderType", context["unknowns"])

    def test_one_note_per_distinct_excluded_type_not_per_row(self):
        # Three ski days in the window must not flood `unknowns` with three
        # near-identical notes -- one distinct-type note is the useful, stable signal.
        payload = [
            {
                "id": f"i600{i}",
                "type": "AlpineSki",
                "start_date_local": f"2026-01-0{i}T07:00:00",
                "moving_time": 3600,
                "distance": 30000.0,
                "average_speed": 8.3,
                "average_heartrate": 140,
            }
            for i in (2, 3, 4)
        ]
        context = self._context_for(payload)
        matches = [u for u in context["unknowns"] if u == "activity_type_excluded:AlpineSki"]
        self.assertEqual(1, len(matches))


class SessionLabelTests(unittest.TestCase):
    """A strength session's own name reaches recent_actuals as session_label -- the one
    thing the provider knows about a strength session that no exercise, set or rep ever
    accompanies (see source_intervals._build_recent_actuals). A run's session_label stays
    None unconditionally: the product derives everything it needs about a run from its
    numbers, and a run's own name commonly carries location text a coach has no use for.
    """

    def _label(self, raw_name: Any) -> str | None:
        from garmin_coach_loop.source_intervals import _session_label

        return _session_label(raw_name)

    def test_a_normal_name_passes_through(self):
        self.assertEqual("chest day", self._label("chest day"))

    def test_missing_name_is_none(self):
        self.assertIsNone(self._label(None))

    def test_blank_name_is_none(self):
        self.assertIsNone(self._label("   "))

    def test_an_absurdly_long_name_is_truncated_to_eighty_characters(self):
        result = self._label("x" * 200)
        self.assertEqual("x" * 80, result)

    def _context_for(self, activities_payload: list[Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(activities_payload, WELLNESS_PAYLOAD),
            ):
                report = build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)
        self.assertEqual("passed", report["status"], report)
        return report["context"]

    def test_weighttraining_activity_name_reaches_recent_actuals_as_session_label(self):
        payload = [
            {
                "id": "i9001",
                "type": "WeightTraining",
                "name": "胸日",
                "start_date_local": "2026-01-06T18:00:00",
                "moving_time": 3300,
                "distance": None,
                "average_speed": 0.0,
                "average_heartrate": 118,
            }
        ]
        context = self._context_for(payload)
        self.assertEqual(1, len(context["recent_actuals"]))
        self.assertEqual("胸日", context["recent_actuals"][0]["session_label"])

    def test_running_activity_session_label_is_none_even_when_named(self):
        # A run's name is commonly a location ("Neighborhood Loop"), not a grouping the
        # coach would ever read back -- unlike a strength session, a run's own numbers
        # already say everything this product acts on.
        payload = [
            {
                "id": "i9002",
                "type": "Run",
                "name": "Neighborhood Loop",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 1800,
                "distance": 4500.0,
                "average_speed": 2.5,
                "average_heartrate": 148,
            }
        ]
        context = self._context_for(payload)
        self.assertEqual(1, len(context["recent_actuals"]))
        # Omitted rather than null (issue #240 §3): a label is the provider's name
        # for a strength session, so on a run the concept does not apply at all.
        self.assertNotIn("session_label", context["recent_actuals"][0])

    def test_missing_or_blank_name_on_a_strength_activity_is_none(self):
        payload = [
            {
                "id": "i9003",
                "type": "WeightTraining",
                # no "name" key at all
                "start_date_local": "2026-01-06T18:00:00",
                "moving_time": 3000,
                "distance": None,
                "average_speed": 0.0,
                "average_heartrate": 110,
            },
            {
                "id": "i9004",
                "type": "WeightTraining",
                "name": "   ",
                "start_date_local": "2026-01-05T18:00:00",
                "moving_time": 3000,
                "distance": None,
                "average_speed": 0.0,
                "average_heartrate": 110,
            },
        ]
        context = self._context_for(payload)
        by_id = {a["activity_id"]: a for a in context["recent_actuals"]}
        self.assertIsNone(by_id["intervals:i9003"]["session_label"])
        self.assertIsNone(by_id["intervals:i9004"]["session_label"])

    def test_an_absurdly_long_strength_name_is_truncated_in_recent_actuals(self):
        payload = [
            {
                "id": "i9005",
                "type": "WeightTraining",
                "name": "x" * 200,
                "start_date_local": "2026-01-06T18:00:00",
                "moving_time": 3000,
                "distance": None,
                "average_speed": 0.0,
                "average_heartrate": 110,
            }
        ]
        context = self._context_for(payload)
        label = context["recent_actuals"][0]["session_label"]
        self.assertEqual("x" * 80, label)


class ProviderRootShapeTests(unittest.TestCase):
    """``/activities`` and ``/wellness`` must be JSON lists (issue #111): a new blocking
    validator, so AGENTS.md rule 6 applies. Invariant, harm, and false-positive cost are
    documented beside the guard itself (source_intervals._require_json_list). The
    harmful-case regressions below prove the actual bug this issue reports -- a
    non-list root (an error envelope, ``null``, a scalar) previously read as a silent,
    successful empty training/wellness history -- now blocks instead. The
    false-positive control proves the common, valid case (a genuine empty list) is
    unaffected.
    """

    def _build_or_raise(self, activities_payload: Any, wellness_payload: Any) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(activities_payload, wellness_payload),
            ):
                return build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)

    # -- Harmful case: the exact bug this issue reports -----------------------------

    def test_activities_object_root_blocks_with_no_body_leak(self):
        # An error envelope returned with HTTP 200 is exactly the shape a permission or
        # schema change on the provider side would take -- the case issue #111 names.
        poisoned = {
            "error": "not authorized",
            "athleteId": "i-should-not-leak",
            "token": "leaked-material-not-real",
        }
        with self.assertRaises(ContextBuildError) as ctx:
            self._build_or_raise(poisoned, WELLNESS_PAYLOAD)
        message = str(ctx.exception)
        self.assertIn("/activities", message)
        self.assertIn("object", message)
        self.assertNotIn("not authorized", message)
        self.assertNotIn("leaked-material-not-real", message)
        self.assertNotIn("i-should-not-leak", message)

    def test_activities_null_root_blocks(self):
        with self.assertRaisesRegex(ContextBuildError, r"/activities.*JSON list.*null"):
            self._build_or_raise(None, WELLNESS_PAYLOAD)

    def test_activities_scalar_root_blocks(self):
        with self.assertRaisesRegex(ContextBuildError, r"/activities.*JSON list.*string"):
            self._build_or_raise("unexpected-error-string", WELLNESS_PAYLOAD)

    def test_wellness_object_root_blocks_with_no_body_leak(self):
        poisoned = {"error": "internal", "secret": "should-not-appear-either"}
        with self.assertRaises(ContextBuildError) as ctx:
            self._build_or_raise(ACTIVITIES_PAYLOAD, poisoned)
        message = str(ctx.exception)
        self.assertIn("/wellness", message)
        self.assertNotIn("should-not-appear-either", message)

    def test_wellness_null_root_blocks(self):
        with self.assertRaisesRegex(ContextBuildError, r"/wellness.*JSON list.*null"):
            self._build_or_raise(ACTIVITIES_PAYLOAD, None)

    def test_wellness_scalar_root_blocks(self):
        with self.assertRaisesRegex(ContextBuildError, r"/wellness.*JSON list.*number"):
            self._build_or_raise(ACTIVITIES_PAYLOAD, 42)

    # -- False-positive control: the common, valid case is unaffected ----------------

    def test_genuine_empty_lists_for_both_endpoints_stay_valid_and_fresh(self):
        report = self._build_or_raise([], [])
        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        self.assertEqual("fresh", context["freshness"]["activities"])
        self.assertEqual([], context["recent_actuals"])


class WellnessOutageLeavesRecoveryUnreadTests(unittest.TestCase):
    """The five ways a wellness read can end, and which of them cost the turn.

    The athlete asking what to do today has a plan, a week and a today; a provider that
    cannot answer for their sleep does not take those with it. Activities are the other
    half of the same sentence and still do: matching, the cycle record and baseline
    evidence all run on them, so a turn without them has nothing to reconcile against.

    Not every wellness failure is that outage, though, and collapsing them would hide the
    two that do not go away on their own:

    ==============================  =========  ======================================
    how the read ended              the turn   what the recovery half says
    ==============================  =========  ======================================
    answered, values in the window  continues  fresh / stale, per ``_recovery_freshness``
    answered, no value anywhere     continues  "failed" -- looked, nothing there
    network error or 5xx            continues  "unknown" + ``..._read_failed``
    403, capability not granted     continues  "unknown" + ``..._permission_denied``
    401, credential refused         blocked    -- (the gateway forgets the connection)
    200 with a body it cannot read  blocked    -- (provider contract drift)
    ==============================  =========  ======================================

    The bottom two are why the catch is an allow-list of named classes: a refusal the
    athlete has to act on, and drift this code has to be told about, must not read as
    weather that will pass.
    """

    def _fetch_with_outage(self, endpoint: str, status: int = 500):
        """The real fetch, except one endpoint answers `status` -- to both tries."""
        inner = _fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD)

        def fetch(request: urllib.request.Request) -> bytes:
            if endpoint in request.full_url:
                raise urllib.error.HTTPError(request.full_url, status, "denied", None, None)
            return inner(request)

        return fetch

    def _fetch_returning(self, endpoint: str, body: bytes):
        """The real fetch, except one endpoint answers 200 with `body` verbatim."""
        inner = _fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD)

        def fetch(request: urllib.request.Request) -> bytes:
            return body if endpoint in request.full_url else inner(request)

        return fetch

    def _build(self, fetch) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch", new=fetch
            ):
                return build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )

    def test_the_turn_survives_and_says_the_read_failed(self):
        report = self._build(self._fetch_with_outage("/wellness"))

        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        self.assertEqual("unknown", context["freshness"]["recovery"])
        self.assertEqual("fresh", context["freshness"]["activities"])
        self.assertIn("intervals_wellness_read_failed", context["unknowns"])
        for signal in ("sleep", "hrv", "resting_hr"):
            self.assertEqual(0, context["coverage"][signal]["observed_days"], signal)
            self.assertEqual("missing", context["coverage"][signal]["status"], signal)
            self.assertEqual("unknown", context["recovery_trends"][signal]["status"], signal)
        # The read that did happen is untouched: an outage on one endpoint costs the
        # turn that endpoint's evidence and nothing else.
        self.assertEqual(2, len(context["recent_actuals"]))

    def test_a_feed_that_answered_with_nothing_is_a_different_grade(self):
        """The control that makes the grade above mean something.

        Both reads observe zero days. Only one of them asked the provider and got an
        answer, and ``failed`` is the grade that already meant that -- so the unread
        feed had to be graded apart from it rather than folded in.
        """
        report = self._build(_fake_fetch(ACTIVITIES_PAYLOAD, []))

        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        self.assertEqual("failed", context["freshness"]["recovery"])
        self.assertNotIn("intervals_wellness_read_failed", context["unknowns"])

    def test_a_permission_the_athlete_can_restore_does_not_read_as_weather(self):
        """403 keeps the turn but not the outage's wording.

        The Intervals consent page grants permissions separately, so a wellness read the
        connection may not make fails the same way every turn until the athlete
        reconnects with it. Reporting that as a bad minute tells them to wait for
        something that is not coming back on its own.
        """
        report = self._build(self._fetch_with_outage("/wellness", status=403))

        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        self.assertEqual("unknown", context["freshness"]["recovery"])
        denied = [
            unknown
            for unknown in context["unknowns"]
            if unknown.startswith("intervals_wellness_permission_denied")
        ]
        self.assertEqual(1, len(denied), context["unknowns"])
        self.assertIn("reconnected with that permission", denied[0])
        self.assertNotIn("intervals_wellness_read_failed", context["unknowns"])

    def test_a_refused_credential_is_not_degraded_into_an_outage(self):
        """401 has to reach the gateway: it is what forgets the connection.

        Activities is read first, so a credential refused for the whole account already
        blocks there. This pins the narrow window -- the grant revoked between the two
        reads of one turn -- where the wellness read is the one that sees it, with the
        status still on the error so the caller can act on it.
        """
        with self.assertRaises(ContextBuildError) as raised:
            self._build(self._fetch_with_outage("/wellness", status=401))

        self.assertEqual(401, raised.exception.upstream_status)

    def test_an_activities_outage_still_ends_the_build(self):
        with self.assertRaises(ContextBuildError):
            self._build(self._fetch_with_outage("/activities"))

    def test_a_wellness_root_the_product_cannot_parse_still_ends_the_build(self):
        """A failed read and an unreadable answer are not the same thing.

        A 500 is the provider saying it cannot answer this turn. A 200 carrying a root
        that is not a list is this code no longer understanding the provider, which
        would go on being unsaid for every turn after it -- so that one still blocks.
        """
        with self.assertRaisesRegex(ContextBuildError, r"/wellness.*JSON list"):
            self._build(_fake_fetch(ACTIVITIES_PAYLOAD, {"error": "internal"}))

    def test_a_wellness_body_that_is_not_json_at_all_still_ends_the_build(self):
        """The shape guard's sibling, and the one the first cut of this let through.

        An HTML error page served with 200 is the same provider-contract drift as an
        object root, one layer earlier -- so it fails closed the same way rather than
        passing for an outage that will clear.
        """
        with self.assertRaisesRegex(ContextBuildError, "invalid JSON"):
            self._build(self._fetch_returning("/wellness", b"<html>502 Bad Gateway</html>"))


class MalformedListRowsTests(unittest.TestCase):
    """A non-dict entry inside an otherwise-valid ``/activities`` or ``/wellness`` list
    is still excluded from parsing -- unchanged from before -- but is now counted, so
    broad row-schema drift cannot be reported as an unqualified fresh empty training
    history (issue #111)."""

    def _context_for(self, activities_payload: list[Any], wellness_payload: list[Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials", return_value=FAKE_CREDENTIALS
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(activities_payload, wellness_payload),
            ):
                report = build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)
        self.assertEqual("passed", report["status"], report)
        return report["context"]

    def test_malformed_activity_rows_are_counted_and_real_rows_still_parse(self):
        payload = ["not-a-row", None, 42, ACTIVITIES_PAYLOAD[0], ACTIVITIES_PAYLOAD[1]]
        context = self._context_for(payload, WELLNESS_PAYLOAD)
        self.assertIn("intervals_activities_malformed_rows:3", context["unknowns"])
        self.assertEqual(2, len(context["recent_actuals"]))

    def test_activities_list_entirely_malformed_is_fresh_but_qualified_not_unqualified_empty(self):
        # The dict entry has no recognizable date/type -- it is excluded downstream the
        # same as any unusable row, but it IS a dict, so it is not "malformed" by this
        # adapter's definition (a non-dict row). Only the two non-dict entries count.
        payload = ["oops", 3.14, {"no": "recognizable fields"}]
        context = self._context_for(payload, WELLNESS_PAYLOAD)
        self.assertEqual("fresh", context["freshness"]["activities"])
        self.assertEqual([], context["recent_actuals"])
        self.assertIn("intervals_activities_malformed_rows:2", context["unknowns"])

    def test_malformed_wellness_rows_are_counted(self):
        payload = [WELLNESS_PAYLOAD[0], "bad-row", WELLNESS_PAYLOAD[1]]
        context = self._context_for(ACTIVITIES_PAYLOAD, payload)
        self.assertIn("intervals_wellness_malformed_rows:1", context["unknowns"])

    def test_zero_malformed_rows_adds_no_note(self):
        context = self._context_for(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD)
        self.assertFalse(
            any(u.startswith("intervals_activities_malformed_rows:") for u in context["unknowns"])
        )
        self.assertFalse(
            any(u.startswith("intervals_wellness_malformed_rows:") for u in context["unknowns"])
        )


# Synthetic OAuth credentials only -- never real token material. Mirrors
# gateway.OAUTH_ATHLETE_ID ("0") and gateway._credentials's IntervalsCredentials(token,
# OAUTH_ATHLETE_ID, "bearer") construction, without importing gateway.py itself (out of
# this fix's scope -- see AGENTS.md and the task's file allowlist).
FAKE_BEARER_CREDENTIALS = IntervalsCredentials("synthetic-oauth-material-not-real", "0", "bearer")


class SharedAdapterAuthSchemeParityTests(unittest.TestCase):
    """fetch_domain is the one shared adapter both entry points call: the CLI/API-key
    path resolves basic-scheme IntervalsCredentials via resolve_credentials, and the
    gateway's bearer-token OAuth path builds IntervalsCredentials(token,
    OAUTH_ATHLETE_ID, "bearer") per request (garmin_coach_loop/gateway.py). Everything
    fixed above lives in fetch_domain and the functions it calls, not in either caller,
    so proving each behavior once per auth scheme demonstrates the fix covers both
    entry points without needing to touch gateway.py or its own test module (both out
    of this fix's scope).
    """

    @staticmethod
    def _window() -> BuildWindow:
        return BuildWindow(
            as_of=dt.datetime(2026, 1, 8, 20, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            resolved_now=NOW,
            now_iso="2026-01-08T12:00:00+00:00",
            window_start=dt.date(2026, 1, 2),
            window_end=dt.date(2026, 1, 8),
            window14_start=dt.date(2025, 12, 26),
            window14_end=dt.date(2026, 1, 8),
            window42_start=dt.date(2025, 11, 28),
            window42_end=dt.date(2026, 1, 8),
        )

    def test_trailrun_mapping_is_identical_across_both_auth_schemes(self):
        trailrun_payload = [
            {
                "id": "i7001",
                "type": "TrailRun",
                "start_date_local": "2026-01-06T07:00:00",
                "moving_time": 2400,
                "distance": 8000.0,
                "average_speed": 3.3,
                "average_heartrate": 150,
            }
        ]
        for credentials in (FAKE_CREDENTIALS, FAKE_BEARER_CREDENTIALS):
            with self.subTest(auth_scheme=credentials.auth_scheme):
                domain = fetch_domain(
                    credentials, self._window(), fetch=_fake_fetch(trailrun_payload, [])
                )
                self.assertEqual(1, len(domain.recent_actuals))
                self.assertEqual("running", domain.recent_actuals[0]["sport"])

    def test_non_list_activities_root_blocks_identically_across_both_auth_schemes(self):
        for credentials in (FAKE_CREDENTIALS, FAKE_BEARER_CREDENTIALS):
            with self.subTest(auth_scheme=credentials.auth_scheme):
                with self.assertRaisesRegex(ContextBuildError, r"did not return a JSON list"):
                    fetch_domain(
                        credentials, self._window(), fetch=_fake_fetch({"error": "nope"}, [])
                    )

    def test_malformed_rows_are_counted_identically_across_both_auth_schemes(self):
        payload = ["not-a-row", ACTIVITIES_PAYLOAD[0]]
        for credentials in (FAKE_CREDENTIALS, FAKE_BEARER_CREDENTIALS):
            with self.subTest(auth_scheme=credentials.auth_scheme):
                domain = fetch_domain(credentials, self._window(), fetch=_fake_fetch(payload, []))
                self.assertIn("intervals_activities_malformed_rows:1", domain.extra_unknowns)


if __name__ == "__main__":
    unittest.main()


# One activity's worth of provider segments, shaped like the real endpoint: a warm-up,
# two work reps with a recovery between them, and a cool-down. Deliberately messy in
# the two ways the real thing is -- the recovery is typed RECOVERY but so short it is
# GPS noise, and every other segment comes back typed WORK whether it was the
# prescribed work or not.
SEGMENTS_PAYLOAD = {
    "id": "i2002",
    "icu_intervals": [
        {"type": "WORK", "distance": 1002.7, "moving_time": 492, "average_speed": 2.038,
         "average_heartrate": 129, "max_heartrate": 142, "min_heartrate": 96,
         "total_elevation_gain": 0.0},
        {"type": "WORK", "distance": 998.3, "moving_time": 374, "average_speed": 2.669,
         "average_heartrate": 150, "max_heartrate": 159, "min_heartrate": 131,
         "total_elevation_gain": 4.0},
        {"type": "RECOVERY", "distance": 3.0, "moving_time": 1, "average_speed": 2.98,
         "average_heartrate": 157, "max_heartrate": 157, "min_heartrate": 157,
         "total_elevation_gain": 0.0},
        {"type": "WORK", "distance": 996.5, "moving_time": 367, "average_speed": 2.715,
         "average_heartrate": 158, "max_heartrate": 172, "min_heartrate": 148,
         "total_elevation_gain": 3.0},
        {"type": "WORK", "distance": 843.4, "moving_time": 361, "average_speed": 2.336,
         "average_heartrate": 163, "max_heartrate": 169, "min_heartrate": 150,
         "total_elevation_gain": 2.0},
    ],
}


class SegmentExecutionTests(unittest.TestCase):
    """Per-segment execution evidence: what a session's work actually looked like.

    The whole-session average is the wrong reading for a quality session -- it spans
    the warm-up and the recoveries too -- so these hold the finer evidence to being
    present, honest about its own absence, and free of any verdict.
    """

    def _build(self, segments_payload=None, activities=None):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(
                    activities if activities is not None else ACTIVITIES_PAYLOAD,
                    WELLNESS_PAYLOAD,
                    segments_payload,
                ),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )
            self.assertEqual("passed", report["status"], report)
            return report["context"]

    def test_a_quality_sessions_reps_are_readable_from_the_context_alone(self):
        """The point of the whole feature: no tool outside this product is needed."""
        context = self._build(SEGMENTS_PAYLOAD)
        group = context["segment_execution"]
        self.assertEqual("intervals-icu-api", group["source"])
        activity = group["activities"][0]
        self.assertEqual("intervals:i2002", activity["activity_id"])
        self.assertEqual("running", activity["sport"])

        segments = activity["segments"]
        self.assertEqual(5, len(segments))
        # Pace arrives in this product's unit, not the provider's metres per second.
        self.assertEqual([491, 375, 336, 368, 428],
                         [segment["average_pace_sec_per_km"] for segment in segments])
        self.assertEqual([129, 150, 157, 158, 163],
                         [segment["average_hr"] for segment in segments])
        self.assertEqual([142, 159, 157, 172, 169],
                         [segment["max_hr"] for segment in segments])
        # Why the finer reading has to exist: the warm-up is nearly two minutes per
        # kilometre slower than the reps it precedes, so any single number averaged
        # across both is a reading of neither.
        self.assertGreater(
            segments[0]["average_pace_sec_per_km"],
            segments[1]["average_pace_sec_per_km"] + 100,
        )

    def test_segments_stay_in_provider_order_and_are_never_aligned_to_the_plan(self):
        """The provider's grouping does not correspond to the prescribed steps.

        Here a warm-up, two reps, a noise segment and a cool-down come back with four
        of five typed WORK. Any code that decided which of these was "the work" would
        be guessing; the coach reads the numbers instead (AGENTS.md 1).
        """
        segments = self._build(SEGMENTS_PAYLOAD)["segment_execution"]["activities"][0]["segments"]
        self.assertEqual([0, 1, 2, 3, 4], [segment["index"] for segment in segments])
        self.assertEqual(["WORK", "WORK", "RECOVERY", "WORK", "WORK"],
                         [segment["provider_type"] for segment in segments])
        # No verdict of any kind reaches the context.
        for segment in segments:
            for banned in ("target_met", "completion", "compliance", "score", "percent_of_target"):
                self.assertNotIn(banned, segment)

    def test_a_three_metre_noise_segment_is_reported_rather_than_filtered(self):
        """Dropping it needs a threshold, and a threshold silently deletes a genuinely
        short segment one day. A reader skips it at a glance."""
        segments = self._build(SEGMENTS_PAYLOAD)["segment_execution"]["activities"][0]["segments"]
        self.assertEqual(3.0, segments[2]["distance_m"])
        self.assertEqual(1, segments[2]["moving_time_sec"])

    def test_no_segments_anywhere_reads_as_absent_and_says_so_once(self):
        context = self._build({"icu_intervals": []})
        self.assertIsNone(context["segment_execution"])
        matching = [note for note in context["unknowns"] if note.startswith("segment_execution:")]
        self.assertEqual(1, len(matching), context["unknowns"])
        self.assertIn("whole-session averages", matching[0])

    def test_strength_activities_are_never_read_for_segments(self):
        """A strength entry carries no segments, and per-set truth arrives elsewhere."""
        requested: list[str] = []

        def recording_fetch(request: urllib.request.Request) -> bytes:
            if request.full_url.endswith("/intervals"):
                requested.append(request.full_url)
                return json.dumps(SEGMENTS_PAYLOAD).encode("utf-8")
            if "/activities" in request.full_url:
                return json.dumps(ACTIVITIES_PAYLOAD).encode("utf-8")
            if "/wellness" in request.full_url:
                return json.dumps(WELLNESS_PAYLOAD).encode("utf-8")
            if request.full_url.endswith("/sport-settings"):
                return json.dumps([]).encode("utf-8")
            raise AssertionError(f"unexpected URL: {request.full_url}")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch", new=recording_fetch
            ):
                build_context(_make_request(), state_dir=state_dir, source="intervals", now=NOW)

        self.assertTrue(requested)
        for url in requested:
            self.assertNotIn("i2001", url)  # WeightTraining
            self.assertNotIn("i1999", url)  # WeightTraining

    def test_an_easy_run_is_never_read_for_segments(self):
        """The cut issue #233 makes, and the reason it costs no coaching.

        The fixture's easy run is prescribed as one continuous block. What it can be
        asked -- did it stay easy, how long, how far -- is answered by the average pace
        and average heart rate its attached ``cycle_sessions`` record's activity
        carries (issue #240 §1 moved the reading there); the auto-laps the watch
        happened to cut answer nothing further, and reading them costs a provider
        request and about 1.5 KB of every later turn in the conversation.
        """
        plan = _make_plan()
        easy = copy.deepcopy(plan["week"]["sessions"][0])
        easy.update(
            session_id="run-easy-01",
            scheduled_date="2026-01-07",
            purpose="Aerobic base",
            adaptation="aerobic_base",
            cost="easy",
            priority="flexible",
            hard=False,
            plan=_default_plan("running"),
            execution={
                "publish_supported": True,
                "external_id": "event-easy-2003",
                "delivery_state": "intervals_accepted",
            },
        )
        easy["prescription"] = render_prescription(easy["plan"])
        plan["week"]["sessions"].append(easy)

        activities = copy.deepcopy(ACTIVITIES_PAYLOAD)
        activities.append({
            "id": "i2003",
            "type": "Run",
            "start_date_local": "2026-01-07T07:00:00",
            "moving_time": 1500,
            "distance": 4000.0,
            "average_speed": 2.6,
            "average_heartrate": 145,
            "paired_event_id": "event-easy-2003",
            "total_elevation_gain": 10.0,
        })

        requested: list[str] = []

        def recording_fetch(request: urllib.request.Request) -> bytes:
            if request.full_url.endswith("/intervals"):
                requested.append(request.full_url)
                return json.dumps(SEGMENTS_PAYLOAD).encode("utf-8")
            if "/activities" in request.full_url:
                return json.dumps(activities).encode("utf-8")
            if "/wellness" in request.full_url:
                return json.dumps(WELLNESS_PAYLOAD).encode("utf-8")
            if request.full_url.endswith("/sport-settings"):
                return json.dumps([]).encode("utf-8")
            raise AssertionError(f"unexpected URL: {request.full_url}")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, plan)
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch", new=recording_fetch
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )

        # Not read, so not paid for -- one request, for the quality run only.
        self.assertEqual(1, len(requested), requested)
        self.assertIn("i2002", requested[0])
        ids = [
            item["activity_id"]
            for item in report["context"]["segment_execution"]["activities"]
        ]
        self.assertEqual(["intervals:i2002"], ids)
        # And the easy run is still fully in the context, at the grain that reports it.
        easy_actual = [
            actual
            for actual in report["context"]["recent_actuals"]
            if actual["activity_id"] == "intervals:i2003"
        ]
        self.assertEqual(1, len(easy_actual), report["context"]["recent_actuals"])
        # Attached to a cycle_sessions record, so the recent_actuals row is the
        # reconciliation identity and the reading lives on the record's activity
        # (issue #240 §1) -- the grain moved, the answer did not.
        easy_record = [
            record
            for record in report["context"]["cycle_sessions"]
            if (record.get("activity") or {}).get("activity_id") == "intervals:i2003"
        ]
        self.assertEqual(1, len(easy_record), report["context"]["cycle_sessions"])
        self.assertIsNotNone(easy_record[0]["activity"]["average_pace_sec_per_km"])
        self.assertIsNotNone(easy_record[0]["activity"]["average_hr"])

    def test_one_activity_failing_keeps_the_others_and_names_the_failure(self):
        """A single unreadable activity must not cost the whole build."""
        activities = copy.deepcopy(ACTIVITIES_PAYLOAD)
        # Same day as the quality run, which is what puts it inside the per-segment
        # read at all: segments are read for the days the plan prescribed reps on, and
        # a second run on such a day is read too (issue #233).
        activities.append({
            "id": "i2003",
            "type": "Run",
            "start_date_local": "2026-01-08T18:00:00",
            "moving_time": 1500,
            "distance": 4000.0,
            "average_speed": 2.6,
            "average_heartrate": 145,
            "total_elevation_gain": 10.0,
        })

        def flaky_fetch(request: urllib.request.Request) -> bytes:
            if request.full_url.endswith("/intervals"):
                if "i2003" in request.full_url:
                    raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)
                return json.dumps(SEGMENTS_PAYLOAD).encode("utf-8")
            if "/activities" in request.full_url:
                return json.dumps(activities).encode("utf-8")
            if "/wellness" in request.full_url:
                return json.dumps(WELLNESS_PAYLOAD).encode("utf-8")
            if request.full_url.endswith("/sport-settings"):
                return json.dumps([]).encode("utf-8")
            raise AssertionError(f"unexpected URL: {request.full_url}")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch", new=flaky_fetch
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )

        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        ids = [item["activity_id"] for item in context["segment_execution"]["activities"]]
        self.assertIn("intervals:i2002", ids)
        self.assertNotIn("intervals:i2003", ids)
        self.assertTrue(
            any("segment read(s) failed" in note for note in context["unknowns"]),
            context["unknowns"],
        )


class RunSportSettingsMaxHrTests(unittest.TestCase):
    """The Run sport settings' own max HR: one of the two sources a divergence report
    compares (PlanState.athlete_baseline.max_hr is the other, read from the local
    store). This file only has to read it correctly and never let a failure here cost
    the rest of the build; the comparison itself lives in context_core.

    Every case here states a ``baseline_max_hr``, because that is the condition under
    which the read happens at all -- the value has no other purpose than to disagree
    with that figure, so ``fetch_domain`` does not spend a request on it when there is
    no figure. ``BASELINE_MAX_HR`` below is what the athlete's plan is holding while
    these run."""

    # Deliberately not equal to any payload value used below: the read is what these
    # test, and the comparison this number is the other half of is context_core's.
    BASELINE_MAX_HR = 191

    def _window(self) -> BuildWindow:
        return BuildWindow(
            as_of=dt.datetime(2026, 1, 8, 20, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            resolved_now=NOW,
            now_iso="2026-01-08T12:00:00+00:00",
            window_start=dt.date(2026, 1, 2),
            window_end=dt.date(2026, 1, 8),
            window14_start=dt.date(2025, 12, 26),
            window14_end=dt.date(2026, 1, 8),
            window42_start=dt.date(2025, 11, 28),
            window42_end=dt.date(2026, 1, 8),
        )

    def test_a_run_entrys_max_hr_is_read_into_the_domain(self):
        domain = fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=_fake_fetch(
                [], [], None, sport_settings_payload=[{"types": ["Run"], "max_hr": 180}]
            ),
            baseline_max_hr=self.BASELINE_MAX_HR,
        )
        self.assertEqual(180, domain.sport_settings_max_hr)

    def test_no_run_entry_reads_as_none_not_a_failure(self):
        """Read successfully, nothing there -- an ordinary state, not an error."""
        domain = fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=_fake_fetch(
                [], [], None, sport_settings_payload=[{"types": ["Swim"], "max_hr": 190}]
            ),
            baseline_max_hr=self.BASELINE_MAX_HR,
        )
        self.assertIsNone(domain.sport_settings_max_hr)

    def test_a_run_entry_with_no_max_hr_configured_reads_as_none(self):
        domain = fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=_fake_fetch([], [], None, sport_settings_payload=[{"types": ["Run"]}]),
            baseline_max_hr=self.BASELINE_MAX_HR,
        )
        self.assertIsNone(domain.sport_settings_max_hr)

    def test_zero_is_a_sentinel_not_a_measurement(self):
        domain = fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=_fake_fetch(
                [], [], None, sport_settings_payload=[{"types": ["Run"], "max_hr": 0}]
            ),
            baseline_max_hr=self.BASELINE_MAX_HR,
        )
        self.assertIsNone(domain.sport_settings_max_hr)

    def test_an_unreadable_sport_settings_endpoint_degrades_to_none_and_does_not_block(self):
        """Optional supplementary evidence: a denied or broken read must not cost the
        activities/wellness read it rides alongside."""

        def denying_fetch(request: urllib.request.Request) -> bytes:
            if request.full_url.endswith("/sport-settings"):
                raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)
            if "/activities" in request.full_url:
                return json.dumps([]).encode("utf-8")
            if "/wellness" in request.full_url:
                return json.dumps([]).encode("utf-8")
            raise AssertionError(f"unexpected URL: {request.full_url}")

        domain = fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=denying_fetch,
            baseline_max_hr=self.BASELINE_MAX_HR,
        )
        self.assertIsNone(domain.sport_settings_max_hr)

    def test_a_non_list_sport_settings_root_degrades_to_none_rather_than_raising(self):
        def malformed_fetch(request: urllib.request.Request) -> bytes:
            if request.full_url.endswith("/sport-settings"):
                return json.dumps({"error": "not a list"}).encode("utf-8")
            if "/activities" in request.full_url:
                return json.dumps([]).encode("utf-8")
            if "/wellness" in request.full_url:
                return json.dumps([]).encode("utf-8")
            raise AssertionError(f"unexpected URL: {request.full_url}")

        domain = fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=malformed_fetch,
            baseline_max_hr=self.BASELINE_MAX_HR,
        )
        self.assertIsNone(domain.sport_settings_max_hr)

    def _requested_paths(self, baseline_max_hr: Any) -> list[str]:
        """Every URL one ``fetch_domain`` call issues for this baseline figure."""
        requested: list[str] = []

        def recording_fetch(request: urllib.request.Request) -> bytes:
            requested.append(request.full_url)
            return _fake_fetch(
                [], [], None, sport_settings_payload=[{"types": ["Run"], "max_hr": 180}]
            )(request)

        fetch_domain(
            FAKE_CREDENTIALS,
            self._window(),
            fetch=recording_fetch,
            baseline_max_hr=baseline_max_hr,
        )
        return requested

    def test_the_read_happens_exactly_when_its_one_consumer_could_use_the_answer(self):
        """The gate and the divergence note must never drift apart.

        Written as the relationship and not as a list of accepted shapes: the expected
        answer for each candidate is computed by asking the note's own guard, so a
        change to what that guard accepts moves both sides of this assertion at once and
        a change to only one side fails here. Drifting either way costs something real
        -- too tight and the note loses a side it could have had, too loose and a request
        is spent on an answer nothing will read.
        """
        for baseline in (None, "188", True, 0.0, 188, 188.0):
            with self.subTest(baseline=baseline):
                requested = self._requested_paths(baseline)
                self.assertEqual(
                    _measured_number(baseline),
                    any(url.endswith("/sport-settings") for url in requested),
                )
                # Whatever the gate decided, the activities read is not affected by it.
                self.assertTrue(any("/activities" in url for url in requested))

    def test_the_gate_calls_the_notes_own_guard_rather_than_a_copy_of_it(self):
        """One function, imported, not two that happen to agree today."""
        self.assertIs(context_core._measured_number, source_intervals._measured_number)

    def test_a_disagreeing_sport_settings_reading_reaches_context_unknowns_end_to_end(self):
        """The full pipeline, not just the comparison: a live-shaped Run entry, through
        fetch_domain and build_context, produces the divergence note against the local
        store's athlete_baseline.max_hr (188 in this fixture)."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(
                    [], [], None, sport_settings_payload=[{"types": ["Run"], "max_hr": 180}]
                ),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )

        self.assertEqual("passed", report["status"], report)
        matching = [
            note for note in report["context"]["unknowns"]
            if "max_hr" in note and "diverges" in note
        ]
        self.assertEqual(1, len(matching), report["context"]["unknowns"])
        self.assertIn("188", matching[0])
        self.assertIn("180", matching[0])

    def test_an_agreeing_sport_settings_reading_produces_no_divergence_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(
                    [], [], None, sport_settings_payload=[{"types": ["Run"], "max_hr": 188}]
                ),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )

        self.assertEqual("passed", report["status"], report)
        self.assertFalse(
            any("diverges" in note for note in report["context"]["unknowns"])
        )


class RecentActivityOnlyReadTests(unittest.TestCase):
    """The narrow read for a caller with no plan: activities, and only activities."""

    def _window(self) -> BuildWindow:
        return BuildWindow(
            as_of=dt.datetime(2026, 1, 8, 20, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            resolved_now=NOW,
            now_iso="2026-01-08T12:00:00+00:00",
            window_start=dt.date(2026, 1, 2),
            window_end=dt.date(2026, 1, 8),
            window14_start=dt.date(2025, 12, 26),
            window14_end=dt.date(2026, 1, 8),
            window42_start=dt.date(2025, 11, 28),
            window42_end=dt.date(2026, 1, 8),
        )

    def test_only_the_activities_endpoint_is_requested(self):
        requested: list[str] = []

        def recording_fetch(request: urllib.request.Request) -> bytes:
            requested.append(request.full_url)
            return _fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD)(request)

        fetch_recent_activity(FAKE_CREDENTIALS, self._window(), fetch=recording_fetch)

        self.assertEqual(1, len(requested), requested)
        self.assertIn("/activities?", requested[0])

    def test_the_rows_are_the_rows_a_full_domain_would_have_carried(self):
        """The narrow read is the same read, not a second implementation of it.

        A caller reading this instead of a whole domain must not thereby see a different
        training history, so the three fields it does carry are asserted against the
        domain built from the identical payload.
        """
        window = self._window()
        domain = fetch_domain(
            FAKE_CREDENTIALS, window, fetch=_fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD)
        )
        activity = fetch_recent_activity(
            FAKE_CREDENTIALS, window, fetch=_fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD)
        )

        self.assertEqual(domain.actuals_window_start, activity.actuals_window_start)
        self.assertEqual(domain.activity_days, activity.activity_days)
        self.assertEqual(domain.recent_actuals, activity.recent_actuals)

    def test_an_unreadable_activities_endpoint_raises_rather_than_reading_as_empty(self):
        def denying_fetch(request: urllib.request.Request) -> bytes:
            raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)

        with self.assertRaises(ContextBuildError):
            fetch_recent_activity(FAKE_CREDENTIALS, self._window(), fetch=denying_fetch)


class SnapshotReuseAcrossReconciliationTests(unittest.TestCase):
    """Reconcile-then-rebuild reads the provider once, and gets the same answer for it."""

    def test_the_reused_snapshot_rebuilds_what_a_second_fetch_would_have_built(self):
        """Two rebuilds of the moved plan, one from the snapshot and one from a fresh
        read of an unchanged account, must be the same context.

        The reused build is handed a fetcher that raises on contact, so passing it also
        proves the reuse is a replacement for the provider read rather than a cache in
        front of one.
        """

        def forbidden_fetch(request: urllib.request.Request) -> bytes:
            raise AssertionError(f"provider was read again: {request.full_url}")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            request = _make_request()

            first, domain = build_context_with_domain(
                request,
                state_dir=state_dir,
                source="intervals",
                credentials=FAKE_CREDENTIALS,
                fetch=_fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD),
                now=NOW,
            )
            self.assertEqual("passed", first["status"], first)

            reconciliation = apply_reconciliation(state_dir, first["context"], now=NOW)
            self.assertEqual("passed", reconciliation["status"], reconciliation)
            self.assertEqual(
                ["run-quality-01"],
                [entry["session_id"] for entry in reconciliation["applied"]],
            )

            reused = build_context(
                request,
                state_dir=state_dir,
                source="intervals",
                credentials=FAKE_CREDENTIALS,
                fetch=forbidden_fetch,
                now=NOW,
                domain=domain,
            )
            refetched = build_context(
                request,
                state_dir=state_dir,
                source="intervals",
                credentials=FAKE_CREDENTIALS,
                fetch=_fake_fetch(ACTIVITIES_PAYLOAD, WELLNESS_PAYLOAD),
                now=NOW,
            )

        self.assertEqual(refetched, reused)
        # And it is genuinely the rebuild, not the first report handed back: the plan
        # moved underneath it, and the rebuilt context says so.
        self.assertEqual(2, reused["context"]["goal_context"]["plan_version"])
        self.assertNotEqual(
            first["context"]["current_calendar"], reused["context"]["current_calendar"]
        )


class RecordedIndoorsTests(unittest.TestCase):
    """Where a run was recorded, read from the provider's own flag.

    A treadmill's distance is the machine's reading rather than a measurement, so the
    pace derived from it and a pace measured outdoors are two different kinds of
    number. Which kind a given run is has always been in the provider's payload and
    was never read, so every recorded pace reached the coach looking measured. These
    hold the fact to being carried, to being read from the flag rather than guessed
    from the activity type, and to keeping its third answer.

    Verified live 2026-08-26 across six weeks of the development account: the flag is
    present on every row, set on treadmill sessions and null otherwise -- and set on
    one the provider typed plain ``Run``, which is why the type is not what is read.
    """

    def _actual(self, row_overrides, *, activity_id="i2002"):
        rows = copy.deepcopy(ACTIVITIES_PAYLOAD)
        row = next(item for item in rows if item["id"] == activity_id)
        for key, value in row_overrides.items():
            if value is _ABSENT:
                row.pop(key, None)
            else:
                row[key] = value
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, _make_plan())
            with mock.patch(
                "garmin_coach_loop.source_intervals.resolve_credentials",
                return_value=FAKE_CREDENTIALS,
            ), mock.patch(
                "garmin_coach_loop.source_intervals._default_fetch",
                new=_fake_fetch(rows, WELLNESS_PAYLOAD, SEGMENTS_PAYLOAD),
            ):
                report = build_context(
                    _make_request(), state_dir=state_dir, source="intervals", now=NOW
                )
        self.assertEqual("passed", report["status"], report)
        context = report["context"]
        actual = next(
            item for item in context["recent_actuals"]
            if item["activity_id"] == f"intervals:{activity_id}"
        )
        return actual, context

    def test_a_flagged_run_reads_as_recorded_indoors(self):
        actual, _ = self._actual({"trainer": True})
        self.assertIs(True, actual["recorded_indoors"])

    def test_the_flag_is_read_rather_than_the_activity_type(self):
        """The case the type alone misses.

        The provider types most treadmill runs ``VirtualRun``, but not all of them: on
        2026-08-11 this account recorded one typed plain ``Run`` with the flag set. A
        reader keyed on the type would have called that run outdoors and compared its
        pace to a prescribed one.
        """
        actual, _ = self._actual({"type": "Run", "trainer": True})
        self.assertIs(True, actual["recorded_indoors"])

    def test_an_unflagged_run_reads_as_not_indoors(self):
        actual, _ = self._actual({"trainer": None})
        self.assertIs(False, actual["recorded_indoors"])

    def test_a_row_without_the_flag_stays_unknown(self):
        """The third answer, and the reason this is not a plain boolean.

        A provider that stops carrying the flag is not a provider reporting outdoor
        runs, and turning its silence into ``False`` would be the conversion AGENTS.md
        3 forbids -- the coach would read a measured pace where none was established.
        """
        actual, _ = self._actual({"trainer": _ABSENT})
        self.assertIsNone(actual["recorded_indoors"])

    def test_the_same_fact_reaches_the_group_holding_the_rep_paces(self):
        """Repeated rather than left to a join.

        ``segment_execution`` is where the per-repetition paces are, so it is where
        the kind of number they are has to be legible; a reader that had to find the
        matching ``recent_actuals`` row first is a reader that will compare a
        repetition to its prescribed pace without finding it.
        """
        _, context = self._actual({"trainer": True})
        activity = next(
            item for item in context["segment_execution"]["activities"]
            if item["activity_id"] == "intervals:i2002"
        )
        self.assertIs(True, activity["recorded_indoors"])

    def test_a_lift_is_not_asked_where_it_was_recorded(self):
        """Running only, and the key is gone rather than null.

        On a lift the flag answers nothing a coach reads, and on a ride it would mean
        an indoor trainer, which is a different fact with no consumer here yet. The
        key is dropped rather than carried as null, by the same rule that drops a
        strength row's distance and a non-strength row's session label: a null on a
        key the sport structurally does not have says "looked, found nothing" about a
        question nobody asked.
        """
        actual, _ = self._actual({"trainer": True}, activity_id="i2001")
        self.assertEqual("strength", actual["sport"])
        self.assertNotIn("recorded_indoors", actual)
