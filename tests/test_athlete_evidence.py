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
    PRESCRIBED_CONFIRMED_SOURCE,
    REPORTABLE_SPORTS,
    AthleteEvidenceError,
    body_measurement_series,
    confirm_prescribed_strength,
    effective_availability,
    evidence_path,
    exercise_key,
    load_evidence,
    normalize_weekday,
    profile_language,
    profile_timezone,
    record_activity_summary,
    record_availability,
    record_body_measurement,
    record_profile,
    record_strength_report,
    reported_activity_summaries,
    reported_strength_sessions,
    resolve_settings,
    retract_activity_summary,
    retract_body_measurement,
    retract_strength_report,
    stored_profile,
    week_start_for,
)
from garmin_coach_loop.context_core import ContextRequest, build_window
from garmin_coach_loop.store import StateStoreError, doctor_store, init_store


# A Thursday, so "this week" (Monday 2026-08-10) has already begun and next week has not.
NOW = dt.datetime(2026, 8, 13, 4, 0, 0, tzinfo=dt.timezone.utc)
TODAY = "2026-08-13"  # NOW's date in the athlete's own timezone (Asia/Taipei, UTC+8).
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
        self.assertEqual([], evidence["body_measurements"])
        self.assertEqual([], evidence["reported_activities"])
        # A read must never bring an account into being; the first-use session route
        # depends on exactly this.
        self.assertFalse(self.state_dir.exists())

    def test_a_file_written_before_the_new_groups_existed_still_opens(self):
        """The compatibility property, checked against a real pre-existing file.

        Absent and empty are the same fact -- nothing reported -- so an evidence file from
        a checkout that had never heard of measurements or reported sessions reads as an
        athlete who has stated none of either. Moving the version number instead would
        refuse that whole file, taking the athlete's availability, profile and every
        reported lift with it, for the sake of two keys nobody had written yet.
        """
        self.state_dir.mkdir(parents=True)
        evidence_path(self.state_dir).write_text(
            json.dumps(
                {
                    "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
                    "profile": {
                        "timezone": TIMEZONE,
                        "language": "zh-Hant",
                        "recorded_at": "2026-08-01T00:00:00Z",
                        "source": ATHLETE_REPORTED_SOURCE,
                    },
                    "availability": {
                        "recurring": {
                            "available_days": ["mon", "wed", "fri"],
                            "unavailable_days": [],
                            "recorded_at": "2026-08-01T00:00:00Z",
                            "source": ATHLETE_REPORTED_SOURCE,
                        },
                        "week_overrides": [],
                    },
                    "strength_reports": [
                        {
                            "report_id": "r1",
                            "date": TODAY,
                            "exercise": "bench press",
                            "category": None,
                            "sets": [
                                {
                                    "set": 1,
                                    "weight_kg": 65,
                                    "assist_kg": None,
                                    "reps": 4,
                                    "rpe": None,
                                }
                            ],
                            "notes": [],
                            "recorded_at": "2026-08-01T00:00:00Z",
                            "source": ATHLETE_REPORTED_SOURCE,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        evidence = load_evidence(self.state_dir)

        self.assertEqual([], evidence["body_measurements"])
        self.assertEqual([], evidence["reported_activities"])
        # And nothing that was already in the file was lost on the way through.
        self.assertEqual(TIMEZONE, evidence["profile"]["timezone"])
        self.assertEqual(1, len(evidence["strength_reports"]))
        self.assertEqual(
            ["mon", "wed", "fri"], evidence["availability"]["recurring"]["available_days"]
        )

        # A write lands beside them rather than rewriting the file into a new shape.
        record_body_measurement(self.state_dir, weight_kg=72.5, timezone_name=TIMEZONE, now=NOW)
        reloaded = load_evidence(self.state_dir)
        self.assertEqual(1, len(reloaded["body_measurements"]))
        self.assertEqual(1, len(reloaded["strength_reports"]))
        self.assertEqual(ATHLETE_EVIDENCE_VERSION, reloaded["athlete_evidence_version"])

    def test_a_new_group_that_is_not_an_array_is_refused_like_every_other_container(self):
        self.state_dir.mkdir(parents=True)
        evidence_path(self.state_dir).write_text(
            json.dumps(
                {
                    "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
                    "availability": {"recurring": None, "week_overrides": []},
                    "strength_reports": [],
                    "body_measurements": {"2026-08-13": 72.5},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(StateStoreError) as caught:
            load_evidence(self.state_dir)
        self.assertIn("body_measurements", str(caught.exception))

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


class ProfileTests(unittest.TestCase):
    """Where the athlete is and what they read, said once and standing until restated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def test_an_athlete_who_said_nothing_has_no_profile_and_gets_the_documented_defaults(self):
        self.assertIsNone(stored_profile(load_evidence(self.state_dir)))
        self.assertEqual(("Asia/Taipei", "zh-Hant"), resolve_settings(self.state_dir))
        self.assertFalse(self.state_dir.exists())

    def test_a_stated_timezone_is_what_every_later_call_reads(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", now=NOW)

        # A second, entirely separate read -- the next conversation -- sees it without
        # anybody restating it.
        self.assertEqual(("Europe/Berlin", "zh-Hant"), resolve_settings(self.state_dir))
        profile = stored_profile(load_evidence(self.state_dir))
        self.assertEqual("Europe/Berlin", profile["timezone"])
        self.assertEqual(ATHLETE_REPORTED_SOURCE, profile["source"])
        self.assertEqual("2026-08-13T04:00:00Z", profile["recorded_at"])

    def test_stating_one_field_leaves_the_other_exactly_where_it_was(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", now=NOW)
        record_profile(self.state_dir, language="en", now=NOW)

        self.assertEqual(("Europe/Berlin", "en"), resolve_settings(self.state_dir))

    def test_a_later_statement_replaces_the_earlier_one(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", now=NOW)
        record_profile(self.state_dir, timezone="America/New_York", now=NOW)

        self.assertEqual("America/New_York", resolve_settings(self.state_dir)[0])

    def test_a_request_timezone_stands_in_front_of_the_stored_one_for_that_call_only(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", language="en", now=NOW)

        self.assertEqual(
            ("Asia/Tokyo", "en"),
            resolve_settings(self.state_dir, timezone_override="Asia/Tokyo"),
        )
        # And the next call, which states nothing, is back where the athlete lives.
        self.assertEqual("Europe/Berlin", resolve_settings(self.state_dir)[0])

    def test_a_timezone_that_is_not_an_iana_zone_is_refused_before_anything_is_written(self):
        with self.assertRaises(AthleteEvidenceError) as raised:
            record_profile(self.state_dir, timezone="Nowhere/Nothing", now=NOW)

        self.assertIn("Nowhere/Nothing", str(raised.exception))
        self.assertFalse(evidence_path(self.state_dir).exists())

    def test_an_override_that_is_not_an_iana_zone_is_refused_rather_than_ignored(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", now=NOW)

        # Falling through to the stored value would answer about the wrong day while
        # looking like it had honoured the request.
        with self.assertRaises(AthleteEvidenceError):
            resolve_settings(self.state_dir, timezone_override="Nowhere/Nothing")

    def test_a_language_nothing_can_render_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as raised:
            record_profile(self.state_dir, language="fr", now=NOW)

        self.assertIn("fr", str(raised.exception))

    def test_a_call_stating_neither_field_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_profile(self.state_dir, now=NOW)

    def test_the_profile_sits_beside_the_other_statements_without_disturbing_them(self):
        record_availability(
            self.state_dir,
            recurring={"available_days": ["mon", "wed", "fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )
        record_profile(self.state_dir, timezone="Europe/Berlin", language="en", now=NOW)

        evidence = load_evidence(self.state_dir)
        self.assertEqual(
            ["mon", "wed", "fri"], evidence["availability"]["recurring"]["available_days"]
        )
        self.assertEqual("Europe/Berlin", evidence["profile"]["timezone"])
        # The file version does not move for an additive container.
        self.assertEqual(
            ATHLETE_EVIDENCE_VERSION, evidence["athlete_evidence_version"]
        )

    def test_a_profile_that_is_not_an_object_makes_the_whole_file_unreadable(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", now=NOW)
        path = evidence_path(self.state_dir)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["profile"] = "Europe/Berlin"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(StateStoreError):
            load_evidence(self.state_dir)

    def test_a_field_that_cannot_be_read_degrades_to_unstated_not_to_no_profile(self):
        record_profile(self.state_dir, timezone="Europe/Berlin", language="en", now=NOW)
        path = evidence_path(self.state_dir)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["profile"]["language"] = "klingon"
        path.write_text(json.dumps(raw), encoding="utf-8")

        profile = stored_profile(load_evidence(self.state_dir))
        self.assertEqual("Europe/Berlin", profile_timezone(profile))
        self.assertEqual("zh-Hant", profile_language(profile))

    def test_a_plan_still_initializes_after_a_profile_was_stated(self):
        # The same guarantee availability has: an athlete says where they are in the
        # first message, before there is anything to train.
        plan = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "examples"
                / "garmin-coach-loop-28-day"
                / "plan-state-v1.json"
            ).read_text(encoding="utf-8")
        )
        record_profile(self.state_dir, timezone="Europe/Berlin", now=NOW)

        self.assertEqual("initialized", init_store(self.state_dir, plan)["status"])
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])
        self.assertEqual("Europe/Berlin", resolve_settings(self.state_dir)[0])


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


class WeekStatementTests(unittest.TestCase):
    """A week statement layers onto the recurring default; it never replaces it.

    Every test starts from the same recurring Mon/Wed/Fri default, set once here, so each
    one exercises a different way a week statement then alters it for its own week.
    """

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

    def test_an_unavailable_day_this_week_is_subtracted_from_the_recurring_default(self):
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        effective = self._effective(THIS_WEEK)
        self.assertEqual(["mon", "fri"], effective["available_days"])
        self.assertEqual(["wed"], effective["unavailable_days"])
        self.assertEqual("recurring_adjusted", effective["basis"])
        # The recurring default itself, and any week with no statement, is untouched.
        next_week = self._effective(NEXT_WEEK)
        self.assertEqual(["mon", "wed", "fri"], next_week["available_days"])
        self.assertEqual("recurring", next_week["basis"])

    def test_an_available_day_this_week_is_added_to_the_recurring_default(self):
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "available_days": ["sat"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        effective = self._effective(THIS_WEEK)
        self.assertEqual(["mon", "wed", "fri", "sat"], effective["available_days"])
        self.assertEqual([], effective["unavailable_days"])
        self.assertEqual("recurring_adjusted", effective["basis"])

    def test_only_days_restates_the_week_in_full(self):
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "only_days": ["tue", "thu"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        # "Only" Tue/Thu means the rest of the recurring default -- Mon/Wed/Fri, which
        # the athlete never mentioned -- is out for this week too, not merely unlisted.
        effective = self._effective(THIS_WEEK)
        self.assertEqual(["tue", "thu"], effective["available_days"])
        self.assertEqual(["mon", "wed", "fri"], effective["unavailable_days"])
        self.assertEqual("recurring_adjusted", effective["basis"])

    def test_only_days_combined_with_a_day_list_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(
                self.state_dir,
                week={"week_start": THIS_WEEK, "only_days": ["tue"], "available_days": ["sat"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

    def test_two_week_statements_about_one_week_compose_in_order(self):
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "unavailable_days": ["fri"]},
            timezone_name=TIMEZONE,
            now=NOW + dt.timedelta(hours=2),
        )

        # "Wednesday's out" then "Friday too": Monday is what's left of Mon/Wed/Fri.
        effective = self._effective(THIS_WEEK)
        self.assertEqual(["mon"], effective["available_days"])
        self.assertEqual(["wed", "fri"], effective["unavailable_days"])
        # Both statements are on record; composing at read time is not the same as
        # collapsing them into one on write.
        self.assertEqual(2, len(load_evidence(self.state_dir)["availability"]["week_overrides"]))

    def test_a_week_statement_can_take_back_a_day_it_previously_removed(self):
        # Both calls share one instant: recorded_at alone cannot order them, so this also
        # exercises the list-position tie-break that keeps composition deterministic.
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "available_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        effective = self._effective(THIS_WEEK)
        self.assertEqual(["mon", "wed", "fri"], effective["available_days"])
        self.assertEqual([], effective["unavailable_days"])

    def test_a_week_statement_with_no_week_start_targets_the_current_week(self):
        result = record_availability(
            self.state_dir,
            week={"unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(THIS_WEEK, result["week"]["week_start"])
        self.assertEqual(["mon", "fri"], result["effective_this_week"]["available_days"])

    def test_a_week_start_naming_any_day_in_the_week_resolves_to_its_monday(self):
        # 2026-08-19 is NEXT_WEEK's Wednesday, not its Monday -- the athlete says "next
        # Wednesday" and means the week it falls in.
        result = record_availability(
            self.state_dir,
            week={"week_start": "2026-08-19", "available_days": ["sat"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(NEXT_WEEK, result["week"]["week_start"])

    def test_a_week_statement_expires_by_the_calendar_moving_not_by_being_deleted(self):
        record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "unavailable_days": ["wed"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(["wed"], self._effective(THIS_WEEK)["unavailable_days"])
        self.assertEqual("recurring", self._effective(NEXT_WEEK)["basis"])
        # Still on record: it is what the athlete said about that week, and nothing about
        # the week ending makes it untrue.
        stored = load_evidence(self.state_dir)["availability"]["week_overrides"]
        self.assertEqual([THIS_WEEK], [item["week_start"] for item in stored])

    def test_a_week_that_has_already_passed_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(
                self.state_dir,
                week={"week_start": LAST_WEEK, "available_days": ["tue"]},
                timezone_name=TIMEZONE,
                now=NOW,
            )

    def test_the_current_week_is_still_writable_mid_week(self):
        # NOW is a Thursday: the week has begun but has days left in it, which is exactly
        # when "Wednesday is gone this week" gets said.
        result = record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "unavailable_days": ["fri"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(THIS_WEEK, result["week"]["week_start"])
        self.assertEqual(THIS_WEEK, result["effective_this_week"]["week_start"])
        self.assertEqual("recurring_adjusted", result["effective_this_week"]["basis"])

    def test_a_week_statement_naming_no_day_at_all_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            record_availability(
                self.state_dir,
                week={"week_start": NEXT_WEEK},
                timezone_name=TIMEZONE,
                now=NOW,
            )

    def test_week_start_for_names_the_monday_of_the_natural_week(self):
        self.assertEqual(dt.date(2026, 8, 10), week_start_for(dt.date(2026, 8, 13)))
        self.assertEqual(dt.date(2026, 8, 10), week_start_for(dt.date(2026, 8, 10)))
        self.assertEqual(dt.date(2026, 8, 10), week_start_for(dt.date(2026, 8, 16)))


class EffectiveAvailabilityWithoutRecurringTests(unittest.TestCase):
    """A week statement can stand on its own, and silence is not the same as "no days"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def test_nothing_stated_for_the_week_returns_none(self):
        self.assertIsNone(
            effective_availability(
                load_evidence(self.state_dir), week_start=dt.date.fromisoformat(THIS_WEEK)
            )
        )

    def test_a_week_statement_with_no_recurring_default_answers_on_its_own(self):
        result = record_availability(
            self.state_dir,
            week={"week_start": THIS_WEEK, "available_days": ["tue", "thu"]},
            timezone_name=TIMEZONE,
            now=NOW,
        )

        self.assertEqual(["tue", "thu"], result["effective_this_week"]["available_days"])
        self.assertEqual("week", result["effective_this_week"]["basis"])


class ExerciseKeyTests(unittest.TestCase):
    """The identity a correction has to match, independent of ``record_strength_report``."""

    def test_case_and_separators_fold_to_the_same_key(self):
        self.assertEqual(exercise_key("bench press"), exercise_key("Bench Press"))
        self.assertEqual(exercise_key("bench press"), exercise_key("bench_press"))
        self.assertEqual(exercise_key("bench press"), exercise_key("bench-press"))
        self.assertEqual(exercise_key("bench press"), exercise_key("  BENCH   press "))

    def test_different_movements_keep_different_keys(self):
        # Nothing wider than case/separator folding: "bench" is not "bench press".
        self.assertNotEqual(exercise_key("bench press"), exercise_key("bench"))


class StrengthReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _report(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "date": TODAY,
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
        self.assertIsNone(result["replaced"])
        self.assertEqual(1, result["report_count"])
        report = result["report"]
        self.assertEqual(ATHLETE_REPORTED_SOURCE, report["source"])
        self.assertEqual("2026-08-13T04:00:00Z", report["recorded_at"])
        # Omitted measurements become explicit nulls; nothing is estimated into them.
        self.assertEqual(
            {"set": 1, "weight_kg": 65, "assist_kg": None, "reps": 4, "rpe": None},
            report["sets"][0],
        )

    def test_only_exercise_and_sets_are_required(self):
        result = record_strength_report(
            self.state_dir,
            exercise="deadlift",
            sets=[{"weight_kg": 100, "reps": 3}],
            timezone_name=TIMEZONE,
            now=NOW,
        )

        report = result["report"]
        self.assertEqual(TODAY, report["date"])
        self.assertIsNone(report["category"])
        self.assertEqual(1, len(report["sets"]))
        self.assertEqual(1, report["sets"][0]["set"])

    def test_a_non_string_category_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._report(category=123)

    def test_the_same_report_sent_twice_is_stored_once_and_says_so(self):
        first = self._report()
        second = self._report()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertIsNone(second["replaced"])
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(1, second["report_count"])
        self.assertEqual(1, len(load_evidence(self.state_dir)["strength_reports"]))

    def test_correcting_a_lift_replaces_the_report_instead_of_doubling_it(self):
        first = self._report(sets=[{"weight_kg": 65, "reps": 4}])
        second = self._report(sets=[{"weight_kg": 70, "reps": 4}])

        self.assertFalse(second["idempotent_replay"])
        self.assertEqual(first["report"], second["replaced"])
        self.assertEqual(1, second["report_count"])
        stored = load_evidence(self.state_dir)["strength_reports"]
        # This was the bug: a correction must leave exactly one report holding the
        # corrected weight, never two reports the coach would read as double the volume.
        self.assertEqual(1, len(stored))
        self.assertEqual(
            [{"set": 1, "weight_kg": 70, "assist_kg": None, "reps": 4, "rpe": None}],
            stored[0]["sets"],
        )

    def test_different_spellings_of_one_movement_correct_each_other(self):
        first = self._report(exercise="bench press", sets=[{"weight_kg": 65, "reps": 4}])
        second = self._report(exercise="Bench Press", sets=[{"weight_kg": 70, "reps": 4}])
        third = self._report(exercise="bench_press", sets=[{"weight_kg": 72, "reps": 4}])

        self.assertEqual(first["report"], second["replaced"])
        self.assertEqual(second["report"], third["replaced"])
        self.assertEqual(1, third["report_count"])
        stored = load_evidence(self.state_dir)["strength_reports"]
        self.assertEqual(1, len(stored))
        self.assertEqual(72, stored[0]["sets"][0]["weight_kg"])

    def test_a_different_exercise_or_day_is_a_new_report_not_a_replacement(self):
        self._report(exercise="bench press", sets=[{"weight_kg": 65, "reps": 4}])
        different_exercise = self._report(exercise="squat", sets=[{"weight_kg": 100, "reps": 5}])
        different_day = self._report(
            date="2026-08-12", exercise="bench press", sets=[{"weight_kg": 65, "reps": 4}]
        )

        self.assertIsNone(different_exercise["replaced"])
        self.assertIsNone(different_day["replaced"])
        self.assertEqual(3, len(load_evidence(self.state_dir)["strength_reports"]))

    def test_a_date_in_the_athletes_future_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._report(date="2026-08-14")

        self.assertIn("future", str(caught.exception))
        self.assertFalse(evidence_path(self.state_dir).exists())

    def test_today_in_the_athletes_own_timezone_is_not_the_future(self):
        # NOW is 2026-08-13T04:00Z, which is midday on the 13th in Taipei (UTC+8) and
        # still the evening of the 12th in Honolulu (UTC-10). The athlete's own zone
        # decides which of the two "today" is, never the server's.
        self._report(date=TODAY)
        with self.assertRaises(AthleteEvidenceError):
            record_strength_report(
                Path(self._tmp.name) / "other-owner",
                date=TODAY,
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

    def test_a_set_without_a_number_is_numbered_by_its_position(self):
        result = self._report(
            sets=[{"weight_kg": 65, "reps": 4}, {"weight_kg": 60, "reps": 6}]
        )

        self.assertEqual([1, 2], [item["set"] for item in result["report"]["sets"]])

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


class ReportedStrengthSessionsTests(unittest.TestCase):
    """Reported lifts, shaped for the group ``context_builder`` assembles (issue #47)."""

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

    def test_nothing_reported_returns_an_empty_list(self):
        self.assertEqual(
            [], reported_strength_sessions(load_evidence(self.state_dir), _window())
        )

    def test_sessions_are_ordered_dates_newest_first_then_exercise_alphabetically(self):
        self._record(
            date="2026-08-11",
            exercise="squat",
            category="legs",
            sets=[{"set": 1, "weight_kg": 80, "reps": 5}],
        )
        self._record(
            date=TODAY,
            exercise="bench press",
            category="chest",
            sets=[{"set": 1, "weight_kg": 65, "reps": 4}],
        )
        self._record(
            date=TODAY,
            exercise="pull-up",
            category="back",
            sets=[{"set": 1, "assist_kg": 15, "reps": 8}],
        )

        sessions = reported_strength_sessions(load_evidence(self.state_dir), _window())

        # Dates newest first, exercises alphabetical within a date.
        self.assertEqual(
            [(TODAY, "bench press"), (TODAY, "pull-up"), ("2026-08-11", "squat")],
            [(item["date"], item["exercise"]) for item in sessions],
        )
        self.assertTrue(all(item["source"] == ATHLETE_REPORTED_SOURCE for item in sessions))

    def test_two_different_movements_on_the_same_day_both_survive_as_separate_sessions(self):
        self._record(date=TODAY, exercise="bench press", sets=[{"weight_kg": 65, "reps": 4}])
        self._record(date=TODAY, exercise="squat", sets=[{"weight_kg": 100, "reps": 5}])

        sessions = reported_strength_sessions(load_evidence(self.state_dir), _window())

        self.assertEqual(
            [(TODAY, "bench press"), (TODAY, "squat")],
            [(item["date"], item["exercise"]) for item in sessions],
        )

    def test_a_category_less_report_still_reaches_the_sessions_list(self):
        # A missing category used to be treated as damage and dropped; it is the ordinary
        # case now, since a plan or the provider's own session label usually implies it.
        self._record(date=TODAY, exercise="farmer carry", sets=[{"weight_kg": 24, "reps": 20}])

        sessions = reported_strength_sessions(load_evidence(self.state_dir), _window())

        self.assertEqual(1, len(sessions))
        self.assertIsNone(sessions[0]["category"])

    def test_a_correction_reaches_the_sessions_list_as_one_session_not_two(self):
        self._record(date=TODAY, exercise="bench press", sets=[{"weight_kg": 65, "reps": 4}])
        self._record(
            date=TODAY,
            exercise="bench press",
            sets=[{"weight_kg": 70, "reps": 4}],
            now=NOW + dt.timedelta(minutes=20),
        )

        sessions = reported_strength_sessions(load_evidence(self.state_dir), _window())

        self.assertEqual(1, len(sessions))
        self.assertEqual(
            [{"set": 1, "weight_kg": 70, "assist_kg": None, "reps": 4, "rpe": None}],
            sessions[0]["sets"],
        )

    def test_a_report_outside_the_window_is_not_in_the_list(self):
        self._record(
            date="2026-06-01",
            exercise="squat",
            category="legs",
            sets=[{"set": 1, "weight_kg": 80, "reps": 5}],
        )

        self.assertEqual(
            [], reported_strength_sessions(load_evidence(self.state_dir), _window())
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
                date=TODAY,
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


def _strength_session(**overrides: Any) -> dict[str, Any]:
    """A prescribed strength session, in the shape PlanState actually stores one."""
    session: dict[str, Any] = {
        "session_id": "strength-thu-01",
        "sport": "strength",
        "scheduled_date": TODAY,
        "purpose": "胸日",
        "plan": {
            "kind": "movement_list",
            "movements": [
                {
                    "exercise": "bench press",
                    "display_name": "臥推",
                    "sets": 4,
                    "reps": 5,
                    "load_kg": 65,
                    "assist_kg": None,
                    "load_basis": "measured_baseline",
                },
                {
                    "exercise": "pull up",
                    "display_name": "引體向上",
                    "sets": 3,
                    "reps": 8,
                    "load_kg": None,
                    "assist_kg": 15,
                    "load_basis": "measured_baseline",
                },
            ],
        },
    }
    session.update(overrides)
    return session


class PrescribedStrengthConfirmationTests(unittest.TestCase):
    """Issue #76: the plan already holds the sets, so confirming is one sentence.

    Running closes its own loop -- delivered, executed, returned, reconciled. Lifting has
    no return path a device can supply, so the athlete's word is the evidence, and making
    them dictate a prescription back is the friction that produced a phantom baseline.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _confirm(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"session": _strength_session()}
        payload.update(overrides)
        return confirm_prescribed_strength(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def test_confirming_records_every_prescribed_set_without_dictating_them(self):
        result = self._confirm()

        self.assertEqual(PRESCRIBED_CONFIRMED_SOURCE, result["source"])
        self.assertEqual(TODAY, result["date"])
        self.assertEqual(2, result["report_count"])
        bench = result["movements"][0]["report"]
        self.assertEqual("bench press", bench["exercise"])
        # The session's own purpose becomes the category: the athlete's label for the
        # session, never a question put to them about which body part a lift trains.
        self.assertEqual("胸日", bench["category"])
        self.assertEqual(PRESCRIBED_CONFIRMED_SOURCE, bench["source"])
        self.assertEqual(
            [
                {"set": number, "weight_kg": 65, "assist_kg": None, "reps": 5, "rpe": None}
                for number in (1, 2, 3, 4)
            ],
            bench["sets"],
        )
        # An assisted movement keeps its assistance and stays weightless, rather than
        # having a load invented for it.
        pull_up = result["movements"][1]["report"]
        self.assertEqual(3, len(pull_up["sets"]))
        self.assertEqual(
            {"set": 1, "weight_kg": None, "assist_kg": 15, "reps": 8, "rpe": None},
            pull_up["sets"][0],
        )

    def test_a_deviation_overwrites_only_the_set_it_names(self):
        result = self._confirm(
            deviations=[{"exercise": "bench press", "set": 4, "reps": 3}]
        )

        bench = result["movements"][0]["report"]
        self.assertEqual([5, 5, 5, 3], [item["reps"] for item in bench["sets"]])
        # The load was not mentioned, so it stays exactly as prescribed.
        self.assertEqual([65, 65, 65, 65], [item["weight_kg"] for item in bench["sets"]])
        # And the movement nobody mentioned is untouched.
        self.assertEqual(
            [8, 8, 8], [item["reps"] for item in result["movements"][1]["report"]["sets"]]
        )

    def test_confirming_twice_is_a_replay_and_stores_nothing_twice(self):
        first = self._confirm()
        again = self._confirm()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(2, again["report_count"])
        self.assertEqual(
            [item["report_id"] for item in first["movements"]],
            [item["report_id"] for item in again["movements"]],
        )

    def test_a_correction_after_confirming_replaces_rather_than_appends(self):
        self._confirm()
        corrected = record_strength_report(
            self.state_dir,
            timezone_name=TIMEZONE,
            now=NOW,
            date=TODAY,
            exercise="bench press",
            sets=[{"set": number, "weight_kg": 70, "reps": 5} for number in (1, 2, 3, 4)],
        )

        # One record per movement per day still holds across the two ways of writing one,
        # and the later statement wins: a measured recollection displaces a confirmation.
        self.assertEqual(2, corrected["report_count"])
        self.assertEqual(PRESCRIBED_CONFIRMED_SOURCE, corrected["replaced"]["source"])
        self.assertEqual(ATHLETE_REPORTED_SOURCE, corrected["report"]["source"])

    def test_the_context_group_keeps_the_two_kinds_of_statement_apart(self):
        self._confirm()
        record_strength_report(
            self.state_dir,
            timezone_name=TIMEZONE,
            now=NOW,
            date="2026-08-11",
            exercise="squat",
            sets=[{"set": 1, "weight_kg": 80, "reps": 5}],
        )

        by_exercise = {
            session["exercise"]: session["source"]
            for session in reported_strength_sessions(load_evidence(self.state_dir), _window())
        }
        self.assertEqual(
            {
                "bench press": PRESCRIBED_CONFIRMED_SOURCE,
                "pull up": PRESCRIBED_CONFIRMED_SOURCE,
                "squat": ATHLETE_REPORTED_SOURCE,
            },
            by_exercise,
        )

    def test_a_session_still_in_the_future_cannot_have_been_done(self):
        with self.assertRaisesRegex(AthleteEvidenceError, "still in the future"):
            self._confirm(session=_strength_session(scheduled_date=NEXT_WEEK))

    def test_a_session_with_nothing_prescribed_says_so_instead_of_recording_nothing(self):
        with self.assertRaisesRegex(AthleteEvidenceError, "prescribes no movements"):
            self._confirm(
                session=_strength_session(plan={"kind": "unstructured"})
            )

    def test_a_running_session_is_not_confirmable_this_way(self):
        with self.assertRaisesRegex(AthleteEvidenceError, "only a strength session"):
            self._confirm(session=_strength_session(sport="running"))

    def test_a_deviation_naming_a_movement_the_session_does_not_hold_is_refused(self):
        # Naming the wrong movement means the athlete and the coach are talking about
        # different sessions; recording the prescription as if they agreed would bury it.
        with self.assertRaisesRegex(AthleteEvidenceError, "is not in this session"):
            self._confirm(deviations=[{"exercise": "deadlift", "set": 1, "reps": 3}])

    def test_a_deviation_beyond_the_prescribed_sets_is_refused(self):
        with self.assertRaisesRegex(AthleteEvidenceError, "beyond the 4 set"):
            self._confirm(deviations=[{"exercise": "bench press", "set": 5, "reps": 3}])

    def test_a_deviation_that_names_nothing_that_differed_is_refused(self):
        with self.assertRaisesRegex(AthleteEvidenceError, "names no measurement"):
            self._confirm(deviations=[{"exercise": "bench press", "set": 4}])

    def test_nothing_is_written_when_a_deviation_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._confirm(deviations=[{"exercise": "deadlift", "set": 1, "reps": 3}])

        # One statement, one outcome: a refused confirmation leaves no half-written
        # session behind for the next context to read as evidence.
        self.assertEqual([], load_evidence(self.state_dir)["strength_reports"])

    def test_a_movement_prescribed_twice_joins_into_one_continuous_run_of_sets(self):
        """Top sets and a back-off set are two rows for one movement, not a broken plan.

        The owner's own plan does exactly this -- `臥推 4x5 65公斤` then `臥推 1x5 60公斤` --
        and evidence holds one record per movement per day, so the rows join. Numbering
        continuously is also what makes "the last set" mean the last set of the movement.
        """
        bench = _strength_session()["plan"]["movements"][0]
        result = self._confirm(
            session=_strength_session(
                plan={
                    "kind": "movement_list",
                    "movements": [bench, {**bench, "sets": 1, "load_kg": 60}],
                }
            )
        )

        self.assertEqual(1, result["report_count"])
        sets = result["movements"][0]["report"]["sets"]
        self.assertEqual([1, 2, 3, 4, 5], [item["set"] for item in sets])
        self.assertEqual([65, 65, 65, 65, 60], [item["weight_kg"] for item in sets])

    def test_a_deviation_addresses_the_joined_numbering(self):
        bench = _strength_session()["plan"]["movements"][0]
        result = self._confirm(
            session=_strength_session(
                plan={
                    "kind": "movement_list",
                    "movements": [bench, {**bench, "sets": 1, "load_kg": 60}],
                }
            ),
            deviations=[{"exercise": "bench press", "set": 5, "reps": 3}],
        )

        sets = result["movements"][0]["report"]["sets"]
        self.assertEqual([5, 5, 5, 5, 3], [item["reps"] for item in sets])
        self.assertEqual(60, sets[4]["weight_kg"])


class RetractStrengthReportTests(unittest.TestCase):
    """Taking a lift back rather than correcting it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _report(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "date": TODAY,
            "exercise": "bench press",
            "category": "chest",
            "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
        }
        payload.update(overrides)
        return record_strength_report(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def _retract(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"exercise": "bench press"}
        payload.update(overrides)
        return retract_strength_report(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def test_record_and_retraction_share_one_identity_rule(self):
        """The retraction finds what the report wrote, through any spelling of it.

        Both paths now locate a record through the same helper, so the normalization
        that lets "Bench  Press" correct "bench press" is also what lets it be taken
        back -- a fix to record identity cannot land on one side only.
        """
        self._report(exercise="bench press")

        result = self._retract(exercise="  Bench  Press ")

        self.assertIsNotNone(result["removed"])
        self.assertEqual(0, result["report_count"])

    def test_retracting_removes_the_record_and_echoes_it_in_full(self):
        stored = self._report()["report"]

        result = self._retract()

        self.assertTrue(result["retracted"])
        self.assertEqual(stored, result["removed"])
        self.assertEqual(0, result["report_count"])
        self.assertIsNone(result["note"])
        self.assertIsNone(result["on_record_that_day"])
        self.assertEqual([], load_evidence(self.state_dir)["strength_reports"])

    def test_only_the_named_movement_on_the_named_day_is_removed(self):
        self._report(exercise="bench press", sets=[{"weight_kg": 65, "reps": 4}])
        self._report(exercise="squat", sets=[{"weight_kg": 100, "reps": 5}])
        self._report(
            date="2026-08-12", exercise="bench press", sets=[{"weight_kg": 60, "reps": 5}]
        )

        result = self._retract()

        self.assertEqual(2, result["report_count"])
        stored = load_evidence(self.state_dir)["strength_reports"]
        self.assertEqual(
            {("2026-08-13", "squat"), ("2026-08-12", "bench press")},
            {(item["date"], item["exercise"]) for item in stored},
        )

    def test_retracting_a_prescribed_confirmation_works(self):
        """A confirmed session is removed the same way a described one is (source is not a key)."""
        confirmed = confirm_prescribed_strength(
            self.state_dir, session=_strength_session(), timezone_name=TIMEZONE, now=NOW
        )
        bench = confirmed["movements"][0]["report"]
        self.assertEqual(PRESCRIBED_CONFIRMED_SOURCE, bench["source"])

        result = self._retract(exercise="bench press", date=TODAY)

        self.assertEqual(bench, result["removed"])
        self.assertEqual(PRESCRIBED_CONFIRMED_SOURCE, result["removed"]["source"])
        remaining = load_evidence(self.state_dir)["strength_reports"]
        self.assertEqual(1, len(remaining))
        self.assertEqual("pull up", remaining[0]["exercise"])

    def test_a_second_retraction_is_an_idempotent_no_op(self):
        self._report()
        first = self._retract()
        second = self._retract()

        self.assertTrue(first["retracted"])
        self.assertIsNotNone(first["removed"])
        self.assertTrue(second["retracted"])
        self.assertIsNone(second["removed"])
        self.assertEqual(0, second["report_count"])
        self.assertIn("bench press", second["note"])

    def test_sets_category_or_notes_alongside_a_retraction_are_refused(self):
        self._report()
        for overrides in (
            {"sets": [{"weight_kg": 65, "reps": 4}]},
            {"category": "chest"},
            {"notes": ["最後一組沒做完"]},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(AthleteEvidenceError):
                    self._retract(**overrides)
        # Refused before anything was touched: the report is still there.
        self.assertEqual(1, len(load_evidence(self.state_dir)["strength_reports"]))

    def test_a_future_date_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._retract(date="2026-08-14")
        self.assertIn("future", str(caught.exception))

    def test_an_empty_exercise_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._retract(exercise="")

    def test_on_record_that_day_names_what_is_there_instead(self):
        self._report(exercise="squat", sets=[{"weight_kg": 100, "reps": 5}])
        self._report(exercise="deadlift", sets=[{"weight_kg": 120, "reps": 3}])

        result = self._retract(exercise="bench press")

        self.assertIsNone(result["removed"])
        self.assertEqual(["deadlift", "squat"], result["on_record_that_day"])
        self.assertIn("bench press", result["note"])
        self.assertIn("deadlift", result["note"])
        self.assertIn("squat", result["note"])

    def test_on_record_that_day_is_empty_when_nothing_is_there(self):
        result = self._retract()

        self.assertEqual([], result["on_record_that_day"])
        self.assertNotIn("on record for that day", result["note"])


class BodyMeasurementTests(unittest.TestCase):
    """A number the athlete read off a scale, stored raw and corrected by restating."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _record(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"weight_kg": 72.5}
        payload.update(overrides)
        return record_body_measurement(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def test_one_measurement_is_stored_verbatim_with_its_provenance(self):
        result = self._record()

        self.assertFalse(result["idempotent_replay"])
        self.assertIsNone(result["replaced"])
        self.assertEqual(1, result["measurement_count"])
        measurement = result["measurement"]
        self.assertEqual(TODAY, measurement["date"])
        self.assertEqual(72.5, measurement["weight_kg"])
        # A figure the athlete did not state is an explicit null, never an estimate.
        self.assertIsNone(measurement["body_fat_pct"])
        self.assertEqual(ATHLETE_REPORTED_SOURCE, measurement["source"])
        self.assertEqual("2026-08-13T04:00:00Z", measurement["recorded_at"])

    def test_either_figure_alone_is_a_complete_statement(self):
        result = self._record(weight_kg=None, body_fat_pct=18.4)

        self.assertEqual(18.4, result["measurement"]["body_fat_pct"])
        self.assertIsNone(result["measurement"]["weight_kg"])

    def test_stating_neither_figure_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._record(weight_kg=None)

    def test_correcting_a_weight_replaces_the_day_instead_of_doubling_it(self):
        first = self._record(weight_kg=72.5)
        second = self._record(weight_kg=72.3)

        self.assertFalse(second["idempotent_replay"])
        self.assertEqual(first["measurement"], second["replaced"])
        self.assertEqual(1, second["measurement_count"])
        stored = load_evidence(self.state_dir)["body_measurements"]
        # The whole point: "72.5, sorry, 72.3" is one weigh-in, and two rows would show
        # the coach 200 grams of movement that never happened.
        self.assertEqual(1, len(stored))
        self.assertEqual(72.3, stored[0]["weight_kg"])

    def test_stating_the_second_figure_later_keeps_the_first(self):
        self._record(weight_kg=72.5)
        result = self._record(weight_kg=None, body_fat_pct=18.4)

        self.assertEqual(72.5, result["measurement"]["weight_kg"])
        self.assertEqual(18.4, result["measurement"]["body_fat_pct"])
        self.assertEqual(1, result["measurement_count"])

    def test_the_same_measurement_sent_twice_is_stored_once_and_says_so(self):
        first = self._record()
        second = self._record()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertIsNone(second["replaced"])
        self.assertEqual(first["measurement_id"], second["measurement_id"])
        self.assertEqual(1, len(load_evidence(self.state_dir)["body_measurements"]))

    def test_another_day_is_a_new_record_not_a_replacement(self):
        self._record()
        other = self._record(date="2026-08-12", weight_kg=73.0)

        self.assertIsNone(other["replaced"])
        self.assertEqual(2, other["measurement_count"])

    def test_a_figure_no_scale_could_produce_is_refused_by_name(self):
        for field, value in (
            ("weight_kg", 7.23),
            ("weight_kg", 401),
            ("body_fat_pct", 0.4),
            ("body_fat_pct", 90),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(AthleteEvidenceError) as caught:
                    self._record(**{"weight_kg": None, field: value})
                self.assertIn(field, str(caught.exception))
                self.assertIn(repr(value), str(caught.exception))
        self.assertEqual([], load_evidence(self.state_dir)["body_measurements"])

    def test_a_non_numeric_figure_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._record(weight_kg="72.5")
        # bool is an int subclass, and True is not a weight.
        with self.assertRaises(AthleteEvidenceError):
            self._record(weight_kg=True)

    def test_a_future_date_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._record(date="2026-08-14")

    def test_the_series_reads_newest_first_and_carries_no_derived_figure(self):
        self._record(date="2026-08-11", weight_kg=73.0)
        self._record(date="2026-08-13", weight_kg=72.5, body_fat_pct=18.4)

        series = body_measurement_series(load_evidence(self.state_dir), _window())

        self.assertEqual(["2026-08-13", "2026-08-11"], [row["date"] for row in series])
        # Exactly the stated numbers plus their provenance. No delta, no rate, no
        # direction: what half a kilogram means is the coach's reading.
        self.assertEqual({"date", "weight_kg", "body_fat_pct", "source"}, set(series[0]))
        self.assertEqual(ATHLETE_REPORTED_SOURCE, series[0]["source"])

    def test_a_measurement_outside_the_window_is_not_in_the_series(self):
        self._record(date="2026-05-01", weight_kg=73.0)

        self.assertEqual(
            [], body_measurement_series(load_evidence(self.state_dir), _window())
        )


class RetractBodyMeasurementTests(unittest.TestCase):
    """Taking a day's measurement back rather than correcting it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _record(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"weight_kg": 72.5}
        payload.update(overrides)
        return record_body_measurement(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def _retract(self, **overrides: Any) -> dict[str, Any]:
        return retract_body_measurement(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **overrides
        )

    def test_retracting_removes_the_record_and_echoes_it_in_full(self):
        stored = self._record(weight_kg=72.5, body_fat_pct=18.4)["measurement"]

        result = self._retract()

        self.assertTrue(result["retracted"])
        self.assertEqual(stored, result["removed"])
        self.assertEqual(0, result["measurement_count"])
        self.assertIsNone(result["note"])
        self.assertEqual([], load_evidence(self.state_dir)["body_measurements"])

    def test_only_the_named_day_is_removed(self):
        self._record(date="2026-08-11", weight_kg=73.0)
        self._record(date=TODAY, weight_kg=72.5)

        result = self._retract(date=TODAY)

        self.assertEqual(1, result["measurement_count"])
        remaining = load_evidence(self.state_dir)["body_measurements"]
        self.assertEqual(["2026-08-11"], [item["date"] for item in remaining])

    def test_a_second_retraction_is_an_idempotent_no_op(self):
        self._record()
        first = self._retract()
        second = self._retract()

        self.assertIsNotNone(first["removed"])
        self.assertIsNone(second["removed"])
        self.assertEqual(0, second["measurement_count"])
        self.assertIn(TODAY, second["note"])

    def test_a_figure_alongside_a_retraction_is_refused(self):
        self._record()
        for overrides in ({"weight_kg": 72.5}, {"body_fat_pct": 18.4}):
            with self.subTest(**overrides):
                with self.assertRaises(AthleteEvidenceError):
                    self._retract(**overrides)
        self.assertEqual(1, len(load_evidence(self.state_dir)["body_measurements"]))

    def test_a_future_date_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._retract(date="2026-08-14")
        self.assertIn("future", str(caught.exception))

    def test_a_miss_names_the_day_and_stores_nothing(self):
        result = self._retract()

        self.assertTrue(result["retracted"])
        self.assertIsNone(result["removed"])
        self.assertIn(TODAY, result["note"])
        self.assertEqual(0, result["measurement_count"])


class ActivitySummaryTests(unittest.TestCase):
    """A session the athlete trained that no device recorded -- evidence, never an actual."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _record(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"sport": "running", "duration_minutes": 40}
        payload.update(overrides)
        return record_activity_summary(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def test_sport_and_duration_are_the_whole_required_statement(self):
        result = self._record()

        self.assertFalse(result["idempotent_replay"])
        summary = result["activity"]
        self.assertEqual(TODAY, summary["date"])
        self.assertEqual("running", summary["sport"])
        self.assertEqual(40, summary["duration_minutes"])
        # Everything not stated is an explicit null; nothing is derived from duration.
        self.assertIsNone(summary["distance_km"])
        self.assertIsNone(summary["subjective_feel"])
        self.assertIsNone(summary["note"])
        self.assertEqual(ATHLETE_REPORTED_SOURCE, summary["source"])

    def test_the_optional_figures_are_carried_exactly_as_stated(self):
        summary = self._record(distance_km=8.2, subjective_feel=4, note="飯店跑步機")["activity"]

        self.assertEqual(8.2, summary["distance_km"])
        self.assertEqual(4, summary["subjective_feel"])
        self.assertEqual("飯店跑步機", summary["note"])

    def test_the_sport_vocabulary_is_the_plans_own_minus_rest(self):
        self.assertEqual(
            (
                "cycling",
                "hiking",
                "mobility",
                "recovery",
                "rowing",
                "running",
                "strength",
                "swimming",
            ),
            REPORTABLE_SPORTS,
        )
        for sport in REPORTABLE_SPORTS:
            with self.subTest(sport=sport):
                self.assertEqual(sport, self._record(sport=sport)["activity"]["sport"])
        # Rest is not a session to report, and neither is a sport this product's plans
        # cannot express -- both refusals name what is accepted.
        for sport in ("rest", "climbing"):
            with self.subTest(sport=sport):
                with self.assertRaises(AthleteEvidenceError) as caught:
                    self._record(sport=sport)
                self.assertIn("running", str(caught.exception))

    def test_restating_the_same_sport_and_day_corrects_rather_than_duplicates(self):
        first = self._record(duration_minutes=40)
        second = self._record(duration_minutes=45)

        self.assertFalse(second["idempotent_replay"])
        self.assertEqual(first["activity"], second["replaced"])
        self.assertEqual(1, second["activity_count"])
        stored = load_evidence(self.state_dir)["reported_activities"]
        self.assertEqual(1, len(stored))
        self.assertEqual(45, stored[0]["duration_minutes"])

    def test_a_displaced_summary_is_named_with_the_version_one_limitation(self):
        """The rare case the key cannot express, said out loud rather than swallowed.

        Two genuinely distinct running sessions on one day cannot both be held, so the
        second write says what it displaced and what to do instead. Losing a session
        quietly is the failure this exists to prevent.
        """
        self._record(duration_minutes=40)
        second = self._record(duration_minutes=25)

        self.assertIsNotNone(second["replaced_note"])
        self.assertIn("running", second["replaced_note"])
        self.assertIn(TODAY, second["replaced_note"])
        self.assertIn("combined summary", second["replaced_note"])
        # And nothing is said when nothing was displaced.
        self.assertIsNone(
            self._record(sport="strength", duration_minutes=50)["replaced_note"]
        )

    def test_the_same_summary_sent_twice_is_stored_once_and_says_so(self):
        first = self._record()
        second = self._record()

        self.assertTrue(second["idempotent_replay"])
        self.assertIsNone(second["replaced"])
        self.assertEqual(first["summary_id"], second["summary_id"])
        self.assertEqual(1, len(load_evidence(self.state_dir)["reported_activities"]))

    def test_a_different_sport_or_day_is_a_new_summary(self):
        self._record()
        other_sport = self._record(sport="mobility", duration_minutes=20)
        other_day = self._record(date="2026-08-12")

        self.assertIsNone(other_sport["replaced"])
        self.assertIsNone(other_day["replaced"])
        self.assertEqual(3, other_day["activity_count"])

    def test_a_malformed_figure_is_refused_and_stores_nothing(self):
        cases: tuple[dict[str, Any], ...] = (
            {"duration_minutes": None},
            {"duration_minutes": 0},
            {"duration_minutes": 40.5},
            {"duration_minutes": True},
            {"distance_km": -1},
            {"distance_km": "8"},
            {"subjective_feel": 0},
            {"subjective_feel": 6},
            {"subjective_feel": 3.5},
            {"note": "   "},
            {"date": "2026-08-14"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(AthleteEvidenceError):
                    self._record(**overrides)
        self.assertEqual([], load_evidence(self.state_dir)["reported_activities"])

    def test_the_summaries_read_newest_first_and_carry_nothing_to_attach_with(self):
        self._record(date="2026-08-11", sport="mobility", duration_minutes=20)
        self._record(date="2026-08-13", distance_km=8.2, subjective_feel=4)

        summaries = reported_activity_summaries(load_evidence(self.state_dir), _window())

        self.assertEqual(["2026-08-13", "2026-08-11"], [row["date"] for row in summaries])
        # The absent keys are the contract: nothing here can be read as a provider
        # activity, because there is no id, no confidence and no completion to read.
        self.assertEqual(
            {
                "date",
                "sport",
                "duration_minutes",
                "distance_km",
                "subjective_feel",
                "note",
                "source",
                "imported_from",
            },
            set(summaries[0]),
        )
        self.assertEqual(ATHLETE_REPORTED_SOURCE, summaries[0]["source"])
        # Null rather than absent: a spoken session states that no upload supplied it,
        # the same way every other unstated field here states its own absence.
        self.assertIsNone(summaries[0]["imported_from"])

    def test_a_summary_outside_the_window_is_not_in_the_series(self):
        self._record(date="2026-05-01")

        self.assertEqual(
            [], reported_activity_summaries(load_evidence(self.state_dir), _window())
        )


class RetractActivitySummaryTests(unittest.TestCase):
    """Taking a reported session back rather than correcting it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "owner"

    def _record(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"sport": "running", "duration_minutes": 40}
        payload.update(overrides)
        return record_activity_summary(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def _retract(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"sport": "running"}
        payload.update(overrides)
        return retract_activity_summary(
            self.state_dir, timezone_name=TIMEZONE, now=NOW, **payload
        )

    def test_retracting_removes_the_record_and_echoes_it_in_full(self):
        stored = self._record()["activity"]

        result = self._retract()

        self.assertTrue(result["retracted"])
        self.assertEqual(stored, result["removed"])
        self.assertEqual(0, result["activity_count"])
        self.assertIsNone(result["note"])
        self.assertIsNone(result["on_record_that_day"])
        self.assertEqual([], load_evidence(self.state_dir)["reported_activities"])

    def test_only_the_named_sport_on_the_named_day_is_removed(self):
        self._record(sport="running", duration_minutes=40)
        self._record(sport="mobility", duration_minutes=20)
        self._record(date="2026-08-12", sport="running", duration_minutes=35)

        result = self._retract()

        self.assertEqual(2, result["activity_count"])
        remaining = load_evidence(self.state_dir)["reported_activities"]
        self.assertEqual(
            {("2026-08-13", "mobility"), ("2026-08-12", "running")},
            {(item["date"], item["sport"]) for item in remaining},
        )

    def test_a_second_retraction_is_an_idempotent_no_op(self):
        self._record()
        first = self._retract()
        second = self._retract()

        self.assertIsNotNone(first["removed"])
        self.assertIsNone(second["removed"])
        self.assertEqual(0, second["activity_count"])
        self.assertIn("running", second["note"])

    def test_duration_distance_feel_or_note_alongside_a_retraction_are_refused(self):
        self._record()
        for overrides in (
            {"duration_minutes": 40},
            {"distance_km": 8.0},
            {"subjective_feel": 4},
            {"note": "沒帶錶"},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(AthleteEvidenceError):
                    self._retract(**overrides)
        self.assertEqual(1, len(load_evidence(self.state_dir)["reported_activities"]))

    def test_an_unknown_sport_names_the_accepted_vocabulary(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._retract(sport="climbing")
        self.assertIn("running", str(caught.exception))

    def test_a_future_date_is_refused(self):
        with self.assertRaises(AthleteEvidenceError) as caught:
            self._retract(date="2026-08-14")
        self.assertIn("future", str(caught.exception))

    def test_on_record_that_day_names_what_is_there_instead(self):
        self._record(sport="mobility", duration_minutes=20)
        self._record(sport="strength", duration_minutes=50)

        result = self._retract(sport="running")

        self.assertIsNone(result["removed"])
        self.assertEqual(["mobility", "strength"], result["on_record_that_day"])
        self.assertIn("running", result["note"])

    def test_on_record_that_day_is_empty_when_nothing_is_there(self):
        result = self._retract()

        self.assertEqual([], result["on_record_that_day"])
        self.assertNotIn("on record for that day", result["note"])
