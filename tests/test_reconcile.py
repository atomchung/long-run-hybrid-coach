from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import fit_fixtures
from garmin_coach_loop.context_builder import (
    ALL_DAYS,
    DEFAULT_SESSION_MINUTES,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    ContextRequest,
    build_context,
)
from garmin_coach_loop.reconcile import (
    apply_reconciliation,
    build_reconcile_bundle,
    propose_reconciliation,
)
from garmin_coach_loop.source_intervals import IntervalsCredentials, ProviderResponse
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
        "measurement_protocol": plan["goal"]["measurement_protocol"],
        "measurement": plan["goal"].get("measurement") or None,
    }
    context["measurement_evidence"] = None
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


def reduced_actual(
    session_id: str,
    *,
    date: str,
    sport: str = "running",
    duration_minutes: int = 48,
    completion: str = "completed",
    confidence: str = "owned",
    paired_event_id: str | None = None,
) -> dict:
    """The reconciliation identity a settled attachment leaves on the row (issue #240
    §1) -- what reconciliation actually receives for any elapsed session, since the
    projection reduces exactly those rows."""
    return {
        "activity_id": f"fixture-reduced-{session_id}",
        "date": date,
        "sport": sport,
        "planned_session_id": session_id,
        "match_confidence": confidence,
        "duration_minutes": duration_minutes,
        "completion": completion,
        "paired_event_id": paired_event_id,
    }


def attached_cycle_record(session_id: str, actual: dict, *, cost: str = "hard") -> dict:
    """The cycle_sessions record whose ``activity`` holds the reduced row's reading --
    the pair the builder always emits together, rebuilt here so a hand-built context
    carries the same invariant the validator now checks."""
    return {
        "session_id": session_id,
        "date": actual["date"],
        "week_start": "2026-08-10",
        "sport": actual["sport"],
        "cost": cost,
        "match_status": "planned",
        "planned_minutes": 45,
        "prescription": "fixture prescription",
        "activity": {
            "activity_id": actual["activity_id"],
            "match_confidence": actual["match_confidence"],
            "duration_minutes": actual["duration_minutes"],
            "distance_km": 10.0,
            "average_pace_sec_per_km": 285,
            "average_hr": 168.0,
            "subjective_feel": None,
            "elevation_gain_m": 40.0,
        },
        "activity_evidence": "attached",
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

    def test_probable_match_carries_enough_to_ask_in_one_sentence(self):
        # issue #30: a probable match used to carry only session_id and activity_id, so
        # asking a human meant a second lookup against the plan and the actual just to
        # form the question. The fixture session (plan-state-v1.json) is a 50-minute run
        # scheduled 2026-08-13; matched_actual fixes duration_minutes=42 and sport
        # "running" for every confidence level, this one included.
        plan = make_plan()
        context = make_context()
        context["recent_actuals"].append(
            matched_actual("run-quality-01", date="2026-08-13", confidence="probable")
        )

        report = propose_reconciliation(plan, context)

        entry = next(a for a in report["ambiguous"] if a["session_id"] == "run-quality-01")
        self.assertEqual("2026-08-13", entry["scheduled_date"])
        self.assertEqual("running", entry["sport"])
        self.assertEqual(50, entry["planned_minutes"])
        self.assertEqual(42, entry["actual_minutes"])
        # Only fields were added -- the write this session may still receive is exactly
        # as gated as test_probable_match_is_reported_not_written already pins.
        self.assertEqual([], report["proposals"])

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

    def test_an_owned_reduced_row_still_reconciles_and_still_re_derives(self):
        """Issue #240 §1: for any elapsed session the gate receives the reduced row,
        so the ownership re-derivation must run entirely on the reconciliation
        identity -- and it does, because that identity was chosen as exactly what the
        deterministic readers consume."""
        context = make_context()
        actual = reduced_actual("run-quality-01", date="2026-08-13")
        context["recent_actuals"].append(actual)
        context["cycle_sessions"] = list(context.get("cycle_sessions") or [])
        context["cycle_sessions"].append(attached_cycle_record("run-quality-01", actual))

        report = propose_reconciliation(self.plan, context)
        self.assertEqual(["run-quality-01"], [p["session_id"] for p in report["proposals"]])

        after, event = self._bundle(context)
        validation = validate_bundle(context, self.plan, after, event)
        self.assertEqual("passed", validation["status"], validation)

    def test_a_second_full_row_on_the_day_still_breaks_ownership_of_a_reduced_one(self):
        """The mixed shape the product actually emits: the attached row is reduced,
        the unmatched second run keeps its whole reading. Ownership counts both --
        date and sport survive reduction -- so the ambiguous day still refuses."""
        context = make_context()
        actual = reduced_actual("run-quality-01", date="2026-08-13")
        context["recent_actuals"].append(actual)
        context["cycle_sessions"] = list(context.get("cycle_sessions") or [])
        context["cycle_sessions"].append(attached_cycle_record("run-quality-01", actual))
        after, event = self._bundle(context)

        second = owned_actual("run-quality-01", date="2026-08-13", duration_minutes=20)
        second["activity_id"] = "fixture-owned-second-run"
        second["planned_session_id"] = None
        second["match_confidence"] = "unmatched"
        context["recent_actuals"].append(second)

        report = validate_bundle(context, self.plan, after, event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("paired provider event or product ownership" in e for e in report["errors"]),
            report["errors"],
        )

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

    def test_a_reported_symptom_does_not_stop_the_record_of_what_was_trained(self):
        """The false-positive control for #84's boundary, on the mechanical path.

        Reconciliation writes down what the athlete already did; it prescribes nothing.
        A symptomatic athlete must not lose the ability to have yesterday's run recorded
        -- and without this exemption they would, because the week still holds today's
        planned session and reconciliation cannot rest it.
        """
        context = copy.deepcopy(self.context)
        context["constraints"]["red_flags"]["chest_pain"] = True

        report = validate_bundle(context, self.plan, self.after, self.event)

        self.assertEqual([], report["errors"])
        self.assertEqual("passed", report["status"])

    def test_a_coaching_edit_wearing_the_reconcile_reason_code_is_still_blocked(self):
        """The exemption is for the mechanical transition, not for the code word.

        A bundle claiming the reconcile reason code while editing the week is refused by
        the reconcile semantics gate, so borrowing the code to escape the symptom
        boundary buys nothing.
        """
        context = copy.deepcopy(self.context)
        context["constraints"]["red_flags"]["chest_pain"] = True
        after = copy.deepcopy(self.after)
        for session in after["week"]["sessions"]:
            if session["session_id"] == "run-long-01":
                session["planned_minutes"] = 90

        report = validate_bundle(context, self.plan, after, self.event)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("match_status" in error for error in report["errors"]), report["errors"]
        )

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


# Synthetic credentials only -- never real key material (see test_intervals_source.py's
# FAKE_CREDENTIALS, the same value, already proven safe against
# scripts/check_repo_safety.py).
FAKE_INTERVALS_CREDENTIALS = IntervalsCredentials("synthetic-test-key-not-real", "i0")


class TrailRunEndToEndReconciliationTests(unittest.TestCase):
    """Issue #111 acceptance: "A TrailRun paired to a current session must participate
    in matching/reconciliation."

    Every other test in this file hand-builds ``context["recent_actuals"]`` and never
    calls ``source_intervals`` at all, so the vocabulary bug this issue fixes (a
    TrailRun's mapped sport being ``None``, which drops the row inside
    ``source_intervals._build_recent_actuals`` before ``context_core._match_actuals_to_
    plan`` -- the pairing logic reconcile.py itself relies on -- ever sees it) is
    invisible at that level; ``reconcile.py`` has no sport-mapping logic of its own to
    fix, and none is added here. This instead runs the real pipeline end to end: a
    provider ``TrailRun`` activity, through ``build_context``, into a reconcile
    proposal and a written decision -- exactly as it would run for an athlete whose GPS
    watch tags a trail session "TrailRun" instead of "Run".
    """

    NOW = dt.datetime(2026, 8, 13, 12, 0, 0, tzinfo=dt.timezone.utc)

    @staticmethod
    def _request() -> ContextRequest:
        return ContextRequest(
            as_of_raw="2026-08-13T20:00:00+08:00",
            timezone_name=DEFAULT_TIMEZONE,
            available_days=list(ALL_DAYS),
            session_minutes=DEFAULT_SESSION_MINUTES,
            red_flags={field: None for field in RED_FLAG_FIELDS},
            leg_fatigue="unknown",
            soreness="unknown",
            schedule_changed=None,
            equipment_changed=None,
            extra_unknowns=[],
        )

    @staticmethod
    def _fake_fetch(activities_payload: list[dict]):
        def fetch(request):
            return ProviderResponse(body_for(request))

        def body_for(request):
            # Suffix, not substring: the host is intervals.icu, so "/intervals"
            # appears in every URL this fake sees.
            if "/streams?" in request.full_url:
                # Shorter than the drift reader's floor, so it reports nothing for this run.
                return json.dumps(fit_fixtures.STREAMS_TOO_SHORT_FOR_DRIFT).encode("utf-8")
            if request.full_url.endswith("/file"):
                # A session whose file carries no sets: not an error, and not a parse failure.
                return fit_fixtures.fit_file_without_sets()
            if request.full_url.endswith("/intervals"):
                return json.dumps({"icu_intervals": []}).encode("utf-8")
            if "/activities" in request.full_url:
                return json.dumps(activities_payload).encode("utf-8")
            if "/wellness" in request.full_url:
                return json.dumps([]).encode("utf-8")
            if request.full_url.endswith("/sport-settings"):
                return json.dumps([]).encode("utf-8")
            raise AssertionError(f"unexpected intervals.icu URL in test: {request.full_url}")

        return fetch

    def _build_context_with_trailrun(self, state_dir: Path) -> dict:
        # run-quality-01 (the example plan's only actionable running session) is
        # scheduled 2026-08-13 with execution.external_id "event-run-quality-01" --
        # make_plan() (above) enriches every session with `event-{session_id}`.
        payload = [
            {
                "id": "trail-9001",
                "type": "TrailRun",
                "start_date_local": "2026-08-13T07:00:00",
                "moving_time": 3000,
                "distance": 8200.0,
                "average_speed": 2.73,
                "average_heartrate": 152,
                "paired_event_id": "event-run-quality-01",
                "feel": 3,
            }
        ]
        with mock.patch(
            "garmin_coach_loop.source_intervals.resolve_credentials",
            return_value=FAKE_INTERVALS_CREDENTIALS,
        ), mock.patch(
            "garmin_coach_loop.source_intervals._default_fetch",
            new=self._fake_fetch(payload),
        ):
            report = build_context(self._request(), state_dir=state_dir, source="intervals", now=self.NOW)
        self.assertEqual("passed", report["status"], report)
        return report["context"]

    def test_trailrun_activity_reaches_matched_confidence_through_the_real_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, make_plan())
            context = self._build_context_with_trailrun(state_dir)

            trail_run = next(
                a for a in context["recent_actuals"] if a["activity_id"] == "intervals:trail-9001"
            )
            self.assertEqual("running", trail_run["sport"])
            self.assertEqual("matched", trail_run["match_confidence"])
            self.assertEqual("run-quality-01", trail_run["planned_session_id"])

    def test_trailrun_activity_is_proposed_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, make_plan())
            context = self._build_context_with_trailrun(state_dir)

            plan = status_store(state_dir)["current_plan"]
            report = propose_reconciliation(plan, context)
            self.assertEqual(["run-quality-01"], [p["session_id"] for p in report["proposals"]])

    def test_trailrun_activity_reconciles_and_writes_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            init_store(state_dir, make_plan())
            context = self._build_context_with_trailrun(state_dir)

            result = apply_reconciliation(state_dir, context, now=self.NOW)
            self.assertEqual("passed", result["status"], result)
            self.assertEqual(["run-quality-01"], [entry["session_id"] for entry in result["applied"]])

            status = status_store(state_dir)
            by_id = {
                s["session_id"]: s["match_status"] for s in status["current_plan"]["week"]["sessions"]
            }
            self.assertEqual("completed", by_id["run-quality-01"])


if __name__ == "__main__":
    unittest.main()
