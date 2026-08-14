"""The projection from a coaching change request to the two product artifacts.

These are the properties the HTTP layer relies on and cannot restate: that nothing the
coach did not mention is re-authored, that the mechanical half is derived rather than
claimed, and that the same request always projects to the same bytes -- which is what
lets one confirmation bind a candidate the agent never holds.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.plan_change import ChangeRequestError, project_change_request
from garmin_coach_loop.prescription import render_prescription
from garmin_coach_loop.store import canonical_hash
from garmin_coach_loop.validation import validate_bundle


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "garmin-coach-loop-28-day"
ISSUED_AT = dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc)


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def coaching_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "summary": "調整本週安排",
        "reason_codes": ["schedule_or_equipment_changed"],
        "evidence": [{"field": "constraints", "observation": "本週行程改變"}],
        "goal_effect": {"week": "本週安排調整", "cycle": "28 天方向不變"},
        "next_review_condition": "下一次 anchor 前重新評估",
        "sessions": [],
    }
    request.update(overrides)
    return request


EASY_RUN_WORKOUT = {
    "kind": "time_axis",
    "name": "45 分鐘輕鬆跑",
    "steps": [
        {
            "kind": "work",
            "name": "輕鬆跑",
            "duration": {"kind": "time", "seconds": 2700},
            "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 150},
        }
    ],
}

# What a strength session prescribes, structured -- the half a change request could not
# carry before #92: `replace` and `add` had no field for it, so revising one lift meant
# writing prose, and the record the store keeps went stale on the first change.
PULL_UP_MOVEMENTS = {
    "kind": "movement_list",
    "movements": [
        {
            "exercise": "pull-up", "display_name": "引體向上", "sets": 3, "reps": 8, "load_kg": None,
            "assist_kg": 15.0, "load_basis": "measured_baseline",
        }
    ],
}


class PlanChangeTestCase(unittest.TestCase):
    def setUp(self):
        self.before = load("plan-state-v1.json")
        self.context = load("coach-context-day-4.json")

    def project(self, request: dict[str, Any], before: dict[str, Any] | None = None):
        return project_change_request(
            self.before if before is None else before,
            request,
            context=self.context,
            issued_at=ISSUED_AT,
        )

    def sessions(self, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {session["session_id"]: session for session in plan["week"]["sessions"]}


class CopiedMaterialTests(PlanChangeTestCase):
    def test_every_session_and_field_nobody_mentioned_is_copied_verbatim(self):
        request = coaching_request(
            sessions=[
                {
                    "operation": "reduce",
                    "session_id": "run-long-01",
                    "planned_minutes": 40,
                    "plan": EASY_RUN_WORKOUT,
                }
            ]
        )

        after = self.project(request)["after_plan"]

        before_sessions = self.sessions(self.before)
        after_sessions = self.sessions(after)
        for session_id, session in before_sessions.items():
            if session_id != "run-long-01":
                self.assertEqual(session, after_sessions[session_id], session_id)
        for field in ("plan_id", "schema_version", "status", "goal", "cycle", "athlete_baseline"):
            self.assertEqual(self.before[field], after[field], field)
        self.assertEqual(self.before["week"]["intent"], after["week"]["intent"])
        # And the reduced session kept everything the request did not name.
        changed = after_sessions["run-long-01"]
        untouched = ("purpose", "priority", "body_stress", "adaptation", "time_window", "fallback")
        for field in untouched:
            self.assertEqual(before_sessions["run-long-01"][field], changed[field], field)

    def test_the_same_request_projects_to_the_same_bytes_every_time(self):
        request = coaching_request(
            sessions=[{"operation": "move", "session_id": "rest-01", "scheduled_date": "2026-08-14"}]
        )

        first = self.project(request)
        second = self.project(request)

        self.assertEqual(canonical_hash(first["after_plan"]), canonical_hash(second["after_plan"]))
        self.assertEqual(
            canonical_hash(first["decision_event"]), canonical_hash(second["decision_event"])
        )

    def test_context_unknowns_survive_into_the_event(self):
        context = copy.deepcopy(self.context)
        context["unknowns"] = ["strength load for the new movement is not measured"]
        projection = project_change_request(
            self.before,
            coaching_request(unknowns=["athlete has not confirmed Saturday"]),
            context=context,
            issued_at=ISSUED_AT,
        )

        self.assertEqual(
            [
                "athlete has not confirmed Saturday",
                "strength load for the new movement is not measured",
            ],
            projection["decision_event"]["unknowns"],
        )


class DerivedMaterialTests(PlanChangeTestCase):
    def test_hard_and_publish_support_are_derived_from_the_session_itself(self):
        request = coaching_request(
            sessions=[
                {
                    "operation": "add",
                    "sport": "running",
                    "scheduled_date": "2026-08-12",
                    "purpose": "補一次輕鬆有氧",
                    "adaptation": "aerobic_base",
                    "body_stress": "lower",
                    "cost": "easy",
                    "priority": "flexible",
                    "planned_minutes": 45,
                    "plan": EASY_RUN_WORKOUT,
                    "fallback": {"action": "reduce", "description": "縮短但維持輕鬆"},
                }
            ]
        )

        after = self.project(request)["after_plan"]

        added = self.sessions(after)["running-2026-08-12"]
        self.assertFalse(added["hard"])
        self.assertEqual(
            {
                "publish_supported": True,
                "external_id": None,
                "delivery_state": "not_published",
            },
            added["execution"],
        )
        self.assertEqual("planned", added["match_status"])
        # It landed on its own day rather than at the end of the week.
        self.assertEqual(
            [
                "strength-full-01",
                "run-easy-01",
                "mobility-01",
                "running-2026-08-12",
                "run-quality-01",
                "strength-upper-01",
                "rest-01",
                "run-long-01",
            ],
            [session["session_id"] for session in after["week"]["sessions"]],
        )

    def test_a_strength_session_added_beside_a_run_publishes_as_a_calendar_entry(self):
        """Strength reaches the calendar as a titled entry, so an added strength day is
        deliverable on its purpose alone. Deriving publish support from the time axis a
        run is planned along said no to every strength session, which left the calendar
        entry path unreachable for anything these writers authored."""
        request = coaching_request(
            sessions=[
                {
                    "operation": "add",
                    "sport": "strength",
                    "scheduled_date": "2026-08-12",
                    "purpose": "補一次上肢",
                    "adaptation": "strength",
                    "body_stress": "upper",
                    "cost": "moderate",
                    "priority": "optional",
                    "planned_minutes": 30,
                    "plan": PULL_UP_MOVEMENTS,
                    "fallback": {"action": "reduce", "description": "減少組數"},
                }
            ]
        )

        added = self.sessions(self.project(request)["after_plan"])["strength-2026-08-12"]

        self.assertTrue(added["execution"]["publish_supported"])
        self.assertEqual("movement_list", added["plan"]["kind"])
        # And its prescription is the rendering of the movements it just gained (#92).
        self.assertEqual("引體向上 3x8 輔助15公斤", added["prescription"])

    def test_reducing_a_run_re_derives_whether_it_still_publishes(self):
        """A reduce hands the session a new structured workout, so its publish support is
        a different fact afterwards -- and the athlete would otherwise be refused delivery
        of a session that visibly carries one."""
        reduce_request = coaching_request(
            sessions=[
                {
                    "operation": "reduce",
                    "session_id": "run-long-01",
                    "planned_minutes": 40,
                    "plan": EASY_RUN_WORKOUT,
                }
            ]
        )
        self.assertFalse(
            self.sessions(self.before)["run-long-01"]["execution"]["publish_supported"]
        )

        projection = self.project(reduce_request)

        reduced = self.sessions(projection["after_plan"])["run-long-01"]
        self.assertTrue(reduced["execution"]["publish_supported"])
        report = validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

        # A move executes the same content on another day, so it settles nothing anew.
        moved = self.sessions(
            self.project(
                coaching_request(
                    sessions=[
                        {
                            "operation": "move",
                            "session_id": "run-long-01",
                            "scheduled_date": "2026-08-15",
                        }
                    ]
                )
            )["after_plan"]
        )["run-long-01"]
        self.assertFalse(moved["execution"]["publish_supported"])

    def test_a_version_is_spent_only_when_the_plan_actually_moved(self):
        frozen = self.project(
            coaching_request(sessions=[{"operation": "keep", "session_id": "run-long-01"}])
        )
        moved = self.project(
            coaching_request(
                sessions=[
                    {"operation": "move", "session_id": "rest-01", "scheduled_date": "2026-08-14"}
                ]
            )
        )

        self.assertFalse(frozen["material_change"])
        self.assertEqual(1, frozen["after_plan"]["version"])
        self.assertEqual("keep", frozen["decision_event"]["action"])
        self.assertIn("plan_kept_no_material_change", frozen["decision_event"]["reason_codes"])
        self.assertTrue(moved["material_change"])
        self.assertEqual(2, moved["after_plan"]["version"])
        self.assertEqual("adjust", moved["decision_event"]["action"])
        self.assertEqual("moved", self.sessions(moved["after_plan"])["rest-01"]["match_status"])

    def test_a_goal_or_cycle_change_becomes_a_cycle_review_and_merges_partially(self):
        projection = self.project(
            coaching_request(
                reason_codes=["goal_priority_changed"],
                cycle={"maintenance_adaptation": "hypertrophy"},
            )
        )

        self.assertEqual("review_cycle", projection["decision_event"]["mode"])
        cycle = projection["after_plan"]["cycle"]
        self.assertEqual("hypertrophy", cycle["maintenance_adaptation"])
        self.assertEqual(self.before["cycle"]["start"], cycle["start"])
        self.assertEqual(self.before["cycle"]["planned_evidence"], cycle["planned_evidence"])


class DeliveryBookkeepingTests(PlanChangeTestCase):
    def delivered_plan(self) -> dict[str, Any]:
        plan = copy.deepcopy(self.before)
        for session in plan["week"]["sessions"]:
            if session["session_id"] == "run-long-01":
                session["execution"] = {
                    "publish_supported": True,
                    "external_id": "9001",
                    "delivery_state": "intervals_accepted",
                }
        return plan

    def test_moving_a_delivered_session_withdraws_its_delivery_observation(self):
        before = self.delivered_plan()
        request = coaching_request(
            sessions=[
                {"operation": "move", "session_id": "run-long-01", "scheduled_date": "2026-08-15"}
            ]
        )

        projection = self.project(request, before)

        moved = self.sessions(projection["after_plan"])["run-long-01"]
        self.assertEqual("not_published", moved["execution"]["delivery_state"])
        self.assertIsNone(moved["execution"]["external_id"])
        self.assertTrue(moved["execution"]["publish_supported"])
        # The transition validation demands is satisfied without the caller knowing it.
        report = validate_bundle(
            self.context, before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

    def test_retitling_a_delivered_session_withdraws_its_delivery_observation(self):
        # purpose is delivered content, not a coaching label: a movement_list session
        # sends it as the calendar entry's own name. The projection holds it for every
        # sport, so a time_axis session pays one redelivery for a reword -- the cheap
        # side of that trade, and the side this fixture happens to sit on.
        before = self.delivered_plan()
        session = self.sessions(before)["run-long-01"]
        request = coaching_request(
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "run-long-01",
                    "purpose": "Build aerobic endurance on legs that are still tired",
                    "adaptation": session["adaptation"],
                    "cost": session["cost"],
                    "planned_minutes": session["planned_minutes"],
                    "plan": session["plan"],
                }
            ]
        )

        projection = self.project(request, before)

        retitled = self.sessions(projection["after_plan"])["run-long-01"]
        # Nothing the watch executes moved; only the title did.
        self.assertEqual(session["plan"], retitled["plan"])
        self.assertEqual(session["prescription"], retitled["prescription"])
        self.assertTrue(retitled["execution"]["publish_supported"])
        self.assertEqual("not_published", retitled["execution"]["delivery_state"])
        self.assertIsNone(retitled["execution"]["external_id"])
        report = validate_bundle(
            self.context, before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

    def test_a_delivered_session_that_only_gets_kept_keeps_its_observation(self):
        before = self.delivered_plan()
        request = coaching_request(
            sessions=[{"operation": "keep", "session_id": "run-long-01"}]
        )

        kept = self.sessions(self.project(request, before)["after_plan"])["run-long-01"]

        self.assertEqual("intervals_accepted", kept["execution"]["delivery_state"])
        self.assertEqual("9001", kept["execution"]["external_id"])

    def test_restating_a_delivered_strength_day_leaves_it_deliverable_again(self):
        """Withdrawing the observation is right -- the calendar entry no longer describes
        the day. Withdrawing publish support with it was not: the athlete was left with a
        strength session already on their calendar that delivery would then refuse to
        send at all, which is the state a live plan reaches on its first revision."""
        before = copy.deepcopy(self.before)
        for session in before["week"]["sessions"]:
            if session["session_id"] == "strength-upper-01":
                session["execution"] = {
                    "publish_supported": True,
                    "external_id": "9002",
                    "delivery_state": "intervals_accepted",
                }
        request = coaching_request(
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "strength-upper-01",
                    "purpose": "腿日",
                    "adaptation": "strength",
                    "cost": "moderate",
                    "planned_minutes": 45,
                    "plan": {
                        "kind": "movement_list",
                        "movements": [{
                            "exercise": "back squat", "display_name": "深蹲", "sets": 4,
                            "reps": 6, "load_kg": 70.0, "assist_kg": None,
                            "load_basis": "measured_baseline",
                        }],
                    },
                }
            ]
        )

        projection = self.project(request, before)

        replaced = self.sessions(projection["after_plan"])["strength-upper-01"]
        self.assertEqual("not_published", replaced["execution"]["delivery_state"])
        self.assertIsNone(replaced["execution"]["external_id"])
        self.assertTrue(replaced["execution"]["publish_supported"])
        report = validate_bundle(
            self.context, before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])


class RefusedRequestTests(PlanChangeTestCase):
    def assertRefused(self, request: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(ChangeRequestError) as caught:
            self.project(request)
        self.assertIn(fragment, str(caught.exception))

    def test_a_reduce_that_raises_the_number_it_reduces_is_refused(self):
        self.assertRefused(
            coaching_request(
                sessions=[
                    {
                        "operation": "reduce",
                        "session_id": "run-long-01",
                        "planned_minutes": 70,
                        "plan": EASY_RUN_WORKOUT,
                    }
                ]
            ),
            "not below its current 55",
        )

    def test_shortening_work_laid_out_along_time_needs_the_plan_that_matches(self):
        self.assertRefused(
            coaching_request(
                sessions=[
                    {"operation": "reduce", "session_id": "run-long-01", "planned_minutes": 40}
                ]
            ),
            "needs the plan that now matches it",
        )

    def test_one_session_may_not_carry_two_operations(self):
        self.assertRefused(
            coaching_request(
                sessions=[
                    {"operation": "keep", "session_id": "rest-01"},
                    {"operation": "move", "session_id": "rest-01", "scheduled_date": "2026-08-14"},
                ]
            ),
            "give each session one operation",
        )

    def test_a_mechanical_reason_code_cannot_be_claimed(self):
        for code in ("planned_actual_reconciled", "delivery_verified"):
            with self.subTest(code=code):
                self.assertRefused(coaching_request(reason_codes=[code]), "reason_codes[0]")

    def test_a_field_the_contract_does_not_carry_is_refused(self):
        self.assertRefused(
            coaching_request(after_plan={"version": 2}), "does not accept after_plan"
        )
        self.assertRefused(
            coaching_request(
                sessions=[
                    {"operation": "keep", "session_id": "rest-01", "match_status": "completed"}
                ]
            ),
            "does not accept match_status",
        )

    def test_the_coaching_half_is_required_in_full(self):
        request = coaching_request()
        request.pop("evidence")
        self.assertRefused(request, "is missing evidence")

    def test_an_operation_outside_the_vocabulary_is_refused(self):
        self.assertRefused(
            coaching_request(sessions=[{"operation": "remove", "session_id": "rest-01"}]),
            "must be one of add, keep, move, reduce, replace",
        )


class MovementRecordThroughAChangeTests(PlanChangeTestCase):
    """What a change does to the movements a strength session prescribes (issue #100).

    Issue #100 protected six behaviours while `strength_movements` and `prescription`
    were two independently authored statements of the same session: the record could go
    stale against the sentence, so every operation had to be told when to drop it. Under
    issue #93 there is one statement. `plan` is assigned whole, `prescription` is rendered
    from it after every operation, and validation refuses any stored sentence that is not
    that rendering -- so "stale" has no state to name. What is left to protect is that the
    right plan ends up stored, and these are the cases where that could go wrong.
    """

    STRENGTH_ID = "strength-upper-01"

    def entry(self, projection: dict[str, Any], session_id: str) -> dict[str, Any]:
        return next(
            item
            for item in projection["preview"]["sessions"]
            if item["session_id"] == session_id
        )

    def assertRefused(self, request: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(ChangeRequestError) as caught:
            self.project(request)
        self.assertIn(fragment, str(caught.exception))

    def blocking_errors(self, projection: dict[str, Any]) -> list[str]:
        report = validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )
        return report["errors"]

    # -- 1. a replace carries the movements it now prescribes --------------------------

    def test_replacing_a_strength_session_carries_the_movements_it_now_prescribes(self):
        request = coaching_request(
            sessions=[{
                "operation": "replace",
                "session_id": self.STRENGTH_ID,
                "purpose": "改成上拉為主",
                "adaptation": "strength",
                "cost": "moderate",
                "planned_minutes": 45,
                "plan": PULL_UP_MOVEMENTS,
            }]
        )

        projection = self.project(request)

        replaced = self.sessions(projection["after_plan"])[self.STRENGTH_ID]
        self.assertEqual(PULL_UP_MOVEMENTS, replaced["plan"])
        # What the athlete confirms is the record, not a paraphrase of it: issue #100 had
        # to show the list beside the sentence because the two were authored separately,
        # and here the sentence is generated from the very plan being adopted.
        self.assertEqual(
            render_prescription(PULL_UP_MOVEMENTS),
            self.entry(projection, self.STRENGTH_ID)["after"]["prescription"],
        )
        self.assertEqual("引體向上 3x8 輔助15公斤", replaced["prescription"])
        self.assertEqual([], self.blocking_errors(projection))

    # -- 2. a replace cannot restate nothing -------------------------------------------

    def test_a_replace_that_restates_no_plan_is_refused_rather_than_dropping_one(self):
        """Issue #100 dropped the list here; now the request never gets that far.

        `plan` is required on replace, so a replace that says nothing about what the
        session executes is a request error rather than a session quietly left with less
        structure than it had. The protection is the same one -- no session keeps a
        prescription nobody restated -- moved from a rule about what to discard to a rule
        about what a request must contain.
        """
        self.assertRefused(
            coaching_request(
                sessions=[{
                    "operation": "replace",
                    "session_id": self.STRENGTH_ID,
                    "purpose": "換成徒手循環",
                    "adaptation": "strength",
                    "cost": "moderate",
                    "planned_minutes": 40,
                }]
            ),
            "plan",
        )

    def test_a_replace_that_declares_no_structure_adopts_with_a_warning(self):
        """The RPE-only strength session issue #100 kept, still expressible here.

        Issue #100 named prose-only strength a supported path, because a sentence could
        prescribe by feel where a movement list had nothing to record. Issue #93 removed
        the sentence as an input, and the athlete's own direction (2026-08-14) keeps the
        blank available on the structured path: a strength session may decline
        quantification by declaring `unstructured`. Nothing of the old list survives,
        the rendered sentence says exactly that nothing is quantified, and adoption goes
        through with a warning naming what the blank costs -- not a refusal forcing a
        list the conversation never produced.
        """
        request = coaching_request(
            sessions=[{
                "operation": "replace",
                "session_id": self.STRENGTH_ID,
                "purpose": "換成徒手循環",
                "adaptation": "strength",
                "cost": "moderate",
                "planned_minutes": 40,
                "plan": {"kind": "unstructured"},
            }]
        )

        projection = self.project(request)

        replaced = self.sessions(projection["after_plan"])[self.STRENGTH_ID]
        self.assertEqual({"kind": "unstructured"}, replaced["plan"])
        self.assertEqual("不設定量化目標", replaced["prescription"])
        self.assertIn("plan", self.entry(projection, self.STRENGTH_ID)["changed_fields"])
        report = validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])
        self.assertIn(
            f"adopted strength session {self.STRENGTH_ID} declares no quantified "
            "structure; nothing is verified against the athlete's baseline",
            report["warnings"],
        )

    # -- 3. a replace onto another sport leaves no movement behind ---------------------

    def test_replacing_a_strength_session_with_a_run_leaves_no_movement_behind(self):
        request = coaching_request(
            sessions=[{
                "operation": "replace",
                "session_id": self.STRENGTH_ID,
                "sport": "running",
                "purpose": "改成輕鬆有氧",
                "adaptation": "aerobic_base",
                "cost": "easy",
                "body_stress": "lower",
                "planned_minutes": 45,
                "plan": EASY_RUN_WORKOUT,
            }]
        )

        replaced = self.sessions(self.project(request)["after_plan"])[self.STRENGTH_ID]

        self.assertEqual("running", replaced["sport"])
        self.assertEqual(EASY_RUN_WORKOUT, replaced["plan"])
        self.assertNotIn("movements", replaced["plan"])
        self.assertEqual("輕鬆跑 45分 心率上限 150 bpm", replaced["prescription"])

    # -- 4 and 5. a reduce keeps the record, or settles it anew ------------------------

    def test_a_minutes_only_reduce_keeps_the_movements_and_the_sentence(self):
        """A movement list carries no duration for shorter minutes to contradict.

        Contrast `test_shortening_work_laid_out_along_time_needs_the_plan_that_matches`:
        a time axis *is* the duration, so lowering the minutes without restating it leaves
        the structure describing a session that no longer exists.
        """
        before_session = self.sessions(self.before)[self.STRENGTH_ID]
        request = coaching_request(
            sessions=[{
                "operation": "reduce",
                "session_id": self.STRENGTH_ID,
                "planned_minutes": 40,
            }]
        )

        projection = self.project(request)

        reduced = self.sessions(projection["after_plan"])[self.STRENGTH_ID]
        self.assertEqual(before_session["plan"], reduced["plan"])
        self.assertEqual(before_session["prescription"], reduced["prescription"])
        # `execution` moves too: this fixture predates strength being publishable, so the
        # first operation to re-derive the flag corrects it -- and says so.
        self.assertEqual(
            ["execution", "planned_minutes"],
            self.entry(projection, self.STRENGTH_ID)["changed_fields"],
        )

    def test_a_reduce_that_changes_what_is_lifted_settles_the_record_anew(self):
        """Issue #100's "a reduce that rewrites the prescription" has no referent now.

        A reduce could rewrite the sentence and leave the movements behind, which is why
        issue #100 dropped the list whenever `prescription` was restated without it. There
        is no `prescription` on a request any more -- `StrengthStructureThroughAChangeTests`
        records that it is refused as an unexpected key -- so the only way to change what
        is lifted is to restate the plan, and the sentence follows it by construction.
        """
        before_session = self.sessions(self.before)[self.STRENGTH_ID]
        request = coaching_request(
            sessions=[{
                "operation": "reduce",
                "session_id": self.STRENGTH_ID,
                "planned_minutes": 40,
                "plan": PULL_UP_MOVEMENTS,
            }]
        )

        projection = self.project(request)

        reduced = self.sessions(projection["after_plan"])[self.STRENGTH_ID]
        self.assertEqual(PULL_UP_MOVEMENTS, reduced["plan"])
        self.assertEqual("引體向上 3x8 輔助15公斤", reduced["prescription"])
        self.assertNotEqual(before_session["prescription"], reduced["prescription"])
        self.assertEqual(
            ["execution", "plan", "planned_minutes", "prescription"],
            self.entry(projection, self.STRENGTH_ID)["changed_fields"],
        )

    # -- 6. lifted work belongs to a session that lifts --------------------------------

    def test_lifted_work_on_a_session_that_does_not_lift_is_refused(self):
        """Issue #100's refusal, checked against the plan rather than against the request.

        It was a request-shape rule: `strength_movements` was rejected unless the session's
        sport was strength. A request shape can only see the request, which is why issue
        #100 needed the rule on all three operations separately. Here the same claim is one
        fact about the plan being adopted -- a run is executed along a time axis, a lift as
        a list of movements -- so every route into a mismatch meets it, and so does the
        mirror case a request-shape rule never covered: a strength session claiming the
        structure a watch executes.
        """
        cases = {
            "add": ({
                "operation": "add",
                "sport": "running",
                "scheduled_date": "2026-08-15",
                "purpose": "補一次輕鬆有氧",
                "adaptation": "aerobic_base",
                "body_stress": "lower",
                "cost": "easy",
                "priority": "flexible",
                "planned_minutes": 45,
                "plan": PULL_UP_MOVEMENTS,
                "fallback": {"action": "reduce", "description": "縮短但維持輕鬆"},
            }, "running-2026-08-15", "time_axis"),
            "reduce": ({
                "operation": "reduce",
                "session_id": "run-long-01",
                "planned_minutes": 40,
                "plan": PULL_UP_MOVEMENTS,
            }, "run-long-01", "time_axis"),
            "replace": ({
                "operation": "replace",
                "session_id": "run-long-01",
                "purpose": "改成上拉為主",
                "adaptation": "strength",
                "cost": "moderate",
                "planned_minutes": 45,
                "plan": PULL_UP_MOVEMENTS,
            }, "run-long-01", "time_axis"),
            # The mirror: a lift cannot claim what a watch executes either.
            "replace-strength-with-a-time-axis": ({
                "operation": "replace",
                "session_id": self.STRENGTH_ID,
                "purpose": "改成跑步結構",
                "adaptation": "strength",
                "cost": "moderate",
                "planned_minutes": 45,
                "plan": EASY_RUN_WORKOUT,
            }, self.STRENGTH_ID, "movement_list"),
        }
        for label, (session_change, session_id, required) in cases.items():
            with self.subTest(operation=label):
                projection = self.project(coaching_request(sessions=[session_change]))

                self.assertIn(
                    f"session {session_id} must carry a {required} plan",
                    "\n".join(self.blocking_errors(projection)),
                )

    def test_a_rest_day_cannot_be_handed_the_movements_it_is_resting_from(self):
        """The far end of issue #100's sport binding, which nothing else was watching.

        Rest is outside every actionability filter the validator has -- it is the one
        sport with no execution to record -- so a movement list left on a rest day is read
        by no gate and rendered straight to the athlete as a set of lifts on the day the
        plan told them to stop. Issue #100 caught it as a request-shape rule; here it is a
        fact about a rest day, so it holds whichever operation produced one.
        """
        request = coaching_request(
            sessions=[{
                "operation": "replace",
                "session_id": "rest-01",
                "purpose": "改成上肢",
                "adaptation": "recovery",
                "cost": "easy",
                "planned_minutes": 20,
                "plan": PULL_UP_MOVEMENTS,
            }]
        )

        projection = self.project(request)

        # The projection stores what it was handed; adoption is what refuses it.
        self.assertEqual("引體向上 3x8 輔助15公斤",
                         self.sessions(projection["after_plan"])["rest-01"]["prescription"])
        self.assertIn(
            "rest session rest-01 must carry an unstructured plan",
            "\n".join(self.blocking_errors(projection)),
        )

    def test_a_session_this_product_does_not_train_may_still_be_a_list_of_movements(self):
        """Where issue #93 deliberately parts from issue #100's sport binding.

        Issue #100 refused a movement list on any session whose sport was not strength,
        because "anywhere else it is a second prescription nothing validates". That premise
        is gone: a movement list is validated wherever it appears, it renders the sentence
        the athlete reads, and its loads are anchored against the baseline by the same gate.
        So mobility planned as a list of movements is a session planned honestly, and the
        binding is kept only for the two sports the product does prescribe.
        """
        request = coaching_request(
            sessions=[{
                "operation": "add",
                "sport": "mobility",
                "scheduled_date": "2026-08-15",
                "purpose": "補一次上肢活動度",
                "adaptation": "recovery",
                "body_stress": "upper",
                "cost": "easy",
                "priority": "optional",
                "planned_minutes": 20,
                "plan": PULL_UP_MOVEMENTS,
                "fallback": {"action": "rest", "description": "太累就整段略過"},
            }]
        )

        projection = self.project(request)

        added = self.sessions(projection["after_plan"])["mobility-2026-08-15"]
        self.assertEqual(PULL_UP_MOVEMENTS, added["plan"])
        self.assertEqual("引體向上 3x8 輔助15公斤", added["prescription"])
        self.assertEqual([], self.blocking_errors(projection))


class ReplacementTests(PlanChangeTestCase):
    def test_replacing_a_run_with_strength_replaces_what_it_executes(self):
        request = coaching_request(
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "run-quality-01",
                    "sport": "strength",
                    "purpose": "把品質課換成下肢肌力",
                    "adaptation": "strength",
                    "cost": "moderate",
                    "body_stress": "lower",
                    "planned_minutes": 45,
                    "plan": {
                        "kind": "movement_list",
                        "movements": [{
                            "exercise": "back squat", "display_name": "深蹲", "sets": 4, "reps": 6, "load_kg": 70.0,
                            "assist_kg": None, "load_basis": "measured_baseline",
                        }],
                    },
                }
            ]
        )

        projection = self.project(request)

        replaced = self.sessions(projection["after_plan"])["run-quality-01"]
        self.assertEqual("movement_list", replaced["plan"]["kind"])
        self.assertEqual("深蹲 4x6 70公斤", replaced["prescription"])
        self.assertEqual("strength", replaced["sport"])
        self.assertEqual("replaced", replaced["match_status"])
        # The workout the watch would have executed is gone; the day still reaches the
        # calendar as the titled entry its new purpose describes.
        self.assertTrue(replaced["execution"]["publish_supported"])
        report = validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])


class StrengthStructureThroughAChangeTests(PlanChangeTestCase):
    """A strength session can be revised, not only created (issue #92).

    Before this, `SessionChange` exposed no field for a movement list. The only lawful
    way to change one lift was prose in `prescription`, so the structured record the
    store keeps went stale the first time a strength session changed -- structure set at
    birth and never after. `plan` closes that: the same object the initialization path
    carries, derived and validated the same way.
    """

    def assertRefused(self, request: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(ChangeRequestError) as caught:
            self.project(request)
        self.assertIn(fragment, str(caught.exception))

    def _revised(self, operation_fields: dict[str, Any]) -> dict[str, Any]:
        request = coaching_request(
            sessions=[{
                "operation": operation_fields.pop("operation"),
                "session_id": "strength-upper-01",
                **operation_fields,
            }]
        )
        return self.sessions(self.project(request)["after_plan"])["strength-upper-01"]

    def test_one_lift_can_be_changed_without_writing_a_sentence(self):
        revised = self._revised({
            "operation": "reduce",
            "planned_minutes": 30,
            "plan": {
                "kind": "movement_list",
                "movements": [{
                    "exercise": "bench press", "display_name": "臥推", "sets": 4, "reps": 5, "load_kg": None,
                    "assist_kg": None, "load_basis": "pending_confirmation",
                }],
            },
        })

        self.assertEqual(
            [("bench press", 4, 5)],
            [(m["exercise"], m["sets"], m["reps"]) for m in revised["plan"]["movements"]],
        )
        self.assertEqual("臥推 4x5 待確認", revised["prescription"])
        self.assertEqual(30, revised["planned_minutes"])

    def test_replacing_the_session_replaces_the_movements_it_prescribes(self):
        revised = self._revised({
            "operation": "replace",
            "purpose": "改成下肢",
            "adaptation": "strength",
            "cost": "moderate",
            "planned_minutes": 45,
            "plan": {
                "kind": "movement_list",
                "movements": [{
                    "exercise": "back squat", "display_name": "深蹲", "sets": 4, "reps": 6, "load_kg": 70.0,
                    "assist_kg": None, "load_basis": "measured_baseline",
                }],
            },
        })

        self.assertEqual("深蹲 4x6 70公斤", revised["prescription"])
        self.assertEqual("replaced", revised["match_status"])
        # It reaches the calendar as a title, never as executable structure -- and a
        # revision leaves it just as deliverable as the session it revised.
        self.assertTrue(revised["execution"]["publish_supported"])

    def test_the_revised_movements_are_what_the_evidence_gate_then_reads(self):
        """The structure a change carries is not decoration: it is what gets checked."""
        request = coaching_request(
            sessions=[{
                "operation": "reduce",
                "session_id": "strength-upper-01",
                "planned_minutes": 30,
                "plan": {
                    "kind": "movement_list",
                    "movements": [{
                        "exercise": "overhead press", "display_name": "肩推", "sets": 4, "reps": 5, "load_kg": 45.0,
                        "assist_kg": None, "load_basis": "measured_baseline",
                    }],
                },
            }]
        )
        projection = self.project(request)

        report = validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("'overhead press'" in error for error in report["errors"]), report["errors"]
        )

    def test_a_change_request_has_nowhere_to_write_a_prescription(self):
        # Not "must not" but "cannot": the field is absent from every operation, so an
        # authored sentence is refused as an unexpected key rather than quietly used.
        for operation_fields in (
            {"operation": "reduce", "session_id": "strength-upper-01",
             "planned_minutes": 30, "prescription": "臥推 5x5 100 公斤"},
            {"operation": "replace", "session_id": "strength-upper-01", "purpose": "x",
             "adaptation": "strength", "cost": "easy", "planned_minutes": 30,
             "plan": {"kind": "unstructured"}, "prescription": "臥推 5x5 100 公斤"},
        ):
            with self.subTest(operation=operation_fields["operation"]):
                self.assertRefused(
                    coaching_request(sessions=[operation_fields]),
                    "does not accept prescription",
                )


if __name__ == "__main__":
    unittest.main()
