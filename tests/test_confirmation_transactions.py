"""Issue #435 handoff A: public prepare/apply characterization from real producer output.

Apply bodies are copied from keys the matching prepare actually returned. A missing
required producer key fails the helper rather than being invented from the consumer
schema. Optional identity keys are copied only when the producer returned them.
No production behaviour is changed here.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop import athlete_evidence, privacy_request
from garmin_coach_loop.identity import lookup_or_create_owner, record_token_fingerprint
from garmin_coach_loop.mcp_transport import PROTOCOL_VERSION, RETIRED_TOOLS, TOOLS_BY_NAME
from garmin_coach_loop.privacy_request import PrivacyRequestError
from garmin_coach_loop.store import init_store, read_current_plan, resolve_state_dir
from test_gateway import (
    HMAC_KEY,
    ONBOARDING,
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    TOKEN_B,
    WEEKLY_CHANGE,
    as_change_request,
    publishable_plan,
)
from test_mcp_gateway import McpTestCase


UNWRAP_ENV = "ISSUE_435_UNWRAP_280"
EXCLUDED_PUBLIC_DELETION = ("prepareOwnerDeletion", "applyOwnerDeletion")
OPERATOR_ATHLETE = "i-anon-requester"
OPERATOR_EVIDENCE = "athlete-id-only"


def apply_from_prepare(
    prepared: dict[str, Any],
    *,
    token_key: str = "proposal",
    confirmed: bool = True,
    extra_if_present: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build an apply body from producer output.

    ``token_key`` is the opaque token the producer actually returned. The target
    public apply is ``proposal`` plus ``confirmed`` only; first-plan and warm
    prepare already use ``proposal``. Delivery prepare today returns
    ``proposal_hash`` — that current field is characterized separately, not
    chosen as an alternate target.
    """
    if token_key not in prepared or not prepared[token_key]:
        raise AssertionError(
            f"producer did not return {token_key!r}; refusing to synthesize it"
        )
    body: dict[str, Any] = {token_key: prepared[token_key], "confirmed": confirmed}
    for key in extra_if_present:
        if key in prepared and prepared[key] is not None:
            body[key] = prepared[key]
    return body


class PublicDeletionBoundaryTests(unittest.TestCase):
    """Retired public deletion tools stay out of the catalogue (issue #417)."""

    def test_public_deletion_tools_stay_retired_and_are_not_in_the_catalogue(self):
        for name in EXCLUDED_PUBLIC_DELETION:
            with self.subTest(tool=name):
                self.assertIn(name, RETIRED_TOOLS)
                self.assertNotIn(name, TOOLS_BY_NAME)


class PublicFlowCase(McpTestCase):
    """JSON-RPC tools/call, one handshake, apply built from prepare output.

    Helpers are local copies of the journey handshake, not a subclass of
    ``McpJourneyTests``: that class's own tests seed a plan and would collide
    with a cold-start fixture.
    """

    def setUp(self):
        super().setUp()
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]

    def handshake(self) -> None:
        response = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "issue-435-a", "version": "0"},
            },
        )
        self.assertEqual(PROTOCOL_VERSION, response["result"]["protocolVersion"])
        status, _, body = self.post_mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(202, status)
        self.assertEqual(b"", body)

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.tool_result(name, arguments)
        self.assertNotEqual(True, result.get("isError"), result)
        return self.tool_payload(result)

    def apply_result(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return self.tool_result(name, arguments, **kwargs)


class FirstPlanPublicFlowTests(PublicFlowCase):
    """Cold start: identity only, no PlanState."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)
        self.handshake()

    def prepare_first_plan(self) -> dict[str, Any]:
        session = self.tool("startCoachSession", {"all_clear": True})
        self.assertEqual("no_plan_state", session["status"], session)
        prepared = self.tool(
            "prepareCoachDecision",
            {"change_request": as_change_request(ONBOARDING)},
        )
        self.assertTrue(prepared["confirmation_required"], prepared)
        self.assertTrue(prepared.get("proposal"), prepared)
        return prepared

    def test_first_plan_prepare_still_exposes_candidate_plan_identity_today(self):
        """Current-behavior lock. Delete in C when prepare no longer returns these."""
        prepared = self.prepare_first_plan()
        self.assertIsInstance(prepared["plan_id"], str)
        self.assertTrue(prepared["plan_id"])
        self.assertEqual(1, prepared["plan_version"])
        self.assertFalse((self.state_dir / "store.json").exists())

    def test_first_plan_round_trip_persists_the_previewed_week(self):
        prepared = self.prepare_first_plan()
        preview = prepared["preview"]
        applied = self.tool(
            "applyCoachDecision", apply_from_prepare(prepared)
        )
        self.assertEqual("passed", applied["status"], applied)
        stored = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual(1, stored["version"])
        self.assertEqual(preview["goal"]["outcome"], stored["goal"]["outcome"])
        self.assertEqual(
            [item["scheduled_date"] for item in preview["sessions"]],
            [item["scheduled_date"] for item in stored["week"]["sessions"]],
        )
        self.assertEqual(
            [item["prescription"] for item in preview["sessions"]],
            [item["prescription"] for item in stored["week"]["sessions"]],
        )
        self.assertEqual(
            preview["weekly_planned_minutes"],
            sum(item["planned_minutes"] for item in stored["week"]["sessions"]),
        )

    def test_issue_280_echoed_ids_are_refused_on_public_mcp_today(self):
        """Current-behavior lock. Delete in C when prepare omits candidate ids."""
        prepared = self.prepare_first_plan()
        self.assertIn("plan_id", prepared)
        self.assertIn("plan_version", prepared)
        result = self.apply_result(
            "applyCoachDecision",
            apply_from_prepare(
                prepared, extra_if_present=("plan_id", "plan_version")
            ),
        )
        payload = self.tool_payload(result)
        self.assertTrue(result.get("isError"), result)
        self.assertEqual("invalid_request", payload["error"], payload)
        self.assertIn("this account has no plan yet", payload["detail"])
        self.assertIn("omit it to author the first plan", payload["detail"])
        self.assertFalse((self.state_dir / "store.json").exists())

    @unittest.expectedFailure
    def test_issue_280_first_plan_apply_from_producer_must_persist(self):
        """Intended repair for issues #280 and #435.

        Apply is proposal plus confirmed. Candidate plan_id / plan_version are
        copied only if this prepare actually returned them — they are not
        synthesized, and C must not add an ignore-or-bind compatibility path.
        After C, prepare omits those non-durable ids, this body is proposal plus
        confirmed only, and the previewed week persists. Today the producer
        still returns the ids, copying them is invalid_request, and this test
        is expected to fail. Unwrap with ISSUE_435_UNWRAP_280=1.
        """
        prepared = self.prepare_first_plan()
        preview = prepared["preview"]
        result = self.apply_result(
            "applyCoachDecision",
            apply_from_prepare(
                prepared, extra_if_present=("plan_id", "plan_version")
            ),
        )
        self.assertFalse(result.get("isError"), result)
        applied = self.tool_payload(result)
        stored = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual("passed", applied["status"], applied)
        self.assertEqual(preview["goal"]["outcome"], stored["goal"]["outcome"])
        self.assertEqual(
            [item["scheduled_date"] for item in preview["sessions"]],
            [item["scheduled_date"] for item in stored["week"]["sessions"]],
        )

    def test_issue_280_unwrapped_intended_repair(self):
        if os.environ.get(UNWRAP_ENV) != "1":
            self.skipTest(
                f"set {UNWRAP_ENV}=1 to unwrap the #280 intended-repair assertion"
            )
        prepared = self.prepare_first_plan()
        echoed = apply_from_prepare(
            prepared, extra_if_present=("plan_id", "plan_version")
        )
        result = self.apply_result("applyCoachDecision", echoed)
        self.assertFalse(
            result.get("isError"),
            "unwrapped #280 intended repair still red; producer="
            + json.dumps(
                {
                    "returned_plan_id": "plan_id" in prepared,
                    "returned_plan_version": "plan_version" in prepared,
                    "apply_keys": sorted(echoed),
                    "has_proposal": bool(prepared.get("proposal")),
                }
            )
            + " consumer="
            + json.dumps(self.tool_payload(result)),
        )

    def test_wrong_owner_cannot_apply_this_proposal(self):
        prepared = self.prepare_first_plan()
        other_id = self.seed_owner(TOKEN_B, athlete_id="i2")
        result = self.apply_result(
            "applyCoachDecision",
            apply_from_prepare(prepared),
            token=TOKEN_B,
        )
        payload = self.tool_payload(result)
        self.assertTrue(result.get("isError"), result)
        self.assertEqual("proposal_mismatch", payload["error"], payload)
        self.assertFalse((self.state_dir / "store.json").exists())
        self.assertFalse((self.owner_dir(other_id) / "store.json").exists())

    def test_unconfirmed_first_plan_writes_nothing(self):
        prepared = self.prepare_first_plan()
        result = self.apply_result(
            "applyCoachDecision",
            apply_from_prepare(prepared, confirmed=False),
        )
        payload = self.tool_payload(result)
        self.assertTrue(result.get("isError"), result)
        self.assertEqual("confirmation_required", payload["error"], payload)
        self.assertFalse((self.state_dir / "store.json").exists())

    def test_duplicate_identical_first_apply_replays(self):
        prepared = self.prepare_first_plan()
        first = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        committed = self.snapshot(self.state_dir)
        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(replayed["idempotent_replay"], replayed)
        self.assertEqual(first["plan_id"], replayed["plan_id"])
        self.assertEqual(committed, self.snapshot(self.state_dir))
        stored = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual(prepared["preview"]["goal"]["outcome"], stored["goal"]["outcome"])


class WarmPlanPublicFlowTests(PublicFlowCase):
    """Existing PlanState: prepare against startCoachSession, apply from prepare output."""

    def setUp(self):
        super().setUp()
        self.before = publishable_plan()
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.before)
        self.state_dir = self.owner_dir(self.owner_id)
        self.handshake()

    def prepare_week_change(self) -> dict[str, Any]:
        session = self.tool("startCoachSession", {"all_clear": True})
        prepared = self.tool(
            "prepareCoachDecision",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
        )
        self.assertTrue(prepared["confirmation_required"], prepared)
        return prepared

    def test_warm_round_trip_persists_the_previewed_replacement(self):
        prepared = self.prepare_week_change()
        preview = prepared["preview"]
        applied = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual("passed", applied["status"], applied)
        stored = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual(2, stored["version"])
        replaced = next(
            item
            for item in stored["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual(45, replaced["planned_minutes"])
        self.assertEqual(WEEKLY_CHANGE["sessions"][0]["purpose"], replaced["purpose"])
        preview_row = next(
            item
            for item in preview["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual(preview_row["after"]["prescription"], replaced["prescription"])
        self.assertEqual(preview_row["after"]["planned_minutes"], replaced["planned_minutes"])


class DeliveryPublicFlowTests(PublicFlowCase):
    """Current delivery prepare still names the opaque token ``proposal_hash``.

    That is today's public field, characterized here. The target apply is
    ``proposal`` plus ``confirmed`` only; C renames the field once. These tests
    do not treat the FakeIntervals double as a live provider acceptance.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)
        self.handshake()

    def prepare_one_run(self) -> dict[str, Any]:
        session = self.tool("startCoachSession", {"all_clear": True})
        prepared = self.tool(
            "prepareWorkoutDelivery",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
        )
        self.assertTrue(prepared["confirmation_required"], prepared)
        return prepared

    def test_delivery_round_trip_records_the_previewed_session_on_the_plan(self):
        prepared = self.prepare_one_run()
        self.assertEqual(
            ["run-quality-01"],
            [row["session_id"] for row in prepared["preview"]],
        )
        applied = self.tool(
            "applyWorkoutDelivery",
            apply_from_prepare(prepared, token_key="proposal_hash"),
        )
        self.assertEqual(
            ["run-quality-01"],
            [item["session_id"] for item in applied["delivered"]],
        )
        stored = read_current_plan(self.state_dir)["current_plan"]
        execution = next(
            item["execution"]
            for item in stored["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("intervals_accepted", execution["delivery_state"])
        self.assertTrue(execution.get("external_id"))

    def test_withdrawal_round_trip_persists_the_previewed_rest_and_clears_the_fake_event(self):
        """Separate calendar-removal effect, via the product's one-confirmation path.

        After a delivery, replacing the session with rest previews the withdrawal
        on prepareCoachDecision. Apply is proposal plus confirmed. The fake
        calendar is the test double, not a live Intervals acceptance.
        """
        delivered_prepare = self.prepare_one_run()
        self.tool(
            "applyWorkoutDelivery",
            apply_from_prepare(delivered_prepare, token_key="proposal_hash"),
        )
        current = self.tool("startCoachSession", {"all_clear": True})
        prepared = self.tool(
            "prepareCoachDecision",
            {
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "context": {"context_id": current["context"]["context_id"]},
                "change_request": {
                    "summary": "改成完全休息",
                    "reason_codes": ["multi_signal_recovery_down"],
                    "evidence": [
                        {
                            "field": "recovery_trends.hrv",
                            "observation": "HRV 連三天偏低",
                        }
                    ],
                    "goal_effect": {
                        "week": "本週少一次刺激",
                        "cycle": "28 天方向不變",
                    },
                    "next_review_condition": "休息後重新評估",
                    "sessions": [
                        {
                            "operation": "replace",
                            "session_id": "run-quality-01",
                            "sport": "rest",
                            "purpose": "完全休息",
                            "adaptation": "recovery",
                            "cost": "easy",
                            "planned_minutes": 0,
                            "plan": {"kind": "unstructured"},
                        }
                    ],
                },
            },
        )
        withdrawals = prepared["preview"]["calendar_delivery"]["withdrawals"]
        self.assertEqual("run-quality-01", withdrawals[0]["session_id"])
        self.assertEqual(1, len(self.fake.events))
        applied = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual(
            [{"session_id": "run-quality-01"}],
            applied["calendar_delivery"]["withdrawn"],
        )
        stored = read_current_plan(self.state_dir)["current_plan"]
        rest = next(
            item
            for item in stored["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("rest", rest["sport"])
        self.assertEqual(0, rest["planned_minutes"])
        self.assertEqual("完全休息", rest["purpose"])
        self.assertEqual([], self.fake.events)
        # Same proposal again: the compound plan+calendar approval is already
        # committed. Partial-calendar resume without a second yes is
        # `test_decision_delivery.CombinedDecisionJourneyTests`.
        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual("passed", replayed["status"], replayed)
        self.assertEqual(
            "rest",
            next(
                item["sport"]
                for item in read_current_plan(self.state_dir)["current_plan"]["week"][
                    "sessions"
                ]
                if item["session_id"] == "run-quality-01"
            ),
        )


class OperatorDeletionCharacterizationTests(unittest.TestCase):
    """Operator preview → exact scope confirmation → commit → absence.

    Excluded from the public MCP catalogue. Equivalent confirmation-bound pair
    on the operator CLI path (issue #417). Isolated anonymous local owner.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name) / "coach-state"
        self.state_root.mkdir(parents=True)
        self.identity_db = self.state_root / "identity.db"
        self.owner_id = lookup_or_create_owner(
            self.identity_db, "intervals", OPERATOR_ATHLETE
        )
        record_token_fingerprint(
            self.identity_db, "fp-anon-requester", self.owner_id, "intervals"
        )
        self.state_dir = resolve_state_dir(
            self.owner_id, state_root=self.state_root
        )
        init_store(self.state_dir, publishable_plan())
        athlete_evidence.record_profile(
            self.state_dir, timezone="Asia/Taipei", language="zh-Hant"
        )

    def preview(self) -> dict[str, Any]:
        return privacy_request.deletion_scope(
            self.state_root,
            self.identity_db,
            athlete_id=OPERATOR_ATHLETE,
            identity_evidence=OPERATOR_EVIDENCE,
            hmac_key=HMAC_KEY,
        )

    def test_preview_output_commits_erasure_and_read_back_is_absent(self):
        prepared = self.preview()
        self.assertTrue(prepared.get("scope_digest"))
        self.assertEqual(
            "fixture-plan-001", prepared["removes"]["plan_id"]
        )
        self.assertFalse(prepared["reversible"])
        erased = privacy_request.apply_deletion(
            self.state_root,
            self.identity_db,
            athlete_id=OPERATOR_ATHLETE,
            identity_evidence=OPERATOR_EVIDENCE,
            now=dt.datetime.now(dt.timezone.utc),
            hmac_key=HMAC_KEY,
            scope_digest=prepared["scope_digest"],
            confirmed=True,
        )
        self.assertTrue(erased["deleted"])
        checked = erased["verified_after_deletion"]
        self.assertEqual("verified", checked["status"])
        self.assertTrue(checked["state_directory_absent"])
        self.assertTrue(checked["deletion_tombstone"])
        self.assertFalse((self.state_dir / "store.json").is_file())

    def test_a_scope_that_moved_after_preview_is_refused(self):
        prepared = self.preview()
        athlete_evidence.record_body_measurement(
            self.state_dir,
            weight_kg=72.1,
            timezone_name="Asia/Taipei",
        )
        with self.assertRaises(PrivacyRequestError) as caught:
            privacy_request.apply_deletion(
                self.state_root,
                self.identity_db,
                athlete_id=OPERATOR_ATHLETE,
                identity_evidence=OPERATOR_EVIDENCE,
                now=dt.datetime.now(dt.timezone.utc),
                hmac_key=HMAC_KEY,
                scope_digest=prepared["scope_digest"],
                confirmed=True,
            )
        self.assertIn("the account changed after the requester", str(caught.exception))
        self.assertTrue((self.state_dir / "store.json").is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
