from __future__ import annotations

import ast
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop import validation

from garmin_coach_loop.intent_text import (
    prescribed_token_in_coach_note,
    prescribed_token_in_intent,
)
from garmin_coach_loop.prescription import render_prescription
from garmin_coach_loop.validation import (
    RECONCILIATION_ACTUAL_REQUIRED_FIELDS,
    validate_bundle,
    validate_coach_context,
    validate_decision_event,
    validate_plan_state,
)

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # The runtime validators remain dependency-free.
    Draft202012Validator = None
    FormatChecker = None


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"
CONTRACTS = ROOT / "contracts"


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not a JSON object")
    return value


def unstructured(session: dict) -> dict:
    """Strip a session down to the model that declares nothing, and say so in its text."""
    session["plan"] = {"kind": "unstructured"}
    return rerendered(session)


def rerendered(session: dict) -> dict:
    """Keep a hand-edited session's prescription the rendering of its own plan.

    Every production write path renders it; a test that edits a plan directly has to do
    the same, or the plan it built is refused for describing itself as what it used to be.
    """
    session["prescription"] = render_prescription(session["plan"])
    return session


def project_context(context: dict, plan: dict) -> dict:
    """Mirror the builder projection without rewriting append-only example files."""
    projected = copy.deepcopy(context)
    projected["goal_context"] = {
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "primary_goal": f"{plan['cycle']['primary_adaptation']} — {plan['goal']['outcome']}",
        "maintenance_goal": plan["cycle"]["maintenance_adaptation"],
        "measurement_protocol": plan["goal"]["measurement_protocol"],
        "measurement": plan["goal"].get("measurement") or None,
    }
    projected["measurement_evidence"] = (
        None
        if projected["goal_context"]["measurement"] is None
        else {
            "comparison_session_id": None,
            "reference_result": "not_in_record",
            "comparison_result": "not_scheduled",
        }
    )
    projected["athlete_baseline"] = copy.deepcopy(plan.get("athlete_baseline"))
    projected["current_calendar"] = [
        {
            "session_id": session["session_id"],
            "date": session["scheduled_date"],
            "sport": session["sport"],
            "cost": session["cost"],
            "status": "completed" if session["match_status"] == "partial" else session["match_status"],
        }
        for session in plan["week"]["sessions"]
    ]
    return projected


class CoachLoopV1Tests(unittest.TestCase):
    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.after = load(EXAMPLE / "plan-state-v2-day-4.json")
        self.context = project_context(load(EXAMPLE / "coach-context-day-4.json"), self.before)
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def test_bundle_rejects_context_that_is_not_an_exact_plan_projection(self):
        contexts = []
        stale_goal = copy.deepcopy(self.context)
        stale_goal["goal_context"]["primary_goal"] = "stale goal"
        contexts.append(stale_goal)
        stale_baseline = copy.deepcopy(self.context)
        stale_baseline["athlete_baseline"]["max_hr"] = 999
        contexts.append(stale_baseline)
        stale_calendar = copy.deepcopy(self.context)
        stale_calendar["current_calendar"][0]["date"] = "2026-08-09"
        contexts.append(stale_calendar)
        for context in contexts:
            with self.subTest(field=context):
                report = validate_bundle(context, self.before, self.after, self.event)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(any("exactly project" in error for error in report["errors"]))

    def test_cycle_and_week_with_complete_running_and_strength_prescriptions_pass(self):
        for mode, action in (("plan_cycle", "create"), ("plan_week", "adjust")):
            with self.subTest(mode=mode):
                event = copy.deepcopy(self.event)
                event.update({"mode": mode, "action": action})
                report = validate_bundle(self.context, self.before, self.after, event)
                self.assertEqual("passed", report["status"], report)

    def test_cycle_mode_can_make_a_goal_only_change_when_judgment_requires_it(self):
        after = copy.deepcopy(self.before)
        after["version"] += 1
        after["goal"]["outcome"] = "build repeatable 5K execution under the updated constraint"
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_cycle", "action": "adjust"})
        report = validate_bundle(self.context, self.before, after, event)
        self.assertEqual("passed", report["status"], report)

    def test_a_session_missing_its_plan_entirely_does_not_validate(self):
        # `plan` is required, with no `optional=` escape: a PlanState stored before the
        # field existed does not open, which is the compatibility decision itself.
        after = copy.deepcopy(self.after)
        after["week"]["sessions"][0].pop("plan")
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust"})
        report = validate_bundle(self.context, self.before, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("plan is required" in error for error in report["errors"]))

    def test_adoption_rejects_a_blank_run_and_warns_on_a_blank_strength_session(self):
        # One binding, two verdicts. A run declaring `unstructured` is the "go for a
        # run" case and blocks -- its structure is what the watch executes. A strength
        # session declaring it is the athlete's own decision (2026-08-14) to leave the
        # session unquantified: nothing is delivered either way, so it adopts with a
        # warning naming what the blank costs instead of a refusal.
        for mode, sport, expected in (
            ("plan_cycle", "running", "blocked"),
            ("plan_week", "strength", "passed"),
        ):
            with self.subTest(mode=mode, sport=sport):
                after = copy.deepcopy(self.after)
                target = next(
                    session for session in after["week"]["sessions"]
                    if session["sport"] == sport and session["match_status"] == "planned"
                )
                unstructured(target)
                event = copy.deepcopy(self.event)
                event.update({"mode": mode, "action": "create" if mode == "plan_cycle" else "adjust"})
                report = validate_bundle(self.context, self.before, after, event)
                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(
                        any("prescribes nothing to do" in error for error in report["errors"]),
                        report["errors"],
                    )
                else:
                    self.assertTrue(
                        any(
                            "declares no quantified structure" in warning
                            for warning in report["warnings"]
                        ),
                        report["warnings"],
                    )

    def test_daily_replace_rejects_a_run_with_nothing_to_execute(self):
        # The false-positive control sits beside it: a run deliberately left to the
        # athlete is not blocked, it is an `open` target on a stated duration.
        open_run = {
            "kind": "time_axis",
            "name": "Easy 50 minutes",
            "steps": [{
                "kind": "work", "name": "Easy run",
                "duration": {"kind": "time", "seconds": 3000},
                "target": {"kind": "open"},
            }],
        }
        for plan, expected in ((None, "blocked"), (open_run, "passed")):
            with self.subTest(expected=expected):
                after = copy.deepcopy(self.before)
                after["version"] += 1
                target = next(
                    session for session in after["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )
                if plan is None:
                    unstructured(target)
                else:
                    target["plan"] = copy.deepcopy(plan)
                    rerendered(target)
                target["match_status"] = "replaced"
                event = copy.deepcopy(self.event)
                report = validate_bundle(self.context, self.before, after, event)
                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(
                        any("prescribes nothing to do" in error for error in report["errors"])
                    )

    def test_daily_move_must_leave_the_session_in_moved_status(self):
        after = copy.deepcopy(self.before)
        after["version"] += 1
        target = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        target["scheduled_date"] = "2026-08-14"
        event = copy.deepcopy(self.event)
        event["action"] = "move"

        report = validate_bundle(self.context, self.before, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("match_status=moved" in error for error in report["errors"]))

    def test_daily_delivery_relevant_change_must_clear_old_intervals_observation(self):
        for action in ("move", "replace"):
            with self.subTest(action=action):
                before = copy.deepcopy(self.before)
                target_before = next(
                    session for session in before["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )
                target_before["execution"] = {
                    "publish_supported": True,
                    "external_id": "intervals-event-123",
                    "delivery_state": "intervals_accepted",
                }
                context = project_context(self.context, before)
                after = copy.deepcopy(before)
                after["version"] += 1
                target_after = next(
                    session for session in after["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )
                target_after["match_status"] = "moved" if action == "move" else "replaced"
                if action == "move":
                    target_after["scheduled_date"] = "2026-08-14"
                else:
                    target_after["plan"] = {
                        "kind": "time_axis",
                        "name": "Easy 50 minutes",
                        "steps": [{
                            "kind": "work", "name": "Easy run",
                            "duration": {"kind": "time", "seconds": 3000},
                            "target": {"kind": "open"},
                        }],
                    }
                    rerendered(target_after)
                event = copy.deepcopy(self.event)
                event["action"] = action

                stale = validate_bundle(context, before, after, event)
                self.assertEqual("blocked", stale["status"])
                self.assertTrue(any("reset execution.delivery_state" in error for error in stale["errors"]))

                target_after["execution"].update(
                    {"external_id": None, "delivery_state": "not_published"}
                )
                reset = validate_bundle(context, before, after, event)
                self.assertEqual("passed", reset["status"], reset)

    def test_retitling_a_delivered_strength_session_must_clear_its_observation(self):
        before = copy.deepcopy(self.before)
        delivered = next(
            session for session in before["week"]["sessions"]
            if session["session_id"] == "strength-upper-01"
        )
        delivered["execution"] = {
            "publish_supported": True,
            "external_id": "intervals-event-456",
            "delivery_state": "intervals_accepted",
        }
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "strength-upper-01"
        )
        target["match_status"] = "replaced"
        event = copy.deepcopy(self.event)
        event.update({"action": "replace", "session_id": "strength-upper-01"})

        # The control: match_status moving is not what Intervals holds, so the observation
        # stands. Whatever the next assertion blocks, it is blocking the retitle itself.
        self.assertEqual("passed", validate_bundle(context, before, after, event)["status"])

        # A movement_list session reaches the calendar as a titled entry whose name is
        # purpose. Reword it and the accepted delivery is a title the plan no longer says.
        # The rendering does not move with it -- prescription is rendered from plan alone.
        target["purpose"] = "Hold upper-body strength while the legs recover"

        stale = validate_bundle(context, before, after, event)
        self.assertEqual("blocked", stale["status"])
        self.assertTrue(
            any("changed delivered workout content" in error for error in stale["errors"]),
            stale["errors"],
        )

        target["execution"].update({"external_id": None, "delivery_state": "not_published"})
        reset = validate_bundle(context, before, after, event)
        self.assertEqual("passed", reset["status"], reset)

    def test_purpose_only_reword_on_a_delivered_session_must_clear_its_observation(self):
        """A session whose *only* change is its purpose -- match_status stays planned,
        nothing else moves -- is the case the retitle test above does not reach, because
        that one also flips match_status to replaced, which was already material on its
        own. Here purpose alone must (a) not be refused as immaterial and (b) still force
        the same delivery-observation reset as any other delivered-content change."""
        before = copy.deepcopy(self.before)
        delivered = next(
            session for session in before["week"]["sessions"]
            if session["session_id"] == "strength-upper-01"
        )
        delivered["execution"] = {
            "publish_supported": True,
            "external_id": "intervals-event-789",
            "delivery_state": "intervals_accepted",
        }
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "strength-upper-01"
        )
        target["purpose"] = "Hold upper-body strength while the legs recover"
        # week mode, not the daily mode the other delivery tests use here: revisit_today
        # ties `replace`/`move` to a required match_status, which this change deliberately
        # never touches.
        event = copy.deepcopy(self.event)
        event.update({"mode": "review_week", "action": "adjust", "session_id": "strength-upper-01"})

        stale = validate_bundle(context, before, after, event)
        self.assertEqual("blocked", stale["status"])
        self.assertTrue(
            any("changed delivered workout content" in error for error in stale["errors"]),
            stale["errors"],
        )
        self.assertFalse(
            any("nothing material moved" in error for error in stale["errors"]),
            stale["errors"],
        )
        self.assertEqual("planned", target["match_status"])

        target["execution"].update({"external_id": None, "delivery_state": "not_published"})
        reset = validate_bundle(context, before, after, event)
        self.assertEqual("passed", reset["status"], reset)

    def test_same_week_plan_cannot_remove_an_intervals_accepted_session(self):
        before = copy.deepcopy(self.before)
        delivered = next(
            session for session in before["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        delivered["execution"] = {
            "publish_supported": True,
            "external_id": "intervals-event-123",
            "delivery_state": "intervals_accepted",
        }
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        after["week"]["sessions"] = [
            session for session in after["week"]["sessions"]
            if session["session_id"] != "run-quality-01"
        ]
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust", "session_id": None})

        report = validate_bundle(context, before, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("same-week plan removed delivered session run-quality-01" in error for error in report["errors"])
        )

    def test_new_week_rollover_may_retire_last_weeks_delivered_session(self):
        before = copy.deepcopy(self.before)
        delivered = next(
            session for session in before["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        delivered["execution"] = {
            "publish_supported": True,
            "external_id": "intervals-event-123",
            "delivery_state": "intervals_accepted",
        }
        context = project_context(self.context, before)
        next_week_session = copy.deepcopy(delivered)
        next_week_session.update(
            {
                "session_id": "run-quality-week-2",
                "scheduled_date": "2026-08-20",
                "match_status": "planned",
            }
        )
        next_week_session["execution"].update(
            {"external_id": None, "delivery_state": "not_published"}
        )
        after = copy.deepcopy(before)
        after["version"] += 1
        after["cycle"]["outlook"] = after["cycle"]["outlook"][1:]
        after["week"].update(
            {
                "start": "2026-08-17",
                "intent": "Continue the cycle with the next threshold anchor",
                "sessions": [next_week_session],
            }
        )
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust", "session_id": None})

        report = validate_bundle(context, before, after, event)

        self.assertEqual("passed", report["status"], report)
        self.assertFalse(any("removed delivered session" in error for error in report["errors"]))

    def test_week_mode_cannot_rewrite_the_cycle_but_may_move_the_baseline(self):
        """The 28-day direction stays out of a week decision's reach. The baseline does
        not (issue #32): judging whether the anchor still describes the athlete is part
        of prescribing, and week modes are where prescriptions get written."""
        after = copy.deepcopy(self.after)
        after["cycle"]["primary_adaptation"] = "vo2"
        after["athlete_baseline"]["max_hr"] = 199
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust"})

        report = validate_bundle(self.context, self.before, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("preserve the current 28-day cycle" in error for error in report["errors"]))
        self.assertFalse(any("athlete_baseline" in error for error in report["errors"]))

    def test_delivery_state_requires_the_verified_event_id_pair(self):
        plan = copy.deepcopy(self.before)
        session = plan["week"]["sessions"][0]
        session["execution"].update({"delivery_state": "intervals_accepted", "external_id": None})
        self.assertEqual("blocked", validate_plan_state(plan)["status"])

        plan = copy.deepcopy(self.before)
        session = plan["week"]["sessions"][0]
        session["execution"].update({"delivery_state": "not_published", "external_id": "123"})
        self.assertEqual("blocked", validate_plan_state(plan)["status"])

    def _quality_step(self, plan: dict, path: tuple[int, ...]) -> dict:
        step = next(
            s for s in plan["week"]["sessions"] if s["session_id"] == "run-quality-01"
        )["plan"]["steps"]
        node = step[path[0]]
        for index in path[1:]:
            node = node["steps"][index]
        return node

    def test_hr_ceiling_target_rejects_extra_keys(self):
        # No floor/low field on purpose: a lower HR bound must be structurally
        # unrepresentable, not merely discouraged (#38 dogfood: 77-83 %hr resolved
        # against max HR, enforcing a floor during a recovery run meant to stay
        # under 140).
        for extra_key in ("floor_bpm", "low_bpm", "start_bpm"):
            with self.subTest(extra_key=extra_key):
                plan = copy.deepcopy(self.before)
                self._quality_step(plan, (0,))["target"] = {
                    "kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140, extra_key: 100,
                }
                report = validate_plan_state(plan)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(any("target" in e and "not allowed" in e for e in report["errors"]))

    def test_workout_target_kind_must_be_open_pace_or_hr_ceiling(self):
        plan = copy.deepcopy(self.before)
        self._quality_step(plan, (0,))["target"] = {
            "kind": "hr_range", "unit": "bpm", "low_bpm": 120, "high_bpm": 140,
        }
        report = validate_plan_state(plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must be open, pace, or hr_ceiling" in e for e in report["errors"]))

    def test_hr_ceiling_is_not_allowed_inside_a_repeat(self):
        plan = copy.deepcopy(self.before)
        self._quality_step(plan, (1, 0))["target"] = {
            "kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140,
        }
        report = validate_plan_state(plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("hr_ceiling is not allowed inside a repeat" in e for e in report["errors"]))

    def test_workout_cannot_mix_pace_and_hr_ceiling_targets(self):
        # One target per session (#38 constraint 4): pace and heart rate must
        # never both bind the device on the same workout. run-quality-01 already
        # carries a pace target inside its repeat; adding a top-level hr_ceiling
        # step must be rejected even though hr_ceiling itself is legal there.
        plan = copy.deepcopy(self.before)
        self._quality_step(plan, (0,))["target"] = {
            "kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140,
        }
        report = validate_plan_state(plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must not mix pace and hr_ceiling" in e for e in report["errors"]))

    def test_anonymous_28_day_example_passes_deterministic_validation(self):
        self.assertEqual("passed", validate_coach_context(self.context)["status"])
        self.assertEqual("passed", validate_plan_state(self.before)["status"])
        self.assertEqual("passed", validate_plan_state(self.after)["status"])
        self.assertEqual("passed", validate_decision_event(self.event)["status"])
        report = validate_bundle(self.context, self.before, self.after, self.event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertEqual("deterministic_coach_boundary", report["policy"])

    @unittest.skipIf(Draft202012Validator is None, "jsonschema dev dependency is unavailable")
    def test_anonymous_artifacts_match_public_json_schemas(self):
        pairs = (
            ("coach-context.schema.json", self.context),
            ("plan-state.schema.json", self.before),
            ("plan-state.schema.json", self.after),
            ("decision-event.schema.json", self.event),
        )
        for schema_name, artifact in pairs:
            with self.subTest(schema=schema_name):
                schema = load(CONTRACTS / schema_name)
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema, format_checker=FormatChecker())
                self.assertEqual([], list(validator.iter_errors(artifact)))

    def test_context_with_legacy_partial_recovery_freshness_still_validates(self):
        # "partial" is a legacy grade (issue #95): no current builder emits it on
        # freshness.recovery any more -- _recovery_freshness now grades recency only,
        # mechanically, and sufficiency is the coach's judgment, read from coverage.
        # doctor_store revalidates the full stored history against this module on
        # every read, so a context written before the change, still carrying
        # "partial", must keep validating against both the deterministic layer and
        # the public schema.
        context = copy.deepcopy(self.context)
        context["freshness"]["recovery"] = "partial"
        self.assertEqual("passed", validate_coach_context(context)["status"])

        if Draft202012Validator is not None:
            schema = load(CONTRACTS / "coach-context.schema.json")
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            self.assertEqual([], list(validator.iter_errors(context)))

    def test_a_context_written_before_subjective_states_existed_still_validates(self):
        """Issue #188 adds an optional key, on the precedent every group before it set.

        The example context this suite validates carries no `subjective_states` at all,
        and a caller confirming a decision hands back the context it was given in an
        earlier turn -- which is the one artifact it cannot rebuild. A required key here
        would refuse exactly that.
        """
        self.assertNotIn("subjective_states", self.context)
        self.assertEqual("passed", validate_coach_context(self.context)["status"])

        stated = copy.deepcopy(self.context)
        stated["subjective_states"] = {
            "source": "athlete_reported",
            "window_start": "2026-07-31",
            "window_end": "2026-08-13",
            "states": [
                {
                    "date": "2026-08-13",
                    "note": "這幾天覺得很累",
                    "recorded_at": "2026-08-13T04:00:00Z",
                }
            ],
        }
        self.assertEqual("passed", validate_coach_context(stated)["status"])

        if Draft202012Validator is not None:
            schema = load(CONTRACTS / "coach-context.schema.json")
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            for artifact in (self.context, stated):
                self.assertEqual([], list(validator.iter_errors(artifact)))

    def test_a_subjective_state_row_may_not_carry_a_score_the_store_never_took(self):
        """The row shape is the guarantee: there is nowhere to put a reading of the words."""
        scored = copy.deepcopy(self.context)
        scored["subjective_states"] = {
            "source": "athlete_reported",
            "window_start": "2026-07-31",
            "window_end": "2026-08-13",
            "states": [
                {
                    "date": "2026-08-13",
                    "note": "很累",
                    "recorded_at": "2026-08-13T04:00:00Z",
                    "severity": 4,
                }
            ],
        }

        report = validate_coach_context(scored)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("severity" in error for error in report["errors"]), report["errors"])

    def test_a_context_written_before_training_history_existed_still_validates(self):
        """issue #101, on the precedent subjective_states already set (test above):
        an optional key added after a plan was already persisted must not refuse the
        one artifact a caller cannot rebuild -- the context it was handed in an earlier
        turn, now being confirmed against."""
        self.assertNotIn("training_history", self.context)
        self.assertEqual("passed", validate_coach_context(self.context)["status"])

        stated = copy.deepcopy(self.context)
        stated["training_history"] = {
            "source": "athlete_reported",
            "months": [
                {
                    "month": "2026-06",
                    "sport": "running",
                    "session_count": 3,
                    "total_minutes": 120,
                    "total_km": 24.5,
                    "provenance_counts": {
                        "athlete_reported": 3,
                        "athlete_imported": 0,
                        "prescribed_confirmed": 0,
                    },
                }
            ],
            "truncated": False,
            "earliest_observed_month": "2026-06",
            "movement_longevity": [
                {
                    "exercise": "bench_press",
                    "display_name": "臥推",
                    "earliest": {
                        "date": "2026-06-01",
                        "weight_kg": 60.0,
                        "assist_kg": None,
                        "held_every_set": True,
                    },
                    "heaviest": {
                        "date": "2026-06-01",
                        "weight_kg": 60.0,
                        "assist_kg": None,
                        "held_every_set": True,
                    },
                }
            ],
            "movement_longevity_truncated": False,
        }
        self.assertEqual("passed", validate_coach_context(stated)["status"])

        if Draft202012Validator is not None:
            schema = load(CONTRACTS / "coach-context.schema.json")
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            for artifact in (self.context, stated):
                self.assertEqual([], list(validator.iter_errors(artifact)))

    def test_a_training_history_month_may_not_carry_an_activity_id(self):
        """The row shape is the guarantee: a coarse monthly bucket can never be misread
        as recent_actuals's per-session truth (AGENTS.md 3) because there is nowhere on
        it for an activity id to sit."""
        leaked = copy.deepcopy(self.context)
        leaked["training_history"] = {
            "source": "athlete_reported",
            "months": [
                {
                    "month": "2026-06",
                    "sport": "running",
                    "session_count": 1,
                    "total_minutes": 40,
                    "total_km": 8.0,
                    "provenance_counts": {
                        "athlete_reported": 1,
                        "athlete_imported": 0,
                        "prescribed_confirmed": 0,
                    },
                    "activity_id": "i4001",
                }
            ],
            "truncated": False,
            "earliest_observed_month": "2026-06",
            "movement_longevity": [],
            "movement_longevity_truncated": False,
        }

        report = validate_coach_context(leaked)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("activity_id" in error for error in report["errors"]), report["errors"]
        )

    def test_training_history_months_must_not_be_empty_when_the_group_is_present(self):
        """Null already says "nothing long-range reported" -- an empty list would be a
        second spelling of the same fact, and two spellings drift."""
        empty = copy.deepcopy(self.context)
        empty["training_history"] = {
            "source": "athlete_reported",
            "months": [],
            "truncated": False,
            "earliest_observed_month": "2026-06",
            "movement_longevity": [],
            "movement_longevity_truncated": False,
        }

        report = validate_coach_context(empty)

        self.assertEqual("blocked", report["status"])

    def test_daily_change_cannot_increase_weekly_minutes(self):
        after = copy.deepcopy(self.after)
        quality = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        quality["planned_minutes"] = 60
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must not increase planned weekly minutes" in error for error in report["errors"]))

    def test_daily_change_cannot_add_a_hard_session(self):
        after = copy.deepcopy(self.after)
        easy = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-easy-01"
        )
        quality = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        easy["cost"] = "hard"
        easy["hard"] = True
        quality["cost"] = "hard"
        quality["hard"] = True
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must not increase hard-session count" in error for error in report["errors"]))

    def test_daily_change_cannot_modify_a_second_session(self):
        after = copy.deepcopy(self.after)
        easy = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-easy-01"
        )
        easy["purpose"] = "Unrelated daily rewrite"
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("modify only the bound session_id" in error for error in report["errors"]))

    def test_daily_change_cannot_remove_an_unrelated_session(self):
        after = copy.deepcopy(self.after)
        after["week"]["sessions"] = [
            session for session in after["week"]["sessions"]
            if session["session_id"] != "mobility-01"
        ]
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("preserve the exact weekly session_id set" in error for error in report["errors"]))

    def test_daily_change_cannot_change_cycle_goal(self):
        after = copy.deepcopy(self.after)
        after["cycle"]["primary_adaptation"] = "vo2"
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must not change the goal or 28-day cycle" in error for error in report["errors"]))

    def test_stale_activities_allows_normal_daily_decision_with_uncertainty(self):
        # #43 false-positive control: non-fresh optional evidence lowers confidence
        # through warnings and preserved unknowns; it no longer rejects a legitimate
        # daily action on its own.
        context = copy.deepcopy(self.context)
        event = copy.deepcopy(self.event)
        context["freshness"]["activities"] = "stale"
        context["unknowns"] = ["activities_after_2026-08-11"]
        event["unknowns"] = ["activities_after_2026-08-11"]
        report = validate_bundle(context, self.before, self.after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertTrue(any("activities freshness is stale" in warning for warning in report["warnings"]))

    def test_stale_evidence_still_requires_unknowns_to_be_preserved(self):
        # The uncertainty channel is what replaced the freshness gate: an event that
        # drops the context unknowns is still blocked.
        context = copy.deepcopy(self.context)
        event = copy.deepcopy(self.event)
        context["freshness"]["activities"] = "stale"
        context["unknowns"] = ["activities_after_2026-08-11"]
        event["unknowns"] = []
        report = validate_bundle(context, self.before, self.after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must preserve every context unknown" in error for error in report["errors"]))

    def test_every_non_fresh_recovery_grade_allows_normal_daily_and_human_review(self):
        # The intervals source can emit partial/stale/failed; each stays visible as
        # a warning and a preserved unknown, and none of them alone rejects the
        # normal daily decision (#43). The unchanged human_review escalation stays
        # open too -- it is just no longer the only outcome.
        for grade in ("partial", "stale", "failed"):
            with self.subTest(grade=grade):
                context = copy.deepcopy(self.context)
                context["freshness"]["recovery"] = grade
                context["unknowns"] = ["recovery_signals_not_current"]

                normal = copy.deepcopy(self.event)
                normal["unknowns"] = ["recovery_signals_not_current"]
                report = validate_bundle(context, self.before, self.after, normal)
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])
                self.assertTrue(
                    any(f"recovery freshness is {grade}" in warning for warning in report["warnings"])
                )

                review = copy.deepcopy(self.event)
                review.update(
                    {
                        "action": "human_review",
                        "session_id": None,
                        "plan_version_after": self.before["version"],
                        "unknowns": ["recovery_signals_not_current"],
                        "reason_codes": ["data_stale_or_missing"],
                        "change": {
                            "before": "Current plan remains selected",
                            "after": "Current plan remains selected",
                            "summary": "Escalate: recovery inputs are not current enough for a normal decision",
                        },
                    }
                )
                kept_after = copy.deepcopy(self.before)
                report = validate_bundle(context, self.before, kept_after, review)
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    def test_stale_recovery_warns_daily_and_weekly_alike(self):
        # Before #43 the same non-fresh recovery input blocked a daily decision
        # outright while only warning a weekly review, which let evidence quality
        # pick the daily coaching response. Now both surfaces digest imperfect data
        # the same way: a warning plus preserved unknowns, never a refusal.
        context = copy.deepcopy(self.context)
        context["freshness"]["recovery"] = "stale"
        context["unknowns"] = list(context.get("unknowns", [])) + ["recovery_signals_not_current"]

        daily = copy.deepcopy(self.event)
        daily["unknowns"] = list(daily.get("unknowns", [])) + ["recovery_signals_not_current"]
        report = validate_bundle(context, self.before, self.after, daily)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertTrue(any("recovery freshness is stale" in warning for warning in report["warnings"]))

        weekly = copy.deepcopy(self.event)
        weekly["mode"] = "review_week"
        weekly["action"] = "keep"
        weekly["unknowns"] = daily["unknowns"]
        weekly["plan_version_after"] = self.before["version"]
        weekly["reason_codes"] = ["plan_kept_no_material_change"]
        weekly["change"] = {
            "summary": "keep the week as planned",
            "before": "unchanged",
            "after": "unchanged",
        }
        kept_after = copy.deepcopy(self.before)
        report = validate_bundle(context, self.before, kept_after, weekly)
        self.assertEqual("passed", report["status"], report)
        self.assertTrue(any("recovery freshness is stale" in warning for warning in report["warnings"]))

    def test_null_red_flag_allows_low_risk_daily_actions(self):
        # null means unassessed, not present (#43): an unconfirmed flag stays
        # visible as a context warning and a preserved unknown, but no longer
        # demands a blanket all-clear before an ordinary daily decision.
        context = copy.deepcopy(self.context)
        context["constraints"]["red_flags"]["pain"] = None
        context["unknowns"] = ["red_flags.pain"]

        replace = copy.deepcopy(self.event)
        replace["unknowns"] = ["red_flags.pain"]
        report = validate_bundle(context, self.before, self.after, replace)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertTrue(any("pain is not explicitly false" in warning for warning in report["warnings"]))

        keep = copy.deepcopy(self.event)
        keep.update(
            {
                "action": "keep",
                "session_id": None,
                "plan_version_after": self.before["version"],
                "unknowns": ["red_flags.pain"],
                "reason_codes": ["plan_kept_no_material_change"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Keep today's session; pain is unassessed and recorded as unknown",
                },
            }
        )
        kept_after = copy.deepcopy(self.before)
        report = validate_bundle(context, self.before, kept_after, keep)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_unresolved_red_flag_allows_unchanged_human_review(self):
        context = copy.deepcopy(self.context)
        event = copy.deepcopy(self.event)
        after = copy.deepcopy(self.before)
        context["constraints"]["red_flags"]["pain"] = None
        context["unknowns"] = ["red_flags.pain"]
        event.update(
            {
                "action": "human_review",
                "session_id": None,
                "plan_version_after": 1,
                "unknowns": ["red_flags.pain"],
                "reason_codes": ["pain_or_illness_flag"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Pause normal training decision until pain is explicitly clarified",
                },
            }
        )
        report = validate_bundle(context, self.before, after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_explicit_red_flag_still_blocks_normal_training_actions(self):
        # #43 harmful-case regression: an explicit positive symptom is the hard
        # safety boundary. Every normal daily action is rejected; only the existing
        # low-risk rest/human_review paths may carry the day.
        for flag in ("pain", "illness", "chest_pain", "dizziness", "unusual_symptoms"):
            with self.subTest(flag=flag):
                context = copy.deepcopy(self.context)
                context["constraints"]["red_flags"][flag] = True
                report = validate_bundle(context, self.before, self.after, self.event)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(f"explicit red flag ({flag})" in error for error in report["errors"])
                )

    def test_explicit_red_flag_keeps_human_review_open(self):
        context = copy.deepcopy(self.context)
        context["constraints"]["red_flags"]["chest_pain"] = True
        event = copy.deepcopy(self.event)
        event.update(
            {
                "action": "human_review",
                "session_id": None,
                "plan_version_after": self.before["version"],
                "reason_codes": ["pain_or_illness_flag"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Chest pain reported: stop normal training decisions and hand to a human",
                },
            }
        )
        kept_after = copy.deepcopy(self.before)
        report = validate_bundle(context, self.before, kept_after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_red_flag_integer_is_rejected_as_invalid_context(self):
        # 1 == True and 0 == False in Python, so a membership check would accept
        # integers that the daily safety gate's identity test (`is True`) can never
        # see as a symptom. They must be structural errors, not silent non-symptoms.
        for value in (1, 0):
            with self.subTest(value=value):
                context = copy.deepcopy(self.context)
                context["constraints"]["red_flags"]["chest_pain"] = value
                report = validate_coach_context(context)
                self.assertEqual("blocked", report["status"])
                self.assertIn(
                    "context.constraints.red_flags.chest_pain must be true, false, or null",
                    report["errors"],
                )
                bundle = validate_bundle(context, self.before, self.after, self.event)
                self.assertEqual("blocked", bundle["status"])

    def test_partial_recovery_allows_keep_with_preserved_uncertainty(self):
        # #43: partial recovery plus keep passes, with the uncertainty carried in
        # the report warnings and the event unknowns rather than in a refusal.
        context = copy.deepcopy(self.context)
        context["freshness"]["recovery"] = "partial"
        context["unknowns"] = ["recovery_signals_partial_window"]

        event = copy.deepcopy(self.event)
        event.update(
            {
                "action": "keep",
                "session_id": None,
                "plan_version_after": self.before["version"],
                "unknowns": ["recovery_signals_partial_window"],
                "reason_codes": ["plan_kept_no_material_change"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Keep today's plan; recovery data covers only part of the window",
                },
            }
        )
        kept_after = copy.deepcopy(self.before)
        report = validate_bundle(context, self.before, kept_after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertTrue(any("recovery freshness is partial" in warning for warning in report["warnings"]))

    def test_stale_recovery_allows_legitimate_reduce(self):
        # #43: a small load reduction is exactly what a coach may want on imperfect
        # data; what the validator still owns are the mechanical invariants (bound
        # session, no volume increase), and this reduce satisfies them.
        context = copy.deepcopy(self.context)
        context["freshness"]["recovery"] = "stale"
        context["unknowns"] = ["recovery_signals_not_current"]

        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        target = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        target["planned_minutes"] = 40

        event = copy.deepcopy(self.event)
        event.update(
            {
                "action": "reduce",
                "unknowns": ["recovery_signals_not_current"],
                "reason_codes": ["recovery_signal_mixed", "data_stale_or_missing"],
                "change": {
                    "before": "run-quality-01 planned for 50 minutes",
                    "after": "run-quality-01 reduced to 40 minutes",
                    "summary": "Trim the quality session while recovery evidence is stale",
                },
            }
        )
        report = validate_bundle(context, self.before, after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_incomplete_optional_evidence_allows_legitimate_move(self):
        # #43: several optional feeds degraded at once still leave a legitimate
        # move decidable; every gap rides along as a warning and preserved unknown.
        context = copy.deepcopy(self.context)
        context["freshness"]["activities"] = "partial"
        context["freshness"]["recovery"] = "failed"
        context["unknowns"] = ["activities_window_incomplete", "recovery_signals_unreadable"]

        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        target = next(
            session for session in after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        target["scheduled_date"] = "2026-08-14"
        target["match_status"] = "moved"

        event = copy.deepcopy(self.event)
        event.update(
            {
                "action": "move",
                "unknowns": ["activities_window_incomplete", "recovery_signals_unreadable"],
                "reason_codes": ["schedule_or_equipment_changed"],
                "change": {
                    "before": "run-quality-01 scheduled for 2026-08-13",
                    "after": "run-quality-01 moved to 2026-08-14",
                    "summary": "Move the quality session one day back",
                },
            }
        )
        report = validate_bundle(context, self.before, after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertTrue(any("activities freshness is partial" in warning for warning in report["warnings"]))
        self.assertTrue(any("recovery freshness is failed" in warning for warning in report["warnings"]))

    def _additive_low_cost_session(self) -> dict:
        return {
            "session_id": "strength-easy-02",
            "sport": "strength",
            "scheduled_date": "2026-08-15",
            "time_window": None,
            "purpose": "Low-cost bodyweight circuit to keep strength frequency",
            "adaptation": "strength",
            "body_stress": "full",
            "cost": "easy",
            "priority": "optional",
            "planned_minutes": 20,
            "hard": False,
            "plan": {
                "kind": "movement_list",
                "movements": [
                    {"exercise": "bodyweight squat", "display_name": "徒手深蹲", "sets": 3, "reps": 12, "load_kg": None,
                     "assist_kg": None, "load_basis": "bodyweight"},
                    {"exercise": "push-up", "display_name": "伏地挺身", "sets": 3, "reps": 12, "load_kg": None,
                     "assist_kg": None, "load_basis": "bodyweight"},
                ],
            },
            "prescription": "徒手深蹲 3x12 自重\n伏地挺身 3x12 自重",
            "fallback": {"action": "rest", "description": "Skip the circuit and rest"},
            "execution": {
                "publish_supported": False,
                "external_id": None,
                "delivery_state": "not_published",
            },
            "match_status": "planned",
        }

    def test_daily_mode_cannot_add_a_session(self):
        # #43 keeps the capability boundary: revisit_today may never grow the week,
        # no matter how good or bad the evidence looks. A genuinely additive session
        # goes through plan_week/adjust (next test).
        after = copy.deepcopy(self.after)
        after["week"]["sessions"].append(self._additive_low_cost_session())
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("preserve the exact weekly session_id set" in error for error in report["errors"]))

    def test_plan_week_adjust_supports_a_justified_additive_low_cost_session(self):
        # #43: when extra low-cost training genuinely makes sense, the existing
        # weekly path persists it -- no new mode, router, or action is needed.
        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        after["week"]["sessions"].append(self._additive_low_cost_session())
        event = copy.deepcopy(self.event)
        event.update(
            {
                "mode": "plan_week",
                "action": "adjust",
                "session_id": None,
                "reason_codes": ["goal_priority_changed"],
                "change": {
                    "before": "Six planned sessions this week",
                    "after": "Added strength-easy-02, a 20-minute optional bodyweight circuit",
                    "summary": "Persist one justified low-cost additive session through the weekly path",
                },
            }
        )
        report = validate_bundle(self.context, self.before, after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_non_sanitized_context_is_blocked(self):
        context = copy.deepcopy(self.context)
        context["privacy"]["contains_raw_payloads"] = True
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertIn("context.privacy.contains_raw_payloads must be false", report["errors"])

    def test_credentials_gps_and_connection_state_are_forbidden(self):
        for field in ("contains_credentials", "contains_gps_tracks", "contains_connection_state"):
            with self.subTest(field=field):
                context = copy.deepcopy(self.context)
                context["privacy"][field] = True
                report = validate_coach_context(context)
                self.assertEqual("blocked", report["status"])
                self.assertIn(f"context.privacy.{field} must be false", report["errors"])

    def test_failed_doctor_source_cannot_enter_coach_context(self):
        context = copy.deepcopy(self.context)
        context["sources"][0]["doctor_status"] = "failed"
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertIn("context.sources[0].doctor_status must be passed", report["errors"])

    def test_a_rolling_seven_day_span_cannot_pose_as_the_athletes_week(self):
        # The review frame's whole point is that it is the calendar week (issue #89). A
        # span ending at as_of would read as the week the athlete trained and quietly
        # answer a different question.
        context = copy.deepcopy(self.context)
        context["review_frame"]["week_start"] = "2026-08-07"
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertIn("context.review_frame.week_start must be a Monday", report["errors"])

    def test_a_review_cannot_read_an_outcome_yardstick_the_plan_never_declared(self):
        # Judging progress against a protocol other than the one this plan version
        # declared is judging it against nothing the athlete agreed to.
        context = copy.deepcopy(self.context)
        context["goal_context"]["measurement_protocol"] = "whatever the watch says today"
        report = validate_bundle(context, self.before, self.after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("goal_context must exactly project" in error for error in report["errors"])
        )


class BehaviorReplayTests(unittest.TestCase):
    """Two focused product-behavior cases for #43, pinned as deterministic anchors.

    They assert what the validator leaves possible, which is the deterministic half
    of the product expectation. How the Coach words a decision is model judgment,
    but whether a keep/reduce/move can exist at all on imperfect evidence -- and
    whether a normal training action can exist at all on an explicit symptom -- is
    decided here.
    """

    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.context = load(EXAMPLE / "coach-context-day-4.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def _bundles_for_keep_reduce_move(self) -> list[tuple[str, dict, dict]]:
        """Build (action, after, event) triples for the three ordinary daily outcomes."""
        keep_event = copy.deepcopy(self.event)
        keep_event.update(
            {
                "action": "keep",
                "session_id": None,
                "plan_version_after": self.before["version"],
                "reason_codes": ["plan_kept_no_material_change"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Keep today's session and state the data limits honestly",
                },
            }
        )

        reduced_after = copy.deepcopy(self.before)
        reduced_after["version"] = self.before["version"] + 1
        next(
            session for session in reduced_after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )["planned_minutes"] = 40
        reduce_event = copy.deepcopy(self.event)
        reduce_event.update(
            {
                "action": "reduce",
                "reason_codes": ["recovery_signal_mixed", "data_stale_or_missing"],
                "change": {
                    "before": "run-quality-01 planned for 50 minutes",
                    "after": "run-quality-01 reduced to 40 minutes",
                    "summary": "Small load trim under partial recovery evidence",
                },
            }
        )

        moved_after = copy.deepcopy(self.before)
        moved_after["version"] = self.before["version"] + 1
        moved_target = next(
            session for session in moved_after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        moved_target["scheduled_date"] = "2026-08-14"
        moved_target["match_status"] = "moved"
        move_event = copy.deepcopy(self.event)
        move_event.update(
            {
                "action": "move",
                "reason_codes": ["schedule_or_equipment_changed"],
                "change": {
                    "before": "run-quality-01 scheduled for 2026-08-13",
                    "after": "run-quality-01 moved to 2026-08-14",
                    "summary": "Move the quality session one day back",
                },
            }
        )

        return [
            ("keep", copy.deepcopy(self.before), keep_event),
            ("reduce", reduced_after, reduce_event),
            ("move", moved_after, move_event),
        ]

    def test_false_positive_control_incomplete_evidence_keeps_coaching_open(self):
        # Scenario: recovery is partial and no red flag has been assessed at all --
        # the ordinary morning where the athlete just asks what to do today.
        # Expected product behavior: the Coach may answer keep, reduce, or move
        # while honestly describing the data limits; rest/human_review must not be
        # the only survivors. Before #43 every one of these bundles was blocked.
        context = copy.deepcopy(self.context)
        context["freshness"]["recovery"] = "partial"
        for field in context["constraints"]["red_flags"]:
            context["constraints"]["red_flags"][field] = None
        context["unknowns"] = ["red_flags_not_confirmed", "recovery_signals_not_current"]

        for action, after, event in self._bundles_for_keep_reduce_move():
            with self.subTest(action=action):
                event["unknowns"] = list(context["unknowns"])
                report = validate_bundle(context, self.before, after, event)
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])
                self.assertTrue(
                    any("recovery freshness is partial" in warning for warning in report["warnings"])
                )
                self.assertTrue(
                    any("not explicitly false" in warning for warning in report["warnings"])
                )

    def test_harmful_case_explicit_symptom_forces_low_risk_paths_only(self):
        # Scenario: chest pain is explicitly reported while the athlete still asks
        # for the planned hard quality session. Expected product behavior: no
        # normal training action can be produced -- keeping the hard day included --
        # and the decision falls to the existing low-risk human path. The validator
        # does not diagnose; it only refuses to let a normal prescription through.
        context = copy.deepcopy(self.context)
        context["constraints"]["red_flags"]["chest_pain"] = True

        for action, after, event in self._bundles_for_keep_reduce_move():
            with self.subTest(action=action):
                report = validate_bundle(context, self.before, after, event)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any("explicit red flag (chest_pain)" in error for error in report["errors"])
                )

        review = copy.deepcopy(self.event)
        review.update(
            {
                "action": "human_review",
                "session_id": None,
                "plan_version_after": self.before["version"],
                "reason_codes": ["pain_or_illness_flag"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Chest pain reported: no training prescription, hand to a human decision",
                },
            }
        )
        report = validate_bundle(context, self.before, copy.deepcopy(self.before), review)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

        # The other low-risk path stays open too: converting the bound session to a
        # rest day is a changed action, and the explicit flag must not block it.
        rest_after = copy.deepcopy(self.before)
        rest_after["version"] = self.before["version"] + 1
        rest_target = next(
            session for session in rest_after["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        rest_target.update(
            {
                "sport": "rest",
                "adaptation": "recovery",
                "cost": "easy",
                "hard": False,
                "planned_minutes": 0,
            }
        )
        unstructured(rest_target)
        rest_event = copy.deepcopy(self.event)
        rest_event.update(
            {
                "action": "rest",
                "reason_codes": ["pain_or_illness_flag"],
                "change": {
                    "before": "run-quality-01 planned as a hard quality run",
                    "after": "run-quality-01 converted to a rest day",
                    "summary": "Explicit symptom: drop today's quality session entirely",
                },
            }
        )
        report = validate_bundle(context, self.before, rest_after, rest_event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])


WELL_FORMED_STRENGTH_EXECUTION: dict = {
    "source": "personal-os:strength_log",
    "window_start": "2026-07-02",
    "window_end": "2026-08-12",
    "sessions": [
        {
            "date": "2026-08-11",
            "exercise": "bench_press",
            "category": "chest",
            "sets": [
                {"set": 1, "weight_kg": 65.0, "assist_kg": None, "reps": 5, "rpe": None},
                {"set": 2, "weight_kg": 65.0, "assist_kg": None, "reps": 5, "rpe": None},
            ],
            "notes": ["做不完五組65kg，第五組 60kg 5下"],
            "source": "personal-os:strength_log",
        }
    ],
}


class RecentActualsShapeValidationTests(unittest.TestCase):
    """The one-directional shape rule issue #240 §1 added to validate_coach_context.

    The invariant: a measurement absent from a *full* row is absent from the context,
    so the full field set stays required wherever the builder could not have reduced
    -- a source builder quietly dropping a field must fail the build, not read as
    unknown on every later turn (the harmful case). The reduced shape is exempt
    exactly where the builder reduces: a settled attachment whose cycle_sessions
    record carries the reading (the false-positive control).
    """

    def setUp(self):
        self.context = load(EXAMPLE / "coach-context-day-4.json")

    def _one_full_row(self) -> dict:
        return next(
            actual
            for actual in self.context["recent_actuals"]
            if "adaptation" in actual
        )

    def test_a_full_row_missing_a_measurement_field_is_blocked(self):
        # An unmatched row is never reducible, so its full shape is what the rule
        # holds: this is the harmful case, a source builder quietly dropping a field.
        row = self._one_full_row()
        row["match_confidence"] = "unmatched"
        row["planned_session_id"] = None
        del row["adaptation"]
        report = validate_coach_context(self.context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("adaptation is required" in e for e in report["errors"]), report["errors"])

    def test_a_reduced_row_on_a_settled_attachment_passes(self):
        row = self._one_full_row()
        attached = {
            "session_id": "fixture-elapsed-01",
            "date": row["date"],
            "week_start": "2026-08-10",
            "sport": row["sport"],
            "cost": "moderate",
            "match_status": "planned",
            "planned_minutes": 45,
            "prescription": "fixture",
            "activity": {
                "activity_id": row["activity_id"],
                "match_confidence": "owned",
                "duration_minutes": row["duration_minutes"],
                "distance_km": row.get("distance_km"),
                "average_pace_sec_per_km": row.get("average_pace_sec_per_km"),
                "average_hr": row.get("average_hr"),
            },
            "activity_evidence": "attached",
        }
        self.context["cycle_sessions"] = list(self.context.get("cycle_sessions") or [])
        self.context["cycle_sessions"].append(attached)
        row["match_confidence"] = "owned"
        row["planned_session_id"] = "fixture-elapsed-01"
        for name in ("adaptation", "body_stress", "cost", "elevation_gain_m",
                     "subjective_feel", "distance_km", "average_pace_sec_per_km",
                     "average_hr", "session_label"):
            row.pop(name, None)
        report = validate_coach_context(self.context)
        self.assertEqual("passed", report["status"], report)

    def test_a_key_outside_both_shapes_is_still_rejected(self):
        row = self._one_full_row()
        row["adaptaton"] = "threshold"
        report = validate_coach_context(self.context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("adaptaton" in e for e in report["errors"]), report["errors"])

    def test_the_schema_and_the_validator_name_the_same_reconciliation_identity(self):
        schema = load(CONTRACTS / "coach-context.schema.json")
        self.assertEqual(
            set(RECONCILIATION_ACTUAL_REQUIRED_FIELDS),
            set(schema["$defs"]["actual"]["required"]),
            "contracts/coach-context.schema.json's actual.required drifted from "
            "validation.RECONCILIATION_ACTUAL_REQUIRED_FIELDS",
        )


class StrengthExecutionValidationTests(unittest.TestCase):
    """validate_coach_context's shape checks for the standalone strength_execution
    evidence group (issue #37): null is always valid (unconfigured), a configured
    group has exact keys throughout, and -- the boundary this feature must not
    cross -- no deterministic rule compares it against athlete_baseline.
    strength_loads. Uses the same anonymous fixture as CoachLoopV1Tests.
    """

    def setUp(self):
        self.context = load(EXAMPLE / "coach-context-day-4.json")
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.after = load(EXAMPLE / "plan-state-v2-day-4.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def test_null_strength_execution_passes(self):
        self.assertIsNone(self.context["strength_execution"])
        report = validate_coach_context(self.context)
        self.assertEqual("passed", report["status"], report)

    def test_well_formed_strength_execution_group_passes(self):
        context = copy.deepcopy(self.context)
        context["strength_execution"] = copy.deepcopy(WELL_FORMED_STRENGTH_EXECUTION)
        report = validate_coach_context(context)
        self.assertEqual("passed", report["status"], report)

    def test_session_with_extra_key_fails(self):
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_STRENGTH_EXECUTION)
        group["sessions"][0]["unexpected"] = "nope"
        context["strength_execution"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("is not allowed" in e for e in report["errors"]))

    def test_session_with_missing_key_fails(self):
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_STRENGTH_EXECUTION)
        del group["sessions"][0]["category"]
        context["strength_execution"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("category is required" in e for e in report["errors"]))

    def test_non_list_notes_fails(self):
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_STRENGTH_EXECUTION)
        group["sessions"][0]["notes"] = "not a list"
        context["strength_execution"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("notes must be an array" in e for e in report["errors"]))

    def test_context_missing_strength_execution_key_entirely_fails(self):
        context = copy.deepcopy(self.context)
        del context["strength_execution"]
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertIn("context.strength_execution is required", report["errors"])

    def test_no_deterministic_rule_compares_baseline_against_strength_execution(self):
        """Boundary control (issue #3 direction, explicitly out of scope for #37):
        athlete_baseline.strength_loads says 62.5kg for bench press; strength_execution
        shows a set actually completed at 40kg for the same exercise on the same day
        -- a large, deliberately implausible gap. Whether that gap means the baseline
        is stale is coaching judgment, not a deterministic rule: validate_bundle must
        let the mismatch through untouched, the same way it already lets any other
        unverified-but-plausible prescription through. If this test ever starts
        failing because someone added a comparison rule, that rule is the regression,
        not this test.
        """
        before = copy.deepcopy(self.before)
        after = copy.deepcopy(self.after)
        bench_baseline = {
            "exercise": "bench press", "load_kg": 62.5, "assist_kg": None, "scheme": "5x5",
        }
        before["athlete_baseline"]["strength_loads"].append(copy.deepcopy(bench_baseline))
        after["athlete_baseline"]["strength_loads"].append(copy.deepcopy(bench_baseline))

        context = project_context(load(EXAMPLE / "coach-context-day-4.json"), before)
        context["strength_execution"] = {
            "source": "personal-os:strength_log",
            "window_start": "2026-07-02",
            "window_end": "2026-08-12",
            "sessions": [
                {
                    "date": "2026-08-11",
                    "exercise": "bench_press",
                    "category": "chest",
                    "sets": [{"set": 1, "weight_kg": 40.0, "assist_kg": None, "reps": 5, "rpe": None}],
                    "notes": ["far below the 62.5kg baseline on purpose"],
                    "source": "personal-os:strength_log",
                }
            ],
        }

        report = validate_bundle(context, before, after, self.event)
        self.assertEqual("passed", report["status"], report)


WELL_FORMED_RECOVERY_SIGNALS: dict = {
    "source": "personal-os:recovery_daily+daily_metrics",
    "window_start": "2026-08-02",
    "window_end": "2026-08-08",
    "days": [
        {
            "date": "2026-08-08",
            "readiness_score": 56.0,
            "readiness_level": "MODERATE",
            "hrv_status": "NONE",
            "hrv_7d_avg_ms": 80.0,
            "acute_load": 409.0,
            "recovery_time_sec": 682.0,
            "body_battery_high": 100.0,
            "body_battery_low": 55.0,
            "avg_stress": 18.0,
        }
    ],
}


class RecoverySignalsValidationTests(unittest.TestCase):
    """validate_coach_context's shape checks for the standalone recovery_signals
    evidence group (issue #37 slice 2): null is always valid (unconfigured), a
    configured group has exact keys throughout, and -- the boundary this feature must
    not cross -- no deterministic rule reacts to any reading in it. Uses the same
    anonymous fixture as CoachLoopV1Tests.
    """

    def setUp(self):
        self.context = load(EXAMPLE / "coach-context-day-4.json")
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.after = load(EXAMPLE / "plan-state-v2-day-4.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def test_null_recovery_signals_passes(self):
        self.assertIsNone(self.context["recovery_signals"])
        report = validate_coach_context(self.context)
        self.assertEqual("passed", report["status"], report)

    def test_well_formed_recovery_signals_group_passes(self):
        context = copy.deepcopy(self.context)
        context["recovery_signals"] = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        report = validate_coach_context(context)
        self.assertEqual("passed", report["status"], report)

    def test_day_with_extra_key_fails(self):
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        group["days"][0]["unexpected"] = "nope"
        context["recovery_signals"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("is not allowed" in e for e in report["errors"]))

    def test_day_missing_a_reading_reads_as_unknown_rather_than_failing(self):
        """Issue #187: an absent reading and an explicit null say the same thing.

        Demanding the null made a client holding one number write nine more to say it
        held nothing, which is the opposite of what omission means on every other field
        of this product. Nothing downstream distinguishes the two, so the validator
        stopped distinguishing them either.
        """
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        del group["days"][0]["acute_load"]
        context["recovery_signals"] = group
        report = validate_coach_context(context)
        self.assertEqual("passed", report["status"], report)

    def test_day_without_a_date_still_fails(self):
        """The one key that is still required: a reading nobody can place is not one."""
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        del group["days"][0]["date"]
        context["recovery_signals"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("date is required" in e for e in report["errors"]))

    def test_the_five_later_readings_validate_beside_the_original_nine(self):
        """sleep, resting HR and a single night's HRV, which no wearable brand owns."""
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        group["days"][0].update(
            {
                "sleep_score": 78.0,
                "sleep_duration_sec": 25200.0,
                "sleep_history_score": 64.0,
                "hrv_last_night_ms": 69.0,
                "resting_hr_bpm": 47.0,
            }
        )
        context["recovery_signals"] = group
        self.assertEqual("passed", validate_coach_context(context)["status"])

        group["days"][0]["resting_hr_bpm"] = "forty-seven"
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("resting_hr_bpm must be a number or null" in e for e in report["errors"])
        )

    def test_day_with_bad_date_fails(self):
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        group["days"][0]["date"] = "not-a-date"
        context["recovery_signals"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("date must be an ISO date" in e for e in report["errors"]))

    def test_context_missing_recovery_signals_key_entirely_fails(self):
        context = copy.deepcopy(self.context)
        del context["recovery_signals"]
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertIn("context.recovery_signals is required", report["errors"])

    def test_no_deterministic_rule_reacts_to_any_recovery_signals_reading(self):
        """Boundary control (issue #3 direction; the #39 test arm this guards):
        readiness_score 56 and body_battery_low 55 sit in the group on the same day
        as an adopted hard session, deliberately not paired with any plan adjustment.
        Whether that reading should have changed the plan is coaching judgment, not a
        deterministic rule: validate_bundle must let it through untouched -- no
        threshold, no readiness rule. A readiness rule was already tried here once
        and withdrawn (plan v16 -> v17), because over-reacting to one day's number was
        itself the failure mode. If this test starts failing because someone added a
        threshold/readiness rule, that rule is the regression, not this test.
        """
        for source in (
            "personal-os:recovery_daily+daily_metrics",
            "client-uploaded:personal-os:recovery_daily+daily_metrics",
        ):
            with self.subTest(source=source):
                context = copy.deepcopy(self.context)
                context["recovery_signals"] = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
                context["recovery_signals"]["source"] = source

                report = validate_bundle(context, self.before, self.after, self.event)
                self.assertEqual("passed", report["status"], report)


class AthleteBaselineConsistencyTests(unittest.TestCase):
    """Covers the dogfood gap: validate-bundle used to pass a prescription that no
    athlete_baseline could support (5x1000m @5:50/km against a 6:10/km threshold, with
    only a 90-second jog recovery). These checks read every threshold from
    athlete_baseline and must never fall back to a hard-coded number, and a null
    baseline field must skip its check (recorded as unknown) rather than pass or fail
    by assumption.
    """

    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.context = project_context(load(EXAMPLE / "coach-context-day-4.json"), self.before)
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def _keep_event(self) -> dict:
        # A "keep" decision requires after == before and leaves plan_version unchanged,
        # which keeps these tests focused purely on the new athlete_baseline checks
        # instead of the pre-existing daily no-upshift bookkeeping.
        event = copy.deepcopy(self.event)
        event.update(
            {
                "action": "keep",
                "session_id": None,
                "plan_version_after": 1,
                "reason_codes": ["plan_kept_no_material_change"],
                "change": {
                    "before": "Current plan remains selected",
                    "after": "Current plan remains selected",
                    "summary": "Keep the selected session and weekly plan",
                },
            }
        )
        return event

    def _session(self, plan: dict, session_id: str) -> dict:
        return next(s for s in plan["week"]["sessions"] if s["session_id"] == session_id)

    def _validate(self, context: dict, plan: dict) -> dict:
        return validate_bundle(project_context(context, plan), plan, plan, self._keep_event())

    def _pace_workout(self, low: int, high: int | None = None) -> dict:
        return {
            "kind": "time_axis",
            "name": "5x1000m",
            "steps": [{
                "kind": "repeat", "repetitions": 5,
                "steps": [{
                    "kind": "work", "name": "Interval",
                    "duration": {"kind": "distance", "meters": 1000},
                    "target": {
                        "kind": "pace", "unit": "sec_per_km",
                        "low_seconds_per_km": low,
                        "high_seconds_per_km": high if high is not None else low,
                    },
                }],
            }],
        }

    def _hr_ceiling_workout(self, ceiling_bpm: int = 145) -> dict:
        return {
            "kind": "time_axis",
            "name": "Easy run",
            "steps": [{
                "kind": "work", "name": "Easy run",
                "duration": {"kind": "time", "seconds": 3000},
                "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": ceiling_bpm},
            }],
        }

    # -- an intensity target vs. the anchor it claims -----------------------------

    def test_pace_faster_than_threshold_is_coaching_judgement_not_a_hard_cap(self):
        # How far a repetition sits from threshold is the coach's call. What the gate
        # owns is only whether the number stands on a measurement at all.
        for low, high in ((350, 350), (355, 355)):
            with self.subTest(low=low):
                plan = copy.deepcopy(self.before)
                session = self._session(plan, "run-quality-01")
                session["plan"] = self._pace_workout(low, high)
                rerendered(session)
                report = self._validate(self.context, plan)
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    def test_a_pace_target_without_a_measured_anchor_is_blocked(self):
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        session = self._session(plan, "run-quality-01")
        session["plan"] = self._pace_workout(350)
        rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("threshold_pace_sec_per_km is not measured" in error for error in report["errors"]),
            report["errors"],
        )

    def test_an_open_target_needs_no_anchor_at_all(self):
        # The false-positive control, and the escape the block above points at: a run
        # left to the athlete states its duration and prescribes no intensity.
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"].update({"threshold_pace_sec_per_km": None, "max_hr": None})
        for session in plan["week"]["sessions"]:
            if session["plan"]["kind"] == "time_axis":
                session["plan"] = {
                    "kind": "time_axis",
                    "name": "Open run",
                    "steps": [{
                        "kind": "work", "name": "Run",
                        "duration": {"kind": "time", "seconds": 3000},
                        "target": {"kind": "open"},
                    }],
                }
                rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_a_target_the_schema_cannot_express_cannot_be_prescribed_at_all(self):
        # %HR and a heart-rate floor each had their own free-text pattern and their own
        # anchor rule. Neither exists in the structured vocabulary, so both are refused
        # by being unrepresentable rather than by being recognised and rejected.
        for target in (
            {"kind": "hr_percent", "unit": "percent_max_hr", "value": 85},
            {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 150, "floor_bpm": 140},
        ):
            with self.subTest(target=target["kind"]):
                plan = copy.deepcopy(self.before)
                session = self._session(plan, "run-quality-01")
                session["plan"] = self._hr_ceiling_workout()
                session["plan"]["steps"][0]["target"] = target
                rerendered(session)
                report = self._validate(self.context, plan)
                self.assertEqual("blocked", report["status"], report)

    def test_a_recorded_load_needs_a_matching_measured_exercise_anchor(self):
        # 62.5 kg against a 60 kg anchor passes: how far a session may progress past the
        # last measurement is the coach's judgment. An exercise the athlete never
        # measured does not, and the error names the movement, because that is where the
        # fix is -- measure it, or say the load is bodyweight or still to be confirmed.
        for exercise, load_kg, expected in (
            ("bench press", 60.0, "passed"),
            ("bench press", 62.5, "passed"),
            ("unknown lift", 999.0, "blocked"),
        ):
            with self.subTest(exercise=exercise, load_kg=load_kg):
                before = copy.deepcopy(self.before)
                before["athlete_baseline"]["strength_loads"].append({
                    "exercise": "bench press", "load_kg": 60.0,
                    "assist_kg": None, "scheme": "5x5",
                })
                context = project_context(self.context, before)
                after = copy.deepcopy(before)
                after["version"] += 1
                target = self._session(after, "strength-upper-01")
                target["plan"] = {
                    "kind": "movement_list",
                    "movements": [{
                        "exercise": exercise, "display_name": "臥推",
                        "sets": 3, "reps": 8,
                        "load_kg": load_kg, "assist_kg": None,
                        "load_basis": "measured_baseline",
                    }],
                }
                rerendered(target)
                target["match_status"] = "replaced"
                event = copy.deepcopy(self.event)
                event["session_id"] = "strength-upper-01"

                report = validate_bundle(context, before, after, event)

                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(
                        any("without a matching established strength baseline" in error
                            and repr(exercise) in error
                            for error in report["errors"]),
                        report["errors"],
                    )

    # -- structured hr_ceiling vs. max_hr and easy_hr_ceiling ---------------------

    def test_structured_hr_ceiling_within_measured_max_hr_passes(self):
        plan = copy.deepcopy(self.before)
        session = self._session(plan, "run-easy-01")
        session["plan"] = self._hr_ceiling_workout(140)
        rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_structured_hr_ceiling_above_max_hr_is_blocked(self):
        plan = copy.deepcopy(self.before)
        session = self._session(plan, "run-easy-01")
        session["plan"] = self._hr_ceiling_workout(181)
        rerendered(session)
        plan["athlete_baseline"]["max_hr"] = 180
        report = self._validate(self.context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("exceeds athlete_baseline.max_hr" in error for error in report["errors"]),
            report["errors"],
        )

    def test_structured_hr_ceiling_without_any_anchor_is_blocked(self):
        # The watch obeys the ceiling either way, so a number with no measurement
        # behind it is invented precision the device enforces.
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"].update({"max_hr": None, "easy_hr_ceiling": None})
        session = self._session(plan, "run-easy-01")
        session["plan"] = self._hr_ceiling_workout(140)
        rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "without a measured athlete_baseline.max_hr or a stated "
                "easy_hr_ceiling anchor" in error
                for error in report["errors"]
            ),
            report["errors"],
        )

    def test_structured_hr_ceiling_within_stated_easy_hr_ceiling_passes(self):
        """The false-positive control: an athlete-stated ceiling anchors the target too."""
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["max_hr"] = None
        # athlete_baseline.easy_hr_ceiling stays at the fixture default, 150.
        session = self._session(plan, "run-easy-01")
        session["plan"] = self._hr_ceiling_workout(150)
        rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_structured_hr_ceiling_above_stated_easy_hr_ceiling_is_blocked(self):
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["max_hr"] = None
        # athlete_baseline.easy_hr_ceiling stays at the fixture default, 150.
        session = self._session(plan, "run-easy-01")
        session["plan"] = self._hr_ceiling_workout(151)
        rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "exceeds athlete_baseline.easy_hr_ceiling" in error
                for error in report["errors"]
            ),
            report["errors"],
        )

    def test_structured_hr_ceiling_uses_the_larger_of_both_anchors(self):
        # max_hr 185 governs over easy_hr_ceiling 145; a ceiling above the smaller
        # anchor but at or below the larger one still passes.
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"].update({"max_hr": 185, "easy_hr_ceiling": 145})
        session = self._session(plan, "run-easy-01")
        session["plan"] = self._hr_ceiling_workout(160)
        rerendered(session)
        report = self._validate(self.context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_session_minutes_exceeds_max_is_blocked(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-long-01")["planned_minutes"] = 90
        report = self._validate(self.context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "run-long-01 planned_minutes 90" in error
                and "exceeds athlete_baseline max_session_minutes 75" in error
                for error in report["errors"]
            )
        )

    def test_strength_session_is_not_bound_by_the_running_duration_ceiling(self):
        # The ceiling is how long the athlete will run, not a cap on training in
        # general. A 70-minute lift is normal for an athlete who lifts most days;
        # blocking it would force the plan to understate training that already happens.
        plan = copy.deepcopy(self.before)
        strength = next(s for s in plan["week"]["sessions"] if s["sport"] == "strength")
        strength["planned_minutes"] = 90
        report = self._validate(self.context, plan)
        self.assertEqual("passed", report["status"])
        self.assertFalse(
            any("max_session_minutes" in error for error in report["errors"])
        )

    def test_session_minutes_check_skipped_when_baseline_is_null(self):
        context = copy.deepcopy(self.context)
        context["athlete_baseline"]["max_session_minutes"] = None
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["max_session_minutes"] = None
        self._session(plan, "run-long-01")["planned_minutes"] = 90
        report = self._validate(context, plan)
        self.assertEqual("passed", report["status"])
        self.assertTrue(
            any(
                "unknown: athlete_baseline.max_session_minutes is not set" in warning
                for warning in report["warnings"]
            )
        )

class SessionPlanTests(unittest.TestCase):
    """One structure per session, classified by execution model (#93).

    Every defect this repository filed about strength or running had one shape: the
    Coach decided something, wrote it into a sentence, and deterministic code re-derived
    it out of that sentence. `session.plan` ends that by making the structure the only
    statement of the session, and by rendering the sentence from it.

    What these tests hold is the half that is easy to lose while deleting a layer: the
    evidence boundaries still block what they always blocked, now read from the record.
    """

    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.context = project_context(load(EXAMPLE / "coach-context-day-4.json"), self.before)
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def _adopt(self, session_id: str, plan: dict | None, **kwargs) -> dict:
        """Adopt one session's new plan through the daily replace path."""
        before = copy.deepcopy(self.before)
        before["athlete_baseline"]["strength_loads"].extend(
            copy.deepcopy(list(kwargs.get("strength_baselines", ())))
        )
        if kwargs.get("baseline"):
            before["athlete_baseline"].update(copy.deepcopy(kwargs["baseline"]))
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(s for s in after["week"]["sessions"] if s["session_id"] == session_id)
        target["plan"] = copy.deepcopy(plan) if plan is not None else {"kind": "unstructured"}
        if kwargs.get("prescription") is None:
            rerendered(target)
        else:
            target["prescription"] = kwargs["prescription"]
        target["match_status"] = "replaced"
        event = copy.deepcopy(self.event)
        event["session_id"] = session_id
        return validate_bundle(context, before, after, event)

    # -- kind decides which validation runs, sport does not ------------------------

    def test_each_kind_is_validated_as_the_model_it_declares(self):
        for plan, expected_error in (
            ({"kind": "sequence"}, "kind must be one of"),
            ({"kind": "time_axis", "movements": []}, "name is required"),
            ({"kind": "movement_list", "steps": []}, "movements is required"),
            ({"kind": "unstructured", "movements": []}, "movements is not allowed"),
        ):
            with self.subTest(plan=plan):
                report = self._adopt("strength-upper-01", plan, prescription="anything")
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(expected_error in error for error in report["errors"]), report["errors"]
                )

    def test_a_new_sport_reusing_an_existing_model_needs_no_validator_change(self):
        """Adding a sport is one `sport` enum value and nothing else.

        The point of classifying by execution model rather than by sport: a paddle
        session is a sequence with an intensity target a device follows, which is
        exactly a run. This patches only the sport vocabulary -- `validation` itself is
        untouched -- and the session validates, is held to the same pace anchor, and
        needs no `kind` of its own. Swimming proved this once from exactly this test,
        then joined the real vocabulary; the sport here stays imaginary so the test
        keeps proving the principle rather than the vocabulary.
        """
        paddle = {
            "kind": "time_axis",
            "name": "Paddle 1500",
            "steps": [{
                "kind": "work", "name": "Paddle",
                "duration": {"kind": "distance", "meters": 1500},
                "target": {"kind": "open"},
            }],
        }
        before = copy.deepcopy(self.before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(s for s in after["week"]["sessions"] if s["session_id"] == "run-easy-01")
        target["sport"] = "paddling"
        target["plan"] = paddle
        rerendered(target)
        target["match_status"] = "replaced"
        event = copy.deepcopy(self.event)
        event["session_id"] = "run-easy-01"

        with mock.patch.object(validation, "SPORTS", validation.SPORTS | {"paddling"}):
            context = project_context(self.context, before)
            report = validate_bundle(context, before, after, event)

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    # -- prescription is generated, never authored --------------------------------

    def test_two_sessions_with_the_same_structure_read_identically(self):
        plan = {
            "kind": "movement_list",
            "movements": [{
                "exercise": "back_squat", "display_name": "深蹲", "sets": 5, "reps": 5, "load_kg": None,
                "assist_kg": None, "load_basis": "pending_confirmation",
            }],
        }
        self.assertEqual(
            render_prescription(copy.deepcopy(plan)), render_prescription(copy.deepcopy(plan))
        )
        report = self._adopt("strength-upper-01", plan)
        self.assertEqual([], report["errors"])

    def test_an_authored_prescription_cannot_reach_the_store(self):
        # The #38 incident's shape: a sentence that says something its structure does
        # not. There is no wording that survives, because the only value the field may
        # hold is the one the renderer produces.
        for authored in ("5x1000m @4:00/km", "Zone 2 有氧跑 50 分鐘", "深蹲 5x5 100 公斤"):
            with self.subTest(authored=authored):
                report = self._adopt(
                    "strength-upper-01",
                    {
                        "kind": "movement_list",
                        "movements": [{
                            "exercise": "back_squat", "display_name": "深蹲", "sets": 5, "reps": 5, "load_kg": None,
                            "assist_kg": None, "load_basis": "bodyweight",
                        }],
                    },
                    prescription=authored,
                )
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any("is generated from" in error for error in report["errors"]),
                    report["errors"],
                )

    def test_the_same_plan_reads_in_the_athletes_own_language(self):
        # Structure in, sentence out -- the numbers are the same numbers, and the
        # movement keeps the name the athlete gave it either way.
        plan = {
            "kind": "movement_list",
            "movements": [
                {
                    "exercise": "back_squat", "display_name": "深蹲", "sets": 5, "reps": 5,
                    "load_kg": 60.0, "assist_kg": None, "load_basis": "measured_baseline",
                },
                {
                    "exercise": "pull_up", "display_name": "引體向上", "sets": 3, "reps": None,
                    "load_kg": None, "assist_kg": None, "load_basis": "bodyweight",
                },
            ],
        }

        self.assertEqual(
            "深蹲 5x5 60公斤\n引體向上 3組力竭 自重", render_prescription(plan)
        )
        self.assertEqual(
            "深蹲 5x5 60 kg\n引體向上 3 sets to failure bodyweight",
            render_prescription(plan, "en"),
        )

    def test_a_time_axis_reads_in_the_athletes_own_language_too(self):
        plan = {
            "kind": "time_axis",
            "name": "間歇",
            "steps": [
                {
                    "kind": "work", "name": "熱身",
                    "duration": {"kind": "time", "seconds": 600},
                    "target": {"kind": "open"},
                },
                {
                    "kind": "repeat", "repetitions": 4,
                    "steps": [
                        {
                            "kind": "work", "name": "快跑",
                            "duration": {"kind": "distance", "meters": 400},
                            "target": {
                                "kind": "pace", "unit": "sec_per_km",
                                "low_seconds_per_km": 300, "high_seconds_per_km": 310,
                            },
                        },
                        {
                            "kind": "work", "name": "慢跑",
                            "duration": {"kind": "time", "seconds": 90},
                            "target": {"kind": "open"},
                        },
                    ],
                },
            ],
        }

        self.assertEqual(
            "熱身 10分\n4趟：快跑 400公尺 配速 5:00-5:10/km、慢跑 1分30秒",
            render_prescription(plan),
        )
        self.assertEqual(
            "熱身 10 min\n4 rounds: 快跑 400 m pace 5:00-5:10/km, 慢跑 1 min 30 s",
            render_prescription(plan, "en"),
        )

    def test_a_plan_written_in_either_language_still_validates(self):
        """Language is the athlete's to change, so it cannot be what opens their store.

        The validator's job is that the sentence is a rendering rather than something
        somebody wrote. Pinning it to one language would mean an athlete who switched
        could no longer open the history they trained under -- every commit failing at
        once over a cosmetic fact.
        """
        plan = {
            "kind": "movement_list",
            "movements": [{
                "exercise": "back_squat", "display_name": "深蹲", "sets": 5, "reps": 5,
                "load_kg": None, "assist_kg": None, "load_basis": "bodyweight",
            }],
        }

        for language in ("zh-Hant", "en"):
            with self.subTest(language=language):
                report = self._adopt(
                    "strength-upper-01",
                    plan,
                    prescription=render_prescription(plan, language),
                )
                self.assertEqual([], report["errors"])

    def test_a_language_the_renderer_does_not_speak_still_produces_a_valid_sentence(self):
        # A rendering is an output on a path that already accepted the plan, so an
        # unknown language falls back rather than refusing a session over its wording.
        plan = {"kind": "unstructured"}
        self.assertEqual(
            render_prescription(plan), render_prescription(plan, "klingon")
        )

    def test_an_unstructured_session_declares_no_numbers_and_can_hide_none(self):
        # Mobility, recovery and rest validate while declaring nothing -- and there is
        # nowhere in the shape for a load or a pace to ride along: the plan holds no
        # field but its own kind, and the sentence is rendered from that.
        before = copy.deepcopy(self.before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(s for s in after["week"]["sessions"] if s["session_id"] == "mobility-01")
        target["plan"] = {"kind": "unstructured"}
        target["match_status"] = "replaced"
        rerendered(target)
        event = copy.deepcopy(self.event)
        event["session_id"] = "mobility-01"
        report = validate_bundle(project_context(self.context, before), before, after, event)
        self.assertEqual([], report["errors"])
        self.assertEqual("不設定量化目標", target["prescription"])

    # -- the evidence boundaries, read from the record ----------------------------

    def test_a_run_that_declares_nothing_to_execute_is_blocked(self):
        report = self._adopt("run-quality-01", None)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("prescribes nothing to do" in error for error in report["errors"]),
            report["errors"],
        )

    def test_strength_that_declares_nothing_to_execute_adopts_with_a_warning(self):
        # The athlete's decision (2026-08-14): a strength session may decline
        # quantification. The cost is named, not silent -- and not a refusal.
        report = self._adopt("strength-upper-01", None)
        self.assertEqual("passed", report["status"], report)
        self.assertTrue(
            any(
                "strength-upper-01 declares no quantified structure" in warning
                for warning in report["warnings"]
            ),
            report["warnings"],
        )

    def test_the_canonical_key_never_reaches_the_athlete(self):
        """`exercise` matches, `display_name` is read (issue #93 review).

        The two names are two jobs. `exercise` is the key the evidence gate compares
        field to field against the baseline, and it is written the way a baseline writes
        it -- `back_squat`. That string used to be what the renderer printed, which put
        it on the athlete's first screen and, for a strength session, on the calendar
        entry the watch shows. Everything the athlete reads is Traditional Chinese, and
        an internal identifier is not a name.
        """
        plan = {
            "kind": "movement_list",
            "movements": [{
                "exercise": "back_squat", "display_name": "深蹲", "sets": 5, "reps": 5,
                "load_kg": 60.0, "assist_kg": None, "load_basis": "measured_baseline",
            }],
        }
        rendered = render_prescription(plan)

        self.assertEqual("深蹲 5x5 60公斤", rendered)
        self.assertNotIn("back_squat", rendered)
        # And the two are independent: the key still anchors, whatever the name says.
        squat = {"exercise": "back_squat", "load_kg": 60.0, "assist_kg": None, "scheme": "5x5"}
        report = self._adopt("strength-upper-01", plan, strength_baselines=(squat,))
        self.assertEqual([], report["errors"])

    def test_a_movement_that_names_itself_only_for_matching_is_blocked(self):
        # Required, not optional: the only fallback an absent display_name could have is
        # `exercise`, which is the leak. A field whose default is the defect is not
        # optional, and there is no stored history to accommodate.
        report = self._adopt(
            "strength-upper-01",
            {
                "kind": "movement_list",
                "movements": [{
                    "exercise": "back_squat", "sets": 5, "reps": 5, "load_kg": None,
                    "assist_kg": None, "load_basis": "bodyweight",
                }],
            },
            prescription="anything",
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("display_name is required" in error for error in report["errors"]),
            report["errors"],
        )

    def test_a_movement_binds_to_its_baseline_by_canonical_key_or_display_name(self):
        squat = {
            "exercise": "back_squat", "display_name": "深蹲",
            "load_kg": 60.0, "assist_kg": None, "scheme": "5x5",
        }
        for exercise in ("back_squat", "back squat", "深蹲"):
            with self.subTest(exercise=exercise):
                report = self._adopt(
                    "strength-upper-01",
                    {
                        "kind": "movement_list",
                        "movements": [{
                            "exercise": exercise, "display_name": "深蹲",
                            "sets": 5, "reps": 5, "load_kg": 60.0,
                            "assist_kg": None, "load_basis": "measured_baseline",
                        }],
                    },
                    strength_baselines=(squat,),
                )
                self.assertEqual([], report["errors"])

    def test_an_assisted_movement_binds_to_the_assist_figure_its_baseline_measured(self):
        # Which column holds the measurement is a property of the lift: an assisted
        # pull-up records assist_kg and leaves load_kg empty, and reading both is what
        # lets the gate accept it without inferring assistance from a word.
        pull_up = {
            "exercise": "pull_up_assisted", "display_name": "引體向上",
            "load_kg": None, "assist_kg": 24.0, "scheme": "5x5",
        }
        report = self._adopt(
            "strength-upper-01",
            {
                "kind": "movement_list",
                "movements": [{
                    "exercise": "引體向上", "display_name": "引體向上", "sets": 5, "reps": 5, "load_kg": None,
                    "assist_kg": 22.0, "load_basis": "measured_baseline",
                }],
            },
            strength_baselines=(pull_up,),
        )
        self.assertEqual([], report["errors"])

    def test_a_baseline_entry_that_measured_nothing_anchors_nothing(self):
        unmeasured = {
            "exercise": "overhead_press", "display_name": "肩推",
            "load_kg": None, "assist_kg": None, "scheme": "5x5",
        }
        report = self._adopt(
            "strength-upper-01",
            {
                "kind": "movement_list",
                "movements": [{
                    "exercise": "肩推", "display_name": "肩推", "sets": 5, "reps": 5, "load_kg": 40.0,
                    "assist_kg": None, "load_basis": "measured_baseline",
                }],
            },
            strength_baselines=(unmeasured,),
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("without a matching established strength baseline" in error
                for error in report["errors"]),
            report["errors"],
        )

    def test_a_loadless_basis_needs_no_anchor_at_all(self):
        # The escape the block points at, and the reason the field has three values: an
        # unmeasured athlete can still be given a bodyweight movement or a lift whose
        # load is explicitly still to be confirmed.
        for load_basis in ("bodyweight", "pending_confirmation"):
            with self.subTest(load_basis=load_basis):
                report = self._adopt(
                    "strength-upper-01",
                    {
                        "kind": "movement_list",
                        "movements": [{
                            "exercise": "nothing anyone measured", "display_name": "沒人量過的動作", "sets": 3, "reps": None,
                            "load_kg": None, "assist_kg": None, "load_basis": load_basis,
                        }],
                    },
                )
                self.assertEqual([], report["errors"])

    def test_each_movement_is_anchored_on_its_own(self):
        # #49 in its structural form: one anchored movement beside an unanchored one
        # used to depend on which comma was typed between them. Field to field, the
        # second movement is simply its own record and its own verdict.
        squat = {
            "exercise": "back_squat", "display_name": "深蹲",
            "load_kg": 60.0, "assist_kg": None, "scheme": "5x5",
        }
        report = self._adopt(
            "strength-upper-01",
            {
                "kind": "movement_list",
                "movements": [
                    {"exercise": "back_squat", "display_name": "深蹲", "sets": 5, "reps": 5, "load_kg": 60.0,
                     "assist_kg": None, "load_basis": "measured_baseline"},
                    {"exercise": "bench_press", "display_name": "臥推", "sets": 4, "reps": 8, "load_kg": 50.0,
                     "assist_kg": None, "load_basis": "measured_baseline"},
                ],
            },
            strength_baselines=(squat,),
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("plan.movements[1] 'bench_press'" in error for error in report["errors"]),
            report["errors"],
        )
        self.assertFalse(
            any("movements[0]" in error for error in report["errors"]), report["errors"]
        )

    def test_load_basis_must_agree_with_the_load_it_carries(self):
        # A movement that declares bodyweight and carries 60 kg contradicts itself inside
        # one object, and the evidence gate downstream would have to pick which half to
        # believe -- reinstating the guess the field exists to remove.
        for overrides in (
            {"load_basis": "measured_baseline"},
            {"load_kg": 60.0, "load_basis": "bodyweight"},
            {"assist_kg": 10.0, "load_basis": "pending_confirmation"},
            {"load_basis": "rpe"},
            {"sets": 0},
            {"reps": 0},
            {"exercise": " "},
        ):
            with self.subTest(overrides=overrides):
                movement = {
                    "exercise": "back_squat", "display_name": "深蹲",
                    "sets": 5, "reps": 5, "load_kg": None,
                    "assist_kg": None, "load_basis": "pending_confirmation",
                }
                movement.update(overrides)
                report = self._adopt(
                    "strength-upper-01",
                    {"kind": "movement_list", "movements": [movement]},
                    prescription="anything",
                )
                self.assertEqual("blocked", report["status"], report)

    def test_an_empty_or_malformed_movement_list_is_blocked(self):
        for movements in (
            [],
            "深蹲 5x5",
            [{"exercise": "back_squat"}],
            # Named for matching but not for reading: the athlete would be shown the
            # canonical key, which is the one thing display_name exists to prevent.
            [{"exercise": "back_squat", "sets": 5, "reps": 5, "load_kg": None,
              "assist_kg": None, "load_basis": "bodyweight"}],
        ):
            with self.subTest(movements=movements):
                report = self._adopt(
                    "strength-upper-01",
                    {"kind": "movement_list", "movements": movements},
                    prescription="anything",
                )
                self.assertEqual("blocked", report["status"])

    def test_the_stored_example_plan_matches_the_public_json_schema(self):
        if Draft202012Validator is None:
            self.skipTest("jsonschema is not installed")
        schema = load(CONTRACTS / "plan-state.schema.json")
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for name in ("plan-state-v1.json", "plan-state-v2-day-4.json"):
            with self.subTest(name=name):
                self.assertEqual([], sorted(
                    error.message for error in validator.iter_errors(load(EXAMPLE / name))
                ))
        kinds = {
            session["plan"]["kind"]
            for session in load(EXAMPLE / "plan-state-v1.json")["week"]["sessions"]
        }
        self.assertEqual({"time_axis", "movement_list", "unstructured"}, kinds)


class DecisionProvenanceTests(unittest.TestCase):
    """A decision records who authored it and what earlier decision it supersedes."""

    def setUp(self):
        self.plan = load(EXAMPLE / "plan-state-v1.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def test_event_without_provenance_still_validates(self):
        self.assertEqual("passed", validate_decision_event(self.event)["status"])

    def test_authored_by_and_supersedes_validate(self):
        event = copy.deepcopy(self.event)
        event["authored_by"] = {"model": "claude-opus-5", "skill_version": "gcl@0.3.0"}
        event["initiative"] = "reactive"
        event["supersedes"] = {
            "event_id": "evt-earlier",
            "kind": "policy_changed",
            "reason": "the skill's pace derivation changed",
        }
        self.assertEqual("passed", validate_decision_event(event)["status"])

    def test_supersede_kind_is_a_closed_vocabulary(self):
        # "corrected" vs "new_evidence" vs "policy_changed" is the whole point; free
        # text would let the distinction rot back into an untyped note.
        event = copy.deepcopy(self.event)
        event["supersedes"] = {"event_id": "evt-earlier", "kind": "whatever", "reason": "x"}
        report = validate_decision_event(event)
        self.assertEqual("blocked", report["status"])

    def test_event_cannot_supersede_itself(self):
        event = copy.deepcopy(self.event)
        event["supersedes"] = {
            "event_id": event["event_id"], "kind": "corrected", "reason": "loop",
        }
        report = validate_decision_event(event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must not be the event itself" in e for e in report["errors"]))


class ExplicitSymptomBoundaryTests(unittest.TestCase):
    """The symptom boundary triggered by evidence rather than by a declared mode (#84).

    The rule it replaces read ``event.mode == "revisit_today"``. Since the gateway began
    deriving mode from what actually moved (#71, #83), that mode cannot occur on the
    hosted path, so the athlete could say "今天胸口有點悶" and every following week
    change went through untouched. These cases pin both halves: the plan changes an
    explicit symptom must stop, and the ones it must leave alone.

    The example week is the fixture throughout. ``as_of`` is 2026-08-13, and
    ``run-quality-01`` -- a hard 50-minute session -- is scheduled for that day.
    """

    def setUp(self):
        self.plan = load(EXAMPLE / "plan-state-v1.json")
        self.raw_context = load(EXAMPLE / "coach-context-day-4.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")
        self.today = "2026-08-13"

    # -- fixture helpers ---------------------------------------------------------------

    def _context(self, plan: dict, *, flags: dict[str, object] | None = None) -> dict:
        context = project_context(self.raw_context, plan)
        for field, value in (flags or {}).items():
            context["constraints"]["red_flags"][field] = value
        return context

    def _session(self, plan: dict, session_id: str) -> dict:
        return next(
            session
            for session in plan["week"]["sessions"]
            if session["session_id"] == session_id
        )

    def _rested(self, plan: dict, session_id: str) -> dict:
        """Turn one session into the rest day a symptom asks for."""
        session = self._session(plan, session_id)
        session.update(
            {
                "sport": "rest",
                "adaptation": "recovery",
                "cost": "easy",
                "hard": False,
                "planned_minutes": 0,
                "match_status": "replaced",
            }
        )
        # A rest day declares the model that prescribes nothing, and reads as that
        # (issue #93). Popping the old structure would leave the session with no `plan`
        # at all, which is a malformed artifact rather than a rest day.
        unstructured(session)
        return plan

    def _week_event(self, before: dict, after: dict, **overrides) -> dict:
        event = copy.deepcopy(self.event)
        changed = json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True)
        event.update(
            {
                "mode": "review_week",
                "action": "adjust" if changed else "keep",
                "session_id": None,
                "plan_version_before": before["version"],
                "plan_version_after": after["version"],
                "reason_codes": (
                    ["schedule_or_equipment_changed"]
                    if changed
                    else ["plan_kept_no_material_change"]
                ),
            }
        )
        event.update(overrides)
        return event

    def _blocking_errors(self, report: dict) -> list[str]:
        return [error for error in report["errors"] if "explicit red flag" in error]

    # -- harmful-case regressions ------------------------------------------------------

    def test_a_week_change_that_still_trains_today_is_blocked_under_a_symptom(self):
        """The exact scenario from #84, in the mode the hosted path actually produces.

        The change even lowers the week's load -- today's hard 50 minutes become an easy
        30 -- and it is still refused, because trimming what today asks for is not the
        same as not asking for it. Before this rule read the context, nothing stopped it.
        """
        after = copy.deepcopy(self.plan)
        after["version"] = self.plan["version"] + 1
        session = self._session(after, "run-quality-01")
        session.update(
            {
                "cost": "easy",
                "hard": False,
                "planned_minutes": 30,
                "adaptation": "aerobic_base",
                "plan": {
                    "kind": "time_axis",
                    "name": "30 分鐘輕鬆跑",
                    "steps": [{
                        "kind": "work", "name": "輕鬆跑",
                        "duration": {"kind": "time", "seconds": 1800},
                        "target": {"kind": "open"},
                    }],
                },
                "match_status": "replaced",
            }
        )
        rerendered(session)
        context = self._context(self.plan, flags={"chest_pain": True})

        report = validate_bundle(context, self.plan, after, self._week_event(self.plan, after))

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                f"explicit red flag (chest_pain) limits {self.today}" in error
                and "run-quality-01 running" in error
                for error in report["errors"]
            ),
            report["errors"],
        )

    def test_every_flag_blocks_a_week_change_that_leaves_today_training(self):
        for flag in ("pain", "illness", "chest_pain", "dizziness", "unusual_symptoms"):
            with self.subTest(flag=flag):
                after = copy.deepcopy(self.plan)
                after["version"] = self.plan["version"] + 1
                self._session(after, "run-long-01")["planned_minutes"] = 45
                context = self._context(self.plan, flags={flag: True})

                report = validate_bundle(
                    context, self.plan, after, self._week_event(self.plan, after)
                )

                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(f"explicit red flag ({flag})" in error for error in report["errors"]),
                    report["errors"],
                )

    def test_a_cycle_review_is_bound_by_the_same_evidence_as_a_week_review(self):
        """Mode is not the trigger: renaming the change does not unlock today."""
        after = copy.deepcopy(self.plan)
        after["version"] = self.plan["version"] + 1
        after["goal"]["outcome"] = "改以 10K 為主要目標"
        context = self._context(self.plan, flags={"illness": True})
        event = self._week_event(
            self.plan, after, mode="review_cycle", action="pivot",
            reason_codes=["goal_priority_changed"],
        )

        report = validate_bundle(context, self.plan, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("run-quality-01 running" in error for error in self._blocking_errors(report)),
            report["errors"],
        )

    def test_keeping_the_week_untouched_is_not_an_answer_to_a_symptom(self):
        """"Carry on as planned" is refused for the same reason ``keep`` always was."""
        context = self._context(self.plan, flags={"dizziness": True})
        kept = copy.deepcopy(self.plan)

        report = validate_bundle(context, self.plan, kept, self._week_event(self.plan, kept))

        self.assertEqual("blocked", report["status"])
        self.assertTrue(self._blocking_errors(report), report["errors"])

    def test_moving_another_session_onto_today_is_blocked(self):
        """``after`` is what gets read, so a session moved onto today counts as today."""
        after = self._rested(copy.deepcopy(self.plan), "run-quality-01")
        after["version"] = self.plan["version"] + 1
        moved = self._session(after, "strength-upper-01")
        moved.update({"scheduled_date": self.today, "match_status": "moved"})
        context = self._context(self.plan, flags={"pain": True})

        report = validate_bundle(context, self.plan, after, self._week_event(self.plan, after))

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("strength-upper-01 strength" in error for error in self._blocking_errors(report)),
            report["errors"],
        )

    def test_added_volume_is_blocked_even_when_today_is_already_rest(self):
        before = self._rested(copy.deepcopy(self.plan), "run-quality-01")
        after = copy.deepcopy(before)
        after["version"] = before["version"] + 1
        self._session(after, "run-long-01")["planned_minutes"] = 80
        context = self._context(before, flags={"illness": True})

        report = validate_bundle(context, before, after, self._week_event(before, after))

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "forbids adding volume" in error and "215 -> 240" in error
                for error in report["errors"]
            ),
            report["errors"],
        )

    def test_an_added_hard_session_is_blocked_even_when_today_is_already_rest(self):
        before = self._rested(copy.deepcopy(self.plan), "run-quality-01")
        after = copy.deepcopy(before)
        after["version"] = before["version"] + 1
        promoted = self._session(after, "run-long-01")
        promoted.update({"cost": "hard", "hard": True, "match_status": "replaced"})
        context = self._context(before, flags={"unusual_symptoms": True})

        report = validate_bundle(context, before, after, self._week_event(before, after))

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("forbids adding hard sessions: 0 -> 1" in error for error in report["errors"]),
            report["errors"],
        )

    # -- false-positive controls -------------------------------------------------------

    def test_a_symptomatic_week_that_reduces_load_and_rests_today_passes(self):
        """The control the issue names, and the answer the rule steers toward.

        A week carrying an explicit symptom that genuinely reduces load must still be
        adoptable: today becomes rest, Saturday's long run is trimmed, and the athlete
        gets the plan they asked for rather than a refusal.
        """
        after = self._rested(copy.deepcopy(self.plan), "run-quality-01")
        after["version"] = self.plan["version"] + 1
        self._session(after, "run-long-01")["planned_minutes"] = 40
        context = self._context(self.plan, flags={"chest_pain": True})

        report = validate_bundle(context, self.plan, after, self._week_event(self.plan, after))

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_a_hard_session_later_this_week_is_not_todays_decision(self):
        """The rule ends at today. Thursday gets its own conversation and its own flags."""
        before = self._rested(copy.deepcopy(self.plan), "run-quality-01")
        after = copy.deepcopy(before)
        after["version"] = before["version"] + 1
        self._session(after, "run-long-01")["planned_minutes"] = 50
        context = self._context(before, flags={"pain": True})
        self.assertTrue(
            any(
                session["scheduled_date"] > self.today and session["sport"] == "running"
                for session in after["week"]["sessions"]
            )
        )

        report = validate_bundle(context, before, after, self._week_event(before, after))

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_moving_a_rest_day_under_a_symptom_passes(self):
        before = self._rested(copy.deepcopy(self.plan), "run-quality-01")
        after = copy.deepcopy(before)
        after["version"] = before["version"] + 1
        # rest-01 is Saturday's rest day and run-long-01 is Sunday's long run. Swapping
        # the two days moves no work onto today and adds nothing to the week.
        self._session(after, "rest-01").update(
            {"scheduled_date": "2026-08-16", "match_status": "moved"}
        )
        self._session(after, "run-long-01").update(
            {"scheduled_date": "2026-08-15", "match_status": "moved"}
        )
        context = self._context(before, flags={"unusual_symptoms": True})

        report = validate_bundle(context, before, after, self._week_event(before, after))

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_a_session_today_that_already_happened_does_not_block_the_evening(self):
        """Completed is not "asked for": the plan is recording it, not prescribing it."""
        before = copy.deepcopy(self.plan)
        self._session(before, "run-quality-01")["match_status"] = "completed"
        after = copy.deepcopy(before)
        after["version"] = before["version"] + 1
        self._session(after, "run-long-01")["planned_minutes"] = 40
        context = self._context(before, flags={"chest_pain": True})

        report = validate_bundle(context, before, after, self._week_event(before, after))

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_human_review_that_changes_nothing_stays_open_at_week_level(self):
        context = self._context(self.plan, flags={"chest_pain": True})
        kept = copy.deepcopy(self.plan)
        event = self._week_event(
            self.plan, kept, action="human_review", reason_codes=["pain_or_illness_flag"],
        )

        report = validate_bundle(context, self.plan, kept, event)

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_unassessed_flags_neither_trigger_the_boundary_nor_prove_safety(self):
        """AGENTS.md 3, both directions.

        Null and unknown leave the plan exactly as free as it was: the week that adds a
        hard session is adoptable, and it would be blocked the moment a flag says True.
        """
        after = copy.deepcopy(self.plan)
        after["version"] = self.plan["version"] + 1
        promoted = self._session(after, "run-long-01")
        promoted.update({"cost": "hard", "hard": True, "match_status": "replaced"})
        unassessed = self._context(
            self.plan,
            flags={
                field: None
                for field in ("pain", "illness", "chest_pain", "dizziness", "unusual_symptoms")
            },
        )

        report = validate_bundle(
            unassessed, self.plan, after, self._week_event(self.plan, after)
        )

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])
        self.assertTrue(
            any("not explicitly false" in warning for warning in report["warnings"]),
            report["warnings"],
        )

        symptomatic = self._context(self.plan, flags={"chest_pain": True})
        blocked = validate_bundle(
            symptomatic, self.plan, after, self._week_event(self.plan, after)
        )
        self.assertEqual("blocked", blocked["status"])

    def test_the_daily_rule_keeps_its_own_vocabulary(self):
        """#43's single-session rule is unchanged where its vocabulary exists.

        Emptying today by moving its session to another day satisfies the plan-shaped
        rule, and a daily decision still may not do it: under an explicit symptom the
        only daily answers are rest and human_review.
        """
        after = copy.deepcopy(self.plan)
        after["version"] = self.plan["version"] + 1
        self._session(after, "run-quality-01").update(
            {"scheduled_date": "2026-08-15", "match_status": "moved"}
        )
        context = self._context(self.plan, flags={"chest_pain": True})
        event = copy.deepcopy(self.event)
        event.update({"action": "move", "reason_codes": ["pain_or_illness_flag"]})

        report = validate_bundle(context, self.plan, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "limits today to rest or human_review" in error
                for error in report["errors"]
            ),
            report["errors"],
        )


class MaterialChangeTests(unittest.TestCase):
    """A revision has to earn itself: wording nobody delivers is not a training decision."""

    def setUp(self):
        self.context = load(EXAMPLE / "coach-context-day-4.json")
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def _bound_session(self, plan):
        return next(
            s for s in plan["week"]["sessions"] if s["session_id"] == self.event["session_id"]
        )

    def test_fallback_wording_only_change_is_blocked(self):
        # fallback names no delivered artifact -- contrast purpose below, which does.
        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        self._bound_session(after)["fallback"]["description"] = "Reworded fallback, same fallback"
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("nothing material moved" in error for error in report["errors"])
        )

    def test_purpose_reword_counts_as_material(self):
        """Purpose reaches the athlete as delivered content --
        delivery_content.delivery_session_content already holds it, because a
        movement_list session reaches Intervals as a calendar entry whose name is built
        from purpose. A coach rewording an unclear title is a deliverable change, not
        decoration, so it must not be refused as immaterial."""
        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        self._bound_session(after)["purpose"] = "Reworded, same training"
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertFalse(
            any("nothing material moved" in error for error in report["errors"])
        )

    def test_a_ten_second_pace_change_counts_as_material(self):
        # Small in magnitude, large in consequence -- the check is by field, not size.
        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        session = self._bound_session(after)
        target = session["plan"]["steps"][1]["steps"][0]["target"]
        target["low_seconds_per_km"] = target["high_seconds_per_km"] = 370
        rerendered(session)
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertFalse(
            any("nothing material moved" in error for error in report["errors"])
        )

    def test_keep_is_never_asked_to_justify_itself(self):
        event = copy.deepcopy(self.event)
        event.update({
            "action": "keep",
            "plan_version_after": self.before["version"],
            "reason_codes": ["plan_kept_no_material_change"],
        })
        report = validate_bundle(self.context, self.before, self.before, event)
        self.assertFalse(
            any("nothing material moved" in error for error in report["errors"])
        )


class IntentLineMayNotPrescribeTests(unittest.TestCase):
    """`purpose` says what a session is for; the numbers live in `plan` (issue #99).

    Issue #93 made `prescription` a rendering, which closed that field to an authored
    number. `purpose` stayed the Coach's own words, so issue #38's incident stayed
    reachable through it: a `5x1000m @5:50/km` with no measured anchor sat in this field
    for two days and reached the athlete, because the field nothing parses is also the
    field nothing checks.

    The two halves AGENTS.md 6 asks for are the two tests below. The harmful case is a
    pace that never met a baseline; the false-positive control is the reason a blanket
    "no digits" rule was not viable -- an intent line is allowed to count things.
    """

    def setUp(self):
        self.plan = load(EXAMPLE / "plan-state-v1.json")

    def _with_purpose(self, purpose: str) -> dict:
        plan = copy.deepcopy(self.plan)
        plan["week"]["sessions"][0]["purpose"] = purpose
        return plan

    def test_a_pace_no_baseline_vouches_for_cannot_hide_in_the_intent_line(self):
        # Issue #38's own token, in the field it sat in.
        report = validate_plan_state(self._with_purpose("5x1000m @5:50/km 維持節奏"))

        self.assertEqual("blocked", report["status"])
        offending = [error for error in report["errors"] if ".purpose" in error]
        self.assertEqual(1, len(offending), report["errors"])
        # Actionable means naming the token and where it belongs, not "invalid purpose".
        self.assertIn("'1000m'", offending[0])
        self.assertIn(".plan", offending[0])

    def test_every_shape_of_prescription_is_refused_by_the_token_it_carries(self):
        for purpose, token in (
            ("門檻 4:30/km", "4:30"),
            # No space anywhere: the athlete's titles are all Chinese, so the token has to
            # be caught where CJK runs straight into the unit as well as beside a space.
            ("800m重複跑", "800m"),
            ("配速 4 分 30 秒/公里", "/公里"),
            ("深蹲加到 80kg", "80kg"),
            ("臥推 62.5 公斤", "62.5 公斤"),
            ("心率壓在 150bpm 以下", "150bpm"),
            ("強度 85% 為主", "85%"),
            ("長跑拉到 20km", "20km"),
        ):
            with self.subTest(purpose=purpose):
                report = validate_plan_state(self._with_purpose(purpose))
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(f"{token!r}" in error for error in report["errors"]),
                    report["errors"],
                )

    def test_an_intent_line_may_still_count_things(self):
        """The control: a digit alone is intent, and blocking it would empty the field.

        These are the lines issue #99 named when it ruled out a blanket "no digits in
        purpose" rule, plus the every-stored-purpose check below -- nothing this
        repository has written into the field is refused, so the mechanism costs no
        existing workflow anything.
        """
        for purpose in (
            "維持 Zone 2 有氧基礎",
            "本週第 3 次長跑",
            "第 2 組開始加重",
            "8 週目標的第 1 週",
            "上拉為主的上肢課",
            # A minute count is not refused: no baseline anchors one, so there is nothing
            # to smuggle past, and `planned_minutes` already carries the real figure.
            "跑 30 分鐘就好",
            "30 minutes easy",
        ):
            with self.subTest(purpose=purpose):
                self.assertEqual("passed", validate_plan_state(self._with_purpose(purpose))["status"])

    def test_no_purpose_this_repository_already_stores_is_refused(self):
        for session in self.plan["week"]["sessions"]:
            with self.subTest(session=session["session_id"]):
                self.assertIsNone(prescribed_token_in_intent(session))

    def test_a_session_with_no_intent_line_is_the_shape_check_s_error_alone(self):
        # Absent or non-string purpose is `_nonempty`'s to report; this check stays quiet
        # rather than adding a second error about a value that is not there.
        self.assertIsNone(prescribed_token_in_intent({"purpose": None}))
        self.assertIsNone(prescribed_token_in_intent({}))
        self.assertIsNone(prescribed_token_in_intent("not a session"))


class CoachNoteMayNotPrescribeTests(unittest.TestCase):
    """A session note is what the coach wants to say; the numbers live in `plan` (#56).

    Issue #56 opened the only free-text channel that travels with a delivery, and named
    the risk itself: the reason authored prose was deleted in the first place is that
    numbers hide in sentences and reach the athlete with nothing anchoring them. So the
    note is held to `purpose`'s rule by running `purpose`'s own check -- one pattern, in
    `intent_text`, with two callers -- rather than by a second rule that could drift.

    The two halves AGENTS.md 6 asks for are below. The harmful case is a pace or a
    distance smuggled into a sentence; the false-positive control is that ordinary
    coaching language, including the sentences issue #56 was written to make possible,
    still passes.
    """

    def setUp(self):
        self.plan = load(EXAMPLE / "plan-state-v1.json")

    def _with_note(self, note: str) -> dict:
        plan = copy.deepcopy(self.plan)
        plan["week"]["sessions"][0]["coach_note"] = note
        return plan

    def test_a_number_wearing_a_unit_cannot_hide_in_a_sentence(self):
        report = validate_plan_state(self._with_note("今天輕鬆跑就好，配速壓在 5:30/km"))

        self.assertEqual("blocked", report["status"])
        offending = [error for error in report["errors"] if ".coach_note" in error]
        self.assertEqual(1, len(offending), report["errors"])
        self.assertIn("'5:30'", offending[0])
        self.assertIn(".plan", offending[0])

    def test_every_shape_the_intent_line_refuses_is_refused_here_too(self):
        """One pattern, two fields: the two cannot disagree about what a prescription is."""
        for note, token in (
            ("最後一段配速抓 4:30/km", "4:30"),
            ("熱身完直接接 800m重複跑", "800m"),
            ("配速維持在 4 分 30 秒/公里", "/公里"),
            ("深蹲今天可以試 80kg", "80kg"),
            ("臥推那組加到 62.5 公斤", "62.5 公斤"),
            ("心率不要超過 150bpm", "150bpm"),
            ("強度大概 85% 就好", "85%"),
            ("長跑不要超過 20km", "20km"),
            # Issue #56's own third example sentence. The issue's design section rules
            # that a unit-bearing number is refused, and the example it wrote earlier
            # carries one -- the ruling wins, and this is the wording it costs. The note
            # is written without the figure and the 2 km lives in `plan`.
            ("最後 2 公里維持住配速", "2 公里"),
        ):
            with self.subTest(note=note):
                report = validate_plan_state(self._with_note(note))
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(f"{token!r}" in error for error in report["errors"]),
                    report["errors"],
                )

    def test_the_sentences_this_field_exists_for_are_not_refused(self):
        """The false-positive control (AGENTS.md 6): a rule that ate these bought nothing.

        Two of issue #56's three example sentences are here verbatim, plus the shapes a
        coach actually writes. A note is allowed to count things and to name a Chinese
        duration -- neither carries a baseline anchor to smuggle past.
        """
        for note in (
            "這週的長跑故意排短，是為了下週的測試——不要自己加量",
            "臥推最後一組如果覺得肩膀不穩就停，不用硬做完",
            "前面寧可慢，最後撐住就好",
            "本週第 3 次長跑，照平常的感覺跑",
            "第 2 組開始加重，感覺不對就退回去",
            "跑 30 分鐘就好，不要勉強",
            "Keep it relaxed and stop if the knee complains",
        ):
            with self.subTest(note=note):
                self.assertEqual(
                    "passed", validate_plan_state(self._with_note(note))["status"]
                )

    def test_a_session_with_no_note_is_exactly_the_plan_it_always_was(self):
        """The field is optional, and its absence changes nothing about validation."""
        for session in self.plan["week"]["sessions"]:
            self.assertNotIn("coach_note", session)
        self.assertEqual("passed", validate_plan_state(self.plan)["status"])
        self.assertIsNone(prescribed_token_in_coach_note({}))
        self.assertIsNone(prescribed_token_in_coach_note({"coach_note": None}))
        self.assertIsNone(prescribed_token_in_coach_note("not a session"))

    def test_an_empty_note_is_refused_as_a_shape_rather_than_stored(self):
        report = validate_plan_state(self._with_note("   "))

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(".coach_note" in error for error in report["errors"]), report["errors"]
        )

    @unittest.skipIf(Draft202012Validator is None, "jsonschema dev dependency is unavailable")
    def test_the_public_schema_agrees_with_the_validator_about_the_field(self):
        schema = load(CONTRACTS / "plan-state.schema.json")
        validator = Draft202012Validator(schema, format_checker=FormatChecker())

        self.assertEqual([], list(validator.iter_errors(self.plan)))
        self.assertEqual(
            [], list(validator.iter_errors(self._with_note("這週故意排短，不要自己加量")))
        )
        self.assertTrue(list(validator.iter_errors(self._with_note(""))))


class FreeTextLayerCannotGrowBackTests(unittest.TestCase):
    """The layer that read prose is gone, and this is what keeps it gone (issue #93).

    Five separate repairs -- #47, #49, #52, #62 and #79 -- each added to a set of eleven
    regular expressions that re-derived the plan's own numbers out of the sentence
    reporting them, and none of them removed it. A comment saying "do not read prose
    here" is exactly what the repository already had while that happened, so the guard
    is a test rather than a convention: it walks the validator's own syntax tree and
    fails on any expression that reads the *value* of `prescription` or `purpose`.

    Three uses stay lawful, and they are the three that read no prose:

    - asking whether the field is a non-empty string (`_nonempty`), which is shape;
    - asking whether it is present at all (`is not None`), which is also shape;
    - comparing a stored prescription against `render_prescription`'s output, which is
      the opposite of parsing -- it confirms the sentence is still a rendering.

    Naming either field as a required key, in a field path, or in an error message is
    untouched: the validator still has to say which field it is talking about.

    Two modules hold a read this guard would refuse, and both are pinned here rather than
    left as somewhere quieter to put one: `delivery_content` copies `purpose` into the
    delivered-content projection, and `intent_text` refuses an intent line that carries a
    prescription (issue #99). Each is allowed exactly one shape and nothing else, and the
    validator reaches both by handing over the whole session -- which is why the rule has
    to be written down twice more instead of once here.
    """

    # `coach_note` joins the set the day it exists (issue #56): it is the second authored
    # field, it reaches the athlete verbatim, and a guard that watched one of the two
    # would simply have named the field the deleted layer grows back into.
    FREE_TEXT_FIELDS = {"prescription", "purpose", "coach_note"}
    #: Reads that ask about shape rather than content.
    SHAPE_CALLS = {"_nonempty"}
    #: The one function whose output a stored prescription may be compared against.
    RENDERER = "render_prescription"

    def setUp(self):
        self.source = (ROOT / "garmin_coach_loop" / "validation.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.parents = {
            child: parent
            for parent in ast.walk(self.tree)
            for child in ast.iter_child_nodes(parent)
        }

    def _reads(self) -> list[ast.AST]:
        """Every expression that evaluates to a free-text field's value."""
        found = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                name = node.slice.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop", "setdefault"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                name = node.args[0].value
            else:
                continue
            if name in self.FREE_TEXT_FIELDS:
                found.append(node)
        return found

    def _ancestors(self, node: ast.AST):
        while node in self.parents:
            node = self.parents[node]
            yield node

    def _is_renderer_call(self, value: ast.AST) -> bool:
        """One call to the renderer, or a collection built entirely out of such calls.

        The collection form is what "compare against every rendering" looks like: one plan
        renders into as many sentences as there are languages, and a stored prescription
        is lawful when it is any of them. That is still only the renderer's output -- the
        guard is about where the comparison value came from, not how many there are.
        """
        if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            value = value.elt
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == self.RENDERER
        )

    def _rendered_names(self, node: ast.AST) -> set[str]:
        """Names bound from the renderer inside the function this read sits in."""
        function = next(
            (
                ancestor
                for ancestor in self._ancestors(node)
                if isinstance(ancestor, ast.FunctionDef)
            ),
            None,
        )
        if function is None:
            return set()
        return {
            target.id
            for statement in ast.walk(function)
            if isinstance(statement, ast.Assign)
            and self._is_renderer_call(statement.value)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }

    def _is_lawful(self, node: ast.AST) -> bool:
        rendered = self._rendered_names(node)
        for ancestor in self._ancestors(node):
            if (
                isinstance(ancestor, ast.Call)
                and isinstance(ancestor.func, ast.Name)
                and ancestor.func.id in self.SHAPE_CALLS | {self.RENDERER}
            ):
                return True
            if isinstance(ancestor, ast.Compare):
                others = [
                    other
                    for other in [ancestor.left, *ancestor.comparators]
                    if other is not node
                ]
                if all(
                    (isinstance(other, ast.Constant) and other.value is None)
                    or (isinstance(other, ast.Name) and other.id in rendered)
                    for other in others
                ):
                    return True
            if isinstance(ancestor, ast.FunctionDef):
                break
        return False

    def _iterated_reads(self) -> list[ast.AST]:
        """Free-text names listed in a collection that is looped over to index a session.

        `_reads` matches the field name where it is written: inside the subscript or the
        `.get()` call. Loop over a tuple of field names and the read becomes
        `session.get(field)`, whose argument is a variable -- so the expression that
        evaluates to the prose is invisible, and the name sits one indent up in the tuple.
        That is not an exotic shape. It is how any projection over several fields is
        written, which is exactly why the guard has to see it.

        Two things this deliberately does not do. It does not ask what the loop body is
        for, so a loop that only shape-checks both fields is refused as well and has to be
        written out -- fail closed, and the cost is one unrolled loop. And it reads literal
        collections only: bind the field list to a name first and the guard is blind again.
        Following that would take dataflow, and a guard whose limit is written down is
        worth more than one whose reach is assumed.
        """
        return [
            element
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.comprehension, ast.For))
            and isinstance(node.iter, (ast.Tuple, ast.List, ast.Set))
            for element in node.iter.elts
            if isinstance(element, ast.Constant) and element.value in self.FREE_TEXT_FIELDS
        ]

    def test_no_expression_in_the_validator_reads_a_free_text_value(self):
        offenders = [
            f"line {node.lineno}"
            for node in [*self._reads(), *self._iterated_reads()]
            if not self._is_lawful(node)
        ]
        self.assertEqual(
            [],
            offenders,
            "validation.py must not read the value of prescription or purpose: prose is "
            "an output now, and every check reads session.plan instead",
        )

    def test_the_guard_sees_a_read_made_through_a_loop_variable(self):
        # Watched failing against the shape that walked past it: the delivery projection
        # gained `purpose` by listing it beside six structural fields, and this class
        # stayed green because no expression named the field. It lives in its own module
        # now, and here is the read the guard would have had to catch.
        reintroduced = ast.parse(
            "def _project(session):\n"
            "    return {f: session.get(f) for f in ('scheduled_date', 'purpose')}\n"
        )
        self.tree = reintroduced
        self.parents = {
            child: parent
            for parent in ast.walk(reintroduced)
            for child in ast.iter_child_nodes(parent)
        }
        self.assertEqual(1, len([n for n in self._iterated_reads() if not self._is_lawful(n)]))

    def test_the_module_the_delivery_projection_moved_to_may_only_copy_free_text(self):
        """The one place a free-text value legitimately leaves the validator, guarded.

        A session reaches Intervals under a title, and for a movement_list session that
        title *is* `purpose`, so the projection deciding whether a delivered entry went
        stale has to compare that value. This guard forbids that in `validation`, which is
        why the projection lives in `delivery_content` instead. Moving a read somewhere
        quieter is how a guard becomes decoration, so the new module carries the same rule
        with one shape allowed: a free-text value may be stored and nothing else. Copying
        a value to compare it byte for byte is not reading prose. Calling anything on it is
        where the deleted layer started, and it is refused here too.
        """
        source = (ROOT / "garmin_coach_loop" / "delivery_content.py").read_text(encoding="utf-8")
        self.source = source
        self.tree = ast.parse(source)
        self.parents = {
            child: parent
            for parent in ast.walk(self.tree)
            for child in ast.iter_child_nodes(parent)
        }

        stored = [
            node
            for node in [*self._reads(), *self._iterated_reads()]
            if not isinstance(self.parents[node], (ast.Assign, ast.Tuple, ast.List, ast.Set))
        ]
        self.assertEqual(
            [],
            [f"line {node.lineno}" for node in stored],
            "delivery_content.py may store a free-text value into the projection and do "
            "nothing else with it",
        )
        self.assertNotIn("import re", source, "the projection parses nothing")

    def test_the_module_that_refuses_a_prescribed_intent_line_may_only_refuse(self):
        """The second place a free-text value leaves the validator, held to one shape.

        An intent line carrying `5:50/km` reaches the athlete with no anchor behind it,
        and the only boundary that can refuse it is the one the rest of the plan is
        validated at. `validation` may not read that value -- correctly, and this class is
        why -- so `intent_text` holds the read and the validator hands it the whole
        session. That is the shape the delivery projection already established, and it is
        only honest while the moved read is pinned as tightly as the one that stayed.

        The pin is the line between refusing prose and re-deriving it. `intent_text` may
        notice a token and hand its text back. It may not turn one into a number, and it
        may not import anything from this package -- so it cannot reach a baseline, a
        plan, or a threshold to compare a number against even if it produced one. The
        eleven deleted patterns all failed on the far side of that line: they measured.
        """
        source = (ROOT / "garmin_coach_loop" / "intent_text.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        attribute_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        named_calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        self.assertEqual(
            {"join", "compile", "get", "search", "group"},
            attribute_calls,
            "intent_text may read the intent line, match one pattern against it, and "
            "return what matched",
        )
        self.assertEqual(
            set(),
            named_calls & {"int", "float", "round", "divmod", "sum", "min", "max"},
            "refusing a token is not measuring one: no number is derived from prose here",
        )
        self.assertEqual(
            {"__future__", "re", "typing"},
            imported,
            "intent_text reaches no baseline, no plan and no validator",
        )
        self.assertEqual(
            1,
            source.count("re.compile"),
            "one pattern: a second one is the layer growing back somewhere new",
        )

    def test_the_guard_itself_catches_a_reintroduced_read(self):
        # A guard nobody has seen fail is a guard nobody knows works. This is the exact
        # shape of the layer that was deleted -- a pattern matched against the sentence.
        reintroduced = ast.parse(
            "def _check(session):\n"
            "    return _PATTERN.search(session.get('prescription'))\n"
        )
        self.tree = reintroduced
        self.parents = {
            child: parent
            for parent in ast.walk(reintroduced)
            for child in ast.iter_child_nodes(parent)
        }
        self.assertEqual(1, len([n for n in self._reads() if not self._is_lawful(n)]))

    def test_no_regular_expression_in_the_validator_matches_free_text(self):
        # The eleven patterns by name, so a reader can see exactly what was deleted and
        # a `git log -S` on any of them lands here.
        for name in (
            "_PACE_PATTERN", "_RUN_TARGET_PATTERN", "_STRENGTH_SCHEME_PATTERN",
            "_STRENGTH_LOAD_PATTERN", "_KG_PATTERN", "_BPM_PATTERN",
            "_HR_ABSOLUTE_PATTERN", "_HR_RANGE_PATTERN", "_HR_PERCENT_PATTERN",
            "_INTERVAL_METERS_PATTERN", "_SET_SCHEME_PATTERN",
        ):
            self.assertNotIn(name, self.source, f"{name} is back")

        # And no new one is compiled at all: the only `re` use left is normalising an
        # exercise name so a movement and its baseline compare field to field.
        self.assertEqual(
            {"findall"},
            {
                node.func.attr
                for node in ast.walk(self.tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
            },
            "validation.py uses re only to normalise an exercise name",
        )


if __name__ == "__main__":
    unittest.main()
