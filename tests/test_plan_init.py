"""The projection from a coaching initialization request to a first PlanState.

These are the properties the HTTP layer relies on and cannot restate: that every
mechanical field is derived rather than claimed, that a baseline nobody measured survives
as unknown rather than as a number, and that the same request always projects to the same
bytes -- which is what lets one confirmation bind a plan the agent never holds.
"""

from __future__ import annotations

import copy
import datetime as dt
import unittest
from typing import Any

from garmin_coach_loop.plan_change import ChangeRequestError
from garmin_coach_loop.plan_init import project_initialization_request
from garmin_coach_loop.store import canonical_hash
from garmin_coach_loop.validation import validate_adopted_plan, validate_plan_state


ISSUED_AT = dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc)

EASY_RUN: dict[str, Any] = {
    "sport": "running",
    "scheduled_date": "2026-08-19",
    "time_window": "evening",
    "purpose": "建立有氧底子",
    "adaptation": "aerobic_base",
    "body_stress": "lower",
    "cost": "easy",
    "priority": "anchor",
    "planned_minutes": 30,
    # An open target: the athlete has no measured threshold pace, so the plan states
    # the duration and leaves the intensity to them.
    "plan": {
        "kind": "time_axis",
        "name": "輕鬆跑 30分",
        "steps": [{
            "kind": "work", "name": "輕鬆跑",
            "duration": {"kind": "time", "seconds": 1800},
            "target": {"kind": "open"},
        }],
    },
    "fallback": {"action": "reduce", "description": "縮到 20 分鐘"},
}

REST_DAY: dict[str, Any] = {
    "sport": "rest",
    "scheduled_date": "2026-08-18",
    "purpose": "跑步之間留一天恢復",
    "adaptation": "recovery",
    "body_stress": "systemic",
    "cost": "easy",
    "priority": "flexible",
    "planned_minutes": 0,
    "plan": {"kind": "unstructured"},
    "fallback": {"action": "rest", "description": "維持休息"},
}


def initialization_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "goal": {
            "outcome": "四週後能連續跑完 5 公里不用走路",
            "measurement_protocol": "第 28 天在同一條平路上再跑一次 5 公里",
        },
        "cycle": {
            "start": "2026-08-17",
            "primary_adaptation": "aerobic_base",
            "maintenance_adaptation": None,
            "planned_evidence": ["每週兩次輕鬆跑都完成"],
            "adjust_conditions": ["連續兩週有一次跑步沒做到"],
            "stop_conditions": ["出現疼痛、生病或不尋常症狀時交給人判斷"],
            "outlook": [
                {
                    "week_start": "2026-08-24",
                    "intent": "先把量拉起來，強度不動",
                    "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                    "relation_to_primary": "推進主要適應",
                },
                {
                    "week_start": "2026-08-31",
                    "intent": "維持同樣的形狀，讓身體吸收",
                    "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                    "relation_to_primary": "維持主要適應",
                },
                {
                    "week_start": "2026-09-07",
                    "intent": "量降下來，做這個週期自己的測量",
                    "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                    "relation_to_primary": "量測主要適應",
                },
            ],
        },
        "week_intent": "先把一週兩次的節奏建立起來",
        "sessions": [copy.deepcopy(EASY_RUN), copy.deepcopy(REST_DAY)],
        "summary": "先建立節奏，配速等到量得出來再談",
        "evidence": [{"field": "athlete_reported", "observation": "目前最長跑 3 公里"}],
    }
    request.update(overrides)
    return request


class PlanInitTestCase(unittest.TestCase):
    def project(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return project_initialization_request(
            initialization_request() if request is None else request, issued_at=ISSUED_AT
        )

    def sessions(self, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {session["session_id"]: session for session in plan["week"]["sessions"]}


class DerivedMaterialTests(PlanInitTestCase):
    def test_the_same_request_projects_to_the_same_bytes_every_time(self):
        request = initialization_request()

        first, second = self.project(request), self.project(request)

        self.assertEqual(canonical_hash(first["plan"]), canonical_hash(second["plan"]))
        self.assertEqual(first["preview"], second["preview"])

    def test_two_different_conversations_do_not_share_a_plan_id(self):
        other = initialization_request(week_intent="改成一週三次")

        self.assertNotEqual(
            self.project()["plan"]["plan_id"], self.project(other)["plan"]["plan_id"]
        )

    def test_the_cycle_ends_28_days_after_it_starts_and_the_week_starts_with_it(self):
        plan = self.project()["plan"]

        self.assertEqual("2026-08-17", plan["cycle"]["start"])
        self.assertEqual("2026-09-13", plan["cycle"]["end"])
        self.assertEqual("2026-08-17", plan["week"]["start"])
        self.assertEqual("passed", validate_plan_state(plan)["status"])

    def test_session_bookkeeping_follows_the_session_rather_than_the_request(self):
        plan = self.project(
            initialization_request(
                sessions=[
                    {**copy.deepcopy(EASY_RUN), "cost": "hard"},
                    copy.deepcopy(REST_DAY),
                ]
            )
        )["plan"]

        run = self.sessions(plan)["running-2026-08-19"]
        self.assertTrue(run["hard"])
        self.assertEqual("planned", run["match_status"])
        # Publishing follows the structure the session actually carries: a run planned
        # along a time axis is what delivery can send, and a rest day never is.
        self.assertEqual(
            {"publish_supported": True, "external_id": None, "delivery_state": "not_published"},
            run["execution"],
        )
        self.assertEqual(
            {"publish_supported": False, "external_id": None, "delivery_state": "not_published"},
            self.sessions(plan)["rest-2026-08-18"]["execution"],
        )

    def test_sessions_are_named_and_ordered_by_what_they_are(self):
        plan = self.project()["plan"]

        self.assertEqual(
            ["rest-2026-08-18", "running-2026-08-19"],
            [session["session_id"] for session in plan["week"]["sessions"]],
        )

    def test_the_request_cannot_carry_a_field_the_projection_owns(self):
        for field, value in (
            ("plan_id", "plan-mine"),
            ("version", 1),
            ("status", "active"),
            ("week", {"start": "2026-08-17"}),
        ):
            with self.assertRaises(ChangeRequestError) as raised:
                self.project(initialization_request(**{field: value}))
            self.assertIn(field, str(raised.exception))

    def test_a_cycle_may_not_name_its_own_end_date(self):
        request = initialization_request()
        request["cycle"]["end"] = "2026-09-20"

        with self.assertRaises(ChangeRequestError) as raised:
            self.project(request)

        self.assertIn("end", str(raised.exception))


class UnmeasuredBaselineTests(PlanInitTestCase):
    def test_a_baseline_nobody_gave_stays_null_and_is_named(self):
        projection = self.project()

        baseline = projection["plan"]["athlete_baseline"]
        self.assertEqual(
            {
                "threshold_pace_sec_per_km": None,
                "max_hr": None,
                "easy_hr_ceiling": None,
                "max_session_minutes": None,
                "longest_recent_run_km": None,
                "weekly_volume_km_4wk_avg": None,
                "strength_loads": [],
            },
            baseline,
        )
        self.assertIn("athlete_baseline.max_hr is not measured", projection["unknowns"])
        self.assertIn(
            "athlete_baseline.strength_loads has no measured lift", projection["unknowns"]
        )

    def test_zero_is_never_accepted_in_place_of_an_unmeasured_anchor(self):
        for field in ("threshold_pace_sec_per_km", "max_hr", "easy_hr_ceiling"):
            with self.assertRaises(ChangeRequestError) as raised:
                self.project(initialization_request(baselines={field: 0}))
            self.assertIn("null when it is not measured", str(raised.exception))

    def test_an_assisted_lift_records_its_assistance_and_leaves_the_load_empty(self):
        projection = self.project(
            initialization_request(
                baselines={
                    "strength_loads": [
                        {"exercise": "pull-up", "assist_kg": 15, "scheme": "3x8"}
                    ]
                }
            )
        )

        self.assertEqual(
            [{"exercise": "pull-up", "load_kg": None, "assist_kg": 15, "scheme": "3x8"}],
            projection["plan"]["athlete_baseline"]["strength_loads"],
        )
        self.assertNotIn(
            "athlete_baseline.strength_loads has no measured lift", projection["unknowns"]
        )

    def test_a_plan_with_no_anchor_and_no_exact_target_is_adoptable(self):
        """Missing evidence lowers what can be prescribed; it does not block coaching."""
        plan = self.project()["plan"]

        report = validate_adopted_plan(plan)

        self.assertEqual("passed", report["status"])
        self.assertIn(
            "unknown: athlete_baseline.max_session_minutes is not set; "
            "skipped session duration check",
            report["warnings"],
        )


class DeclaredMinutesTests(PlanInitTestCase):
    """A first plan may not declare a session length its own steps deny (issue #322).

    The same refusal a later change gets, on the one path where the athlete has no prior
    plan to fall back on -- which is where an unchecked contradiction would be worst,
    because the first week is the only description of the athlete's training that exists.
    """

    def _running(self, **over: Any) -> dict[str, Any]:
        return self.project(
            initialization_request(sessions=[{**copy.deepcopy(EASY_RUN), **over}, copy.deepcopy(REST_DAY)])
        )["plan"]

    def test_a_first_plan_declaring_a_length_its_steps_deny_is_refused(self):
        plan = self._running(planned_minutes=45)

        report = validate_adopted_plan(plan)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "declares planned_minutes 45" in error and "own steps take 30-30 minutes" in error
                for error in report["errors"]
            ),
            report["errors"],
        )

    def test_the_first_plan_the_request_actually_sends_is_adoptable(self):
        """The false-positive control: the default request already agrees with itself.

        Thirty minutes on the clock, thirty declared. It passed before this check and has
        to keep passing, or the check would be refusing the product's own example.
        """
        report = validate_adopted_plan(self._running())

        self.assertEqual("passed", report["status"])
        self.assertEqual([], report["errors"])


class StatedSymptomTests(PlanInitTestCase):
    """The explicit-symptom boundary as the validator itself takes it (issue #19).

    The default week runs on the 19th and rests on the 18th, so the same plan answers
    both questions depending only on which day the athlete is reporting about.
    """

    def test_a_symptom_refuses_the_day_the_plan_still_trains(self):
        plan = self.project()["plan"]

        report = validate_adopted_plan(
            plan, red_flags={"chest_pain": True}, today=dt.date(2026, 8, 19)
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            ["explicit red flag (chest_pain) limits 2026-08-19 to rest or a human "
             "decision; this plan still trains today: running-2026-08-19 running"],
            report["errors"],
        )

    def test_the_same_symptom_leaves_a_day_the_plan_rests_alone(self):
        plan = self.project()["plan"]

        for today in (dt.date(2026, 8, 18), dt.date(2026, 8, 20)):
            with self.subTest(today=today):
                report = validate_adopted_plan(
                    plan, red_flags={"chest_pain": True}, today=today
                )

                self.assertEqual("passed", report["status"])

    def test_a_symptom_with_no_day_to_apply_it_to_fails_closed(self):
        """A caller holding a report but no day is a caller that cannot be answered.

        Skipping the check would read a stated symptom as no symptom, which is the one
        direction this must never fail in -- so the plan is refused instead, exactly as
        a change bundle is when its context cannot say which day it is about.
        """
        plan = self.project()["plan"]

        report = validate_adopted_plan(plan, red_flags={"pain": True})

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            ["explicit red flag (pain) cannot be applied: nothing names the day this "
             "plan is being authored on"],
            report["errors"],
        )

    def test_nothing_stated_is_never_read_as_an_all_clear(self):
        """Unknown stays unknown, and unknown is not a refusal either."""
        plan = self.project()["plan"]

        for red_flags in (None, {}, {"chest_pain": None}, {"chest_pain": False}):
            with self.subTest(red_flags=red_flags):
                report = validate_adopted_plan(
                    plan, red_flags=red_flags, today=dt.date(2026, 8, 19)
                )

                self.assertEqual("passed", report["status"])


class PreviewTests(PlanInitTestCase):
    def test_every_number_in_the_preview_is_copied_out_of_the_plan(self):
        projection = self.project(
            initialization_request(
                sessions=[
                    {**copy.deepcopy(EASY_RUN), "cost": "hard"},
                    {**copy.deepcopy(EASY_RUN), "scheduled_date": "2026-08-22",
                     "priority": "flexible", "planned_minutes": 45},
                ]
            )
        )

        plan, preview = projection["plan"], projection["preview"]
        self.assertEqual(75, preview["weekly_planned_minutes"])
        self.assertEqual(1, preview["hard_sessions"])
        self.assertEqual(plan["plan_id"], preview["plan_id"])
        self.assertEqual(plan["goal"], preview["goal"])
        self.assertEqual(plan["athlete_baseline"], preview["athlete_baseline"])
        for session, view in zip(plan["week"]["sessions"], preview["sessions"]):
            for field in ("session_id", "scheduled_date", "planned_minutes", "prescription"):
                self.assertEqual(session[field], view[field], field)

    def test_the_facts_the_plan_was_built_from_travel_with_it(self):
        projection = self.project(
            initialization_request(
                availability={"days": ["週一晚上"], "equipment": ["可調式啞鈴"]}
            )
        )

        preview = projection["preview"]
        self.assertEqual(
            {"days": ["週一晚上"], "equipment": ["可調式啞鈴"]}, preview["availability"]
        )
        self.assertEqual("先建立節奏，配速等到量得出來再談", preview["summary"])
        self.assertEqual(
            [{"field": "athlete_reported", "observation": "目前最長跑 3 公里"}],
            preview["evidence"],
        )
        self.assertEqual(projection["unknowns"], preview["unknowns"])


if __name__ == "__main__":
    unittest.main()


class NoAuthoredProseTests(PlanInitTestCase):
    def test_a_first_plan_has_nowhere_to_write_a_prescription(self):
        # Not "must not" but "cannot": the session shape has no such field, so a
        # sentence sent alongside the structure is refused rather than quietly used.
        request = initialization_request(
            sessions=[
                {**copy.deepcopy(EASY_RUN), "prescription": "5x1000m @4:00/km"},
                copy.deepcopy(REST_DAY),
            ]
        )
        with self.assertRaises(ChangeRequestError) as caught:
            self.project(request)
        self.assertIn("does not accept prescription", str(caught.exception))

    def test_every_session_reads_as_the_rendering_of_its_own_plan(self):
        plan = self.project()["plan"]
        sessions = self.sessions(plan)
        self.assertEqual("輕鬆跑 30分", sessions["running-2026-08-19"]["prescription"])
        self.assertEqual("不設定量化目標", sessions["rest-2026-08-18"]["prescription"])
        self.assertEqual("passed", validate_plan_state(plan)["status"])
