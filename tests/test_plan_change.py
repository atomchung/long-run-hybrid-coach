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

    def test_a_purpose_only_keep_spends_a_version_without_touching_match_status(self):
        """A coach who finds a watch title unclear can reword it through ``keep`` -- the
        only operation that changes a session's content without also moving its
        schedule, structure, or match_status. Contrast ``move``/``replace`` above, where
        match_status itself supplies the material change; here purpose has to carry that
        weight alone."""
        before_purpose = self.sessions(self.before)["strength-upper-01"]["purpose"]
        reworded = self.project(
            coaching_request(
                sessions=[{
                    "operation": "keep",
                    "session_id": "strength-upper-01",
                    "purpose": "Hold upper-body strength while the legs recover",
                }]
            )
        )

        self.assertTrue(reworded["material_change"])
        self.assertEqual(2, reworded["after_plan"]["version"])
        self.assertEqual("adjust", reworded["decision_event"]["action"])
        session = self.sessions(reworded["after_plan"])["strength-upper-01"]
        self.assertEqual("Hold upper-body strength while the legs recover", session["purpose"])
        self.assertNotEqual(before_purpose, session["purpose"])
        self.assertEqual("planned", session["match_status"])
        report = validate_bundle(
            self.context, self.before, reworded["after_plan"], reworded["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

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

    def test_a_purpose_only_keep_on_a_delivered_session_withdraws_its_observation(self):
        """The gap ``test_retitling_a_delivered_session_withdraws_its_delivery_observation``
        does not reach: that one uses ``replace``, which also flips match_status to
        replaced -- itself enough to satisfy materiality. Here nothing else moves;
        match_status stays planned, so purpose alone has to carry both the materiality
        and the delivery-content reset."""
        before = self.delivered_plan()
        session = self.sessions(before)["run-long-01"]
        request = coaching_request(
            sessions=[{
                "operation": "keep",
                "session_id": "run-long-01",
                "purpose": "Build aerobic endurance on legs that are still tired",
            }]
        )

        projection = self.project(request, before)

        reworded = self.sessions(projection["after_plan"])["run-long-01"]
        self.assertEqual("planned", reworded["match_status"])
        self.assertEqual(session["plan"], reworded["plan"])
        self.assertNotEqual(session["purpose"], reworded["purpose"])
        self.assertEqual("not_published", reworded["execution"]["delivery_state"])
        self.assertIsNone(reworded["execution"]["external_id"])
        self.assertEqual("9001", reworded["execution"]["superseded_external_id"])
        self.assertTrue(reworded["execution"]["publish_supported"])
        self.assertTrue(projection["material_change"])
        self.assertEqual("adjust", projection["decision_event"]["action"])
        report = validate_bundle(
            self.context, before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

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


class CoachNoteThroughAChangeTests(PlanChangeTestCase):
    """Issue #56: the sentence the coach wants beside one session, on any operation.

    The case it exists for -- "this week's long run is deliberately short, do not add to
    it" -- is usually attached to a session the coach is *not* otherwise rewriting, which
    is why the field is optional on `keep` as well as on the four that change something.
    """

    NOTE = "這週故意排短，是為了下週的測試——不要自己加量"

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

    def test_a_keep_may_carry_a_note_without_moving_anything_else(self):
        request = coaching_request(
            sessions=[
                {"operation": "keep", "session_id": "run-long-01", "coach_note": self.NOTE}
            ]
        )

        projection = self.project(request)

        noted = self.sessions(projection["after_plan"])["run-long-01"]
        before = self.sessions(self.before)["run-long-01"]
        self.assertEqual(self.NOTE, noted["coach_note"])
        self.assertEqual("planned", noted["match_status"])
        self.assertEqual(before["plan"], noted["plan"])
        self.assertEqual(before["purpose"], noted["purpose"])
        self.assertEqual(before["planned_minutes"], noted["planned_minutes"])
        report = validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

    def test_every_operation_can_attach_one(self):
        session = self.sessions(self.before)["run-long-01"]
        operations = (
            {"operation": "keep", "session_id": "run-long-01"},
            {
                "operation": "move",
                "session_id": "run-long-01",
                "scheduled_date": "2026-08-15",
            },
            {
                "operation": "reduce",
                "session_id": "run-long-01",
                "planned_minutes": session["planned_minutes"] - 10,
                "plan": session["plan"],
            },
            {
                "operation": "replace",
                "session_id": "run-long-01",
                "purpose": session["purpose"],
                "adaptation": session["adaptation"],
                "cost": session["cost"],
                "planned_minutes": session["planned_minutes"],
                "plan": session["plan"],
            },
            {
                "operation": "add",
                "sport": "running",
                "scheduled_date": "2026-08-16",
                "purpose": "補一趟輕鬆跑",
                "adaptation": "aerobic_base",
                "body_stress": "lower",
                "cost": "easy",
                "priority": "optional",
                "planned_minutes": 30,
                "plan": EASY_RUN_WORKOUT,
                "fallback": {"action": "rest", "description": "太累就休息"},
            },
        )
        for operation in operations:
            with self.subTest(operation=operation["operation"]):
                request = coaching_request(
                    sessions=[{**operation, "coach_note": self.NOTE}]
                )

                projection = self.project(request)

                touched = [
                    item
                    for item in projection["after_plan"]["week"]["sessions"]
                    if item.get("coach_note") == self.NOTE
                ]
                self.assertEqual(1, len(touched))

    def test_null_takes_the_note_off_without_rewriting_the_session(self):
        """A note explaining a deliberate cutback outlives the cutback."""
        before = copy.deepcopy(self.before)
        self.sessions(before)["run-long-01"]["coach_note"] = self.NOTE
        request = coaching_request(
            sessions=[
                {"operation": "keep", "session_id": "run-long-01", "coach_note": None}
            ]
        )

        cleared = self.sessions(self.project(request, before)["after_plan"])["run-long-01"]

        self.assertNotIn("coach_note", cleared)

    def test_a_replace_keeps_the_note_it_was_not_asked_about(self):
        before = copy.deepcopy(self.before)
        session = self.sessions(before)["run-long-01"]
        session["coach_note"] = self.NOTE
        request = coaching_request(
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "run-long-01",
                    "purpose": session["purpose"],
                    "adaptation": session["adaptation"],
                    "cost": session["cost"],
                    "planned_minutes": session["planned_minutes"],
                    "plan": session["plan"],
                }
            ]
        )

        replaced = self.sessions(self.project(request, before)["after_plan"])["run-long-01"]

        self.assertEqual(self.NOTE, replaced["coach_note"])

    def test_a_note_only_keep_on_a_delivered_session_withdraws_its_observation(self):
        """The stale path, reached by the one operation that otherwise skips bookkeeping.

        The note is delivered content, so a session already on the calendar under the old
        one no longer describes what the plan says -- and `keep` is exactly the operation
        a note is most often attached to, which is why it has to be checked here rather
        than left to the four that change something.
        """
        before = self.delivered_plan()
        request = coaching_request(
            sessions=[
                {"operation": "keep", "session_id": "run-long-01", "coach_note": self.NOTE}
            ]
        )

        projection = self.project(request, before)

        noted = self.sessions(projection["after_plan"])["run-long-01"]
        self.assertEqual("planned", noted["match_status"])
        self.assertEqual("not_published", noted["execution"]["delivery_state"])
        self.assertIsNone(noted["execution"]["external_id"])
        self.assertEqual("9001", noted["execution"]["superseded_external_id"])
        self.assertTrue(projection["material_change"])
        report = validate_bundle(
            self.context, before, projection["after_plan"], projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

    def test_the_note_is_in_the_preview_the_athlete_confirms(self):
        request = coaching_request(
            sessions=[
                {"operation": "keep", "session_id": "run-long-01", "coach_note": self.NOTE}
            ]
        )

        projection = self.project(request)

        row = next(
            item
            for item in projection["preview"]["sessions"]
            if item["after"]["session_id"] == "run-long-01"
        )
        self.assertEqual(self.NOTE, row["after"]["coach_note"])
        self.assertIsNone(row["before"]["coach_note"])

    def test_an_empty_note_is_refused_rather_than_stored(self):
        request = coaching_request(
            sessions=[
                {"operation": "keep", "session_id": "run-long-01", "coach_note": "   "}
            ]
        )

        with self.assertRaises(ChangeRequestError) as caught:
            self.project(request)

        self.assertIn("coach_note", str(caught.exception))


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
    """What a change does to the movements a strength session prescribes (archived issue #100).

    Issue #100 protected six behaviours while `strength_movements` and `prescription`
    were two independently authored statements of the same session: the record could go
    stale against the sentence, so every operation had to be told when to drop it. Under
    archived issue #93 there is one statement. `plan` is assigned whole, `prescription` is rendered
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
        # What the athlete confirms is the record, not a paraphrase of it: archived issue #100 had
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
        """The RPE-only strength session archived issue #100 kept, still expressible here.

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
        archived issue #100 dropped the list whenever `prescription` was restated without it. There
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
        """The far end of archived issue #100's sport binding, which nothing else was watching.

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
        """Where archived issue #93 deliberately parts from archived issue #100's sport binding.

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

    def test_a_cross_training_session_plans_with_structure_and_does_not_publish(self):
        """A cycling session is a plannable answer, not a lie the schema forces.

        The coach substituting a ride for a run used to have two dishonest encodings:
        call it recovery (losing the quantified target) or call it running (polluting
        reconciliation and delivery). With the sport in the vocabulary it adopts under
        its own name, carries the same time-axis structure a run would, renders through
        the same kind-based renderer -- and stays undeliverable, because no delivery
        representation for it has been verified yet.
        """
        ride = {
            "kind": "time_axis",
            "name": "45 分鐘輕鬆騎",
            "steps": [
                {
                    "kind": "work",
                    "name": "輕鬆騎",
                    "duration": {"kind": "time", "seconds": 2700},
                    "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 150},
                }
            ],
        }
        request = coaching_request(
            sessions=[{
                "operation": "add",
                "sport": "cycling",
                "scheduled_date": "2026-08-15",
                "purpose": "膝蓋休兵，改用騎車維持有氧量",
                "adaptation": "aerobic_base",
                "body_stress": "lower",
                "cost": "easy",
                "priority": "flexible",
                "planned_minutes": 45,
                "plan": ride,
                "fallback": {"action": "rest", "description": "不舒服就直接休"},
            }]
        )

        projection = self.project(request)

        added = self.sessions(projection["after_plan"])["cycling-2026-08-15"]
        self.assertEqual(ride, added["plan"])
        self.assertFalse(added["execution"]["publish_supported"])
        self.assertTrue(added["prescription"].strip())
        self.assertEqual([], self.blocking_errors(projection))


class WeekRollTests(PlanChangeTestCase):
    """What happens to last week's sessions when the plan's one week moves on.

    The plan holds exactly one week, so the weekly review has to be expressible as a
    change request: a start that moves forward, the work still ahead moved with it, and
    the days already trained left behind. Without the last of those the projection kept
    every finished session in a week that no longer covered its date, which no operation
    could then repair -- `move` would have rescheduled training the athlete had already
    done, and there is deliberately no remove.
    """

    NEXT_WEEK = "2026-08-17"

    def roll(self, **overrides: Any) -> dict[str, Any]:
        request = coaching_request(
            week={"start": self.NEXT_WEEK, "intent": "下一週"},
            cycle={"outlook": self.before["cycle"]["outlook"][1:]},
            sessions=[{
                "operation": "add",
                "scheduled_date": "2026-08-18",
                "sport": "running",
                "purpose": "下一週的輕鬆跑",
                "adaptation": "aerobic_base",
                "cost": "easy",
                "priority": "flexible",
                "body_stress": "lower",
                "planned_minutes": 45,
                "plan": EASY_RUN_WORKOUT,
                "fallback": {"action": "reduce", "description": "縮短，心率上限不變"},
            }],
        )
        request.update(overrides)
        return request

    def test_the_days_the_new_week_has_passed_are_left_behind(self):
        projection = self.project(self.roll())

        after = projection["after_plan"]
        self.assertEqual(["running-2026-08-18"], list(self.sessions(after)))
        report = validate_bundle(
            self.context, self.before, after, projection["decision_event"]
        )
        self.assertEqual("passed", report["status"], report["errors"])

    def trained_week(self) -> dict[str, Any]:
        """The real case: a week where every session was delivered and then done."""
        before = copy.deepcopy(self.before)
        for session in before["week"]["sessions"]:
            session["match_status"] = "completed"
            session["execution"] = {
                **session["execution"],
                "delivery_state": "intervals_accepted",
                "external_id": f"ext-{session['session_id']}",
            }
        return before

    def test_a_finished_session_keeps_the_delivered_workout_it_was_written_with(self):
        """It leaves the week; it is not withdrawn from the athlete's calendar.

        The commit chain is where ``store.cycle_sessions`` reads a cycle's whole record
        back from, so the version that drops a session is what makes it history -- and
        history has to say what was delivered on the day.
        """
        projection = self.project(self.roll(), before=self.trained_week())

        after = projection["after_plan"]
        self.assertNotIn("run-long-01", self.sessions(after))
        for session in after["week"]["sessions"]:
            self.assertIsNone(session["execution"].get("superseded_external_id"))

    def test_the_preview_names_every_session_the_week_moved_past(self):
        """Otherwise a whole week retiring shows only as a falling minutes total.

        The reason there is no `remove` operation is that a session disappearing without
        the athlete being told is training the week lost that nobody decided to lose. A
        roll retires seven of them at once, so it owes the athlete the same account.
        """
        projection = self.project(self.roll(), before=self.trained_week())

        rolled = projection["preview"]["rolled_out"]
        self.assertEqual(
            list(self.sessions(self.before)), [session["session_id"] for session in rolled]
        )
        # Their last written state travels with them: what they were, and what was on the
        # calendar for them.
        long_run = next(item for item in rolled if item["session_id"] == "run-long-01")
        self.assertEqual("2026-08-16", long_run["scheduled_date"])
        self.assertEqual("completed", long_run["match_status"])
        self.assertEqual("intervals_accepted", long_run["delivery_state"])
        # And the event says it too, because the preview is not kept anywhere.
        narrative = projection["decision_event"]["change"]
        self.assertIn("run-long-01: rolled out of the week", narrative["after"])

    def test_a_roll_says_which_leaving_session_still_names_an_intervals_event(self):
        """The obligation that cannot follow a session out of the week.

        Withdrawal reads `week.sessions`, so once the week rolls past a session carrying
        `superseded_external_id`, nothing can name that Intervals event again. For the
        day it was delivered for that is the product's own rule -- a past day's record is
        never removed -- and for a week the roll skips over it is the last moment the
        event can still come off the calendar. Either way the athlete is told before
        confirming, instead of meeting the entry later with nothing to explain it.
        """
        before = self.trained_week()
        long_run = next(
            session for session in before["week"]["sessions"]
            if session["session_id"] == "run-long-01"
        )
        long_run["execution"]["superseded_external_id"] = "8801"

        rolled = self.project(self.roll(), before=before)["preview"]["rolled_out"]

        leaving = {item["session_id"]: item["unwithdrawn_external_id"] for item in rolled}
        self.assertEqual("8801", leaving["run-long-01"])
        # The control: every other session leaves owing nothing, and says so rather than
        # leaving the field out and making its absence ambiguous.
        self.assertEqual(
            {None}, {value for key, value in leaving.items() if key != "run-long-01"}
        )

    def test_a_session_this_request_operated_on_may_not_be_rolled_out(self):
        """The hazard the filter would otherwise open, refused with both dates.

        `reduce` leaves the session on its own day, which the new week has passed. Rolling
        it out would overrule an operation the request just made, and the decision to drop
        it would be recorded nowhere.
        """
        with self.assertRaises(ChangeRequestError) as raised:
            self.project(
                self.roll(
                    sessions=[{
                        "operation": "reduce",
                        "session_id": "run-long-01",
                        "planned_minutes": 30,
                        "plan": EASY_RUN_WORKOUT,
                    }]
                )
            )

        message = str(raised.exception)
        self.assertIn("run-long-01", message)
        self.assertIn("2026-08-16", message)
        self.assertIn(self.NEXT_WEEK, message)

    def test_work_still_ahead_moves_into_the_new_week_and_stays(self):
        """The control: only a date the new week passed leaves, never a moved session."""
        projection = self.project(
            self.roll(
                sessions=[{
                    "operation": "move",
                    "session_id": "run-long-01",
                    "scheduled_date": "2026-08-23",
                }]
            )
        )

        moved = self.sessions(projection["after_plan"])["run-long-01"]
        self.assertEqual("2026-08-23", moved["scheduled_date"])

    def test_a_week_that_does_not_move_keeps_every_session(self):
        """The other control: rewording the week is not a roll."""
        projection = self.project(
            coaching_request(week={"intent": "同一週，換個說法"})
        )

        self.assertEqual(
            list(self.sessions(self.before)), list(self.sessions(projection["after_plan"]))
        )


class WeekAndCycleAreSeparateDecisionsTests(PlanChangeTestCase):
    """Which decision a change is, and what that decides it may move.

    The mode is derived here rather than declared, and it used to be derived from the
    cycle: any cycle difference at all made a change a cycle decision. Every roll has
    one -- the outlook shortens by the week that just became precise -- so every roll
    was a cycle decision, and a cycle decision may move the 28-day direction freely. An
    athlete asking to roll their week forward could have `primary_adaptation` rewritten
    inside the same act, and the validation rule written to refuse exactly that never
    ran on anything the hosted entry could send.

    Deriving it from the week instead puts that rule back in the path, so these hold
    both halves: what a week decision may not carry, and that the cycle decision it is
    not is still expressible on its own.
    """

    NEXT_WEEK = "2026-08-17"

    def roll(self, **overrides: Any) -> dict[str, Any]:
        """The weekly review: the week moves on and the outlook it leaves shortens."""
        request = coaching_request(
            week={"start": self.NEXT_WEEK, "intent": "把上一週的輪廓變成這一週的精確課表"},
            cycle={"outlook": self.before["cycle"]["outlook"][1:]},
            sessions=[
                {
                    "operation": "move",
                    "session_id": session["session_id"],
                    "scheduled_date": (
                        dt.date.fromisoformat(session["scheduled_date"]) + dt.timedelta(days=7)
                    ).isoformat(),
                }
                for session in self.before["week"]["sessions"]
            ],
        )
        request.update(overrides)
        return request

    def projected(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        projection = self.project(request)
        return projection, validate_bundle(
            self.context, self.before, projection["after_plan"], projection["decision_event"]
        )

    def test_a_roll_is_a_week_decision_and_takes_the_outlook_with_it(self):
        projection, report = self.projected(self.roll())

        self.assertEqual("review_week", projection["decision_event"]["mode"])
        self.assertEqual("passed", report["status"], report["errors"])
        after_cycle = projection["after_plan"]["cycle"]
        self.assertEqual(
            ["2026-08-24", "2026-08-31"], [week["week_start"] for week in after_cycle["outlook"]]
        )
        for field in ("start", "end", "primary_adaptation", "maintenance_adaptation"):
            self.assertEqual(self.before["cycle"][field], after_cycle[field], field)

    def test_a_roll_may_not_carry_the_cycles_adaptation_with_it(self):
        """The failure this class exists for: threshold becomes vo2 on the way past.

        The errors are asserted whole rather than searched, because the test this
        replaces asserted a refusal it never earned -- its request moved every session
        into a week it had not moved, so seven date errors stood in for the rule.
        """
        projection, report = self.projected(
            self.roll(
                cycle={
                    "outlook": self.before["cycle"]["outlook"][1:],
                    "primary_adaptation": "vo2",
                }
            )
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            [
                "a change that moves this week may not also move the 28-day cycle "
                "beyond its outlook; a cycle change is its own decision"
            ],
            report["errors"],
        )
        self.assertEqual("review_week", projection["decision_event"]["mode"])

    def test_a_roll_may_not_carry_the_goal_with_it_either(self):
        projection, report = self.projected(
            self.roll(
                goal={
                    "outcome": "改練半馬完賽",
                    "measurement_protocol": self.before["goal"]["measurement_protocol"],
                }
            )
        )

        self.assertEqual(
            [
                "a change that moves this week may not also move the goal; "
                "a goal change is its own decision"
            ],
            report["errors"],
        )
        self.assertEqual("review_week", projection["decision_event"]["mode"])

    def test_the_cycles_adaptation_still_changes_as_a_decision_of_its_own(self):
        """The control: what the refusal above asks for is a decision that exists."""
        projection, report = self.projected(
            coaching_request(
                summary="改成 vo2，本週先不動",
                reason_codes=["goal_priority_changed"],
                cycle={"primary_adaptation": "vo2"},
            )
        )

        self.assertEqual("review_cycle", projection["decision_event"]["mode"])
        self.assertEqual("passed", report["status"], report["errors"])
        self.assertEqual("vo2", projection["after_plan"]["cycle"]["primary_adaptation"])

    def test_a_new_28_day_window_takes_the_week_with_it(self):
        """The exception, and it is structural: sessions are validated against the
        window they fall in, so a cycle that starts a new one has to move the week."""
        projection, report = self.projected(
            coaching_request(
                summary="開始下一個 28 天週期",
                reason_codes=["goal_priority_changed"],
                cycle={
                    "start": "2026-09-07",
                    "end": "2026-10-04",
                    "primary_adaptation": "vo2",
                    "outlook": [
                        {
                            "week_start": week_start,
                            "intent": "下一個週期的輪廓",
                            "key_sessions": ["一次 vo2 課", "一次長跑"],
                            "relation_to_primary": "累積 vo2 刺激",
                        }
                        for week_start in ("2026-09-14", "2026-09-21", "2026-09-28")
                    ],
                },
                week={"start": "2026-09-07", "intent": "新週期的第一週"},
                sessions=[
                    {
                        "operation": "add",
                        "scheduled_date": "2026-09-08",
                        "sport": "running",
                        "purpose": "新週期的第一次 vo2 課",
                        "adaptation": "vo2",
                        "cost": "hard",
                        "priority": "anchor",
                        "body_stress": "lower",
                        "planned_minutes": 45,
                        "plan": EASY_RUN_WORKOUT,
                        "fallback": {"action": "reduce", "description": "縮短，心率上限不變"},
                    }
                ],
            )
        )

        self.assertEqual("review_cycle", projection["decision_event"]["mode"])
        self.assertEqual("passed", report["status"], report["errors"])


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
    """A strength session can be revised, not only created (archived issue #92).

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


class BaselineChangeTests(PlanChangeTestCase):
    """The hosted vocabulary can move the baseline (issue #78).

    #77 made a baseline update an ordinary decision in any mode; without a vocabulary
    word the hosted coach could see the drift in `baseline_evidence` and not record
    the update. These tests hold the word to the same shape `cycle` set: partial,
    merge-on-top, and impossible to use as a silent eraser.
    """

    def baseline_request(self, **fields: Any) -> dict[str, Any]:
        return coaching_request(
            summary="把長跑基準更新到實際完成的距離",
            reason_codes=["actual_load_above_plan"],
            evidence=[{"field": "baseline_evidence", "observation": "8/14 完成 13.2 公里"}],
            athlete_baseline=fields,
        )

    def assertRefused(self, request: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(ChangeRequestError) as caught:
            self.project(request)
        self.assertIn(fragment, str(caught.exception))

    def test_one_scalar_moves_and_every_other_anchor_stays(self):
        projection = self.project(self.baseline_request(longest_recent_run_km=13.2))
        after = projection["after_plan"]
        expected = copy.deepcopy(self.before["athlete_baseline"])
        expected["longest_recent_run_km"] = 13.2
        self.assertEqual(expected, after["athlete_baseline"])
        self.assertTrue(projection["material_change"])
        self.assertEqual(self.before["version"] + 1, after["version"])

        event = projection["decision_event"]
        self.assertEqual("review_week", event["mode"])
        self.assertEqual("adjust", event["action"])
        self.assertIsNone(event["session_id"])
        report = validate_bundle(self.context, self.before, after, event)
        self.assertEqual("passed", report["status"], report)

    def test_the_preview_carries_the_anchor_change_to_confirm(self):
        projection = self.project(self.baseline_request(longest_recent_run_km=13.2))
        block = projection["preview"]["athlete_baseline"]
        self.assertEqual(12.0, block["before"]["longest_recent_run_km"])
        self.assertEqual(13.2, block["after"]["longest_recent_run_km"])
        self.assertIn("longest_recent_run_km: 12.0", projection["decision_event"]["change"]["before"])
        self.assertIn("longest_recent_run_km: 13.2", projection["decision_event"]["change"]["after"])

    def test_strength_upsert_touches_only_the_movement_it_names(self):
        projection = self.project(
            self.baseline_request(strength_loads=[{"exercise": "pull-up", "assist_kg": 12.0}])
        )
        loads = projection["after_plan"]["athlete_baseline"]["strength_loads"]
        by_name = {load["exercise"]: load for load in loads}
        self.assertEqual(12.0, by_name["pull-up"]["assist_kg"])
        self.assertIsNone(by_name["pull-up"]["load_kg"])
        self.assertEqual("3x8", by_name["pull-up"]["scheme"])  # kept, not restated
        self.assertEqual(
            self.before["athlete_baseline"]["strength_loads"][0], by_name["back squat"]
        )

    def test_naming_either_load_column_replaces_the_pair(self):
        """An assisted lift becoming a loaded one must not drag the old assistance
        along: the two columns are one measurement's two directions."""
        projection = self.project(
            self.baseline_request(strength_loads=[{"exercise": "pull-up", "load_kg": 5.0}])
        )
        loads = projection["after_plan"]["athlete_baseline"]["strength_loads"]
        pull_up = next(load for load in loads if load["exercise"] == "pull-up")
        self.assertEqual(5.0, pull_up["load_kg"])
        self.assertIsNone(pull_up["assist_kg"])

    def test_a_movement_not_yet_anchored_is_added_without_touching_the_rest(self):
        projection = self.project(
            self.baseline_request(
                strength_loads=[{"exercise": "romanian_deadlift", "load_kg": 40.0, "scheme": "3x10"}]
            )
        )
        loads = projection["after_plan"]["athlete_baseline"]["strength_loads"]
        self.assertEqual(3, len(loads))
        self.assertEqual(
            {"exercise": "romanian_deadlift", "load_kg": 40.0, "assist_kg": None, "scheme": "3x10"},
            loads[-1],
        )

    def test_null_is_refused_everywhere_a_measurement_belongs(self):
        """A model that pads fields with null must never silently wipe an anchor.
        Clearing a measurement back to unknown stays a local set-baseline act."""
        for fields, fragment in (
            ({"max_hr": None}, "must be an integer >= 1"),
            ({"longest_recent_run_km": None}, "must be a number >= 0"),
            (
                {"strength_loads": [{"exercise": "pull-up", "load_kg": None}]},
                "must carry a measured load_kg or assist_kg",
            ),
        ):
            with self.subTest(fields=fields):
                self.assertRefused(self.baseline_request(**fields), fragment)

    def test_an_empty_object_and_a_bare_exercise_name_change_nothing_and_say_so(self):
        self.assertRefused(
            self.baseline_request(), "must name at least one baseline field"
        )
        self.assertRefused(
            self.baseline_request(strength_loads=[{"exercise": "pull-up"}]),
            "an exercise name alone changes nothing",
        )


class EntryVocabularyCoverageTests(unittest.TestCase):
    """AGENTS.md 10: coaching capability is entry-agnostic.

    The tripwire that keeps the hosted vocabulary from drifting behind the product
    again (the way athlete_baseline did): every top-level PlanState field that is not
    mechanical store bookkeeping must have a change_request word. A new PlanState
    field fails here until someone decides -- vocabulary word, or explicitly
    mechanical -- instead of the hosted entry silently losing the capability.
    """

    def test_every_coaching_changeable_plan_field_has_a_vocabulary_word(self):
        from garmin_coach_loop.plan_change import _OPTIONAL_FIELDS

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "plan-state.schema.json")
            .read_text(encoding="utf-8")
        )
        # Owned by the store and the projection, never authored by a coaching request.
        mechanical = {"schema_version", "plan_id", "version", "status"}
        coaching_changeable = set(schema["properties"]) - mechanical
        uncovered = coaching_changeable - set(_OPTIONAL_FIELDS)
        self.assertEqual(
            set(),
            uncovered,
            "PlanState fields with no change_request vocabulary word: "
            f"{sorted(uncovered)} -- add a word or classify the field as mechanical here",
        )


if __name__ == "__main__":
    unittest.main()
