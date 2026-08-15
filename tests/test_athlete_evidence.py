"""The two athlete-reported facts no device holds (issues #28 and #47).

Everything here runs against a temporary directory. A state root inside the repository is
refused by ``store._state_root``, and none of these tests may ever touch the machine's own
state directory -- the whole point of the file being owner-scoped is that one athlete's
statements never reach another's.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.athlete_evidence import (
    ATHLETE_EVIDENCE_VERSION,
    ATHLETE_REPORTED_SOURCE,
    AthleteEvidenceError,
    effective_availability,
    evidence_path,
    load_evidence,
    normalize_weekday,
    record_availability,
    record_strength_report,
    strength_execution_from_reports,
    week_start_for,
)
from garmin_coach_loop.context_core import ContextRequest, build_window
from garmin_coach_loop.store import StateStoreError, doctor_store, init_store


# A Thursday, so "this week" (Monday 2026-08-10) has already begun and next week has not.
NOW = dt.datetime(2026, 8, 13, 4, 0, 0, tzinfo=dt.timezone.utc)
THIS_WEEK = "2026-08-10"
NEXT_WEEK = "2026-08-17"
LAST_WEEK = "2026-08-03"
TIMEZONE = "Asia/Taipei"


def _window(as_of: str = "2026-08-13T12:00:00+08:00"):
    request = ContextRequest(
        as_of_raw=as_of,
        timezone_name=TIMEZONE,
        available_days=[],
        session_minutes=None,
        red_flags={},
        leg_fatigue="unknown",
        soreness="unknown",
        schedule_changed=None,
        equipment_changed=None,
        extra_unknowns=[],
    )
    return build_window(request, NOW)


class EvidenceFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def test_an_athlete_who_reported_nothing_reads_as_empty_without_creating_a_file(self):
        evidence = load_evidence(self.state_dir)

        self.assertIsNone(evidence["availability"]["recurring"])
        self.assertEqual([], evidence["availability"]["week_overrides"])
        self.assertEqual([], evidence["strength_reports"])
        # A read must never bring an account into being; the first-use session route
        # depends on exactly this.
        self.assertFalse(self.state_dir.exists())

    def test_one_recurring_statement_round_trips_with_its_provenance(self):
        record_availability(
            self.state_dir,
            recurring={"available_days": ["mon", "wed", "fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        evidence = load_evidence(self.state_dir)
        recurring = evidence["availability"]["recurring"]
        self.assertEqual(["mon", "wed", "fri"], recurring["available_days"])
        self.assertEqual([], recurring["unavailable_days"])
        self.assertEqual(ATHLETE_REPORTED_SOURCE, recurring["source"])
        self.assertEqual("2026-08-13T04:00:00Z", recurring["recorded_at"])
        self.assertEqual(ATHLETE_EVIDENCE_VERSION, evidence["athlete_evidence_version"])

    def test_a_file_that_cannot_be_parsed_is_an_error_not_an_empty_athlete(self):
        self.state_dir.mkdir(parents=True)
        evidence_path(self.state_dir).write_text("{ not json", encoding="utf-8")

        with self.assertRaises(StateStoreError) as caught:
            load_evidence(self.state_dir)

        self.assertIn("athlete-evidence.json", str(caught.exception))

    def test_a_file_from_a_version_this_code_does_not_write_is_refused(self):
        self.state_dir.mkdir(parents=True)
        evidence_path(self.state_dir).write_text(
            json.dumps({"athlete_evidence_version": 99}), encoding="utf-8"
        )

        with self.assertRaises(StateStoreError):
            load_evidence(self.state_dir)

    def test_a_structurally_wrong_file_is_refused_rather_than_read_around(self):
        self.state_dir.mkdir(parents=True)
        evidence_path(self.state_dir).write_text(
            json.dumps(
                {
                    "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
                    "availability": {"recurring": None, "week_overrides": "mon"},
                    "strength_reports": [],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(StateStoreError):
            load_evidence(self.state_dir)


class RecurringAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def test_the_latest_recurring_statement_replaces_the_previous_one(self):
        record_availability(
            self.state_dir,
            recurring={"available_days": ["mon", "wed", "fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )
        later = NOW + dt.timedelta(days=1)

        result = record_availability(
            self.state_dir,
            recurring={"available_days": ["tue", "thu"]},
            timezone_name=TIMEZONE,
            now=later,
        )

        # One value, not two: an athlete who moved their days has one schedule, and the
        # provenance of the surviving one says when they said so.
        self.assertEqual(["tue", "thu"], result["recurring"]["available_days"])
        self.assertEqual("2026-08-14T04:00:00Z", result["recurring"]["recorded_at"])
        stored = load_evidence(self.state_dir)["availability"]
        self.assertEqual(["tue", "thu"], stored["recurring"]["available_days"])
        self.assertEqual([], stored["week_overrides"])

    def test_unavailable_days_are_stored_beside_available_ones(self):
        result = record_availability(
            self.state_dir,
            recurring={"available_days": ["mon", "tue"], "unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(["mon", "tue"], result["recurring"]["available_days"])
        self.assertEqual(["wed"], result["recurring"]["unavailable_days"])

    def test_full_weekday_names_are_accepted_and_normalized(self):
        result = record_availability(
            self.state_dir,
            recurring={"available_days": ["Monday", " THURSDAY "]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(["mon", "thu"], result["recurring"]["available_days"])

    def test_a_day_named_as_both_available_and_unavailable_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            record_availability(
                self.state_dir,
                recurring={"available_days": ["mon", "wed"], "unavailable_days": ["wed"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

        self.assertIn("wed", str(caught.exception))
        self.assertFalse(evidence_path(self.state_dir).exists())

    def test_a_statement_naming_no_day_at_all_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(
                self.state_dir,
                recurring={"available_days": [], "unavailable_days": []},
                timezone_name=TIMEZONE,
                now=NOW,
            )

    def test_a_weekday_outside_the_seven_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            record_availability(
                self.state_dir,
                recurring={"available_days": ["mon", "someday"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

        self.assertIn("someday", str(caught.exception))

    def test_a_call_stating_neither_kind_of_availability_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(self.state_dir, timezone_name=TIMEZONE, now=NOW)

    def test_an_unknown_timezone_is_named_rather_than_silently_defaulted(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            record_availability(
                self.state_dir,
                recurring={"available_days": ["mon"]},
                timezone_name="Mars/Olympus",
                now=NOW,
            )

        self.assertIn("Mars/Olympus", str(caught.exception))

    def test_normalize_weekday_reports_rather_than_guesses(self):
        self.assertEqual("sat", normalize_weekday("Saturday"))
        self.assertEqual("sat", normalize_weekday("SAT"))
        self.assertIsNone(normalize_weekday("週六"))
        self.assertIsNone(normalize_weekday(6))


class WeekOverrideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"
        record_availability(
            self.state_dir,
            recurring={"available_days": ["mon", "wed", "fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

    def _effective(self, week_start: str) -> dict[str, Any] | None:
        return effective_availability(
            load_evidence(self.state_dir), week_start=dt.date.fromisoformat(week_start)
        )

    def test_an_override_wins_for_its_own_week_and_leaves_the_default_alone(self):
        record_availability(
            self.state_dir,
            week_override={"week_start": THIS_WEEK, "available_days": ["tue", "thu", "sat"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        this_week = self._effective(THIS_WEEK)
        self.assertEqual(["tue", "thu", "sat"], this_week["available_days"])
        self.assertEqual("week_override", this_week["basis"])
        # The recurring default is untouched and is what the following week reads.
        next_week = self._effective(NEXT_WEEK)
        self.assertEqual(["mon", "wed", "fri"], next_week["available_days"])
        self.assertEqual("recurring", next_week["basis"])

    def test_an_override_expires_by_the_calendar_moving_not_by_being_deleted(self):
        record_availability(
            self.state_dir,
            week_override={"week_start": THIS_WEEK, "unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(["wed"], self._effective(THIS_WEEK)["unavailable_days"])
        self.assertEqual("recurring", self._effective(NEXT_WEEK)["basis"])
        # Still on record: it is what the athlete said about that week, and nothing about
        # the week ending makes it untrue.
        stored = load_evidence(self.state_dir)["availability"]["week_overrides"]
        self.assertEqual([THIS_WEEK], [item["week_start"] for item in stored])

    def test_a_second_statement_about_one_week_supersedes_the_first(self):
        record_availability(
            self.state_dir,
            week_override={"week_start": NEXT_WEEK, "available_days": ["tue"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )
        record_availability(
            self.state_dir,
            week_override={"week_start": NEXT_WEEK, "available_days": ["thu", "sat"]},
            timezone_name=TIMEZONE,
            now=NOW + dt.timedelta(hours=2),
        )

        self.assertEqual(["thu", "sat"], self._effective(NEXT_WEEK)["available_days"])
        # Both statements survive; only the newest one answers.
        self.assertEqual(2, len(load_evidence(self.state_dir)["availability"]["week_overrides"]))

    def test_two_corrections_inside_one_second_still_resolve_to_the_later_one(self):
        for days in (["tue"], ["fri"]):
            record_availability(
                self.state_dir,
                week_override={"week_start": NEXT_WEEK, "available_days": days},
                timezone_name=TIMEZONE,
                now=NOW,
            )

        self.assertEqual(["fri"], self._effective(NEXT_WEEK)["available_days"])

    def test_a_week_start_that_is_not_a_monday_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            record_availability(
                self.state_dir,
                week_override={"week_start": "2026-08-18", "available_days": ["tue"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

        self.assertIn("Monday", str(caught.exception))

    def test_a_week_that_has_already_begun_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(
                self.state_dir,
                week_override={"week_start": LAST_WEEK, "available_days": ["tue"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

    def test_the_current_week_is_still_writable_mid_week(self):
        # NOW is a Thursday: the week has begun but has days left in it, which is exactly
        # when "Wednesday is gone this week" gets said.
        result = record_availability(
            self.state_dir,
            week_override={"week_start": THIS_WEEK, "unavailable_days": ["fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(THIS_WEEK, result["week_override"]["week_start"])
        self.assertEqual(THIS_WEEK, result["effective_this_week"]["week_start"])
        self.assertEqual("week_override", result["effective_this_week"]["basis"])

    def test_an_override_naming_no_day_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(
                self.state_dir,
                week_override={"week_start": NEXT_WEEK},
                timezone_name=TIMEZONE,
                now=NOW,
            )

    def test_week_start_for_names_the_monday_of_the_natural_week(self):
        self.assertEqual(dt.date(2026, 8, 10), week_start_for(dt.date(2026, 8, 13)))
        self.assertEqual(dt.date(2026, 8, 10), week_start_for(dt.date(2026, 8, 10)))
        self.assertEqual(dt.date(2026, 8, 10), week_start_for(dt.date(2026, 8, 16)))


class StrengthReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _report(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "date": "2026-08-13",
            "exercise": "bench press",
            "category": "chest",
            "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
        }
        payload.update(overrides)
        return record_strength_report(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def test_one_report_is_stored_verbatim_with_every_set_field_present(self):
        result = self._report()

        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(1, result["report_count"])
        report = result["report"]
        self.assertEqual(ATHLETE_REPORTED_SOURCE, report["source"])
        self.assertEqual("2026-08-13T04:00:00Z", report["recorded_at"])
        # Omitted measurements become explicit nulls; nothing is estimated into them.
        self.assertEqual(
            {"set": 1, "weight_kg": 65, "assist_kg": None, "reps": 4, "rpe": None},
            report["sets"][0],
        )

    def test_the_same_report_sent_twice_is_stored_once_and_says_so(self):
        first = self._report()
        second = self._report()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(1, len(load_evidence(self.state_dir)["strength_reports"]))

    def test_a_report_whose_content_differs_is_a_new_report(self):
        self._report()
        second = self._report(sets=[{"set": 1, "weight_kg": 67.5, "reps": 4}])

        self.assertFalse(second["idempotent_replay"])
        self.assertEqual(2, len(load_evidence(self.state_dir)["strength_reports"]))

    def test_a_date_in_the_athletes_future_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._report(date="2026-08-14")

        self.assertIn("future", str(caught.exception))
        self.assertFalse(evidence_path(self.state_dir).exists())

    def test_today_in_the_athletes_own_timezone_is_not_the_future(self):
        # NOW is 2026-08-13T04:00Z, which is midday on the 13th in Taipei (UTC+8) and
        # still the evening of the 12th in Honolulu (UTC-10). The athlete's own zone
        # decides which of the two "today" is, never the server's.
        self._report(date="2026-08-13")
        with self.assertRaises(AthleteEvidenceError):
            record_strength_report(
                Path(self._tmp.name) / "other-owner",
                date="2026-08-13",
                exercise="bench press",
                category="chest",
                sets=[{"set": 1, "weight_kg": 65, "reps": 4}],
                timezone_name="Pacific/Honolulu",
                now=NOW,
            )

    def test_a_set_field_this_shape_never_had_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._report(sets=[{"set": 1, "weigth_kg": 65}])

        self.assertIn("weigth_kg", str(caught.exception))

    def test_a_set_without_a_number_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._report(sets=[{"weight_kg": 65, "reps": 4}])

    def test_set_values_must_hold_the_types_strength_execution_already_uses(self):
        for sets in (
            [{"set": 0, "reps": 4}],
            [{"set": 1, "reps": 4.5}],
            [{"set": 1, "weight_kg": "65"}],
            [{"set": 1, "rpe": True}],
        ):
            with self.subTest(sets=sets):
                with self.assertRaises(AthleteEvidenceError):
                    self._report(sets=sets)

    def test_an_empty_or_missing_exercise_is_refused(self):
        for overrides in ({"exercise": ""}, {"category": "  "}, {"sets": []}):
            with self.subTest(**overrides):
                with self.assertRaises(AthleteEvidenceError):
                    self._report(**overrides)


class StrengthExecutionGroupTests(unittest.TestCase):
    """Reported lifts, assembled into the group the coach already reads (issue #47)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _record(self, **payload: Any) -> None:
        record_strength_report(
            self.state_dir,
            timezone_name=TIMEZONE,
            now=payload.pop("now", NOW),
            **payload,
        )

    def test_nothing_reported_produces_no_group_at_all(self):
        self.assertIsNone(
            strength_execution_from_reports(load_evidence(self.state_dir), _window())
        )

    def test_reports_become_a_group_ordered_the_way_the_health_db_one_is(self):
        self._record(
            date="2026-08-11",
            exercise="squat",
            category="legs",
            sets=[{"set": 1, "weight_kg": 80, "reps": 5}],
        )
        self._record(
            date="2026-08-13",
            exercise="bench press",
            category="chest",
            sets=[{"set": 1, "weight_kg": 65, "reps": 4}],
        )
        self._record(
            date="2026-08-13",
            exercise="pull-up",
            category="back",
            sets=[{"set": 1, "assist_kg": 15, "reps": 8}],
        )

        group = strength_execution_from_reports(load_evidence(self.state_dir), _window())

        self.assertEqual(ATHLETE_REPORTED_SOURCE, group["source"])
        self.assertEqual("2026-08-13", group["window_end"])
        # Dates newest first, exercises alphabetical within a date.
        self.assertEqual(
            [("2026-08-13", "bench press"), ("2026-08-13", "pull-up"), ("2026-08-11", "squat")],
            [(item["date"], item["exercise"]) for item in group["sessions"]],
        )

    def test_several_reports_for_one_exercise_become_one_session_numbered_once(self):
        self._record(
            date="2026-08-13",
            exercise="bench press",
            category="chest",
            sets=[{"set": 1, "weight_kg": 65, "reps": 4}],
            notes=["felt heavy"],
        )
        self._record(
            date="2026-08-13",
            exercise="bench press",
            category="chest",
            sets=[{"set": 1, "weight_kg": 65, "reps": 3}, {"set": 2, "weight_kg": 60, "reps": 4}],
            notes=["felt heavy"],
            now=NOW + dt.timedelta(minutes=20),
        )

        group = strength_execution_from_reports(load_evidence(self.state_dir), _window())

        session = group["sessions"][0]
        # One session, sets renumbered end to end -- two reports each starting at set 1
        # described one session, not two, and would otherwise show two "set 1"s.
        self.assertEqual(1, len(group["sessions"]))
        self.assertEqual([1, 2, 3], [item["set"] for item in session["sets"]])
        self.assertEqual([4, 3, 4], [item["reps"] for item in session["sets"]])
        self.assertEqual(["felt heavy"], session["notes"])

    def test_a_report_outside_the_window_is_not_in_the_group(self):
        self._record(
            date="2026-06-01",
            exercise="squat",
            category="legs",
            sets=[{"set": 1, "weight_kg": 80, "reps": 5}],
        )

        self.assertIsNone(
            strength_execution_from_reports(load_evidence(self.state_dir), _window())
        )


class BeforeAnyPlanTests(unittest.TestCase):
    """Stating availability before deciding what to train is the ordinary first use.

    ``init_store`` refuses a directory that is already in use, and a stored statement made
    before the plan existed would otherwise read as exactly that -- an athlete who
    answered "which days can you train" in the first message would be unable to
    initialize at all.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"
        self.plan = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "examples"
                / "garmin-coach-loop-28-day"
                / "plan-state-v1.json"
            ).read_text(encoding="utf-8")
        )

    def test_a_plan_still_initializes_after_availability_was_stated(self):
        record_availability(
            self.state_dir,
            recurring={"available_days": ["mon", "wed", "fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        result = init_store(self.state_dir, self.plan)

        self.assertEqual("initialized", result["status"])
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])
        # And the statement survives the initialization that followed it.
        self.assertEqual(
            ["mon", "wed", "fri"],
            load_evidence(self.state_dir)["availability"]["recurring"]["available_days"],
        )

    def test_anything_else_in_the_directory_still_refuses_initialization(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "something-else.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(StateStoreError):
            init_store(self.state_dir, self.plan)


class OwnerIsolationTests(unittest.TestCase):
    def test_one_athletes_statements_never_reach_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "owner-a"
            second = Path(tmp) / "owner-b"

            record_availability(
                first,
                recurring={"available_days": ["mon", "wed"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )
            record_strength_report(
                first,
                date="2026-08-13",
                exercise="bench press",
                category="chest",
                sets=[{"set": 1, "weight_kg": 65, "reps": 4}],
                timezone_name=TIMEZONE,
                now=NOW,
            )
            record_availability(
                second,
                recurring={"available_days": ["tue", "thu"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

            first_evidence = load_evidence(first)
            second_evidence = load_evidence(second)
            self.assertEqual(
                ["mon", "wed"], first_evidence["availability"]["recurring"]["available_days"]
            )
            self.assertEqual(
                ["tue", "thu"], second_evidence["availability"]["recurring"]["available_days"]
            )
            self.assertEqual(1, len(first_evidence["strength_reports"]))
            self.assertEqual([], second_evidence["strength_reports"])


if __name__ == "__main__":
    unittest.main()
