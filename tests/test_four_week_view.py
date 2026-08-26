"""The 28 days an athlete can actually see: this week exactly, the rest outlined (#61).

The cycle has always been 28 days and the plan has always held one week, which meant the
other three weeks existed only as a sentence about a direction. `cycle.outlook` is those
weeks, and the whole design question is that it must be visible without becoming a plan:
an outlined week is something to read, never something to publish, reconcile, or measure
staleness against.

That property is structural rather than enforced -- an outlook entry has no session id and
no execution block, so the delivery and reconciliation paths, which read
`plan.week.sessions`, cannot reach one. These tests hold the structure that makes it true,
plus the two places the view is actually produced: a first plan, which must show all four
weeks before anyone confirms anything, and a review, which turns the next outlined week
into the precise one.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.plan_change import ChangeRequestError
from garmin_coach_loop.plan_init import project_initialization_request
from garmin_coach_loop.validation import validate_plan_state


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"

from test_gateway import (  # noqa: E402
    TOKEN_A,
    WEEKLY_CHANGE,
    GatewayTestCase,
    publishable_plan,
)
from test_plan_init import ISSUED_AT, initialization_request  # noqa: E402


def plan() -> dict[str, Any]:
    return json.loads((EXAMPLE / "plan-state-v1.json").read_text(encoding="utf-8"))


class OutlookStructureTests(unittest.TestCase):
    def test_an_outlined_week_holds_nothing_that_could_be_delivered(self):
        """The reason delivery never had to learn about the outlook.

        No session id to name in a delivery set, no execution block to carry a delivery
        state, no prescription to publish. Delivery, reconciliation and staleness all read
        `plan.week.sessions`; an outlined week is not in that list and has nothing they
        could read if it were.
        """
        for week in plan()["cycle"]["outlook"]:
            with self.subTest(week=week["week_start"]):
                self.assertEqual(
                    {"week_start", "intent", "key_sessions", "relation_to_primary"},
                    set(week),
                )

    def test_a_session_shaped_field_is_refused_rather_than_ignored(self):
        candidate = plan()
        candidate["cycle"]["outlook"][0]["session_id"] = "run-quality-week-2"

        report = validate_plan_state(candidate)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("session_id" in error for error in report["errors"]), report["errors"]
        )

    def test_the_outlined_weeks_are_the_weeks_that_follow_this_one(self):
        for index, wrong in enumerate(("2026-08-10", "2026-08-18", "2026-08-24")):
            with self.subTest(week_start=wrong):
                candidate = plan()
                candidate["cycle"]["outlook"][0]["week_start"] = wrong
                report = validate_plan_state(candidate)
                self.assertEqual("blocked", report["status"], wrong)

    def test_it_cannot_run_past_the_cycle_it_outlines(self):
        candidate = plan()
        candidate["cycle"]["outlook"].append(
            {
                "week_start": "2026-09-07",
                "intent": "A fifth week of a four-week cycle",
                "key_sessions": ["Something"],
                "relation_to_primary": "None, because this week is not in the cycle",
            }
        )

        report = validate_plan_state(candidate)

        self.assertEqual("blocked", report["status"])

    def test_a_short_outlook_warns_rather_than_blocks(self):
        """AGENTS.md 5: a cycle can honestly have weeks it has not decided yet.

        Blocking would force the coach to invent a week to satisfy a schema, which is a
        worse answer than an athlete being told the direction stops early.
        """
        candidate = plan()
        candidate["cycle"]["outlook"] = candidate["cycle"]["outlook"][:1]

        report = validate_plan_state(candidate)

        self.assertEqual("passed", report["status"], report["errors"])
        self.assertTrue(
            any("1 of the 3 week(s) left" in warning for warning in report["warnings"]),
            report["warnings"],
        )

    def test_the_last_week_of_a_cycle_outlines_nothing_and_that_is_correct(self):
        candidate = plan()
        candidate["cycle"]["outlook"] = []
        candidate["week"]["start"] = "2026-08-31"
        for session in candidate["week"]["sessions"]:
            session["scheduled_date"] = (
                dt.date.fromisoformat(session["scheduled_date"]) + dt.timedelta(days=21)
            ).isoformat()

        report = validate_plan_state(candidate)

        self.assertEqual("passed", report["status"], report["errors"])
        self.assertEqual([], report["warnings"])


class ReviewRollsTheOutlookForwardTests(GatewayTestCase):
    """The other half of the view: a week becomes precise and leaves the outline.

    This runs through the real change path -- prepare, confirm, apply -- because the
    interesting question is not whether the projection can build an outlook but whether
    the rule that a week may not touch its cycle lets this one through. It has to: the
    outlook is the *rest* of the cycle, so a week that rolls forward necessarily shortens
    it, while everything else about the cycle stays exactly where it was.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        _, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.plan_id = session["plan_state"]["plan_id"]
        self.plan_version = session["plan_state"]["plan_version"]
        self.context = session["context"]
        self.before = session["plan_state"]["current_plan"]

    def change(self, **fields: Any) -> dict[str, Any]:
        request = copy.deepcopy(WEEKLY_CHANGE)
        # Only the parts of a change this test is about; the fixture's own session edit
        # would otherwise leave the replaced session behind in the old week.
        request["sessions"] = [
            {
                "operation": "move",
                "session_id": session["session_id"],
                "scheduled_date": (
                    dt.date.fromisoformat(session["scheduled_date"]) + dt.timedelta(days=7)
                ).isoformat(),
            }
            for session in self.before["week"]["sessions"]
        ]
        request.update(fields)
        return request

    def prepare(self, request: dict[str, Any]) -> tuple[int, Any]:
        return self.call(
            "POST",
            "/v1/coach/decision/prepare",
            body={
                "plan_id": self.plan_id,
                "plan_version": self.plan_version,
                "context": self.context,
                "change_request": request,
            },
            token=TOKEN_A,
        )

    def test_the_week_that_was_outlined_becomes_precise_and_leaves_the_outline(self):
        request = self.change(
            week={"start": "2026-08-17", "intent": "把上一週的輪廓變成這一週的精確課表"},
            cycle={"outlook": self.before["cycle"]["outlook"][1:]},
        )

        status, prepared = self.prepare(request)
        self.assertEqual(200, status, prepared)
        status, applied = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={
                "plan_id": self.plan_id,
                "plan_version": self.plan_version,
                "context": self.context,
                "change_request": request,
                "proposal": prepared["proposal"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)

        _, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        after = session["plan_state"]["current_plan"]
        self.assertEqual("2026-08-17", after["week"]["start"])
        self.assertEqual(
            ["2026-08-24", "2026-08-31"],
            [week["week_start"] for week in after["cycle"]["outlook"]],
        )
        # Everything else about the cycle is exactly where it was: the loosening that let
        # this through reaches the outlook and nothing else.
        for key in ("start", "end", "primary_adaptation", "planned_evidence"):
            self.assertEqual(self.before["cycle"][key], after["cycle"][key], key)

    def test_a_roll_that_leaves_the_stale_outline_behind_is_refused(self):
        """The failure this is worth catching: an outlook still naming the current week.

        The athlete would be shown the week they are in as one of the weeks still ahead.
        """
        status, refused = self.prepare(
            self.change(
                week={"start": "2026-08-17", "intent": "把輪廓變精確，但忘了把它移出輪廓"},
                cycle={"outlook": self.before["cycle"]["outlook"]},
            )
        )

        self.assertEqual(422, status, refused)
        self.assertEqual("validation_failed", refused["error"])

    def test_a_week_still_may_not_move_anything_else_about_the_cycle(self):
        """The control on the loosening: only the outlook became changeable in week mode.

        The roll's own week has to be in the request for the rule to be what refuses it.
        Without it this asked for seven sessions in a week it had not moved, and the
        seven date errors that came back read as a refusal while the rule this is named
        for never ran -- so the errors are asserted whole rather than searched.
        """
        status, refused = self.prepare(
            self.change(
                week={"start": "2026-08-17", "intent": "把上一週的輪廓變成這一週的精確課表"},
                cycle={
                    "outlook": self.before["cycle"]["outlook"][1:],
                    "primary_adaptation": "vo2",
                },
            )
        )

        self.assertEqual(422, status, refused)
        self.assertEqual("validation_failed", refused["error"])
        self.assertEqual(
            [
                "a change that moves this week may not also move the 28-day cycle "
                "beyond its outlook; a cycle change is its own decision"
            ],
            refused["validation"]["errors"],
        )


class FirstPlanShowsFourWeeksTests(unittest.TestCase):
    """#46 and #61 meet here: the first answer is the whole direction, not week one."""

    def project(self, **overrides: Any) -> dict[str, Any]:
        return project_initialization_request(
            initialization_request(**overrides), issued_at=ISSUED_AT
        )

    def test_the_preview_the_athlete_confirms_carries_all_four_weeks(self):
        result = self.project()

        preview = result["preview"]
        self.assertEqual("2026-08-17", preview["week"]["start"])
        self.assertEqual(
            ["2026-08-24", "2026-08-31", "2026-09-07"],
            [week["week_start"] for week in preview["outlook"]],
        )
        # Copied out of the plan the server just built, like every other preview value --
        # confirming the preview and confirming the plan are the same act.
        self.assertEqual(result["plan"]["cycle"]["outlook"], preview["outlook"])

    def test_a_first_plan_that_shows_only_week_one_is_refused(self):
        """The failure #61 names: a 28-day direction that is one week and a sentence.

        An error here rather than the validator's warning, because a first plan is being
        written all at once and has no honest reason to stop after week one.
        """
        request = initialization_request()
        for outlook in ([], request["cycle"]["outlook"][:2]):
            with self.subTest(weeks=len(outlook)):
                broken = copy.deepcopy(request)
                broken["cycle"]["outlook"] = outlook
                with self.assertRaises(ChangeRequestError) as raised:
                    project_initialization_request(broken, issued_at=ISSUED_AT)
                self.assertIn("2026-08-24, 2026-08-31, 2026-09-07", str(raised.exception))

    def test_the_outlined_weeks_must_be_the_ones_that_follow_the_first(self):
        broken = copy.deepcopy(initialization_request())
        broken["cycle"]["outlook"][2]["week_start"] = "2026-09-14"

        with self.assertRaises(ChangeRequestError):
            project_initialization_request(broken, issued_at=ISSUED_AT)

    def test_the_first_plan_validates_with_the_view_it_just_built(self):
        report = validate_plan_state(self.project()["plan"])

        self.assertEqual("passed", report["status"], report["errors"])
        self.assertEqual([], report["warnings"])


if __name__ == "__main__":
    unittest.main()
