"""Internal frozen-record controls; public prepare/apply migration belongs to C."""
from __future__ import annotations

import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop.gateway import (
    CoachGateway, GatewayConfig, GatewayError, HELD_TRANSACTION,
    RETAINED_CONTEXTS_PER_OWNER, CONTEXT_RETENTION_SECONDS,
    _decision_claims, _initialization_claims,
)
from garmin_coach_loop.plan_change import project_change_request
from garmin_coach_loop.plan_init import project_initialization_request
from garmin_coach_loop.proposals import ProposalError, PROPOSAL_TTL_SECONDS
from garmin_coach_loop.store import canonical_hash, init_store, read_current_plan
from failure_injection import isolated_product_home, restart_gateway
from test_gateway import CLIENT_ID_VALUE, CLIENT_SECRET_VALUE, HMAC_KEY, NOW, ONBOARDING, WEEKLY_CHANGE, load


class PreparedTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = NOW
        self.gateway = CoachGateway(
            GatewayConfig(state_root=self.root, token_hmac_key=HMAC_KEY,
                          intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE),
            now=lambda: self.now,
        )
        self.owner = "anonymous-owner"
        home = isolated_product_home(self.root)
        home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)

    def initial(self):
        projection = project_initialization_request(copy.deepcopy(ONBOARDING), issued_at=NOW)
        return {
            "claims": _initialization_claims(
                owner=self.gateway._owner_binding(self.owner), initial_plan=projection["plan"],
            ),
            "effect": {"plan": projection["plan"]},
            "preview": projection["preview"],
            "validation": {"red_flags": {"pain": None}, "today": NOW.date().isoformat()},
            "recovery_inputs": {"initialization_request": copy.deepcopy(ONBOARDING)},
            "confirmation_required": True,
        }

    def decision(self, request=None):
        before, context = load("plan-state-v1.json"), load("coach-context-day-4.json")
        request = copy.deepcopy(WEEKLY_CHANGE if request is None else request)
        projection = project_change_request(before, request, context=context, issued_at=NOW)
        return {
            "claims": _decision_claims(
                owner=self.gateway._owner_binding(self.owner), context=context,
                plan_id=before["plan_id"], base_version=before["version"], before_plan=before,
                after_plan=projection["after_plan"], decision_event=projection["decision_event"],
                preview=projection["preview"], confirmation_required=projection["material_change"],
            ),
            "effect": {"after_plan": projection["after_plan"], "decision_event": projection["decision_event"]},
            "preview": projection["preview"], "validation": {"context": context},
            "recovery_inputs": {"change_request": request},
            "confirmation_required": projection["material_change"],
        }

    def prepare(self, material):
        return self.gateway._prepare_transaction(self.owner, now=self.now, **material)

    def held(self, issued, *, kind="initialization", confirmed=True):
        return self.gateway._held_transaction(
            self.owner, issued["proposal"], kind=kind, confirmed=confirmed,
        )

    def test_input_and_read_mutations_cannot_change_the_plan_that_is_committed(self):
        material = self.initial()
        expected = copy.deepcopy(material["effect"]["plan"])
        issued = self.prepare(material)
        material["effect"]["plan"]["goal"]["outcome"] = "not approved"
        material["validation"]["red_flags"]["pain"] = True
        material["recovery_inputs"]["initialization_request"].clear()
        returned = self.held(issued)["record"]
        returned["effect"]["plan"]["goal"]["outcome"] = "also not approved"
        returned["recovery_inputs"].clear()
        opened = self.held(issued)
        plan = opened["record"]["effect"]["plan"]
        self.gateway._validate_initial_plan(plan)
        init_store(self.root / "committed", plan, proposal_claims=opened["claims"])
        self.assertEqual(expected, read_current_plan(self.root / "committed")["current_plan"])
        self.assertEqual({"pain": None}, opened["record"]["validation"]["red_flags"])
        self.assertEqual(ONBOARDING, opened["record"]["recovery_inputs"]["initialization_request"])

    def test_cache_tampering_of_any_authority_section_is_refused(self):
        for section in ("effect", "preview", "validation", "recovery_inputs", "bindings"):
            with self.subTest(section=section):
                restart_gateway(self)
                issued = self.prepare(self.decision())
                entry = self.gateway._held[self.owner][-1]
                entry.payload[section]["tampered"] = True
                # A cache digest is not approval authority; even rehashing it cannot
                # make this mutation agree with the signed token.
                entry.digest = canonical_hash(entry.payload)
                with self.assertRaises(GatewayError) as caught:
                    self.held(issued, kind="decision")
                self.assertEqual("proposal_mismatch", caught.exception.code)

    def test_claims_cannot_bind_one_context_or_effect_and_hold_another(self):
        for section, key in (("effect", "after_plan"), ("effect", "decision_event"),
                             ("validation", "context")):
            with self.subTest(section=section, key=key):
                material = self.decision()
                material[section][key]["tampered"] = True
                with self.assertRaises(ProposalError):
                    self.prepare(material)
        self.assertEqual({}, self.gateway._held)

    def test_owner_kind_and_signature_are_checked_before_any_receipt_lookup(self):
        issued = self.prepare(self.initial())
        for owner, kind, proposal in (
            ("other-owner", "initialization", issued["proposal"]),
            (self.owner, "decision", issued["proposal"]),
            (self.owner, "initialization", "invalid.signature"),
        ):
            with self.subTest(owner=owner, kind=kind):
                with self.assertRaises(GatewayError) as caught:
                    self.gateway._authenticate_transaction(owner, proposal, kind=kind)
                self.assertEqual("proposal_mismatch", caught.exception.code)

    def test_confirmation_is_the_apply_value_not_recovery_input_or_truthiness(self):
        material = self.initial()
        material["recovery_inputs"]["confirmed"] = True
        issued = self.prepare(material)
        for value in (None, False, 1, "true"):
            with self.subTest(value=value), self.assertRaises(GatewayError) as caught:
                self.held(issued, confirmed=value)
            self.assertEqual("confirmation_required", caught.exception.code)
        self.assertEqual(material["effect"], self.held(issued)["record"]["effect"])

    def test_authentication_survives_cache_loss_clock_and_release_for_durable_lookup(self):
        issued = self.prepare(self.initial())
        restart_gateway(self)
        self.now += dt.timedelta(days=2)
        with mock.patch.object(self.gateway, "_release_binding", return_value="next-release"):
            opened = self.gateway._authenticate_transaction(
                self.owner, issued["proposal"], kind="initialization",
            )
            self.assertTrue(opened["expired"])
            self.assertEqual(issued["claims"], opened["claims"])
            with self.assertRaises(GatewayError):
                self.held(issued)
        # No synthetic idempotent success: only the existing durable receipt reader
        # can decide whether that authenticated token already committed.
        with self.assertRaises(GatewayError) as caught:
            self.held(issued)
        self.assertEqual("proposal_expired", caught.exception.code)

    def test_expiry_is_reported_for_the_kind_specific_adapter_to_decide(self):
        first = self.prepare(self.initial())
        change = self.prepare(self.decision())
        self.now += dt.timedelta(seconds=PROPOSAL_TTL_SECONDS + 1)
        self.assertTrue(self.held(first)["expired"])
        self.assertTrue(self.held(change, kind="decision")["expired"])
        # Warm adapter may compare fresh evidence; first-plan adapter must refuse.
        self.now += dt.timedelta(seconds=CONTEXT_RETENTION_SECONDS)
        with self.assertRaises(GatewayError):
            self.held(change, kind="decision")

    def test_held_transactions_reuse_the_existing_per_owner_bound(self):
        tokens = []
        for index in range(RETAINED_CONTEXTS_PER_OWNER + 1):
            material = self.initial()
            material["recovery_inputs"]["note"] = str(index)
            tokens.append(self.prepare(material))
        entries = self.gateway._held[self.owner]
        self.assertEqual(RETAINED_CONTEXTS_PER_OWNER, len(entries))
        self.assertTrue(all(entry.kind == HELD_TRANSACTION for entry in entries))
        with self.assertRaises(GatewayError):
            self.held(tokens[0])
        self.assertEqual(str(RETAINED_CONTEXTS_PER_OWNER),
                         self.held(tokens[-1])["record"]["recovery_inputs"]["note"])

    def test_compound_calendar_and_publication_intent_stay_with_the_plan(self):
        material = self.decision()
        calendar = {"effects": [{"set": {"items": [{"session_id": "anonymous"}],
                                         "settings_changes": [{"field": "threshold_pace", "proposed": 4.0}]}}],
                    "unresolved": [{"session_id": "another", "reason": "unknown"}]}
        material["effect"]["calendar"] = calendar
        material["claims"].update(delivery_hash=canonical_hash(calendar), publish_new_workouts=True)
        material["recovery_inputs"]["publish_new_workouts"] = True
        issued = self.prepare(material)
        opened = self.held(issued, kind="decision")
        self.assertEqual(calendar, opened["record"]["effect"]["calendar"])
        self.assertTrue(opened["claims"]["publish_new_workouts"])
        self.assertEqual("settings_and_calendar", opened["claims"]["side_effect_scope"])
        material["recovery_inputs"]["publish_new_workouts"] = False
        with self.assertRaises(ProposalError):
            self.prepare(material)
        material["recovery_inputs"]["publish_new_workouts"] = True
        material["effect"].pop("calendar")
        with self.assertRaises(ProposalError):
            self.prepare(material)

    def test_delivery_and_withdrawal_retain_their_exact_set_identity(self):
        for kind, direction in (("delivery", "deliver"), ("withdrawal", "withdraw")):
            with self.subTest(kind=kind):
                effect = {"direction": direction, "plan_id": "anonymous", "plan_version": 1,
                          "items": [{"session_id": "anonymous"}], "settings_changes": [],
                          "target_account": {"binding": "anonymous"}}
                effect["proposal_hash"] = canonical_hash(effect)
                material = dict(claims={"kind": kind, "owner": self.gateway._owner_binding(self.owner)},
                                effect=effect, preview=[], validation={}, recovery_inputs={},
                                confirmation_required=True)
                issued = self.prepare(material)
                self.assertEqual(effect["proposal_hash"], issued["claims"]["effect_hash"])
                self.assertEqual("calendar_effects", issued["claims"]["side_effect_scope"])
                self.assertEqual(effect, self.held(issued, kind=kind)["record"]["effect"])
                effect["direction"] = "swapped"
                with self.assertRaises(ProposalError):
                    self.prepare(material)

    def test_explicit_no_publication_is_valid_without_calendar_effects(self):
        material = self.initial()
        material["recovery_inputs"]["publish_new_workouts"] = False
        issued = self.prepare(material)
        opened = self.held(issued)
        self.assertIs(False, opened["claims"]["publish_new_workouts"])
        self.assertEqual("none", opened["claims"]["side_effect_scope"])
        self.assertEqual(material["effect"], opened["record"]["effect"])

    def test_different_valid_coaching_decisions_and_unknowns_remain_representable(self):
        # Two actual projections: a changed weekly prescription and an unchanged
        # plan with a coaching review. The kernel does not require a training change.
        from test_gateway import coaching_request
        from garmin_coach_loop.validation import validate_bundle
        for request, requires_confirmation in ((WEEKLY_CHANGE, True), (coaching_request(), False)):
            material = self.decision(request)
            self.assertEqual(requires_confirmation, material["confirmation_required"])
            effect = material["effect"]
            valid = validate_bundle(material["validation"]["context"], load("plan-state-v1.json"),
                                    effect["after_plan"], effect["decision_event"])
            self.assertEqual("passed", valid["status"], valid)
            issued = self.prepare(material)
            self.assertEqual(effect, self.held(issued, kind="decision")["record"]["effect"])
            if not material["confirmation_required"]:
                self.assertFalse(issued["claims"]["confirmation_required"])
                for confirmed in (None, False):
                    self.assertEqual(effect, self.held(issued, kind="decision", confirmed=confirmed)["record"]["effect"])
            else:
                with self.assertRaises(GatewayError) as caught:
                    self.held(issued, kind="decision", confirmed=None)
                self.assertEqual("confirmation_required", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
