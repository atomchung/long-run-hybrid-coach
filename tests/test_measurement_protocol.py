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
import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.validation import validate_plan_state


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"

from test_gateway import (  # noqa: E402
    ONBOARDING,
    TOKEN_A,
    GatewayTestCase,
    as_change_request,
    publishable_plan,
)


REFERENCE = "run-quality-01"
MEASUREMENT = {
    "reference_session_id": REFERENCE,
    "measurement_week_start": "2026-08-31",
    "compare": "同一條路線、同配速，比平均心率",
}

# ``ONBOARDING``'s cycle opens on 2026-08-17, so this is the Monday its second week
# begins -- cycle day 8, and the first instant at which a weekly decision is due on a
# plan whose first week is behind the athlete.
WEEK_TWO_MONDAY = dt.datetime(2026, 8, 24, 1, 0, tzinfo=dt.timezone.utc)

# The ordinary weekly roll, carrying no goal: the week moves forward and the outlook
# shortens by the week that just became precise.
WEEK_TWO_ROLL: dict[str, Any] = {
    "summary": "第一週跑完，第二週把量拉起來",
    "reason_codes": ["actual_load_below_plan"],
    "evidence": [{"field": "cycle_sessions", "observation": "第一週三堂都完成"}],
    "goal_effect": {"week": "量增加", "cycle": "28 天方向不變"},
    "next_review_condition": "下週一再看一次",
    "week": {"start": "2026-08-24", "intent": "先把量拉起來，強度不動"},
    "cycle": {
        "outlook": [
            {
                "week_start": "2026-08-31",
                "intent": "維持同樣的形狀，讓身體吸收",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑"],
                "relation_to_primary": "維持主要適應",
            },
            {
                "week_start": "2026-09-07",
                "intent": "量降下來，做這個週期自己的測量",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑"],
                "relation_to_primary": "量測主要適應",
            },
        ]
    },
    "sessions": [
        {
            "operation": "add",
            "sport": "running",
            "scheduled_date": "2026-08-26",
            "time_window": "evening",
            "purpose": "建立有氧底子",
            "adaptation": "aerobic_base",
            "body_stress": "lower",
            "cost": "easy",
            "priority": "anchor",
            "planned_minutes": 35,
            "fallback": {"action": "reduce", "description": "縮到 25 分鐘"},
            "plan": {
                "kind": "time_axis",
                "name": "35 分鐘輕鬆跑",
                "steps": [
                    {
                        "kind": "work",
                        "name": "輕鬆跑",
                        "duration": {"kind": "time", "seconds": 2100},
                        "target": {"kind": "open"},
                    }
                ],
            },
        }
    ],
}

# The same turn's other half, sent on its own: the goal, and nothing that touches the
# week. ``goal`` is filled in per test, because the reference session id is only knowable
# after the first plan has been written.
DECLARE_THE_MEASUREMENT: dict[str, Any] = {
    "summary": "把這個週期的量測定下來：參考第一週那堂輕鬆跑",
    "reason_codes": ["goal_priority_changed"],
    "evidence": [
        {"field": "goal_context.measurement", "observation": "這個週期還沒有可執行的量測"}
    ],
    "goal_effect": {"week": "本週不動", "cycle": "量測排在第四週"},
    "next_review_condition": "第四週那堂比較課跑完",
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
        status, payload = self.route("session", body={}, token=TOKEN_A)
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


class DeclaringItAfterTheFirstPlanTests(GatewayTestCase):
    """#372: the step between "a first plan may not declare it" and "a review reads it".

    Everything above assumes a declared measurement, or a cycle that chose prose. There
    was a third state nobody had a route out of, and it was the one every athlete starts
    in: ``plan_init`` may not accept ``goal.measurement`` at all, because its
    ``reference_session_id`` has to name a session whose id the same request is deriving.
    So the first plan is prose by contract -- and until this, nothing anywhere said that
    the contract's reason expires the moment those sessions exist. The account this was
    found on had reached its third week, and its twenties in plan versions, without one.

    The whole route is driven here rather than asserted field by field, because the gap
    was never in a field: every part worked and no turn joined them.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A)

    def author_the_first_plan(self) -> dict[str, Any]:
        body = {"change_request": as_change_request(ONBOARDING)}
        status, prepared = self.route("decision_prepare", body=body, token=TOKEN_A)
        self.assertEqual(200, status, prepared)
        status, applied = self.route(
            "decision_apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        self.prepared_first_plan = prepared
        return applied

    def read(self, when: dt.datetime) -> dict[str, Any]:
        self.now = when
        status, session = self.route("session", body={}, token=TOKEN_A)
        self.assertEqual(200, status, session)
        return session

    def measurement_lines(self, values: Any) -> list[str]:
        return [line for line in values or [] if "measurement" in line]

    def first_running_session(self, session: dict[str, Any]) -> str:
        return next(
            row["session_id"]
            for row in session["context"]["cycle_sessions"]
            if row["sport"] == "running"
        )

    # -- the contract's own exemption, and its expiry -----------------------------------

    def test_a_first_plan_is_not_asked_for_what_its_own_contract_refuses(self):
        """The one turn that must stay silent, on both channels.

        A first plan's sessions do not have ids until the server has read the request, so
        naming one would be the coach inventing an id. Saying anything here would be the
        product asking for the field it is about to refuse.
        """
        applied = self.author_the_first_plan()

        self.assertEqual([], self.measurement_lines(self.prepared_first_plan["warnings"]))
        self.assertEqual([], self.measurement_lines(self.prepared_first_plan["unknowns"]))
        self.assertEqual([], self.measurement_lines(applied["validation"]["warnings"]))

    def test_inside_the_first_week_nothing_is_said_either(self):
        """The nearest control: the same account three days in.

        The reference sessions exist by now, so this is not a structural impossibility --
        it is the week whose own decisions are still being executed, and a line here
        would be on every daily read from the first conversation onwards.
        """
        self.author_the_first_plan()

        session = self.read(dt.datetime(2026, 8, 19, 1, 0, tzinfo=dt.timezone.utc))

        self.assertEqual(3, session["context"]["review_frame"]["cycle_day"])
        self.assertEqual([], self.measurement_lines(session["unknowns"]))

    def test_past_the_first_week_the_read_says_the_measurement_is_undeclared(self):
        """The Monday the first weekly decision is taken from."""
        self.author_the_first_plan()

        session = self.read(WEEK_TWO_MONDAY)
        context = session["context"]

        self.assertEqual(8, context["review_frame"]["cycle_day"])
        self.assertIsNone(context["goal_context"]["measurement"])
        self.assertIsNone(context["measurement_evidence"])
        line = self.measurement_lines(session["unknowns"])
        self.assertEqual(1, len(line), session["unknowns"])
        # What it has to carry: the gap, why the first plan did not close it, and the
        # boundary that decides which decision closes it now.
        self.assertIn("no reference session", line[0])
        self.assertIn("a first plan could not name one", line[0])
        self.assertIn("its own decision", line[0])
        # And the sessions it would name are on the plan, with ids the coach can read.
        self.assertTrue(
            [row["session_id"] for row in context["cycle_sessions"]], context["cycle_sessions"]
        )

    def test_rolling_the_week_without_declaring_it_is_warned_on_the_plan_it_writes(self):
        """The write-side half, at the moment the exemption stops applying.

        ``before`` is still the first plan, whose week is the cycle's own first, so the
        warning names ``after`` alone -- the plan this decision is about to write.
        """
        self.author_the_first_plan()
        session = self.read(WEEK_TWO_MONDAY)

        status, prepared = self.route(
            "decision_prepare",
            body={
                "change_request": WEEK_TWO_ROLL,
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": session["context"],
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, prepared)
        warnings = self.measurement_lines(prepared["warnings"])
        self.assertEqual(1, len(warnings), prepared["warnings"])
        self.assertTrue(warnings[0].startswith("after: plan.goal.measurement"), warnings[0])
        self.assertIn("moved past the cycle's first week", warnings[0])

    def test_declaring_it_closes_both_signals_and_makes_the_comparison_readable(self):
        """The route out, end to end, and the state it leaves behind.

        A goal change is its own decision -- ``validate_bundle`` refuses one riding along
        on a week change (issue #267) -- which is why the read's own line says so, and
        why this sends the goal alone.
        """
        self.author_the_first_plan()
        session = self.read(WEEK_TWO_MONDAY)
        # Week one's own easy run: an ordinary session the athlete has already done, which
        # is what a reference is. Read off the plan rather than composed, because an id a
        # coach constructs is what every other part of this contract refuses.
        reference = self.first_running_session(session)
        declaration = {
            **DECLARE_THE_MEASUREMENT,
            "goal": {
                **ONBOARDING["goal"],
                "measurement": {
                    "reference_session_id": reference,
                    "measurement_week_start": "2026-09-07",
                    "compare": "同一條平路、同樣 30 分鐘，比平均心率",
                },
            },
        }
        body = {
            "change_request": declaration,
            "plan_id": session["plan_state"]["plan_id"],
            "plan_version": session["plan_state"]["plan_version"],
            "context": session["context"],
        }

        status, prepared = self.route("decision_prepare", body=body, token=TOKEN_A)
        self.assertEqual(200, status, prepared)
        self.assertEqual([], self.measurement_lines(prepared["warnings"]))
        status, applied = self.route(
            "decision_apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)

        after = self.read(WEEK_TWO_MONDAY)
        self.assertEqual([], self.measurement_lines(after["unknowns"]))
        self.assertEqual(
            {
                "reference_session_id": reference,
                "measurement_week_start": "2026-09-07",
                "compare": "同一條平路、同樣 30 分鐘，比平均心率",
            },
            after["context"]["goal_context"]["measurement"],
        )
        # The comparison is not on the calendar yet, and the product says which of the two
        # states that is -- the distinction the whole of #75 exists for.
        self.assertEqual(
            "not_scheduled", after["context"]["measurement_evidence"]["comparison_result"]
        )

    def test_a_week_change_may_not_carry_the_declaration_with_it(self):
        """Why the read's line names a boundary instead of just naming the field.

        Sending both is the obvious first attempt, and it is refused. The refusal is the
        product's, not this change's -- asserted here so that the sentence the athlete's
        coach reads and the rule the gateway enforces cannot drift apart.
        """
        self.author_the_first_plan()
        session = self.read(WEEK_TWO_MONDAY)

        status, refused = self.route(
            "decision_prepare",
            body={
                "change_request": {
                    **WEEK_TWO_ROLL,
                    "goal": {
                        **ONBOARDING["goal"],
                        "measurement": {
                            "reference_session_id": self.first_running_session(session),
                            "measurement_week_start": "2026-09-07",
                            "compare": "同一條平路、同樣 30 分鐘，比平均心率",
                        },
                    },
                },
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": session["context"],
            },
            token=TOKEN_A,
        )

        self.assertEqual(422, status, refused)
        self.assertIn(
            "a goal change is its own decision",
            " ".join(refused["validation"]["errors"]),
        )


if __name__ == "__main__":
    unittest.main()
