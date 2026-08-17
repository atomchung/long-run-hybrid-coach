"""A cycle's measurement, from declared to scheduled to readable (issues #13, #75).

Every review could say "progress is unproven", and until now that was the only thing it
could ever say. The protocol was prose: the product stored it, validated it was present,
put it in front of the coach, and could not tell whether the measurement had been run,
scheduled, or quietly forgotten. A protocol that can never be run is indistinguishable
from no protocol except that it reads like a commitment.

What closes it is two small structures and no new concepts. The cycle names an ordinary
session to measure against and the week that repeats it; the session scheduled in that
week names what it repeats. There is no measurement session type -- the comparison is a
normal quality session, delivered and reconciled like any other -- and there is no
verdict anywhere in the code. The product says which two readings the comparison is
between and whether each is in. What they mean is the coach's answer, and these tests
assert that nothing here computes one.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.validation import validate_plan_state


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"

from test_gateway import TOKEN_A, GatewayTestCase, publishable_plan  # noqa: E402


REFERENCE = "run-quality-01"
MEASUREMENT = {
    "reference_session_id": REFERENCE,
    "measurement_week_start": "2026-08-31",
    "compare": "同一條路線、同配速，比平均心率",
}


def plan(**measurement: Any) -> dict[str, Any]:
    candidate = json.loads((EXAMPLE / "plan-state-v1.json").read_text(encoding="utf-8"))
    candidate["goal"]["measurement"] = {**MEASUREMENT, **measurement}
    return candidate


class DeclaringAMeasurementTests(unittest.TestCase):
    def test_a_cycle_may_still_declare_its_protocol_in_prose_alone(self):
        """#13: a cycle already in flight declared its protocol before any of this existed.

        It must not be invalidated by the change, and the example plan is that cycle.
        """
        candidate = json.loads(
            (EXAMPLE / "plan-state-v1.json").read_text(encoding="utf-8")
        )

        self.assertNotIn("measurement", candidate["goal"])
        report = validate_plan_state(candidate)
        self.assertEqual("passed", report["status"], report["errors"])

    def test_the_measurement_week_has_to_be_one_of_this_cycles_own_weeks(self):
        """A measurement scheduled outside the window it judges measures nothing."""
        for outside in ("2026-09-07", "2026-08-03", "2026-08-27"):
            with self.subTest(week=outside):
                report = validate_plan_state(plan(measurement_week_start=outside))
                self.assertEqual("blocked", report["status"], outside)
                self.assertTrue(
                    any("measurement_week_start" in error for error in report["errors"]),
                    report["errors"],
                )

    def test_every_week_of_the_cycle_is_a_legal_measurement_week(self):
        for week in ("2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"):
            with self.subTest(week=week):
                report = validate_plan_state(plan(measurement_week_start=week))
                self.assertEqual("passed", report["status"], report["errors"])

    def test_nothing_here_reads_or_scores_a_result(self):
        """AGENTS.md 5: the validator must not become the thing that decides.

        A declared measurement changes what the plan *says*, never whether it passes --
        which is the line between making the measurement computable and making the
        verdict computable.
        """
        with_measurement = validate_plan_state(plan())
        without = validate_plan_state(
            json.loads((EXAMPLE / "plan-state-v1.json").read_text(encoding="utf-8"))
        )

        self.assertEqual(without["status"], with_measurement["status"])
        self.assertEqual(without["errors"], with_measurement["errors"])
        self.assertEqual(without["warnings"], with_measurement["warnings"])


class SchedulingTheComparisonTests(unittest.TestCase):
    """#75: the protocol has to be a session on the calendar, not a sentence on a goal."""

    def measurement_week_plan(self, *, schedule: bool) -> dict[str, Any]:
        candidate = plan(measurement_week_start="2026-08-10")
        if schedule:
            for session in candidate["week"]["sessions"]:
                if session["session_id"] == "run-long-01":
                    session["measures"] = REFERENCE
        return candidate

    def test_the_measurement_week_with_nothing_repeating_the_reference_warns(self):
        """The accountability gap, made visible without blocking the plan.

        A warning rather than an error because the coach may be mid-way through writing
        the week, and a validator that refused a plan until it contained a particular
        session would be prescribing training.
        """
        report = validate_plan_state(self.measurement_week_plan(schedule=False))

        self.assertEqual("passed", report["status"], report["errors"])
        self.assertTrue(
            any(
                "the cycle's own measurement" in warning for warning in report["warnings"]
            ),
            report["warnings"],
        )

    def test_scheduling_it_clears_the_warning_without_changing_anything_else(self):
        report = validate_plan_state(self.measurement_week_plan(schedule=True))

        self.assertEqual("passed", report["status"], report["errors"])
        self.assertFalse(
            any("measurement" in warning for warning in report["warnings"]),
            report["warnings"],
        )

    def test_a_week_that_is_not_the_measurement_week_is_never_asked_for_one(self):
        report = validate_plan_state(plan())

        self.assertEqual("passed", report["status"], report["errors"])
        self.assertEqual([], report["warnings"])

    def test_the_comparison_is_an_ordinary_session_in_every_other_respect(self):
        """No new session type, which is the whole shape of the owner's correction.

        The session carrying `measures` keeps every field it had; nothing about how it is
        validated, delivered, or reconciled is different, and dropping the link leaves an
        identical session behind.
        """
        scheduled = self.measurement_week_plan(schedule=True)
        marked = next(
            session
            for session in scheduled["week"]["sessions"]
            if session.get("measures")
        )
        plain = copy.deepcopy(marked)
        plain.pop("measures")

        self.assertEqual(REFERENCE, marked["measures"])
        self.assertEqual({"measures"}, set(marked) - set(plain))


class ReadingTheResultTests(GatewayTestCase):
    """What a review can actually say, through the real context build."""

    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()

    def session(self, *, plan: dict[str, Any]) -> dict[str, Any]:
        self.seed_owner(TOKEN_A, plan=plan)
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        return payload["context"]

    def test_a_cycle_with_no_measurement_says_so_rather_than_unproven_forever(self):
        """#75's third criterion, and the one that changes what a review may claim.

        "This cycle scheduled no measurement" and "the measurement has not been taken"
        are different findings with different next actions, and before this the product
        could only produce the second one, forever.
        """
        context = self.session(plan=self.plan)

        self.assertIsNone(context["goal_context"]["measurement"])
        self.assertIsNone(context["measurement_evidence"])
        # And the prose protocol still travels, so nothing was lost by not declaring one.
        self.assertTrue(context["goal_context"]["measurement_protocol"])

    def test_a_declared_measurement_names_its_two_sessions_and_what_is_owed(self):
        candidate = copy.deepcopy(self.plan)
        candidate["goal"]["measurement"] = MEASUREMENT

        context = self.session(plan=candidate)

        self.assertEqual(MEASUREMENT, context["goal_context"]["measurement"])
        evidence = context["measurement_evidence"]
        # Nothing repeats the reference yet, and the product says exactly that rather
        # than reporting the outcome as unproven for an unstated reason.
        self.assertIsNone(evidence["comparison_session_id"])
        self.assertEqual("not_scheduled", evidence["comparison_result"])

    def test_a_scheduled_comparison_is_named_and_reported_as_not_yet_run(self):
        candidate = copy.deepcopy(self.plan)
        candidate["goal"]["measurement"] = {
            **MEASUREMENT,
            "measurement_week_start": candidate["week"]["start"],
        }
        for session in candidate["week"]["sessions"]:
            if session["session_id"] == "run-long-01":
                session["measures"] = REFERENCE

        evidence = self.session(plan=candidate)["measurement_evidence"]

        self.assertEqual("run-long-01", evidence["comparison_session_id"])
        # Its day has not passed, so there is no reading -- which is "scheduled", not
        # "missing" and not "unproven".
        self.assertEqual("scheduled", evidence["comparison_result"])

    def test_the_context_carries_no_verdict_difference_or_score(self):
        """The line #13 had to draw: the measurement is computable, the verdict is not."""
        candidate = copy.deepcopy(self.plan)
        candidate["goal"]["measurement"] = MEASUREMENT

        evidence = self.session(plan=candidate)["measurement_evidence"]

        self.assertEqual(
            {"comparison_session_id", "reference_result", "comparison_result"},
            set(evidence),
        )
        rendered = json.dumps(evidence)
        for forbidden in ("improved", "delta", "score", "pass", "fail", "verdict"):
            self.assertNotIn(forbidden, rendered, forbidden)


if __name__ == "__main__":
    unittest.main()
