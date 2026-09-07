from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_gateway import (
    CLIENT_ID_VALUE,
    CLIENT_SECRET_VALUE,
    FakeIntervals,
    HMAC_KEY,
    NOW,
    TOKEN_A,
    publishable_plan,
)

from garmin_coach_loop.context_core import (
    _apply_activity_match_confirmations, _apply_activity_match_denials, _match_actuals_to_plan,
)
from garmin_coach_loop.gateway import CoachGateway, GatewayConfig, GatewayError
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    record_token_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME
from garmin_coach_loop.reconcile import (
    activity_match_event_id, build_activity_match_bundle, propose_reconciliation,
)
from garmin_coach_loop.store import history_store, init_store, read_current_plan, resolve_state_dir
from garmin_coach_loop.validation import validate_bundle, validate_coach_context


class ActivityMatchGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity_db = self.root / "identity.db"
        self.fake = FakeIntervals(plan=publishable_plan())
        self.fake.activities = [
            {
                "id": "probable-1",
                "type": "Run",
                "start_date_local": "2026-08-13T06:00:00",
                "moving_time": 45 * 60,
                "distance": 7000,
                "average_speed": 7000 / (45 * 60),
            }
        ]
        self.owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        record_token_fingerprint(
            self.identity_db,
            token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
            self.owner_id,
            "intervals",
        )
        self.state_dir = resolve_state_dir(self.owner_id, state_root=self.root)
        init_store(self.state_dir, publishable_plan())
        self.gateway = CoachGateway(
            GatewayConfig(
                state_root=self.root,
                token_hmac_key=HMAC_KEY,
                intervals_client_id=CLIENT_ID_VALUE,
                intervals_client_secret=CLIENT_SECRET_VALUE,
            ),
            fetch=self.fake,
            now=lambda: NOW,
        )

    def session(self):
        return self.gateway.route("session", self.owner_id, TOKEN_A, {})

    def probable_pair(self):
        session = self.session()
        self.assertEqual("passed", session["status"])
        self.assertEqual(
            [
                {
                    "session_id": "run-quality-01",
                    "activity_id": "intervals:probable-1",
                    "scheduled_date": "2026-08-13",
                    "sport": "running",
                    "planned_minutes": 60,
                    "actual_minutes": 45,
                    "reason": (
                        "match_confidence is probable; a human confirms, "
                        "this tool does not guess"
                    ),
                }
            ],
            session["reconciliation"]["ambiguous"],
        )
        return session, {
            "session_id": "run-quality-01",
            "activity_id": "intervals:probable-1",
        }

    def resolve(self, *, confirmed: bool):
        _, pair = self.probable_pair()
        return self.gateway.route(
            "activity_match",
            self.owner_id,
            TOKEN_A,
            {**pair, "confirmed": confirmed},
        )

    def test_tool_is_registered_with_an_output_schema_and_gateway_handler(self):
        descriptor = TOOLS_BY_NAME["confirmActivityMatch"].descriptor()

        self.assertEqual("activity_match", TOOLS_BY_NAME["confirmActivityMatch"].kind)
        self.assertEqual("object", descriptor["outputSchema"]["type"])
        self.assertIn("status", descriptor["outputSchema"]["properties"])
        self.assertIn("activity_match", CoachGateway.route_kinds())

    def test_confirming_a_probable_pair_completes_only_that_session_and_records_athlete_evidence(self):
        result = self.resolve(confirmed=True)

        self.assertEqual("passed", result["status"])
        self.assertEqual(2, result["plan_version"])
        self.assertEqual("completed", result["match_status"])
        self.assertFalse(result["idempotent_replay"])
        current = read_current_plan(self.state_dir)
        session = next(
            item
            for item in current["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("completed", session["match_status"])
        event_path = sorted(self.state_dir.glob("commits/*/event.json"))[-1]
        event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(["athlete_confirmed_activity_match"], event["reason_codes"])
        self.assertEqual("athlete-confirmed", event["authored_by"]["model"])
        self.assertTrue(
            any("athlete-confirmed" in item["observation"] for item in event["evidence"])
        )

    def test_denial_leaves_both_sides_uncompleted_and_is_not_asked_again(self):
        result = self.resolve(confirmed=False)
        self.assertEqual(1, result["plan_version"])
        self.assertEqual("planned", result["match_status"])

        next_session = self.session()
        self.assertEqual([], next_session["reconciliation"]["ambiguous"])
        self.assertIn(
            {"session_id": "run-quality-01", "scheduled_date": "2026-08-13"},
            next_session["reconciliation"]["unmatched_planned"],
        )
        activity = next(
            item
            for item in next_session["context"]["recent_actuals"]
            if item["activity_id"] == "intervals:probable-1"
        )
        self.assertIsNone(activity["planned_session_id"])
        self.assertEqual("unmatched", activity["match_confidence"])
        current = read_current_plan(self.state_dir)["current_plan"]
        session = next(
            item
            for item in current["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("planned", session["match_status"])

    def late_activity(self):
        return {
            "id": "late-sync-2", "type": "Run", "start_date_local": "2026-08-13T18:00:00",
            "moving_time": 60 * 60, "distance": 5000, "average_speed": 5000 / (60 * 60),
        }

    def test_later_closer_duration_candidate_cannot_replace_the_confirmed_activity(self):
        self.resolve(confirmed=True)
        self.fake.activities.append(self.late_activity())
        self.gateway._now = lambda: NOW + dt.timedelta(days=1)

        response = self.session()

        actuals = {row["activity_id"]: row for row in response["context"]["recent_actuals"]}
        affirmed = actuals["intervals:probable-1"]
        self.assertEqual("run-quality-01", affirmed["planned_session_id"])
        self.assertEqual("athlete_confirmed", affirmed["match_confidence"])
        self.assertIsNone(actuals["intervals:late-sync-2"]["planned_session_id"])
        self.assertEqual("easy", actuals["intervals:late-sync-2"]["cost"])
        cycle = next(row for row in response["context"]["cycle_sessions"] if row["session_id"] == "run-quality-01")
        self.assertEqual("completed", cycle["match_status"])
        self.assertEqual("intervals:probable-1", cycle["activity"]["activity_id"])
        self.assertEqual("athlete_confirmed", cycle["activity"]["match_confidence"])
        self.assertEqual(45, cycle["activity"]["duration_minutes"])
        self.assertEqual(7, cycle["activity"]["distance_km"])
        self.assertEqual(386, cycle["activity"]["average_pace_sec_per_km"])
        self.assertEqual([], response["reconciliation"]["ambiguous"])
        self.assertEqual(2, read_current_plan(self.state_dir)["current_version"])
        self.assertEqual(response["context"], self.gateway._retained_context(self.owner_id, context_id=response["context_id"]))
        self.assertEqual("passed", validate_coach_context(response["context"])["status"])

    def test_missing_confirmed_activity_stays_unknown_instead_of_borrowing_late_actual(self):
        self.resolve(confirmed=True)
        self.fake.activities = [self.late_activity()]
        self.gateway._now = lambda: NOW + dt.timedelta(days=1)

        response = self.session()

        actual = response["context"]["recent_actuals"][0]
        self.assertIsNone(actual["planned_session_id"])
        cycle = next(row for row in response["context"]["cycle_sessions"] if row["session_id"] == "run-quality-01")
        self.assertIsNone(cycle["activity"])
        self.assertEqual("completed", cycle["match_status"])
        self.assertTrue(any("confirmed activity missing" in text for text in response["context"]["unknowns"]))
        self.assertEqual(2, read_current_plan(self.state_dir)["current_version"])
        self.assertEqual("passed", validate_coach_context(response["context"])["status"])

    def test_confirmed_projection_preserves_stronger_provider_identity_and_ownership(self):
        context, plan, _, _ = self.bundle()
        session = copy.deepcopy(next(row for row in plan["week"]["sessions"] if row["session_id"] == "run-quality-01"))
        session["execution"] = {"external_id": "event-quality", "delivery_state": "intervals_accepted", "publish_supported": True}
        source = {**context["recent_actuals"][0], "planned_session_id": None, "match_confidence": "unmatched"}
        pair = [{"session_id": session["session_id"], "activity_id": source["activity_id"]}]
        for confidence in ("matched", "owned"):
            with self.subTest(confidence=confidence, same_pair=True):
                actual = dict(source)
                actual["paired_event_id"] = "event-quality" if confidence == "matched" else None
                # Exactly one same-day 60-minute actual admits the owned match.
                actual["duration_minutes"] = 60
                matched = _match_actuals_to_plan([actual], [session])
                self.assertEqual(confidence, matched[0]["match_confidence"])
                unknowns = []
                projected = _apply_activity_match_confirmations(matched, [actual], [session], pair, unknowns)
                self.assertEqual(matched, projected)
                self.assertEqual([], unknowns)
            with self.subTest(confidence=confidence, same_pair=False):
                other = {**actual, "activity_id": "intervals:later-verified"}
                sources = [source, other] if confidence == "matched" else [other]
                matched = _match_actuals_to_plan(sources, [session])
                self.assertEqual(confidence, matched[-1]["match_confidence"])
                unknowns = []
                projected = _apply_activity_match_confirmations(matched, sources, [session], pair, unknowns)
                self.assertEqual(matched[-1], projected[-1])
                self.assertTrue(unknowns)
                if len(projected) == 2:
                    self.assertIsNone(projected[0]["planned_session_id"])

    def test_confirmed_projection_does_not_override_duplicate_or_foreign_provider_pairing(self):
        context, plan, _, _ = self.bundle()
        session = next(row for row in plan["week"]["sessions"] if row["session_id"] == "run-quality-01")
        source = {**context["recent_actuals"][0], "planned_session_id": None, "match_confidence": "unmatched"}
        pair = [{"session_id": session["session_id"], "activity_id": source["activity_id"]}]
        for sources in ([source, dict(source)], [{**source, "paired_event_id": "someone-else-event"}]):
            with self.subTest(sources=sources):
                matched = _match_actuals_to_plan(sources, [session])
                unknowns = []
                projected = _apply_activity_match_confirmations(matched, sources, [session], pair, unknowns)
                self.assertTrue(unknowns)
                self.assertTrue(all(row["planned_session_id"] is None for row in projected))

    def test_affirmation_cannot_claim_an_activity_now_paired_to_a_different_owned_session(self):
        context, plan, _, _ = self.bundle()
        session = copy.deepcopy(next(row for row in plan["week"]["sessions"] if row["session_id"] == "run-quality-01"))
        other_session = {**session, "session_id": "other-session", "execution": {"external_id": "other-event"}}
        source = {**context["recent_actuals"][0], "planned_session_id": None, "match_confidence": "unmatched", "paired_event_id": "other-event"}
        pair = [{"session_id": session["session_id"], "activity_id": source["activity_id"]}]
        matched = _match_actuals_to_plan([source], [session, other_session])
        unknowns = []

        projected = _apply_activity_match_confirmations(matched, [source], [session, other_session], pair, unknowns)

        self.assertEqual("matched", projected[0]["match_confidence"])
        self.assertEqual("other-session", projected[0]["planned_session_id"])
        self.assertTrue(any("conflicts with current provider" in text for text in unknowns))

    def test_athlete_confirmed_confidence_never_authorizes_automatic_completion(self):
        context, before, _, _ = self.bundle()
        context["recent_actuals"][0]["match_confidence"] = "athlete_confirmed"

        report = propose_reconciliation(before, context)

        self.assertEqual([], report["proposals"])

    def test_a_pair_not_currently_ambiguous_is_rejected_without_a_decision_event(self):
        self.session()
        before = copy.deepcopy(read_current_plan(self.state_dir))
        with self.assertRaises(GatewayError) as caught:
            self.gateway.route(
                "activity_match",
                self.owner_id,
                TOKEN_A,
                {
                    "session_id": "run-quality-01",
                    "activity_id": "intervals:not-the-activity",
                    "confirmed": True,
                },
            )

        self.assertEqual("activity_match_not_ambiguous", caught.exception.code)
        self.assertEqual(
            before["current_version"], read_current_plan(self.state_dir)["current_version"]
        )
        self.assertEqual(1, history_store(self.state_dir)["revision_count"])

    def test_repeating_the_same_confirmation_is_idempotent(self):
        first = self.resolve(confirmed=True)
        second = self.gateway.route(
            "activity_match",
            self.owner_id,
            TOKEN_A,
            {
                "session_id": first["session_id"],
                "activity_id": first["activity_id"],
                "confirmed": True,
            },
        )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["plan_version"], second["plan_version"])
        self.assertEqual(2, history_store(self.state_dir)["revision_count"])

    def bundle(self, *, confirmed=True):
        refreshed, pair = self.probable_pair()
        before = refreshed["plan_state"]["current_plan"]
        context = refreshed["context"]
        after, event = build_activity_match_bundle(
            before, context, **pair, confirmed=confirmed, created_at=NOW.isoformat()
        )
        return context, before, after, event

    def test_denial_is_idempotent_and_cannot_be_reconfirmed_as_still_ambiguous(self):
        first = self.resolve(confirmed=False)
        request = {"session_id": first["session_id"], "activity_id": first["activity_id"], "confirmed": False}
        replay = self.gateway.route("activity_match", self.owner_id, TOKEN_A, request)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(2, history_store(self.state_dir)["revision_count"])
        request["confirmed"] = True
        with self.assertRaises(GatewayError) as caught:
            self.gateway.route("activity_match", self.owner_id, TOKEN_A, request)
        self.assertEqual("activity_match_not_ambiguous", caught.exception.code)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_disappeared_provider_activity_is_not_completed_from_the_old_question(self):
        _, pair = self.probable_pair()
        self.fake.activities = []
        with self.assertRaises(GatewayError) as caught:
            self.gateway.route("activity_match", self.owner_id, TOKEN_A, {**pair, "confirmed": True})
        self.assertEqual("activity_match_not_ambiguous", caught.exception.code)
        self.assertEqual(1, history_store(self.state_dir)["revision_count"])

    def test_confirmation_replay_needs_no_provider_read(self):
        result = self.resolve(confirmed=True)
        with mock.patch.object(self.gateway, "fetch", side_effect=AssertionError("no read on replay")):
            replay = self.gateway.route("activity_match", self.owner_id, TOKEN_A, {
                "session_id": result["session_id"], "activity_id": result["activity_id"], "confirmed": True,
            })
        self.assertTrue(replay["idempotent_replay"])

    def test_denial_projection_is_exact_and_does_not_overrule_new_provider_identity(self):
        context, before, _, _ = self.bundle()
        denied = {activity_match_event_id(before["plan_id"], "run-quality-01", "intervals:probable-1", confirmed=False)}
        def project(context):
            return _apply_activity_match_denials(
                context["recent_actuals"], context["recent_actuals"], before["plan_id"], denied
            )

        for confidence in ("matched", "owned"):
            with self.subTest(confidence=confidence):
                attached = copy.deepcopy(context)
                attached["recent_actuals"][0]["match_confidence"] = confidence
                self.assertEqual(attached["recent_actuals"], project(attached))
        other = copy.deepcopy(context)
        other["recent_actuals"][0]["activity_id"] = "intervals:another"
        self.assertEqual(other["recent_actuals"], project(other))
        projected = project(context)
        self.assertEqual("probable", context["recent_actuals"][0]["match_confidence"])
        self.assertEqual("unmatched", projected[0]["match_confidence"])


    def test_denied_context_retained_for_a_decision_is_the_same_view_as_the_response(self):
        self.gateway._now = lambda: NOW + dt.timedelta(days=1)
        self.fake.activities[0]["distance"] = 4000
        self.fake.activities[0]["average_speed"] = 4000 / 2700
        self.resolve(confirmed=False)
        response = self.session()
        retained = self.gateway._retained_context(self.owner_id, context_id=response["context_id"])
        self.assertEqual(response["context"], retained)
        row = next(r for r in retained["cycle_sessions"] if r["session_id"] == "run-quality-01")
        self.assertIsNone(row["activity"])
        self.assertEqual("other_activity_same_day", row["activity_evidence"])
        actual = retained["recent_actuals"][0]
        self.assertEqual("easy", actual["cost"])
        self.assertEqual("aerobic_base", actual["adaptation"])
        self.assertEqual("passed", validate_coach_context(retained)["status"])

    def test_partial_actual_can_be_denied_but_cannot_claim_completed_work(self):
        context, before, _, _ = self.bundle()
        context["recent_actuals"][0]["completion"] = "partial"
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                after, event = build_activity_match_bundle(
                    before, context, session_id="run-quality-01", activity_id="intervals:probable-1",
                    confirmed=confirmed, created_at=NOW.isoformat(),
                )
                report = validate_bundle(context, before, after, event)
                self.assertEqual("blocked" if confirmed else "passed", report["status"], report)
                if confirmed:
                    self.assertTrue(any("requires the probable activity to be completed" in e for e in report["errors"]))

    def test_symptoms_and_unknown_optional_evidence_do_not_block_recording_past_work(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                context, before, after, event = self.bundle(confirmed=confirmed)
                context["constraints"]["red_flags"]["chest_pain"] = True
                self.assertTrue(context["unknowns"])
                report = validate_bundle(context, before, after, event)
                self.assertEqual("passed", report["status"], report)

    def test_missed_session_can_be_corrected_from_a_probable_pair(self):
        context, before, _, _ = self.bundle()
        for session in before["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session["match_status"] = "missed"
        for row in context["current_calendar"]:
            if row["session_id"] == "run-quality-01":
                row["status"] = "missed"
        for row in context["cycle_sessions"]:
            if row["session_id"] == "run-quality-01":
                row["match_status"] = "missed"
        after, event = build_activity_match_bundle(
            before, context, session_id="run-quality-01", activity_id="intervals:probable-1",
            confirmed=True, created_at=NOW.isoformat(),
        )
        report = validate_bundle(context, before, after, event)
        self.assertEqual("passed", report["status"], report)

    def test_resolution_cannot_smuggle_prescription_changes_through_symptom_exemption(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                context, before, after, event = self.bundle(confirmed=confirmed)
                context["constraints"]["red_flags"]["chest_pain"] = True
                after["week"]["sessions"][0]["planned_minutes"] += 10
                report = validate_bundle(context, before, after, event)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(any("athlete confirmation may change only" in e or "athlete denial must leave" in e for e in report["errors"]))

    def test_resolution_rejects_wrong_or_conflicting_activity_identity(self):
        original, before, after, event = self.bundle()
        for corruption in ("sport", "date", "duplicate_id", "duplicate_session", "unmatched", "fabricated_event_id"):
            with self.subTest(corruption=corruption):
                context, bad_event = copy.deepcopy(original), copy.deepcopy(event)
                actual = context["recent_actuals"][0]
                if corruption == "sport":
                    actual["sport"] = "cycling"
                elif corruption == "date":
                    actual["date"] = "2026-08-12"
                elif corruption.startswith("duplicate"):
                    extra = copy.deepcopy(actual)
                    if corruption == "duplicate_id":
                        extra["planned_session_id"] = None
                        extra["match_confidence"] = "unmatched"
                    else:
                        extra["activity_id"] = "intervals:another"
                    context["recent_actuals"].append(extra)
                elif corruption == "unmatched":
                    actual["match_confidence"] = "unmatched"
                else:
                    bad_event["event_id"] = "arbitrary-event"
                report = validate_bundle(context, before, after, bad_event)
                self.assertEqual("blocked", report["status"], report)
                self.assertTrue(any("athlete activity match resolution" in e for e in report["errors"]), report)

    def test_resolution_cannot_overwrite_a_completed_or_partial_outcome(self):
        original, plan, _, _ = self.bundle()
        for status in ("completed", "partial"):
            with self.subTest(status=status):
                context, before = copy.deepcopy(original), copy.deepcopy(plan)
                for session in before["week"]["sessions"]:
                    if session["session_id"] == "run-quality-01":
                        session["match_status"] = status
                for row in context["current_calendar"]:
                    if row["session_id"] == "run-quality-01":
                        row["status"] = "completed"
                after, event = build_activity_match_bundle(
                    before, context, session_id="run-quality-01", activity_id="intervals:probable-1",
                    confirmed=False, created_at=NOW.isoformat(),
                )
                report = validate_bundle(context, before, after, event)
                self.assertEqual("blocked", report["status"])
                self.assertTrue(any("requires an unresolved session" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
