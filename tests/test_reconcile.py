from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop.reconcile import (
    apply_reconciliation,
    build_reconcile_bundle,
    propose_reconciliation,
)
from garmin_coach_loop.store import StateStoreError, init_store, status_store
from garmin_coach_loop.validation import validate_bundle

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # the runtime validators stay dependency-free
    Draft202012Validator = None
    FormatChecker = None

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_plan() -> dict:
    plan = load(EXAMPLE / "plan-state-v1.json")
    # Provider identity is the only evidence strong enough for automatic completion.
    # Keep the append-only example fixture untouched and enrich only this test copy.
    for session in plan["week"]["sessions"]:
        session["execution"]["external_id"] = f"event-{session['session_id']}"
        session["execution"]["delivery_state"] = "intervals_accepted"
        session["execution"]["publish_supported"] = True
    return plan


def make_context() -> dict:
    plan = make_plan()
    context = load(EXAMPLE / "coach-context-day-4.json")
    context["goal_context"] = {
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "primary_goal": (
            f"{plan['cycle']['primary_adaptation']} — {plan['goal']['outcome']}"
        ),
        "maintenance_goal": plan["cycle"]["maintenance_adaptation"],
    }
    context["athlete_baseline"] = copy.deepcopy(plan["athlete_baseline"])
    context["current_calendar"] = [
        {
            "session_id": session["session_id"],
            "date": session["scheduled_date"],
            "sport": session["sport"],
            "cost": session["cost"],
            "status": "completed" if session["match_status"] == "partial" else session["match_status"],
        }
        for session in plan["week"]["sessions"]
    ]
    return context


def refresh_context_projection(context: dict, plan: dict) -> dict:
    refreshed = copy.deepcopy(context)
    refreshed["goal_context"]["plan_version"] = plan["version"]
    refreshed["current_calendar"] = [
        {
            "session_id": session["session_id"],
            "date": session["scheduled_date"],
            "sport": session["sport"],
            "cost": session["cost"],
            "status": "completed" if session["match_status"] == "partial" else session["match_status"],
        }
        for session in plan["week"]["sessions"]
    ]
    return refreshed


def matched_actual(session_id: str, *, date: str, completion: str = "completed", confidence: str = "matched") -> dict:
    return {
        "activity_id": f"fixture-actual-{session_id}",
        "date": date,
        "sport": "running",
        "paired_event_id": f"event-{session_id}",
        "planned_session_id": session_id,
        "match_confidence": confidence,
        "adaptation": "threshold",
        "body_stress": "lower",
        "cost": "hard",
        "duration_minutes": 42,
        "completion": completion,
        "elevation_gain_m": None,
        "subjective_feel": 3,
    }


def owned_actual(
    session_id: str,
    *,
    date: str,
    sport: str = "running",
    duration_minutes: int = 48,
    completion: str = "completed",
) -> dict:
    """An activity the provider never paired, attached by the product's own ownership of
    the delivered session on a day that admits one reading."""
    return {
        "activity_id": f"fixture-owned-{session_id}",
        "date": date,
        "sport": sport,
        "paired_event_id": None,
        "planned_session_id": session_id,
        "match_confidence": "owned",
        "adaptation": "threshold",
        "body_stress": "lower",
        "cost": "hard",
        "duration_minutes": duration_minutes,
        "completion": completion,
        "elevation_gain_m": None,
        "subjective_feel": 3,
    }


class ProposeReconciliationTests(unittest.TestCase):
    """The pure planning step: what may be written versus what may only be reported."""

    def test_matched_completed_actual_against_planned_session_is_proposed(self):
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
        report = propose_reconciliation(plan, context)
        self.assertEqual(
            ["run-quality-01"], [p["session_id"] for p in report["proposals"]]
        )

    def test_matched_completed_actual_against_moved_or_replaced_session_is_proposed(self):
        for status in ("moved", "replaced"):
            with self.subTest(status=status):
                plan = make_plan()
                target = next(
                    session for session in plan["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )
                target["match_status"] = status
                context = refresh_context_projection(make_context(), plan)
                context["recent_actuals"].append(
                    matched_actual("run-quality-01", date="2026-08-13")
                )
                report = propose_reconciliation(plan, context)
                self.assertEqual(
                    ["run-quality-01"], [proposal["session_id"] for proposal in report["proposals"]]
                )

    def test_already_completed_session_is_never_proposed_again(self):
        # strength-full-01 is already completed in the plan, and the example context
        # already carries its matched actual -- the natural-idempotency case.
        report = propose_reconciliation(make_plan(), make_context())
        self.assertEqual([], [p for p in report["proposals"] if p["session_id"] == "strength-full-01"])

    def test_probable_match_is_reported_not_written(self):
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(
            matched_actual("run-quality-01", date="2026-08-13", confidence="probable")
        )
        report = propose_reconciliation(plan, context)
        self.assertEqual([], report["proposals"])
        self.assertTrue(any(a["session_id"] == "run-quality-01" for a in report["ambiguous"]))

    def test_a_settled_session_is_not_reported_as_waiting_on_a_human(self):
        # Read from the live store on 2026-08-13: two strength sessions recorded as
        # completed still carried a probable actual, so every refresh asked for a
        # confirmation of something already settled -- and would have kept asking,
        # since the actual never stops being probable.
        for status in ("completed", "partial"):
            with self.subTest(status=status):
                plan = make_plan()
                target = next(
                    session for session in plan["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )
                target["match_status"] = status
                context = refresh_context_projection(make_context(), plan)
                context["recent_actuals"].append(
                    matched_actual("run-quality-01", date="2026-08-13", confidence="probable")
                )
                report = propose_reconciliation(plan, context)
                self.assertEqual(
                    [], [a for a in report["ambiguous"] if a["session_id"] == "run-quality-01"]
                )

    def test_an_actual_from_an_earlier_week_is_not_a_question_for_a_human(self):
        # Matching now runs over the whole cycle so the coach can read an earlier week's
        # prescription beside what came back for it (context.cycle_sessions). Nothing
        # outside this week is reconcilable -- this module only writes into
        # week.sessions -- so surfacing one as "a human confirms" would open a question
        # no answer can close, on every refresh, forever.
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(
            matched_actual("run-last-week-01", date="2026-08-06", confidence="probable")
        )
        context["recent_actuals"].append(
            matched_actual("run-last-week-02", date="2026-08-07", confidence="matched")
        )

        report = propose_reconciliation(plan, context)

        self.assertEqual([], [a["session_id"] for a in report["ambiguous"]])
        self.assertEqual([], [p["session_id"] for p in report["proposals"]])

    def test_a_missed_session_with_a_probable_actual_still_reaches_a_human(self):
        # The outcome recorded is "not done" and an activity looks like it anyway. That
        # contradiction is precisely what a person should see.
        plan = make_plan()
        target = next(
            session for session in plan["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        target["match_status"] = "missed"
        context = refresh_context_projection(make_context(), plan)
        context["recent_actuals"].append(
            matched_actual("run-quality-01", date="2026-08-13", confidence="probable")
        )
        report = propose_reconciliation(plan, context)
        self.assertTrue(any(a["session_id"] == "run-quality-01" for a in report["ambiguous"]))

    def test_partial_completion_is_reported_not_written(self):
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(
            matched_actual("run-quality-01", date="2026-08-13", completion="partial")
        )
        report = propose_reconciliation(plan, context)
        self.assertEqual([], report["proposals"])
        entry = next(a for a in report["ambiguous"] if a["session_id"] == "run-quality-01")
        self.assertIn("not completed", entry["reason"])

    def test_planned_sessions_without_actuals_are_listed_and_untouched(self):
        report = propose_reconciliation(make_plan(), make_context())
        listed = {entry["session_id"] for entry in report["unmatched_planned"]}
        self.assertIn("run-quality-01", listed)
        self.assertIn("run-long-01", listed)

    def test_probable_session_is_ambiguous_only_never_also_unmatched(self):
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(
            matched_actual("run-quality-01", date="2026-08-13", confidence="probable")
        )
        report = propose_reconciliation(plan, context)
        self.assertTrue(any(a["session_id"] == "run-quality-01" for a in report["ambiguous"]))
        self.assertNotIn(
            "run-quality-01", {e["session_id"] for e in report["unmatched_planned"]}
        )

    def test_two_completed_actuals_claiming_one_session_are_ambiguous(self):
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
        second = matched_actual("run-quality-01", date="2026-08-13")
        second["activity_id"] = "fixture-actual-run-quality-01-duplicate"
        context["recent_actuals"].append(second)
        report = propose_reconciliation(plan, context)
        self.assertEqual([], report["proposals"])
        entry = next(a for a in report["ambiguous"] if a["session_id"] == "run-quality-01")
        self.assertIn("more than one", entry["reason"])

    def test_completed_plus_partial_is_a_conflict_not_a_tiebreak(self):
        # The matcher is one-to-one, so two matched actuals for one session is
        # evidence of bad data -- never resolved in favour of the completed one.
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
        partial = matched_actual("run-quality-01", date="2026-08-13", completion="partial")
        partial["activity_id"] = "fixture-actual-run-quality-01-partial"
        context["recent_actuals"].append(partial)
        report = propose_reconciliation(plan, context)
        self.assertEqual([], report["proposals"])
        entry = next(a for a in report["ambiguous"] if a["session_id"] == "run-quality-01")
        self.assertIn("more than one", entry["reason"])


class OwnershipBackedReconciliationTests(unittest.TestCase):
    """An activity attached by product ownership reconciles, and the gate re-derives the
    ownership rather than trusting the label the matcher wrote."""

    def setUp(self):
        self.plan = make_plan()
        self.context = make_context()
        self.context["recent_actuals"].append(owned_actual("run-quality-01", date="2026-08-13"))

    def _bundle(self, context: dict, plan: dict | None = None):
        plan = plan if plan is not None else self.plan
        proposal = propose_reconciliation(plan, context)["proposals"][0]
        return build_reconcile_bundle(plan, proposal, context, created_at="2026-08-13T21:00:00+00:00")

    def test_owned_completed_actual_is_proposed_and_validates(self):
        report = propose_reconciliation(self.plan, self.context)
        self.assertEqual(["run-quality-01"], [p["session_id"] for p in report["proposals"]])

        after, event = self._bundle(self.context)
        self.assertEqual(
            "completed",
            next(s for s in after["week"]["sessions"] if s["session_id"] == "run-quality-01")["match_status"],
        )
        validation = validate_bundle(self.context, self.plan, after, event)
        self.assertEqual("passed", validation["status"], validation)

    def test_the_event_says_which_evidence_backed_the_write(self):
        _, owned_event = self._bundle(self.context)
        self.assertTrue(
            any("產品交付的課表" in entry["observation"] for entry in owned_event["evidence"]),
            owned_event["evidence"],
        )

        identity_context = make_context()
        identity_context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
        _, identity_event = self._bundle(identity_context)
        self.assertTrue(
            any("provider 已配對" in entry["observation"] for entry in identity_event["evidence"]),
            identity_event["evidence"],
        )

    def test_gate_refuses_an_owned_claim_on_a_session_never_delivered(self):
        after, event = self._bundle(self.context)
        plan = copy.deepcopy(self.plan)
        for session in (plan, after):
            target = next(s for s in session["week"]["sessions"] if s["session_id"] == "run-quality-01")
            target["execution"]["delivery_state"] = "not_published"
            target["execution"]["external_id"] = None
        report = validate_bundle(self.context, plan, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in e for e in report["errors"]))

    def test_gate_refuses_an_owned_claim_when_a_second_activity_shares_the_day(self):
        # The harmful case: with two runs that day, ownership cannot say which one was
        # the session, and a hand-built event must not be able to assert that it can.
        after, event = self._bundle(self.context)
        context = copy.deepcopy(self.context)
        extra = owned_actual("run-quality-01", date="2026-08-13", duration_minutes=20)
        extra["activity_id"] = "fixture-owned-run-quality-01-second"
        extra["planned_session_id"] = None
        extra["match_confidence"] = "unmatched"
        context["recent_actuals"].append(extra)
        report = validate_bundle(context, self.plan, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in e for e in report["errors"]))

    def test_gate_refuses_an_owned_claim_outside_the_duration_band(self):
        after, event = self._bundle(self.context)
        context = copy.deepcopy(self.context)
        for actual in context["recent_actuals"]:
            if actual.get("planned_session_id") == "run-quality-01":
                actual["duration_minutes"] = 12  # against a 50-minute prescription
        report = validate_bundle(context, self.plan, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in e for e in report["errors"]))

    def test_gate_refuses_an_owned_claim_the_provider_paired_elsewhere(self):
        after, event = self._bundle(self.context)
        context = copy.deepcopy(self.context)
        for actual in context["recent_actuals"]:
            if actual.get("planned_session_id") == "run-quality-01":
                actual["paired_event_id"] = "event-some-other-session"
        report = validate_bundle(context, self.plan, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in e for e in report["errors"]))

    def test_gate_refuses_an_owned_claim_on_a_different_day(self):
        after, event = self._bundle(self.context)
        context = copy.deepcopy(self.context)
        for actual in context["recent_actuals"]:
            if actual.get("planned_session_id") == "run-quality-01":
                actual["date"] = "2026-08-12"
        report = validate_bundle(context, self.plan, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in e for e in report["errors"]))


class ReconcileBundleValidationTests(unittest.TestCase):
    """The generated bundle passes validate_bundle; a disguised edit does not."""

    def setUp(self):
        self.plan = make_plan()
        self.context = make_context()
        self.context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
        proposal = propose_reconciliation(self.plan, self.context)["proposals"][0]
        self.after, self.event = build_reconcile_bundle(
            self.plan, proposal, self.context, created_at="2026-08-13T21:00:00+00:00"
        )

    def test_generated_bundle_passes_validation(self):
        report = validate_bundle(self.context, self.plan, self.after, self.event)
        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_generated_bundle_completes_moved_or_replaced_actionable_session(self):
        for status in ("moved", "replaced"):
            with self.subTest(status=status):
                plan = copy.deepcopy(self.plan)
                next(
                    session for session in plan["week"]["sessions"]
                    if session["session_id"] == "run-quality-01"
                )["match_status"] = status
                context = refresh_context_projection(self.context, plan)
                proposal = propose_reconciliation(plan, context)["proposals"][0]
                after, event = build_reconcile_bundle(
                    plan, proposal, context, created_at="2026-08-13T21:00:00+00:00"
                )
                report = validate_bundle(context, plan, after, event)
                self.assertEqual("passed", report["status"], report)

    def test_prescription_edit_disguised_as_reconcile_is_blocked(self):
        after = copy.deepcopy(self.after)
        for session in after["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session["prescription"] = "6x1000m much harder than planned"
        report = validate_bundle(self.context, self.plan, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("may only move match_status" in error for error in report["errors"]))

    def test_second_session_edit_disguised_as_reconcile_is_blocked(self):
        after = copy.deepcopy(self.after)
        for session in after["week"]["sessions"]:
            if session["session_id"] == "run-long-01":
                session["match_status"] = "completed"
        report = validate_bundle(self.context, self.plan, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("bound session's match_status and nothing else" in error for error in report["errors"])
        )

    def test_reconcile_without_backing_actual_is_blocked(self):
        context = copy.deepcopy(self.context)
        context["recent_actuals"] = [
            a for a in context["recent_actuals"] if a.get("planned_session_id") != "run-quality-01"
        ]
        report = validate_bundle(context, self.plan, self.after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("no completed actual in context.recent_actuals backs" in error for error in report["errors"]))

    def test_reconcile_must_ride_review_week_adjust(self):
        event = copy.deepcopy(self.event)
        event["mode"] = "revisit_today"
        event["action"] = "keep"
        report = validate_bundle(self.context, self.plan, self.after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("requires mode review_week with action adjust" in error for error in report["errors"])
        )

    def test_mixed_reason_codes_are_blocked(self):
        event = copy.deepcopy(self.event)
        event["reason_codes"] = ["planned_actual_reconciled", "actual_load_above_plan"]
        report = validate_bundle(self.context, self.plan, self.after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("stands alone" in error for error in report["errors"]))

    def test_null_session_id_is_blocked(self):
        event = copy.deepcopy(self.event)
        event["session_id"] = None
        report = validate_bundle(self.context, self.plan, self.after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("must bind exactly one session_id" in error for error in report["errors"])
        )

    def test_reverse_transition_is_blocked(self):
        before = copy.deepcopy(self.plan)
        for session in before["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session["match_status"] = "completed"
        after = copy.deepcopy(before)
        after["version"] = before["version"] + 1
        for session in after["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session["match_status"] = "planned"
        event = copy.deepcopy(self.event)
        report = validate_bundle(self.context, before, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("actionable -> completed only" in error for error in report["errors"]))

    def test_smuggled_plan_status_change_is_blocked(self):
        after = copy.deepcopy(self.after)
        after["status"] = "completed"
        report = validate_bundle(self.context, self.plan, after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("must not change status" in error for error in report["errors"]))

    def test_backing_actual_with_wrong_sport_is_rejected(self):
        context = copy.deepcopy(self.context)
        for actual in context["recent_actuals"]:
            if actual.get("planned_session_id") == "run-quality-01":
                actual["sport"] = "strength"
                actual["adaptation"] = "strength"
                actual["body_stress"] = "full"
        report = validate_bundle(context, self.plan, self.after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in error for error in report["errors"]))

    def test_backing_actual_on_another_day_is_allowed_when_provider_identity_matches(self):
        context = copy.deepcopy(self.context)
        for actual in context["recent_actuals"]:
            if actual.get("planned_session_id") == "run-quality-01":
                actual["date"] = "2026-08-12"
        report = validate_bundle(context, self.plan, self.after, self.event)
        self.assertEqual("passed", report["status"], report)

    def test_backing_actual_with_a_different_provider_identity_is_rejected(self):
        context = copy.deepcopy(self.context)
        for actual in context["recent_actuals"]:
            if actual.get("planned_session_id") == "run-quality-01":
                actual["paired_event_id"] = "event-some-other-session"
        report = validate_bundle(context, self.plan, self.after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("paired provider event or product ownership" in error for error in report["errors"]))

    def test_hand_built_event_over_conflicting_actuals_is_blocked(self):
        # The proposer refuses to write when two matched actuals claim one session;
        # a hand-built event over the same conflicting context must be refused by the
        # gate too, not passed on the one claim that fits.
        context = copy.deepcopy(self.context)
        partial = matched_actual("run-quality-01", date="2026-08-13", completion="partial")
        partial["activity_id"] = "fixture-actual-run-quality-01-partial"
        context["recent_actuals"].append(partial)
        report = validate_bundle(context, self.plan, self.after, self.event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("more than one attached actual" in error for error in report["errors"]))

    def test_coaching_reason_codes_keep_their_manual_path(self):
        # The fence binds this reason code, not the whole review_week/adjust shape:
        # a hand-written reconciliation under a coaching code (store commit 7's exact
        # move) must stay possible, and stays visible in history through its code.
        event = copy.deepcopy(self.event)
        event["reason_codes"] = ["actual_load_above_plan"]
        report = validate_bundle(self.context, self.plan, self.after, event)
        self.assertEqual("passed", report["status"], report)

    @unittest.skipIf(Draft202012Validator is None, "jsonschema dev dependency is unavailable")
    def test_generated_event_matches_the_public_json_schema(self):
        schema = load(ROOT / "contracts" / "decision-event.schema.json")
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self.assertEqual([], list(validator.iter_errors(self.event)))


class ApplyReconciliationTests(unittest.TestCase):
    """End-to-end against a real (temporary) store: write, rerun, dry-run."""

    def _store_with_plan(self, tmp: str) -> Path:
        state_dir = Path(tmp) / "state"
        init_store(state_dir, make_plan())
        return state_dir

    def test_apply_writes_one_event_per_matched_session_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._store_with_plan(tmp)
            context = make_context()
            context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
            context["recent_actuals"].append(
                matched_actual("strength-upper-01", date="2026-08-14")
                | {"sport": "strength", "adaptation": "strength", "body_stress": "upper", "cost": "moderate"}
            )

            report = apply_reconciliation(state_dir, context)
            self.assertEqual("passed", report["status"])
            self.assertEqual(2, len(report["applied"]))
            versions = [entry["version"] for entry in report["applied"]]
            self.assertEqual([2, 3], versions)

            status = status_store(state_dir)
            self.assertEqual(3, status["current_version"])
            by_id = {
                s["session_id"]: s["match_status"]
                for s in status["current_plan"]["week"]["sessions"]
            }
            self.assertEqual("completed", by_id["run-quality-01"])
            self.assertEqual("completed", by_id["strength-upper-01"])
            self.assertEqual("planned", by_id["run-long-01"])

            refreshed = refresh_context_projection(context, status["current_plan"])
            rerun = apply_reconciliation(state_dir, refreshed)
            self.assertEqual([], rerun["applied"])
            self.assertEqual(3, status_store(state_dir)["current_version"])

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._store_with_plan(tmp)
            context = make_context()
            context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))

            report = apply_reconciliation(state_dir, context, dry_run=True)
            self.assertEqual(1, len(report["applied"]))
            self.assertTrue(report["applied"][0]["dry_run"])
            self.assertEqual(1, status_store(state_dir)["current_version"])

    def test_probable_and_unmatched_are_reported_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._store_with_plan(tmp)
            context = make_context()
            context["recent_actuals"].append(
                matched_actual("run-quality-01", date="2026-08-13", confidence="probable")
            )
            report = apply_reconciliation(state_dir, context)
            self.assertEqual([], report["applied"])
            self.assertTrue(any(a["session_id"] == "run-quality-01" for a in report["ambiguous"]))
            self.assertEqual(1, status_store(state_dir)["current_version"])

    def test_dry_run_validates_and_reports_a_bundle_that_would_fail(self):
        # dry_run's "would apply" answer must be one an actual run can honor -- a
        # backing actual with the wrong sport shows up as blocked, not as a promise.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._store_with_plan(tmp)
            context = make_context()
            bad = matched_actual("run-quality-01", date="2026-08-13")
            bad["sport"] = "strength"
            bad["adaptation"] = "strength"
            bad["body_stress"] = "full"
            context["recent_actuals"].append(bad)
            report = apply_reconciliation(state_dir, context, dry_run=True)
            self.assertEqual("blocked", report["status"])
            self.assertEqual(1, len(report["applied"]))
            self.assertEqual("blocked", report["applied"][0]["validation"])
            self.assertEqual(1, status_store(state_dir)["current_version"])

    def test_naive_now_still_produces_a_valid_event(self):
        import datetime as dt

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._store_with_plan(tmp)
            context = make_context()
            context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
            report = apply_reconciliation(
                state_dir, context, now=dt.datetime(2026, 8, 13, 21, 0, 0)
            )
            self.assertEqual(1, len(report["applied"]))
            self.assertEqual(2, status_store(state_dir)["current_version"])

    def test_failure_on_the_second_commit_keeps_the_first(self):
        # Append-only: a validation failure mid-run raises, earlier commits stand,
        # and the rerun proposes only what is still planned.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._store_with_plan(tmp)
            context = make_context()
            context["recent_actuals"].append(matched_actual("run-quality-01", date="2026-08-13"))
            second = matched_actual("strength-upper-01", date="2026-08-14") | {
                "sport": "strength",
                "adaptation": "strength",
                "body_stress": "upper",
                "cost": "moderate",
            }
            context["recent_actuals"].append(second)

            original_build = preview_original = None
            from garmin_coach_loop import reconcile as reconcile_module

            original_build = reconcile_module.build_reconcile_bundle

            def sabotage_second(plan, proposal, ctx, *, created_at):
                after, event = original_build(plan, proposal, ctx, created_at=created_at)
                if proposal["session_id"] == "strength-upper-01":
                    event["reason_codes"] = ["planned_actual_reconciled", "actual_load_above_plan"]
                return after, event

            with mock.patch.object(
                reconcile_module, "build_reconcile_bundle", side_effect=sabotage_second
            ):
                with self.assertRaises(StateStoreError):
                    reconcile_module.apply_reconciliation(state_dir, context)

            status = status_store(state_dir)
            self.assertEqual(2, status["current_version"])
            by_id = {
                s["session_id"]: s["match_status"]
                for s in status["current_plan"]["week"]["sessions"]
            }
            self.assertEqual("completed", by_id["run-quality-01"])
            self.assertEqual("planned", by_id["strength-upper-01"])

            refreshed = refresh_context_projection(context, status["current_plan"])
            rerun = apply_reconciliation(state_dir, refreshed)
            self.assertEqual(["strength-upper-01"], [e["session_id"] for e in rerun["applied"]])
            self.assertEqual(3, status_store(state_dir)["current_version"])


if __name__ == "__main__":
    unittest.main()
