from __future__ import annotations

import base64
import contextlib
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

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
)
from garmin_coach_loop.source_intervals import (
    USER_AGENT,
    IntervalsCredentials,
    fetch_domain,
    resolve_credentials,
)
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


def _fake_fetch(activities_payload: list[dict[str, Any]], wellness_payload: list[dict[str, Any]]):
    def fetch(request: urllib.request.Request) -> bytes:
        if "/activities" in request.full_url:
            return json.dumps(activities_payload).encode("utf-8")
        if "/wellness" in request.full_url:
            return json.dumps(wellness_payload).encode("utf-8")
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

            # The 2025-12-01 activity proves the planning window is 42 days, not the
            # old 14-day actuals window. The 2025-11-20 activity remains excluded.
            self.assertEqual(3, len(context["recent_actuals"]))
            by_id = {a["activity_id"]: a for a in context["recent_actuals"]}
            self.assertIn("intervals:i2001", by_id)
            self.assertIn("intervals:i2002", by_id)
            self.assertIn("intervals:i1999", by_id)
            self.assertNotIn("intervals:i1998", by_id)

            # PLAN_FIXTURE's only session is "run-quality-01" (running, 2026-01-08) --
            # no strength session exists anywhere in the plan, so the strength activity
            # has zero same-day/same-sport candidates and must stay unmatched.
            strength = by_id["intervals:i2001"]
            self.assertEqual("strength", strength["sport"])
            self.assertEqual("strength", strength["adaptation"])
            self.assertEqual("full", strength["body_stress"])
            self.assertEqual("moderate", strength["cost"])
            self.assertEqual(55, strength["duration_minutes"])
            # No elevation field on the raw payload -- must be None, never fabricated 0.
            self.assertIsNone(strength["elevation_gain_m"])
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

            self.assertEqual({"observed_days": 2, "expected_days": 7, "status": "partial"}, context["coverage"]["activities"])
            self.assertEqual({"observed_days": 6, "expected_days": 7, "status": "partial"}, context["coverage"]["sleep"])
            self.assertEqual({"observed_days": 6, "expected_days": 7, "status": "partial"}, context["coverage"]["hrv"])
            self.assertEqual({"observed_days": 6, "expected_days": 7, "status": "partial"}, context["coverage"]["resting_hr"])

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
            self.assertEqual({"observed_days": 0, "expected_days": 7, "status": "missing"}, context["coverage"]["sleep"])
            self.assertEqual({"observed_days": 0, "expected_days": 7, "status": "missing"}, context["coverage"]["resting_hr"])

            # Never fabricated: honest "missing" unknowns, never silently treated as zero
            # or as evidence of recovery.
            self.assertIn("sleep_data_unavailable", context["unknowns"])
            self.assertIn("resting_hr_unavailable", context["unknowns"])
            self.assertIn("red_flags_not_confirmed", context["unknowns"])
            self.assertIn("fixture-manual-note", context["unknowns"])
            self.assertNotIn("intervals_source_failed", context["unknowns"])


class RecoveryFreshnessGradingTests(unittest.TestCase):
    """freshness.recovery is graded from observed signal values, never from HTTP success.

    Ladder under test (see source_intervals._recovery_freshness): no field with any
    value -> failed; >=2 fields with a value <=1 day old -> fresh; >=2 fields with
    values but not current -> stale; exactly one field with values -> partial.
    """

    def _recovery_freshness_for(self, wellness_payload: list[dict[str, Any]]) -> str:
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
        return report["context"]["freshness"]["recovery"]

    def test_single_signal_is_partial_not_fresh(self):
        # Only restingHR ever carries a value -- the real account's shape on
        # 2026-08-10. One signal can never support a multi-signal recovery read.
        payload = [
            {"id": "2026-01-08", "sleepScore": None, "hrv": None, "restingHR": 48},
        ]
        self.assertEqual("partial", self._recovery_freshness_for(payload))

    def test_multi_signal_but_days_old_is_stale(self):
        # All three signals exist but the newest value is 4 days before as-of:
        # evidence exists, it just is not current enough for a daily decision.
        payload = [
            {"id": "2026-01-04", "sleepScore": 70, "hrv": 45, "restingHR": 52},
        ]
        self.assertEqual("stale", self._recovery_freshness_for(payload))

    def test_two_current_signals_are_fresh(self):
        # hrv and restingHR carry yesterday's values; sleep never reports. Two
        # current signals make a multi-signal read possible -> fresh, while the
        # missing sleep stays visible through coverage/trends, not freshness.
        payload = [
            {"id": "2026-01-07", "sleepScore": None, "hrv": 52, "restingHR": 49},
        ]
        self.assertEqual("fresh", self._recovery_freshness_for(payload))

    def test_one_current_and_one_old_signal_is_stale(self):
        # Two signals exist but only one is current: a multi-signal read of "now"
        # is not possible, so this is stale, not fresh.
        payload = [
            {"id": "2026-01-03", "sleepScore": None, "hrv": None, "restingHR": 52},
            {"id": "2026-01-08", "sleepScore": None, "hrv": 55, "restingHR": None},
        ]
        self.assertEqual("stale", self._recovery_freshness_for(payload))

    def test_rows_outside_the_seven_day_window_do_not_count(self):
        # The only row with values sits before the 7-day window (2026-01-02..08):
        # inside the window the feed is silent -> failed.
        payload = [
            {"id": "2025-12-20", "sleepScore": 80, "hrv": 55, "restingHR": 48},
        ]
        self.assertEqual("failed", self._recovery_freshness_for(payload))

    def test_zero_values_are_sentinels_not_signals(self):
        # A 0 restingHR/hrv/sleepScore is a not-worn artifact, not a measurement.
        # It must count nowhere: not freshness, not coverage, not trends.
        payload = [
            {"id": "2026-01-08", "sleepScore": 0, "hrv": 0, "restingHR": 0},
        ]
        self.assertEqual("failed", self._recovery_freshness_for(payload))


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

        fetch_domain(FAKE_CREDENTIALS, window, fetch=capturing_fetch)

        self.assertEqual(2, len(captured))
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


if __name__ == "__main__":
    unittest.main()
