from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from garmin_coach_loop.validation import (
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


def project_context(context: dict, plan: dict) -> dict:
    """Mirror the builder projection without rewriting append-only example files."""
    projected = copy.deepcopy(context)
    projected["goal_context"] = {
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "primary_goal": f"{plan['cycle']['primary_adaptation']} — {plan['goal']['outcome']}",
        "maintenance_goal": plan["cycle"]["maintenance_adaptation"],
    }
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
        # The adopted fixture carries an exact 60kg bench prescription. Ground it in
        # test-time PlanState without rewriting append-only public examples.
        bench_baseline = {
            "exercise": "bench press",
            "load_kg": 60.0,
            "assist_kg": None,
            "scheme": "5x5",
        }
        self.before["athlete_baseline"]["strength_loads"].append(copy.deepcopy(bench_baseline))
        self.after["athlete_baseline"]["strength_loads"].append(copy.deepcopy(bench_baseline))
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

    def test_cycle_or_week_adoption_rejects_missing_running_or_strength_prescription(self):
        for mode, sport in (("plan_cycle", "running"), ("plan_week", "strength")):
            with self.subTest(mode=mode, sport=sport):
                after = copy.deepcopy(self.after)
                target = next(
                    session for session in after["week"]["sessions"]
                    if session["sport"] == sport and session["match_status"] == "planned"
                )
                target["prescription"] = None
                event = copy.deepcopy(self.event)
                event.update({"mode": mode, "action": "create" if mode == "plan_cycle" else "adjust"})
                report = validate_bundle(self.context, self.before, after, event)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(any("requires a non-empty prescription" in error for error in report["errors"]))

    def test_adoption_rejects_vague_prescriptions_where_no_structure_answers(self):
        # Both sessions are the unstructured shape: strength never carries a
        # structured_workout, and the running session drops its own, so free text is
        # the only thing left to read. Where a structure exists it decides instead --
        # ExecutableSessionTests covers that half.
        after = copy.deepcopy(self.after)
        running = next(session for session in after["week"]["sessions"] if session["sport"] == "running" and session["match_status"] == "planned")
        running.pop("structured_workout", None)
        running["prescription"] = "Go for a run"
        next(session for session in after["week"]["sessions"] if session["sport"] == "strength" and session["match_status"] == "planned")["prescription"] = "Do some lifting"
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust"})

        report = validate_bundle(self.context, self.before, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("explicit effort target" in error for error in report["errors"]))
        self.assertTrue(any("explicit sets and reps" in error for error in report["errors"]))

    def test_daily_replace_rejects_a_vague_unstructured_run_but_accepts_an_executable_one(self):
        for prescription, expected in (
            ("Go for a run", "blocked"),
            ("50 minutes easy at conversational effort", "passed"),
        ):
            with self.subTest(prescription=prescription):
                after = copy.deepcopy(self.before)
                after["version"] += 1
                target = next(
                    session for session in after["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )
                target.pop("structured_workout", None)
                target["prescription"] = prescription
                target["match_status"] = "replaced"
                event = copy.deepcopy(self.event)
                report = validate_bundle(self.context, self.before, after, event)
                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(any("explicit effort target" in error for error in report["errors"]))

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
                    target_after["prescription"] = "50 minutes easy at conversational effort"
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

    def test_week_mode_cannot_rewrite_cycle_or_baseline(self):
        after = copy.deepcopy(self.after)
        after["cycle"]["primary_adaptation"] = "vo2"
        after["athlete_baseline"]["max_hr"] = 199
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust"})

        report = validate_bundle(self.context, self.before, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("preserve the current 28-day cycle" in error for error in report["errors"]))
        self.assertTrue(any("preserve athlete_baseline" in error for error in report["errors"]))

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
        )["structured_workout"]["steps"]
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
            "prescription": "Bodyweight squat and push-up circuit 3x12, RPE 6",
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
        rest_target.pop("structured_workout", None)
        rest_target.pop("prescription", None)
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
        }
    ],
}


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

    def test_day_with_missing_key_fails(self):
        context = copy.deepcopy(self.context)
        group = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)
        del group["days"][0]["acute_load"]
        context["recovery_signals"] = group
        report = validate_coach_context(context)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("acute_load is required" in e for e in report["errors"]))

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
        context = copy.deepcopy(self.context)
        context["recovery_signals"] = copy.deepcopy(WELL_FORMED_RECOVERY_SIGNALS)

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

    # -- quality pace vs. threshold_pace_sec_per_km -------------------------------

    def test_quality_pace_faster_than_threshold_is_coaching_judgement_not_a_hard_cap(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-quality-01")["prescription"] = (
            "5x1000m @5:50/km, 90 sec jog recovery"
        )
        report = self._validate(self.context, plan)
        self.assertEqual("passed", report["status"], report)

    def test_quality_pace_exactly_at_cap_passes(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-quality-01")["prescription"] = (
            "5x1000m @5:55/km, 90 sec jog recovery"
        )
        report = self._validate(self.context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_pace_prescribed_without_a_measured_anchor_is_blocked(self):
        # An unmeasured threshold cannot support a pace range at all: the estimate is
        # wrong in a direction nobody can see, and every pace derived from it looks
        # exactly as precise as a measured one.
        context = copy.deepcopy(self.context)
        context["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        report = self._validate(context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "threshold_pace_sec_per_km is not measured" in error
                and "prescribe heart rate or effort" in error
                for error in report["errors"]
            )
        )

    def test_heart_rate_prescription_survives_an_unmeasured_anchor(self):
        # The fallback the block exists to push a plan towards: while the anchor is
        # still an estimate, a session is prescribed by heart rate or effort instead.
        context = copy.deepcopy(self.context)
        context["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        for session in plan["week"]["sessions"]:
            if session["sport"] == "running":
                session["prescription"] = "8km easy run, keep heart rate under 150 bpm"
        report = self._validate(context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_exact_bpm_is_bounded_by_measured_hr_anchors_without_a_magic_cap(self):
        for target, expected in ((150, "passed"), (999, "blocked")):
            with self.subTest(target=target):
                plan = copy.deepcopy(self.before)
                for session in plan["week"]["sessions"]:
                    if session["sport"] == "running":
                        session["prescription"] = f"Easy effort, keep heart rate under {target} bpm"
                report = self._validate(self.context, plan)
                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(any("outside its established HR anchors" in error for error in report["errors"]))

    def test_hr_range_validates_both_endpoints(self):
        plan = copy.deepcopy(self.before)
        for session in plan["week"]["sessions"]:
            if session["sport"] == "running":
                session["prescription"] = "Controlled effort at HR 140-999"

        report = self._validate(self.context, plan)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("outside its established HR anchors" in error for error in report["errors"]))

    def test_exact_bpm_without_any_measured_hr_anchor_is_blocked(self):
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["max_hr"] = None
        plan["athlete_baseline"]["easy_hr_ceiling"] = None
        context = project_context(self.context, plan)
        for session in plan["week"]["sessions"]:
            if session["sport"] == "running":
                session["prescription"] = "Easy effort, keep heart rate under 150 bpm"

        report = self._validate(context, plan)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("without a measured max_hr" in error for error in report["errors"]))

    def test_exact_strength_kg_requires_a_matching_measured_exercise_anchor(self):
        for load_kg, exercise, expected in (
            (60, "Bench press", "passed"),
            (62.5, "Bench press", "passed"),
            (999, "Unknown lift", "blocked"),
        ):
            with self.subTest(load_kg=load_kg):
                before = copy.deepcopy(self.before)
                before["athlete_baseline"]["strength_loads"].append(
                    {
                        "exercise": "bench press",
                        "load_kg": 60.0,
                        "assist_kg": None,
                        "scheme": "5x5",
                    }
                )
                context = project_context(self.context, before)
                after = copy.deepcopy(before)
                after["version"] += 1
                target = self._session(after, "strength-upper-01")
                target["prescription"] = f"{exercise} 3x8 @{load_kg}kg"
                target["match_status"] = "replaced"
                event = copy.deepcopy(self.event)
                event["session_id"] = "strength-upper-01"

                report = validate_bundle(context, before, after, event)

                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(any("without a matching established strength baseline" in error for error in report["errors"]))

    def test_prescription_in_the_athletes_own_language_binds_to_its_measured_anchor(self):
        for exercise, display_name, load_kg, assist_kg, prescription, expected in (
            ("split_squat", "分腿蹲", 27.2, None, "分腿蹲 5×5 @27.2kg 為主項", "passed"),
            ("pull_up_assisted", "引體向上", None, 24.0, "引體向上（輔助 24kg）5×5", "passed"),
            ("split_squat", "分腿蹲", 27.2, None, "深蹲 5×5 @100kg 為主項", "blocked"),
            ("split_squat", None, 27.2, None, "分腿蹲 5×5 @27.2kg 為主項", "blocked"),
        ):
            with self.subTest(prescription=prescription, display_name=display_name):
                before = copy.deepcopy(self.before)
                before["athlete_baseline"]["strength_loads"].append(
                    {
                        "exercise": exercise,
                        "display_name": display_name,
                        "load_kg": load_kg,
                        "assist_kg": assist_kg,
                        "scheme": "5x5",
                    }
                )
                context = project_context(self.context, before)
                after = copy.deepcopy(before)
                after["version"] += 1
                target = self._session(after, "strength-upper-01")
                target["prescription"] = prescription
                target["match_status"] = "replaced"
                event = copy.deepcopy(self.event)
                event["session_id"] = "strength-upper-01"

                report = validate_bundle(context, before, after, event)

                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(
                        any(
                            "without a matching established strength baseline" in error
                            for error in report["errors"]
                        )
                    )

    def test_comma_separated_strength_movements_bind_each_load_independently(self):
        before = copy.deepcopy(self.before)
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = self._session(after, "strength-upper-01")
        target["prescription"] = (
            "Back squat 3x8 @70kg, Unknown lift 3x8 @999kg"
        )
        target["match_status"] = "replaced"
        event = copy.deepcopy(self.event)
        event["session_id"] = "strength-upper-01"

        report = validate_bundle(context, before, after, event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("Unknown lift 3x8 @999kg" in error for error in report["errors"])
        )

    def test_quality_pace_check_is_skipped_rather_than_guessed_without_a_pace(self):
        context = copy.deepcopy(self.context)
        context["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        for session in plan["week"]["sessions"]:
            if session["sport"] == "running":
                session["prescription"] = "controlled threshold effort, no pace target"
        report = self._validate(context, plan)
        self.assertEqual("passed", report["status"])
        self.assertFalse(any("threshold_pace" in warning for warning in report["warnings"]))

    def test_pace_in_purpose_without_a_measured_anchor_is_blocked(self):
        # prescription used to be the only field scanned, so a precise pace
        # written in purpose sat unchecked (#38: a too-fast interval pace sat in
        # purpose for two days undetected).
        context = copy.deepcopy(self.context)
        context["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        plan = copy.deepcopy(self.before)
        plan["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        session = self._session(plan, "run-quality-01")
        session["prescription"] = "Controlled threshold reps with jog recovery, no pace target"
        session["purpose"] = "5x1000m @5:50/km, 90 sec jog recovery"
        report = self._validate(context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "threshold_pace_sec_per_km is not measured" in error
                and "prescribe heart rate or effort" in error
                for error in report["errors"]
            )
        )

    def test_pace_in_purpose_survives_a_measured_anchor(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-easy-01")["purpose"] = (
            "Support aerobic base at 6:35/km conversational effort"
        )
        report = self._validate(self.context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    # -- structured hr_ceiling vs. max_hr -----------------------------------------

    def _hr_ceiling_workout(self, ceiling_bpm: int) -> dict:
        return {
            "name": "Easy run",
            "steps": [
                {
                    "kind": "work",
                    "name": "Easy run",
                    "duration": {"kind": "time", "seconds": 1800},
                    "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": ceiling_bpm},
                },
            ],
        }

    def test_structured_hr_ceiling_within_measured_max_hr_passes(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-easy-01")["structured_workout"] = self._hr_ceiling_workout(140)
        plan["athlete_baseline"]["max_hr"] = 180
        context = project_context(self.context, plan)
        report = self._validate(context, plan)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_structured_hr_ceiling_above_max_hr_is_blocked(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-easy-01")["structured_workout"] = self._hr_ceiling_workout(181)
        plan["athlete_baseline"]["max_hr"] = 180
        context = project_context(self.context, plan)
        report = self._validate(context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("exceeds athlete_baseline.max_hr" in e for e in report["errors"]))

    def test_structured_hr_ceiling_without_a_measured_max_hr_is_blocked(self):
        plan = copy.deepcopy(self.before)
        self._session(plan, "run-easy-01")["structured_workout"] = self._hr_ceiling_workout(140)
        plan["athlete_baseline"]["max_hr"] = None
        context = project_context(self.context, plan)
        report = self._validate(context, plan)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("without a measured athlete_baseline.max_hr" in e for e in report["errors"])
        )

    # -- single-session duration vs. max_session_minutes -----------------------------

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

class ExecutableSessionTests(unittest.TestCase):
    """Executability is a structural fact, not a wording rule (#47).

    `structured_workout` is the only executable source at delivery, so where one
    exists it decides and the prescription is free to be the human summary README
    says it is. Where no structure exists -- strength always, running on historical
    PlanStates -- free text is still read, and it has to accept the vocabulary the
    product itself prescribes: the athlete's own language.
    """

    # Anchors for the exact loads in the issue's table. Without them the *evidence*
    # gate blocks these prescriptions, which is a different boundary and stays.
    STRENGTH_BASELINES = (
        {"exercise": "bench_press", "display_name": "臥推", "load_kg": 50.0, "assist_kg": None, "scheme": "4x8"},
        {"exercise": "back_squat", "display_name": "深蹲", "load_kg": 60.0, "assist_kg": None, "scheme": "5x5"},
        {"exercise": "deadlift", "display_name": "硬舉", "load_kg": 80.0, "assist_kg": None, "scheme": "3x5"},
    )

    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.context = project_context(load(EXAMPLE / "coach-context-day-4.json"), self.before)
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def _hr_ceiling_workout(self, ceiling_bpm: int = 145) -> dict:
        return {
            "name": "Easy run",
            "steps": [
                {
                    "kind": "work",
                    "name": "Easy run",
                    "duration": {"kind": "time", "seconds": 3000},
                    "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": ceiling_bpm},
                },
            ],
        }

    def _replace(
        self,
        session_id: str,
        prescription: str,
        *,
        structured_workout: object = "keep",
        strength_baselines: tuple = (),
        baseline: dict | None = None,
    ) -> dict:
        """Adopt one session's new prescription through the daily replace path.

        `structured_workout="keep"` leaves the fixture's own structure in place; None
        removes it, which is the shape of a strength session and of every running
        session stored before the field existed.
        """
        before = copy.deepcopy(self.before)
        before["athlete_baseline"]["strength_loads"].extend(copy.deepcopy(list(strength_baselines)))
        if baseline:
            before["athlete_baseline"].update(copy.deepcopy(baseline))
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(s for s in after["week"]["sessions"] if s["session_id"] == session_id)
        if structured_workout is None:
            target.pop("structured_workout", None)
        elif structured_workout != "keep":
            target["structured_workout"] = copy.deepcopy(structured_workout)
        target["prescription"] = prescription
        target["match_status"] = "replaced"
        event = copy.deepcopy(self.event)
        event["session_id"] = session_id
        return validate_bundle(context, before, after, event)

    # -- the prescriptions the Coach actually writes (issue #47 table) -------------

    def test_athletes_own_vocabulary_is_executable_without_a_structure_to_read(self):
        # Every row of the issue's table, through the surviving free-text path: 下 and
        # 公斤 as units, a set taken to failure instead of a rep count, Zone N, and two
        # explicit effort instructions that carry no number at all.
        for session_id, prescription in (
            ("strength-upper-01", "臥推 4 組，每組 8 下 @ 50kg"),
            ("strength-upper-01", "深蹲 5 組，每組 5 下，60 公斤"),
            ("strength-upper-01", "硬舉 3 組 x 5 下 @ 80kg"),
            ("strength-upper-01", "引體向上 3 組，每組做到力竭，自重"),
            ("run-quality-01", "Zone 2 有氧跑 50 分鐘"),
            ("run-quality-01", "12km 有氧跑，配速隨意"),
            ("run-quality-01", "恢復跑 30 分鐘，全程用鼻子呼吸"),
        ):
            with self.subTest(prescription=prescription):
                report = self._replace(
                    session_id,
                    prescription,
                    structured_workout=None,
                    strength_baselines=self.STRENGTH_BASELINES,
                )
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    def test_structured_target_decides_executability_whatever_the_wording_is(self):
        # The failure the issue opens on: a complete hr_ceiling step -- the exact
        # structure the watch enforces -- rejected because the human summary said
        # "Zone 2". The last row is deliberately the vaguest wording there is: with a
        # structure to execute, wording is not what makes a session executable.
        for prescription in (
            "Zone 2 有氧跑 50 分鐘",
            "12km 有氧跑，配速隨意",
            "恢復跑 30 分鐘，全程用鼻子呼吸",
            "Go for a run",
        ):
            with self.subTest(prescription=prescription):
                report = self._replace(
                    "run-quality-01",
                    prescription,
                    structured_workout=self._hr_ceiling_workout(),
                )
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    def test_run_with_neither_a_structured_target_nor_a_text_target_is_blocked(self):
        # False-positive control for the loosening: with nothing to execute in either
        # artifact, the session is still not a session.
        report = self._replace("run-quality-01", "Go for a run", structured_workout=None)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "needs a structured_workout target" in error and "explicit effort target" in error
                for error in report["errors"]
            )
        )

    def test_strength_still_needs_reps_and_a_load_it_can_execute(self):
        # Strength never carries a structured_workout, so its text check is the only
        # one there is. Widening the vocabulary must not collapse it: a set count with
        # no reps-or-stop-rule, and reps with no load, are still not executable.
        for prescription, expected_error in (
            ("引體向上 3 組，自重", "explicit sets and reps"),
            ("引體向上 3 組，每組做到力竭", "needs a supported load"),
        ):
            with self.subTest(prescription=prescription):
                report = self._replace("strength-upper-01", prescription)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(any(expected_error in error for error in report["errors"]))

    def test_historical_plan_without_any_structured_workout_still_validates(self):
        # Append-only compatibility: PlanStates stored before structured_workout
        # existed keep validating through the text path rather than becoming unreadable.
        before = copy.deepcopy(self.before)
        # Anchors the fixture's own "Bench press 5x5 @60kg", which the evidence gate
        # checks independently of anything this test is about.
        before["athlete_baseline"]["strength_loads"].append(
            {"exercise": "bench press", "load_kg": 60.0, "assist_kg": None, "scheme": "5x5"}
        )
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        for session in after["week"]["sessions"]:
            session.pop("structured_workout", None)
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust"})

        report = validate_bundle(context, before, after, event)

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    # -- the evidence gate is untouched by the executability gate ------------------

    def test_a_duration_written_as_1hr_is_not_read_as_a_heart_rate_target(self):
        # `hr` had no word boundary, so the "hr" inside a duration parsed as a
        # heart-rate label and the next number as its bpm target -- with no measured
        # anchor, blocking the plan over a duration. Issue #47's literal "1hr 30min"
        # was saved by the trailing `\b` against "min"; every spaced variant below did
        # reproduce it. The last two rows are the control: real heart-rate labels are
        # still read, and an unanchored one is still blocked.
        no_anchors = {"max_hr": None, "easy_hr_ceiling": None}
        for prescription, expected in (
            ("輕鬆跑 1hr 30min，全程用鼻子呼吸", "passed"),
            ("輕鬆跑 1hr 30 min，全程用鼻子呼吸", "passed"),
            ("輕鬆跑 2hr 45 分鐘，全程用鼻子呼吸", "passed"),
            ("輕鬆跑 1hr 30-40 min，全程用鼻子呼吸", "passed"),
            ("輕鬆跑 30 分鐘，HR 150 以下", "blocked"),
            ("輕鬆跑 30 分鐘，maxHR 150 以下", "blocked"),
        ):
            with self.subTest(prescription=prescription):
                report = self._replace(
                    "run-quality-01",
                    prescription,
                    structured_workout=None,
                    baseline=no_anchors,
                )
                self.assertEqual(expected, report["status"], report)
                if expected == "blocked":
                    self.assertTrue(
                        any("without a measured max_hr" in error for error in report["errors"])
                    )

    def test_a_load_written_in_chinese_units_still_needs_a_measured_baseline(self):
        # The executability check now accepts 公斤; the evidence check must read the
        # same unit, or exact precision reaches the athlete unanchored through the one
        # spelling only half the validator understands.
        report = self._replace("strength-upper-01", "深蹲 5 組，每組 5 下，60 公斤")
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "without a matching established strength baseline" in error
                for error in report["errors"]
            )
        )

    def test_a_second_movement_cannot_borrow_the_first_movements_anchor(self):
        # #49: clauses were split on ASCII punctuation only, so the same sentence written
        # with ， stayed one clause and the anchored 深蹲 vouched for the 臥推 load beside
        # it. Which comma was typed must not decide whether the gate runs.
        squat = {
            "exercise": "back_squat", "display_name": "深蹲",
            "load_kg": 60.0, "assist_kg": None, "scheme": "5x5",
        }
        for prescription in (
            "深蹲 5 組 5 下 60 公斤，臥推 4 組 8 下 50 公斤",
            "深蹲 5 組 5 下 60 公斤, 臥推 4 組 8 下 50 公斤",
        ):
            with self.subTest(prescription=prescription):
                report = self._replace(
                    "strength-upper-01",
                    prescription,
                    structured_workout=None,
                    strength_baselines=(squat,),
                )
                self.assertEqual("blocked", report["status"], report)
                self.assertTrue(
                    any("臥推 4 組 8 下 50 公斤" in error for error in report["errors"]),
                    report["errors"],
                )

    def test_one_movement_written_across_two_clauses_still_validates(self):
        # The #47 shape: sets and reps split from their load by a comma. Splitting on ，
        # would have separated 臥推 from the 50kg it prescribes and blocked a correct
        # prescription -- the false positive the leak's obvious fix would have created.
        bench = {
            "exercise": "bench_press", "display_name": "臥推",
            "load_kg": 50.0, "assist_kg": None, "scheme": "4x8",
        }
        report = self._replace(
            "strength-upper-01",
            "臥推 4 組，每組 8 下 @ 50kg",
            structured_workout=None,
            strength_baselines=(bench,),
        )
        self.assertEqual("passed", report["status"], report)

    def test_past_loads_cited_as_reasoning_are_not_second_prescriptions(self):
        # Every prescription here is one the live plan actually carries: the movement is
        # named once, then earlier sessions are cited by weight to say why the load is
        # what it is. Reading each of those numbers as a new prescription needing its own
        # anchor would block the coach from explaining the progression at all.
        bench = {
            "exercise": "bench_press", "display_name": "臥推",
            "load_kg": 60.0, "assist_kg": None, "scheme": "5x5",
        }
        pull_up = {
            "exercise": "pull_up_assisted", "display_name": "引體向上",
            "load_kg": None, "assist_kg": 24.0, "scheme": "5x5",
        }
        for prescription, anchor in (
            (
                "臥推 5×5 @60kg 為主項——8/8 的 62.5kg 只做 3 組、8/11 的 65kg 做不完五組，"
                "先把 60kg 五組站穩再談進階",
                bench,
            ),
            (
                "臥推 5×5 @60kg 續攻——8/11 已做到四組 65kg（末組降 60kg），差一組",
                bench,
            ),
            (
                "引體向上 5×5，輔助 24kg 為主項；或改划船。8/13 那堂做完若五組都輕鬆，"
                "這堂降到 22kg 輔助",
                pull_up,
            ),
        ):
            with self.subTest(prescription=prescription):
                report = self._replace(
                    "strength-upper-01",
                    prescription,
                    structured_workout=None,
                    strength_baselines=(anchor,),
                )
                self.assertEqual("passed", report["status"], report)

    def test_accepted_wording_does_not_excuse_an_unanchored_pace_or_hr(self):
        for prescription, structured_workout, baseline, expected_error in (
            # Zone wording is accepted; the exact pace inside it still needs a
            # measured threshold.
            (
                "Zone 2 有氧跑 50 分鐘 @5:50/km",
                None,
                {"threshold_pace_sec_per_km": None},
                "threshold_pace_sec_per_km is not measured",
            ),
            # A Chinese heart-rate target is read exactly like an English one.
            (
                "有氧跑 50 分鐘，心率 250 以下",
                None,
                {},
                "outside its established HR anchors",
            ),
            # Structure deciding executability does not exempt the structure itself
            # from the anchor it invents precision against.
            (
                "Zone 2 有氧跑 50 分鐘",
                "hr_ceiling",
                {"max_hr": None},
                "without a measured athlete_baseline.max_hr",
            ),
        ):
            with self.subTest(prescription=prescription, baseline=baseline):
                report = self._replace(
                    "run-quality-01",
                    prescription,
                    structured_workout=(
                        self._hr_ceiling_workout() if structured_workout == "hr_ceiling" else None
                    ),
                    baseline=baseline,
                )
                self.assertEqual("blocked", report["status"], report)
                self.assertTrue(any(expected_error in error for error in report["errors"]))


class StructuredStrengthSessionTests(unittest.TestCase):
    """A strength session's prescribed work as a recorded field, not a re-derived one (#52).

    `athlete_baseline.strength_loads` was structured all along; the session half was a
    sentence, so the checks split it on punctuation and pattern-matched the numbers back
    out. `strength_movements` closes that gap by naming the same canonical exercise its
    baseline uses, and the checks then compare field to field.

    Compatibility follows `structured_workout` exactly: the field is optional, sessions
    stored before it keep validating through the free-text path, and no plan is
    regenerated.
    """

    SQUAT = {
        "exercise": "back_squat", "display_name": "深蹲",
        "load_kg": 60.0, "assist_kg": None, "scheme": "5x5",
    }
    PULL_UP = {
        "exercise": "pull_up_assisted", "display_name": "引體向上",
        "load_kg": None, "assist_kg": 24.0, "scheme": "5x5",
    }

    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.context = project_context(load(EXAMPLE / "coach-context-day-4.json"), self.before)
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    @staticmethod
    def _movement(exercise: str, **overrides) -> dict:
        movement = {
            "exercise": exercise,
            "sets": 5,
            "reps": 5,
            "load_kg": None,
            "assist_kg": None,
            "load_basis": "pending_confirmation",
        }
        movement.update(overrides)
        return movement

    def _adopt(
        self,
        prescription: str,
        movements: object = "omit",
        *,
        strength_baselines: tuple = (),
        session_id: str = "strength-upper-01",
    ) -> dict:
        """Adopt one session through the daily replace path, with or without structure.

        `movements="omit"` leaves the session shaped exactly as every stored session is
        today: no `strength_movements` key at all.
        """
        before = copy.deepcopy(self.before)
        before["athlete_baseline"]["strength_loads"].extend(copy.deepcopy(list(strength_baselines)))
        context = project_context(self.context, before)
        after = copy.deepcopy(before)
        after["version"] += 1
        target = next(s for s in after["week"]["sessions"] if s["session_id"] == session_id)
        target["prescription"] = prescription
        if movements != "omit":
            target["strength_movements"] = copy.deepcopy(movements)
        target["match_status"] = "replaced"
        event = copy.deepcopy(self.event)
        event["session_id"] = session_id
        return validate_bundle(context, before, after, event)

    # -- the structure decides, and nothing reads the sentence ---------------------

    def test_structured_loads_validate_whatever_language_the_prescription_uses(self):
        # Same recorded movement each time; only the human summary changes. The last row
        # names no movement, no scheme and no unit at all -- with the work recorded, the
        # prescription is free to be the summary README says it is.
        squat = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        for prescription in (
            "深蹲 5 組 5 下 60 公斤",
            "Back squat 5x5 @60kg",
            "スクワット 5×5 60kg",
            "腿日：主項照上週加重",
        ):
            with self.subTest(prescription=prescription):
                report = self._adopt(
                    prescription, [squat], strength_baselines=(self.SQUAT,)
                )
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    def test_the_recorded_movements_are_the_whole_prescription_the_gate_reads(self):
        # The consequence of "structure decides": an unanchored 50kg written only in the
        # prose is not evidence and is not read. So a session that carries the field must
        # list every movement in it -- nothing else will.
        squat = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        report = self._adopt(
            "深蹲 5 組 5 下 60 公斤，臥推 4 組 8 下 50 公斤",
            [squat],
            strength_baselines=(self.SQUAT,),
        )
        self.assertEqual("passed", report["status"], report)

    # -- the evidence gate, read field to field ------------------------------------

    def test_a_recorded_load_with_no_measured_anchor_is_blocked_and_named(self):
        squat = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        press = self._movement(
            "overhead_press", sets=3, reps=8, load_kg=40.0, load_basis="measured_baseline"
        )
        report = self._adopt(
            "深蹲主項，肩推副項", [squat, press], strength_baselines=(self.SQUAT,)
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "without a matching established strength baseline" in error
                and "overhead_press" in error
                for error in report["errors"]
            ),
            report["errors"],
        )
        self.assertFalse(
            any("back_squat" in error for error in report["errors"]), report["errors"]
        )

    def test_the_two_movement_case_is_blocked_once_the_structure_is_present(self):
        # #49: two movements, one anchored, and which comma was typed decided whether the
        # gate ran. With the movements recorded there is no sentence to split, so every
        # spelling below -- full-width comma, ASCII comma, 頓號, line break, English --
        # reaches the same verdict for the same reason.
        squat = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        bench = self._movement(
            "bench_press", sets=4, reps=8, load_kg=50.0, load_basis="measured_baseline"
        )
        for prescription in (
            "深蹲 5 組 5 下 60 公斤，臥推 4 組 8 下 50 公斤",
            "深蹲 5 組 5 下 60 公斤, 臥推 4 組 8 下 50 公斤",
            "深蹲 5 組 5 下 60 公斤、臥推 4 組 8 下 50 公斤",
            "深蹲 5 組 5 下 60 公斤\n臥推 4 組 8 下 50 公斤",
            "Back squat 5x5 @60kg, bench press 4x8 @50kg",
        ):
            with self.subTest(prescription=prescription):
                report = self._adopt(
                    prescription, [squat, bench], strength_baselines=(self.SQUAT,)
                )
                self.assertEqual("blocked", report["status"], report)
                self.assertTrue(
                    any(
                        "without a matching established strength baseline" in error
                        and "bench_press" in error
                        for error in report["errors"]
                    ),
                    report["errors"],
                )

    def test_a_movement_binds_to_its_baseline_by_canonical_key_or_display_name(self):
        for exercise, expected in (
            ("back_squat", "passed"),   # the canonical key itself
            ("back squat", "passed"),   # the separator is not part of the name
            ("深蹲", "passed"),          # display_name, the athlete's own wording
            ("front_squat", "blocked"),  # a different movement, however similar
        ):
            with self.subTest(exercise=exercise):
                movement = self._movement(
                    exercise, load_kg=60.0, load_basis="measured_baseline"
                )
                report = self._adopt(
                    "主項 5 組 5 下", [movement], strength_baselines=(self.SQUAT,)
                )
                self.assertEqual(expected, report["status"], report)

    def test_an_assisted_movement_binds_to_the_assist_figure_its_baseline_measured(self):
        # Which column holds the measurement is a property of the lift: an assisted
        # pull-up records assist_kg and has no load_kg to compare at all.
        movement = self._movement(
            "pull_up_assisted", assist_kg=24.0, reps=None, load_basis="measured_baseline"
        )
        report = self._adopt(
            "引體向上 5 組，每組做到力竭，輔助 24kg",
            [movement],
            strength_baselines=(self.PULL_UP,),
        )
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_a_baseline_entry_that_measured_nothing_anchors_nothing(self):
        # False-positive control in the other direction: the exercise is named in the
        # baseline, but the baseline never measured it, so it cannot vouch for a number.
        unmeasured = {
            "exercise": "pull_up_assisted", "display_name": "引體向上",
            "load_kg": None, "assist_kg": None, "scheme": "5x5",
        }
        movement = self._movement(
            "pull_up_assisted", assist_kg=24.0, reps=None, load_basis="measured_baseline"
        )
        report = self._adopt(
            "引體向上 5 組做到力竭", [movement], strength_baselines=(unmeasured,)
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("pull_up_assisted" in error for error in report["errors"]), report["errors"]
        )

    def test_a_loadless_basis_needs_no_anchor_at_all(self):
        # The two bases that state why there is no kg figure. Neither prescribes exact
        # precision, so neither has anything to anchor -- the whole point of recording
        # which of the two an absent load means.
        for basis in ("bodyweight", "pending_confirmation"):
            with self.subTest(load_basis=basis):
                movement = self._movement("push_up", reps=None, load_basis=basis)
                report = self._adopt("伏地挺身 5 組做到力竭", [movement])
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    # -- executability, and the control that it did not simply stop checking -------

    def test_structure_decides_executability_whatever_the_summary_says(self):
        movement = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        report = self._adopt("腿日", [movement], strength_baselines=(self.SQUAT,))
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_the_same_summary_without_structure_is_still_not_a_session(self):
        # False-positive control for the loosening above: with nothing recorded and
        # nothing in the text, the free-text gate is untouched and still blocks.
        report = self._adopt("腿日", strength_baselines=(self.SQUAT,))
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("explicit sets and reps" in error for error in report["errors"]),
            report["errors"],
        )
        self.assertTrue(
            any("needs a supported load" in error for error in report["errors"]),
            report["errors"],
        )

    # -- shape rules ---------------------------------------------------------------

    def test_load_basis_must_agree_with_the_load_it_carries(self):
        # A movement that says bodyweight and carries 60 kg contradicts itself, and the
        # evidence gate would have to choose which half to believe -- the guess this
        # field exists to remove.
        for overrides, expected_error in (
            ({"load_kg": 60.0, "load_basis": "bodyweight"}, "must leave load_kg and assist_kg null"),
            ({"assist_kg": 10.0, "load_basis": "pending_confirmation"}, "must leave load_kg and assist_kg null"),
            ({"load_basis": "measured_baseline"}, "requires a load_kg or assist_kg figure"),
            ({"load_basis": "rpe"}, "load_basis must be one of"),
            ({"sets": 0}, "sets must be an integer >= 1"),
            ({"reps": 0}, "reps must be an integer >= 1"),
            ({"exercise": " "}, "exercise must be a non-empty string"),
        ):
            with self.subTest(overrides=overrides):
                movement = self._movement("back_squat") | overrides
                report = self._adopt("主項 5 組 5 下", [movement], strength_baselines=(self.SQUAT,))
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(expected_error in error for error in report["errors"]), report["errors"]
                )

    def test_an_empty_or_malformed_movement_list_is_blocked(self):
        anchored = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        for movements, expected_error in (
            ([], "must contain at least one movement"),
            ("深蹲 5x5", "must be an array"),
            ([{"exercise": "back_squat"}], "sets is required"),
            ([{**anchored, "notes": "heavy"}], "notes is not allowed"),
        ):
            with self.subTest(movements=movements):
                report = self._adopt(
                    "深蹲 5 組 5 下 60 公斤", movements, strength_baselines=(self.SQUAT,)
                )
                self.assertEqual("blocked", report["status"])
                self.assertTrue(
                    any(expected_error in error for error in report["errors"]), report["errors"]
                )

    def test_strength_movements_are_rejected_on_a_running_session(self):
        # Bound to the sport whose gate reads it, exactly as structured_workout is bound
        # to running: only a strength session's baseline check ever looks here, so
        # carrying it elsewhere would be a second prescription nothing validates.
        movement = self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline")
        report = self._adopt(
            "5x1000m @6:00/km, 2 min jog recovery",
            [movement],
            strength_baselines=(self.SQUAT,),
            session_id="run-quality-01",
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "strength_movements is only allowed for strength" in error
                for error in report["errors"]
            ),
            report["errors"],
        )

    # -- compatibility: the field is optional and nothing was migrated -------------

    def test_a_session_without_the_field_validates_exactly_as_it_did_before(self):
        # #47's table, unchanged: 下 and 公斤 as units, a set taken to failure instead of
        # a rep count. These sessions carry no strength_movements and must keep reaching
        # the free-text path, or every PlanState already in the store stops opening.
        baselines = (
            {"exercise": "bench_press", "display_name": "臥推", "load_kg": 50.0, "assist_kg": None, "scheme": "4x8"},
            self.SQUAT,
            {"exercise": "deadlift", "display_name": "硬舉", "load_kg": 80.0, "assist_kg": None, "scheme": "3x5"},
        )
        for prescription in (
            "臥推 4 組，每組 8 下 @ 50kg",
            "深蹲 5 組，每組 5 下，60 公斤",
            "硬舉 3 組 x 5 下 @ 80kg",
            "引體向上 3 組，每組做到力竭，自重",
        ):
            with self.subTest(prescription=prescription):
                report = self._adopt(prescription, strength_baselines=baselines)
                self.assertEqual([], report["errors"])
                self.assertEqual("passed", report["status"])

    def test_the_stored_example_plan_carries_no_structure_and_still_validates(self):
        plan = copy.deepcopy(self.before)
        self.assertFalse(
            any("strength_movements" in session for session in plan["week"]["sessions"])
        )
        self.assertEqual("passed", validate_plan_state(plan)["status"])

    @unittest.skipIf(Draft202012Validator is None, "jsonschema dev dependency is unavailable")
    def test_a_structured_strength_session_matches_the_public_json_schema(self):
        # The runtime validator and the published contract have to describe the same
        # field, or an artifact one accepts is a schema violation to every other reader.
        plan = copy.deepcopy(self.before)
        target = next(
            s for s in plan["week"]["sessions"] if s["session_id"] == "strength-upper-01"
        )
        target["strength_movements"] = [
            self._movement("back_squat", load_kg=60.0, load_basis="measured_baseline"),
            self._movement("pull_up_assisted", assist_kg=24.0, reps=None, load_basis="measured_baseline"),
            self._movement("push_up", reps=None, load_basis="bodyweight"),
        ]
        schema = load(CONTRACTS / "plan-state.schema.json")
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self.assertEqual([], sorted(e.message for e in validator.iter_errors(plan)))


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


class MaterialChangeTests(unittest.TestCase):
    """A revision has to earn itself: prose edits are not training decisions."""

    def setUp(self):
        self.context = load(EXAMPLE / "coach-context-day-4.json")
        self.before = load(EXAMPLE / "plan-state-v1.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def _bound_session(self, plan):
        return next(
            s for s in plan["week"]["sessions"] if s["session_id"] == self.event["session_id"]
        )

    def test_prose_only_change_is_blocked(self):
        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        self._bound_session(after)["purpose"] = "Reworded, same training"
        report = validate_bundle(self.context, self.before, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("nothing material moved" in error for error in report["errors"])
        )

    def test_a_ten_second_pace_change_counts_as_material(self):
        # Small in magnitude, large in consequence -- the check is by field, not size.
        after = copy.deepcopy(self.before)
        after["version"] = self.before["version"] + 1
        self._bound_session(after)["prescription"] = "5x1000m @6:00/km"
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


if __name__ == "__main__":
    unittest.main()
