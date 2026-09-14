"""The athlete's own answer for a past session nothing attached to (issue #468).

The shape under test, stated once: a planned session whose day passed unmatched reads
``planned`` for ever, because nothing in the product could record the one fact that
resolves it -- the athlete saying they did not train. ``confirmActivityMatch`` needs an
activity; ``recordSubjectiveState`` fires nothing. So the tests here are about three
properties and not about a status value:

- the statement reaches the coach as ``missed``, and says the athlete is where it came
  from rather than letting it read as a product inference (issue #30 Part B's whole
  distinction between "did not train" and "no data");
- it survives the week rolling past, which is the half no plan write could have covered:
  an elapsed week lives only in the append-only commit chain;
- it is reversible and idempotent, so a misremembered day is an ordinary correction.
"""

from __future__ import annotations

import copy
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_gateway import (
    CLIENT_ID_VALUE,
    CLIENT_SECRET_VALUE,
    FakeIntervals,
    HMAC_KEY,
    NOW,
    TOKEN_A,
    publishable_plan,
)
from tests.coach_session_scenarios import (
    activity_row,
    plan_measuring_week_one_quality,
    roll_the_week_to_the_measurement_week,
)

from garmin_coach_loop import athlete_evidence
from garmin_coach_loop.gateway import CoachGateway, GatewayConfig, GatewayError
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    record_token_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME
from garmin_coach_loop.store import init_store, read_current_plan, resolve_state_dir
from garmin_coach_loop.validation import validate_coach_context


# 2026-08-12, a mobility session three days into the fixture cycle. The fixture marks it
# completed; these tests hand it back as planned and let no activity attach, which is the
# exact state issue #468 was filed on: a day that passed with nothing matched.
UNMATCHED_SESSION = "mobility-01"
UNMATCHED_DATE = "2026-08-12"


def plan_with_an_unmatched_elapsed_session() -> dict[str, object]:
    plan = publishable_plan()
    for session in plan["week"]["sessions"]:
        if session["session_id"] == UNMATCHED_SESSION:
            session["match_status"] = "planned"
    return plan


class SessionNotTrainedTestCase(unittest.TestCase):
    now = NOW

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity_db = self.root / "identity.db"
        # No activities at all: the point of this route is the day nothing came back for.
        self.fake = FakeIntervals(plan=plan_with_an_unmatched_elapsed_session())
        self.owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        record_token_fingerprint(
            self.identity_db,
            token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
            self.owner_id,
            "intervals",
        )
        self.state_dir = resolve_state_dir(self.owner_id, state_root=self.root)
        self.plan = plan_with_an_unmatched_elapsed_session()
        init_store(self.state_dir, copy.deepcopy(self.plan))
        self.gateway = CoachGateway(
            GatewayConfig(
                state_root=self.root,
                token_hmac_key=HMAC_KEY,
                intervals_client_id=CLIENT_ID_VALUE,
                intervals_client_secret=CLIENT_SECRET_VALUE,
            ),
            fetch=self.fake,
            now=lambda: self.now,
        )

    def session(self) -> dict:
        return self.gateway.route("session", self.owner_id, TOKEN_A, {"read": "all"})

    def cycle_row(self, session_id: str = UNMATCHED_SESSION) -> dict:
        context = self.session()["context"]
        # Every assertion below reads a context the product itself accepts, so a row this
        # route invented a shape for would fail here rather than in a later release.
        self.assertEqual([], validate_coach_context(context)["errors"])
        return next(
            row for row in context["cycle_sessions"] if row["session_id"] == session_id
        )

    def confirm(self, session_id: str = UNMATCHED_SESSION) -> dict:
        return self.gateway.route(
            "session_not_trained", self.owner_id, TOKEN_A, {"session_id": session_id}
        )

    def retract(self, session_id: str = UNMATCHED_SESSION) -> dict:
        return self.gateway.route(
            "athlete_record_retract",
            self.owner_id,
            TOKEN_A,
            {"kind": "session_not_trained", "session_id": session_id},
        )


class SessionNotTrainedRouteTests(SessionNotTrainedTestCase):
    def test_the_tool_is_registered_with_a_kind_an_output_schema_and_a_handler(self):
        tool = TOOLS_BY_NAME["confirmSessionNotTrained"]
        descriptor = tool.descriptor()

        self.assertEqual("session_not_trained", tool.kind)
        self.assertEqual("object", descriptor["outputSchema"]["type"])
        self.assertIn("status", descriptor["outputSchema"]["properties"])
        self.assertIn("session_not_trained", CoachGateway.route_kinds())
        # Not destructive: it overwrites nothing, and the retraction exists.
        self.assertFalse(descriptor["annotations"]["destructiveHint"])
        self.assertTrue(descriptor["annotations"]["idempotentHint"])

    def test_an_unmatched_elapsed_session_reads_planned_until_the_athlete_answers(self):
        """The state issue #468 was filed on, pinned before the repair is applied."""
        row = self.cycle_row()

        self.assertEqual(UNMATCHED_DATE, row["date"])
        self.assertEqual("planned", row["match_status"])
        self.assertEqual("none_found", row["activity_evidence"])

    def test_the_statement_makes_the_session_missed_and_names_the_athlete_as_its_source(self):
        result = self.confirm()

        self.assertEqual("passed", result["status"])
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(UNMATCHED_DATE, result["session_outcome"]["date"])
        self.assertEqual("not_trained", result["session_outcome"]["outcome"])
        self.assertEqual("athlete_reported", result["session_outcome"]["source"])

        row = self.cycle_row()
        self.assertEqual("missed", row["match_status"])
        # The half that keeps this apart from a product inference: `none_found` says the
        # build looked and nothing attached; this says the athlete answered.
        self.assertEqual("athlete_confirmed_not_trained", row["activity_evidence"])

    def test_a_today_read_is_told_the_calendar_still_says_planned(self):
        """`current_calendar` is in the `today` read and `cycle_sessions` is not.

        The calendar is the plan's own projection and `validate_bundle` holds it to
        exactly that, so it is not overlaid. Without a line saying so, a daily turn the
        day after the athlete answered reads the one container that still disagrees with
        them, and nothing in that read says the disagreement exists.
        """
        self.confirm()

        response = self.gateway.route(
            "session", self.owner_id, TOKEN_A, {"read": ["today"]}
        )
        context = response["context"]
        self.assertIn("current_calendar", context)
        self.assertNotIn("cycle_sessions", context)
        calendar = next(
            row for row in context["current_calendar"] if row["session_id"] == UNMATCHED_SESSION
        )
        self.assertEqual("planned", calendar["status"])
        self.assertTrue(
            any(
                UNMATCHED_SESSION in note and "not trained" in note
                for note in context["unknowns"]
            ),
            context["unknowns"],
        )

    def test_a_plan_that_already_recorded_the_same_outcome_is_not_a_conflict(self):
        """Agreement must not read as a question to put to the athlete.

        A weekly review may record the session `missed` through the ordinary coaching
        path after the athlete said so. Both sides then say the same thing, and a
        conflict line there would manufacture a question to ask on every later read --
        the opposite of what this route is for.
        """
        self.confirm()
        shutil.rmtree(self.state_dir)
        plan = plan_with_an_unmatched_elapsed_session()
        for session in plan["week"]["sessions"]:
            if session["session_id"] == UNMATCHED_SESSION:
                session["match_status"] = "missed"
        init_store(self.state_dir, plan)
        athlete_evidence.record_session_not_trained(
            self.state_dir,
            plan_id=self.plan["plan_id"],
            session_id=UNMATCHED_SESSION,
            date=UNMATCHED_DATE,
            sport="mobility",
            now=self.now,
        )

        context = self.session()["context"]
        row = next(
            item for item in context["cycle_sessions"] if item["session_id"] == UNMATCHED_SESSION
        )

        self.assertEqual("missed", row["match_status"])
        self.assertEqual(
            [],
            [note for note in context["unknowns"] if "only they can say which stands" in note],
        )

    def test_it_writes_no_plan_version_and_leaves_the_stored_session_alone(self):
        """The append-only chain is not rewritten, and that is the design, not a gap.

        The sessions this answers for have usually left `week.sessions` already. Reaching
        back into a committed plan to restate one would break the property every other
        reader depends on, so the statement is applied over the cycle record instead.
        """
        before = read_current_plan(self.state_dir)
        self.confirm()
        after = read_current_plan(self.state_dir)

        self.assertEqual(before["current_version"], after["current_version"])
        self.assertEqual(before["current_plan"], after["current_plan"])

    def test_repeating_the_statement_converges_instead_of_stacking(self):
        first = self.confirm()
        second = self.confirm()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["outcome_id"], second["outcome_id"])
        self.assertEqual(1, second["outcome_count"])
        evidence = athlete_evidence.load_evidence(self.state_dir)
        self.assertEqual(1, len(evidence["session_outcomes"]))

    def test_a_day_that_has_not_passed_is_refused(self):
        """Today's session has nothing yet to have been missed."""
        with self.assertRaises(GatewayError) as raised:
            self.confirm("run-quality-01")

        self.assertIn("has not passed", str(raised.exception.detail))

    def test_a_session_this_cycle_never_scheduled_is_refused(self):
        with self.assertRaises(GatewayError) as raised:
            self.confirm("run-quality-99")

        self.assertIn("no elapsed session", str(raised.exception.detail))

    def test_a_session_already_recorded_as_trained_is_refused(self):
        """Changing a settled outcome is a coaching decision, not this statement."""
        with self.assertRaises(GatewayError) as raised:
            self.confirm("run-easy-01")

        self.assertIn("already reads 'completed'", str(raised.exception.detail))

    def test_a_session_the_coach_moved_is_refused_and_never_overwritten(self):
        """A coach decision may not be reported back as the athlete's miss.

        `moved` and `replaced` are written by `plan_change` when the coach reschedules or
        rewrites a session. `store.cycle_sessions` already refuses to count the plan's own
        change of mind as the athlete's miss; a statement that overwrote one of those
        would reintroduce exactly that, from the other direction. Refused at the tool, and
        the read path holds the line independently of the tool.
        """
        for status in ("moved", "replaced"):
            with self.subTest(status=status):
                shutil.rmtree(self.state_dir)
                plan = plan_with_an_unmatched_elapsed_session()
                for session in plan["week"]["sessions"]:
                    if session["session_id"] == UNMATCHED_SESSION:
                        session["match_status"] = status
                init_store(self.state_dir, plan)

                with self.assertRaises(GatewayError) as raised:
                    self.confirm()
                self.assertIn(f"already reads {status!r}", str(raised.exception.detail))

                # The read path is the second reader, not the first: a record written
                # before this guard existed must not resolve one either.
                athlete_evidence.record_session_not_trained(
                    self.state_dir,
                    plan_id=self.plan["plan_id"],
                    session_id=UNMATCHED_SESSION,
                    date=UNMATCHED_DATE,
                    sport="mobility",
                    now=self.now,
                )
                row = self.cycle_row()
                self.assertEqual(status, row["match_status"])
                self.assertTrue(
                    any(
                        UNMATCHED_SESSION in note and "not trained" in note
                        for note in self.session()["context"]["unknowns"]
                    ),
                )

    def test_nothing_writes_one_of_these_on_the_athletes_behalf(self):
        """AGENTS.md 3 and issue #30 Part B: an absence is never evidence of a decision.

        Three reads of an account with an unmatched elapsed session, and the container
        stays empty. A watch that was off and a session nobody trained look identical
        here, which is exactly why only the athlete may fill this in.
        """
        for _ in range(3):
            row = self.cycle_row()
            # The read path, not only the container: a build that learned to infer this
            # would leave the container empty and still report the session as missed.
            self.assertEqual("planned", row["match_status"])
            self.assertEqual("none_found", row["activity_evidence"])

        self.assertEqual([], athlete_evidence.load_evidence(self.state_dir)["session_outcomes"])


class SessionNotTrainedRetractionTests(SessionNotTrainedTestCase):
    def test_retracting_returns_the_session_to_its_own_evidence(self):
        self.confirm()
        self.assertEqual("missed", self.cycle_row()["match_status"])

        result = self.retract()

        self.assertTrue(result["retracted"])
        self.assertEqual(UNMATCHED_SESSION, result["removed"]["session_id"])
        self.assertEqual(0, result["record_count"])
        row = self.cycle_row()
        self.assertEqual("planned", row["match_status"])
        self.assertEqual("none_found", row["activity_evidence"])

    def test_a_retraction_removes_one_statement_and_leaves_the_others_standing(self):
        """A mutation making `retract_session_outcome` clear the container survived the
        whole suite, so the property three docstrings assert had nothing holding it."""
        self.confirm(UNMATCHED_SESSION)
        athlete_evidence.record_session_not_trained(
            self.state_dir,
            plan_id=self.plan["plan_id"],
            session_id="run-easy-01",
            date="2026-08-11",
            sport="running",
            now=self.now,
        )
        self.assertEqual(2, len(athlete_evidence.load_evidence(self.state_dir)["session_outcomes"]))

        result = self.retract(UNMATCHED_SESSION)

        self.assertEqual(1, result["record_count"])
        standing = athlete_evidence.load_evidence(self.state_dir)["session_outcomes"]
        self.assertEqual(["run-easy-01"], [row["session_id"] for row in standing])

    def test_a_statement_is_read_back_only_for_the_plan_that_wrote_it(self):
        """The other surviving mutation: dropping the plan_id filter changed nothing.

        A session id is unique only inside the plan that wrote it, so a statement read
        back under another plan would answer for a session nobody answered for.
        """
        self.confirm(UNMATCHED_SESSION)
        evidence = athlete_evidence.load_evidence(self.state_dir)

        own = athlete_evidence.confirmed_session_outcomes(evidence, self.plan["plan_id"])
        other = athlete_evidence.confirmed_session_outcomes(evidence, "some-other-plan")

        self.assertEqual([UNMATCHED_SESSION], sorted(own))
        self.assertEqual({}, other)

    def test_the_retraction_keeps_one_meaning_for_on_record_that_day(self):
        """AGENTS.md 14: a field may not mean two things on two paths of one tool.

        This kind is keyed by a session, not by a day, so `on_record_that_day` is null
        here for the same reason it is null for a long-term goal and a training
        preference. Filling it with every session still carrying a statement would have a
        caller tell the athlete those were "on record that day", which is not what they
        are.
        """
        self.confirm(UNMATCHED_SESSION)
        athlete_evidence.record_session_not_trained(
            self.state_dir,
            plan_id=self.plan["plan_id"],
            session_id="run-easy-01",
            date="2026-08-11",
            sport="running",
            now=self.now,
        )

        result = self.retract(UNMATCHED_SESSION)

        self.assertIsNone(result["on_record_that_day"])
        self.assertEqual([], result["candidates"])
        self.assertIn("run-easy-01", result["note"])

    def test_retracted_true_is_idempotency_and_not_a_receipt_for_a_deletion(self):
        """Issue #460's remaining item, checked rather than only described.

        `retracted: true` comes back identically whether a record went or there was
        never one. `removed` and `note` are what tell the two apart, which is what the
        field's own description now says.
        """
        nothing_there = self.retract()

        self.assertTrue(nothing_there["retracted"])
        self.assertIsNone(nothing_there["removed"])
        self.assertIn("no statement about", nothing_there["note"])

        self.confirm()
        removed = self.retract()
        self.assertTrue(removed["retracted"])
        self.assertIsNotNone(removed["removed"])
        self.assertIsNone(removed["note"])

        again = self.retract()
        self.assertTrue(again["retracted"])
        self.assertIsNone(again["removed"])
        self.assertIsNotNone(again["note"])

    def test_the_retracted_field_says_so_where_the_model_reads_it(self):
        retracted = TOOLS_BY_NAME["retractAthleteRecord"].descriptor()
        description = retracted["outputSchema"]["properties"]["retracted"]["description"]

        self.assertIn("not that a record was deleted", description)
        self.assertIn("nothing matched", description)
        self.assertIn("session_not_trained", retracted["inputSchema"]["properties"]["kind"]["enum"])

    def test_the_deletion_preview_counts_what_it_would_take(self):
        """A statement no provider holds is a statement only this account has."""
        from garmin_coach_loop import owner_data

        self.confirm()
        preview = owner_data.deletion_preview(
            self.state_dir, identity_db=self.identity_db, owner_id=self.owner_id
        )

        self.assertEqual(1, preview["removes"]["session_outcomes"])


class SessionNotTrainedSurvivesTheWeekRollingTests(SessionNotTrainedTestCase):
    """The half a PlanState write could never have covered.

    Three weeks later the session is not in `week.sessions` at all -- it exists only in
    the commit chain -- so "the next conversation still knows" is a property of reading
    the statement beside that chain, and this is where it is checked.
    """

    now = dt.datetime(2026, 9, 4, 0, 30, tzinfo=dt.timezone.utc)

    def roll(self):
        """Three weekly reviews, the way a cycle actually reaches its fourth week."""
        roll_the_week_to_the_measurement_week(
            self.state_dir, copy.deepcopy(self.plan), self.now
        )
        current = read_current_plan(self.state_dir)["current_plan"]
        self.assertNotIn(
            UNMATCHED_SESSION,
            {session["session_id"] for session in current["week"]["sessions"]},
        )

    def test_a_session_that_has_left_the_week_can_still_be_answered_for(self):
        self.roll()

        self.confirm()

        row = self.cycle_row()
        self.assertEqual("missed", row["match_status"])
        self.assertEqual("athlete_confirmed_not_trained", row["activity_evidence"])

    def test_a_statement_made_before_the_roll_survives_it(self):
        """Written while the session was still in the week, read after it left.

        This is the direction a plan write could not have covered even in principle: the
        version that carried the session is committed and the chain is append-only, so a
        statement stored inside the plan would have gone stale at the next weekly review.
        """
        self.confirm()
        self.assertEqual("missed", self.cycle_row()["match_status"])

        self.roll()

        row = self.cycle_row()
        self.assertEqual("missed", row["match_status"])
        self.assertEqual("athlete_confirmed_not_trained", row["activity_evidence"])


# The elapsed running session, handed back as planned so an activity can land on it
# after the athlete has already answered for it.
LATE_SYNC_SESSION = "run-easy-01"
LATE_SYNC_DATE = "2026-08-11"


class SessionNotTrainedUnderADeclaredMeasurementTests(SessionNotTrainedTestCase):
    """The cycle that declared a measurement, which is where the first cut broke.

    `_measurement_evidence` copies `cycle_sessions[].activity_evidence` straight onto
    `measurement_evidence.reference_result` / `comparison_result`, and those two fields
    carry their own enum in `validation.py` and in the published contract. Adding a value
    to the cycle-record enum and not to those refuses the *whole context*: an athlete who
    answered for the reference session -- the session they are most likely to answer for
    -- turned every later `startCoachSession` into a 422 with no detail, which the model
    cannot act on and cannot trace back to a statement it should retract.

    The fixture plan here is the one that declares a measurement, because the plan every
    other test in this file uses has `goal.measurement` null and never reaches this path.
    """

    now = dt.datetime(2026, 9, 4, 0, 30, tzinfo=dt.timezone.utc)

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.state_dir)
        self.plan = plan_measuring_week_one_quality()
        init_store(self.state_dir, copy.deepcopy(self.plan))
        roll_the_week_to_the_measurement_week(
            self.state_dir, copy.deepcopy(self.plan), self.now
        )

    def test_answering_for_either_end_of_the_measurement_keeps_the_context_readable(self):
        for session_id, field in (
            ("run-quality-01", "reference_result"),
            ("run-measure-01", "comparison_result"),
        ):
            with self.subTest(session=session_id):
                self.confirm(session_id)
                # Raises GatewayError 422 if either enum is missing the value.
                context = self.session()["context"]
                self.assertEqual([], validate_coach_context(context)["errors"])
                self.assertEqual(
                    "athlete_confirmed_not_trained",
                    context["measurement_evidence"][field],
                )
                self.retract(session_id)

    def test_the_measurement_reading_says_the_athlete_answered_not_that_nothing_was_found(self):
        """`none_found` and `athlete_confirmed_not_trained` are different next actions.

        One says this build looked and nothing attached -- look again, or wait for a sync.
        The other says the measurement was not run, which is rescheduled rather than
        looked harder for.
        """
        before = self.session()["context"]["measurement_evidence"]
        self.assertEqual("none_found", before["reference_result"])

        self.confirm("run-quality-01")

        after = self.session()["context"]["measurement_evidence"]
        self.assertEqual("athlete_confirmed_not_trained", after["reference_result"])


class SessionNotTrainedConflictTests(SessionNotTrainedTestCase):
    def setUp(self):
        """An account where the elapsed run is unmatched, the way an unsynced day is."""
        super().setUp()
        plan = plan_with_an_unmatched_elapsed_session()
        for session in plan["week"]["sessions"]:
            if session["session_id"] == LATE_SYNC_SESSION:
                session["match_status"] = "planned"
        shutil.rmtree(self.state_dir)
        init_store(self.state_dir, plan)

    def test_an_activity_that_attaches_later_is_reported_and_the_statement_is_named(self):
        """Conflicting data is reported, never reconciled.

        The late sync is the real case: the athlete says they skipped Tuesday, and a week
        later the watch uploads a Tuesday run. The attachment is real and is reported as
        one; their standing statement is named in `unknowns` rather than being silently
        dropped, or silently winning over provider evidence.
        """
        self.confirm(LATE_SYNC_SESSION)

        self.fake.activities = [
            activity_row(
                "late-sync-1", LATE_SYNC_DATE, minutes=42, distance_m=7000, avg_speed=2.78
            )
        ]
        context = self.session()["context"]
        self.assertEqual([], validate_coach_context(context)["errors"])
        row = next(
            item for item in context["cycle_sessions"] if item["session_id"] == LATE_SYNC_SESSION
        )

        self.assertEqual("attached", row["activity_evidence"])
        self.assertIsNotNone(row["activity"])
        self.assertTrue(
            any(
                LATE_SYNC_SESSION in note and "not trained" in note
                for note in context["unknowns"]
            ),
            context["unknowns"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
