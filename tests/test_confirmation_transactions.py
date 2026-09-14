"""Issue #435: public prepared-transaction regressions from real producer output.

Apply bodies are copied from keys the matching prepare actually returned. A missing
required producer key fails the helper rather than being invented from the consumer
schema. Optional identity keys are copied only when the producer returned them.
The matching apply accepts only the signed proposal and confirmation.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import athlete_evidence, privacy_request
from garmin_coach_loop import gateway as gateway_module
from garmin_coach_loop.identity import lookup_or_create_owner, record_token_fingerprint
from garmin_coach_loop.mcp_transport import PROTOCOL_VERSION, RETIRED_TOOLS, TOOLS_BY_NAME
from garmin_coach_loop.privacy_request import PrivacyRequestError
from garmin_coach_loop.store import init_store, read_current_plan, resolve_state_dir
from schema_runtime import apply_body_from_prepare
from test_gateway import (
    HMAC_KEY,
    retained_transaction,
    ONBOARDING,
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    TOKEN_B,
    WEEKLY_CHANGE,
    as_change_request,
    onboarding,
    publishable_plan,
    unwritable,
)
from test_mcp_gateway import McpTestCase


EXCLUDED_PUBLIC_DELETION = ("prepareOwnerDeletion", "applyOwnerDeletion")
OPERATOR_ATHLETE = "i-anon-requester"
OPERATOR_EVIDENCE = "athlete-id-only"


def apply_from_prepare(
    prepared: dict[str, Any],
    apply_tool: str = "applyCoachDecision",
    *,
    confirmed: bool = True,
    extra_if_present: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build an apply body from producer output.

    The echo set is the matching apply tool's published input properties,
    filled from keys the prepare result actually returned. A missing required
    producer key fails rather than being invented. ``extra_if_present`` is
    only the #280 mutation: copy an identifier the apply schema does not
    declare, if this prepare actually returned it.
    """
    body = apply_body_from_prepare(prepared, apply_tool, confirmed=confirmed)
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


    def test_issue_280_first_plan_apply_from_producer_must_persist(self):
        """Intended repair for issues #280 and #435.

        Apply is proposal plus confirmed. Candidate plan_id / plan_version are
        copied only if this prepare actually returned them — they are not
        synthesized, and C must not add an ignore-or-bind compatibility path.
        Prepare omits non-durable identity; echoed business fields remain invalid.
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


    def test_first_plan_commits_frozen_bytes_without_projecting_again(self):
        prepared = self.prepare_first_plan()
        expected = copy.deepcopy(retained_transaction(self.gateway, self.owner_id, prepared["proposal"])["effect"]["plan"])
        prepared["preview"]["goal"]["outcome"] = "a client-side edit is not authority"
        with mock.patch.object(gateway_module, "project_initialization_request", side_effect=AssertionError("apply reprojected")):
            self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual(expected, read_current_plan(self.state_dir)["current_plan"])

    def test_tampered_first_plan_effect_validation_or_recovery_writes_nothing(self):
        prepared = self.prepare_first_plan()
        record = retained_transaction(self.gateway, self.owner_id, prepared["proposal"])
        original = copy.deepcopy(record)
        for section in ("effect", "validation", "recovery_inputs"):
            with self.subTest(section=section):
                record.clear()
                record.update(copy.deepcopy(original))
                if section == "effect":
                    record[section]["plan"]["goal"]["outcome"] = "unapproved goal"
                elif section == "validation":
                    record[section]["today"] = "2026-08-20"
                else:
                    record[section]["initialization_request"]["goal"]["outcome"] = "unapproved replacement preview"
                with mock.patch.object(gateway_module, "project_initialization_request", side_effect=AssertionError("corruption reprojected")):
                    result = self.apply_result("applyCoachDecision", apply_from_prepare(prepared))
                self.assertEqual("proposal_mismatch", self.tool_payload(result)["error"])
                self.assertFalse((self.state_dir / "store.json").exists())
        record.clear()
        record.update(original)
        self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual(original["effect"]["plan"], read_current_plan(self.state_dir)["current_plan"])

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


class FirstPlanAvailabilityReplayTests(PublicFlowCase):
    """Issue #281: first-plan availability survives the post-commit crash window.

    Every case here calls public ``applyCoachDecision`` with the producer proposal.
    Failure is injected after ``init_store`` returns, never by mocking
    ``_store_initial_availability``.
    """

    DAYS = ["mon", "wed", "sat"]

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)
        self.handshake()

    def prepare_first_plan_with_days(self, days: list[str] | None = None) -> dict[str, Any]:
        session = self.tool("startCoachSession", {"all_clear": True})
        self.assertEqual("no_plan_state", session["status"], session)
        prepared = self.tool(
            "prepareCoachDecision",
            {
                "change_request": as_change_request(
                    onboarding(availability={"days": list(days or self.DAYS), "equipment": ["dumbbells"]})
                )
            },
        )
        self.assertTrue(prepared.get("proposal"), prepared)
        return prepared

    def apply_http(self, prepared: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, _, body = self.post_mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "applyCoachDecision",
                    "arguments": apply_from_prepare(prepared),
                },
            }
        )
        return status, json.loads(body)

    def crash_after_init_store(self):
        real = gateway_module.init_store

        def crash(*args, **kwargs):
            result = real(*args, **kwargs)
            raise RuntimeError("injected crash after init_store")

        return mock.patch.object(gateway_module, "init_store", crash)

    def commit_names(self) -> list[str]:
        commits = self.state_dir / "commits"
        return sorted(
            path.name
            for path in commits.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )

    def recurring_days(self) -> list[str] | None:
        recurring = athlete_evidence.load_evidence(self.state_dir)["availability"]["recurring"]
        if not isinstance(recurring, dict):
            return None
        return list(recurring.get("available_days") or [])

    def test_crash_after_init_store_converges_on_the_exact_retry(self):
        """Catch: version-1 replay used to return success without storing the days."""
        prepared = self.prepare_first_plan_with_days()
        with self.crash_after_init_store():
            status, payload = self.apply_http(prepared)
        self.assertEqual(500, status, payload)
        self.assertEqual("internal_error", payload["error"], payload)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])
        self.assertEqual(["00000001-initial"], self.commit_names())
        self.assertIsNone(self.recurring_days())

        self.gateway._held.clear()
        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual("passed", replayed["status"], replayed)
        self.assertTrue(replayed["idempotent_replay"], replayed)
        self.assertNotIn("warnings", replayed)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])
        self.assertEqual(["00000001-initial"], self.commit_names())
        self.assertEqual(self.DAYS, self.recurring_days())

        session = self.tool("startCoachSession", {"all_clear": True})
        constraints = session["context"]["constraints"]
        self.assertEqual(self.DAYS, constraints["available_days"])
        self.assertEqual("athlete_evidence", constraints["availability_source"])

        recorded_at = athlete_evidence.load_evidence(self.state_dir)["availability"]["recurring"][
            "recorded_at"
        ]
        again = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(again["idempotent_replay"], again)
        self.assertEqual(self.DAYS, self.recurring_days())
        self.assertEqual(
            recorded_at,
            athlete_evidence.load_evidence(self.state_dir)["availability"]["recurring"][
                "recorded_at"
            ],
        )
        self.assertEqual(["00000001-initial"], self.commit_names())

    def test_retry_is_a_noop_when_initial_availability_was_already_stored(self):
        """Catch: a successful first apply's retry must not restamp athlete evidence."""
        prepared = self.prepare_first_plan_with_days()
        first = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertFalse(first["idempotent_replay"], first)
        evidence_before = (self.state_dir / "athlete-evidence.json").read_bytes()
        plan_before = self.snapshot(self.state_dir)

        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(replayed["idempotent_replay"], replayed)
        self.assertEqual(evidence_before, (self.state_dir / "athlete-evidence.json").read_bytes())
        self.assertEqual(plan_before, self.snapshot(self.state_dir))
        self.assertEqual(self.DAYS, self.recurring_days())

    def test_late_replay_does_not_overwrite_availability_the_athlete_changed(self):
        """Catch: blindly replaying init days after recordAthleteAvailability."""
        prepared = self.prepare_first_plan_with_days()
        self.tool("applyCoachDecision", apply_from_prepare(prepared))
        changed = self.tool(
            "recordAthleteAvailability",
            {"recurring": {"available_days": ["tue", "thu"]}},
        )
        self.assertEqual(["tue", "thu"], changed["recurring"]["available_days"])

        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(replayed["idempotent_replay"], replayed)
        self.assertEqual(["tue", "thu"], self.recurring_days())
        self.assertEqual(["00000001-initial"], self.commit_names())

    def test_late_replay_does_not_resurrect_days_after_a_week_override(self):
        """Catch: a week retraction after init must still stand on a late apply replay."""
        prepared = self.prepare_first_plan_with_days()
        self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.tool(
            "recordAthleteAvailability",
            {"week": {"unavailable_days": ["wed"], "note": "something came up Wednesday"}},
        )
        before = athlete_evidence.load_evidence(self.state_dir)["availability"]

        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(replayed["idempotent_replay"], replayed)
        after = athlete_evidence.load_evidence(self.state_dir)["availability"]
        self.assertEqual(before, after)
        self.assertEqual(self.DAYS, after["recurring"]["available_days"])
        self.assertEqual(1, len(after["week_overrides"]))

    def test_crash_then_athlete_change_then_retry_keeps_the_later_statement(self):
        """Catch: missing settlement plus a later athlete write is not 'never applied'."""
        prepared = self.prepare_first_plan_with_days()
        with self.crash_after_init_store():
            status, payload = self.apply_http(prepared)
        self.assertEqual(500, status, payload)
        self.gateway._held.clear()
        self.tool(
            "recordAthleteAvailability",
            {"recurring": {"available_days": ["tue", "thu"]}},
        )

        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(replayed["idempotent_replay"], replayed)
        self.assertEqual(["tue", "thu"], self.recurring_days())
        self.assertEqual(["00000001-initial"], self.commit_names())

    def test_persistence_failure_warns_and_the_exact_retry_still_converges(self):
        """Catch: a volume failure used to leave days unrestored even after retry."""
        prepared = self.prepare_first_plan_with_days()
        with unwritable("athlete-evidence.json"):
            applied = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual("passed", applied["status"], applied)
        self.assertFalse(applied["idempotent_replay"], applied)
        self.assertEqual(1, applied["plan_version"])
        self.assertTrue(
            any("available days were not stored" in item for item in applied.get("warnings") or []),
            applied,
        )
        self.assertIsNone(self.recurring_days())

        self.gateway._held.clear()
        replayed = self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertTrue(replayed["idempotent_replay"], replayed)
        self.assertNotIn("warnings", replayed)
        self.assertEqual(self.DAYS, self.recurring_days())
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])
        self.assertEqual(["00000001-initial"], self.commit_names())


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

    def test_warm_effect_and_event_commit_without_a_new_projection(self):
        prepared = self.prepare_week_change()
        effect = copy.deepcopy(retained_transaction(self.gateway, self.owner_id, prepared["proposal"])["effect"])
        with mock.patch.object(gateway_module, "project_change_request", side_effect=AssertionError("apply reprojected")):
            self.tool("applyCoachDecision", apply_from_prepare(prepared))
        stored = read_current_plan(self.state_dir)
        self.assertEqual(effect["after_plan"], stored["current_plan"])
        self.assertEqual(gateway_module.canonical_hash(effect["decision_event"]), stored["receipt"]["event_hash"])

    def test_warm_context_and_recovery_corruption_are_refused(self):
        prepared = self.prepare_week_change()
        record = retained_transaction(self.gateway, self.owner_id, prepared["proposal"])
        original = copy.deepcopy(record)
        before = self.snapshot(self.state_dir)
        for section in ("validation", "recovery_inputs"):
            with self.subTest(section=section):
                record.clear()
                record.update(copy.deepcopy(original))
                if section == "validation":
                    record[section]["context"]["constraints"]["red_flags"]["pain"] = True
                else:
                    record[section]["change_request"]["sessions"][0]["planned_minutes"] = 90
                result = self.apply_result("applyCoachDecision", apply_from_prepare(prepared))
                self.assertEqual("proposal_mismatch", self.tool_payload(result)["error"])
                self.assertEqual(before, self.snapshot(self.state_dir))
        record.clear()
        record.update(original)
        self.tool("applyCoachDecision", apply_from_prepare(prepared))
        self.assertEqual(original["effect"]["after_plan"], read_current_plan(self.state_dir)["current_plan"])


class DeliveryPublicFlowTests(PublicFlowCase):
    """Real public producers and fake provider read-back, not live acceptance."""

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
        with mock.patch.object(gateway_module, "prepare_delivery_set", side_effect=AssertionError("apply reprojected")):
            applied = self.tool(
                "applyWorkoutDelivery",
                apply_from_prepare(prepared, "applyWorkoutDelivery"),
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

    def test_standalone_withdrawal_commits_the_public_prepared_set(self):
        self.tool(
            "applyWorkoutDelivery",
            apply_from_prepare(self.prepare_one_run(), "applyWorkoutDelivery"),
        )
        original_id = self.fake.events[0]["id"]
        # Anonymous fixture: a previously committed plan superseded this delivery.
        import test_gateway as fixtures
        fixtures.GatewayWithdrawalTests._supersede(self)
        current = read_current_plan(self.state_dir)
        prepared = self.tool("prepareWorkoutDelivery", {
            "plan_id": current["plan_id"], "plan_version": current["current_version"],
            "session_ids": ["run-quality-01"], "withdraw": True,
        })
        self.assertEqual("5x1000m threshold", prepared["preview"][0]["event_name"])
        with mock.patch.object(gateway_module, "prepare_withdrawal_set", side_effect=AssertionError("apply reprojected")):
            result = self.tool(
                "applyWorkoutDelivery",
                apply_from_prepare(prepared, "applyWorkoutDelivery"),
            )
        self.assertEqual(["run-quality-01"], [row["session_id"] for row in result["withdrawn"]])
        self.assertEqual([original_id], self.fake.deleted)
        self.assertEqual([], self.fake.events)
        row = next(row for row in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"] if row["session_id"] == "run-quality-01")
        self.assertNotIn("superseded_external_id", row["execution"])

    def test_withdrawal_round_trip_persists_the_previewed_rest_and_clears_the_fake_event(self):
        """Separate calendar-removal effect, via the product's one-confirmation path.

        After a delivery, replacing the session with rest previews the withdrawal
        on prepareCoachDecision. Apply is proposal plus confirmed. The fake
        calendar is the test double, not a live Intervals acceptance.
        """
        delivered_prepare = self.prepare_one_run()
        self.tool(
            "applyWorkoutDelivery",
            apply_from_prepare(delivered_prepare, "applyWorkoutDelivery"),
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
