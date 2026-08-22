from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_gateway import (
    CLIENT_ID_VALUE,
    CLIENT_SECRET_VALUE,
    FakeIntervals,
    HMAC_KEY,
    NOW,
    TOKEN_A,
    publishable_plan,
)

from garmin_coach_loop.gateway import CoachGateway, GatewayConfig, GatewayError, ROUTES
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    record_token_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME
from garmin_coach_loop.store import history_store, init_store, read_current_plan, resolve_state_dir


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

    def test_tool_is_registered_with_an_output_schema_and_rest_route(self):
        descriptor = TOOLS_BY_NAME["confirmActivityMatch"].descriptor()

        self.assertEqual("activity_match", TOOLS_BY_NAME["confirmActivityMatch"].kind)
        self.assertEqual("object", descriptor["outputSchema"]["type"])
        self.assertIn("status", descriptor["outputSchema"]["properties"])
        self.assertEqual(
            ("POST", "activity_match"), ROUTES["/v1/coach/activity-match"]
        )

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


if __name__ == "__main__":
    unittest.main()
