"""Two coaches for one athlete do not share state, grants, proposals or event identity."""

from __future__ import annotations

import dataclasses
import json
import argparse
from unittest import mock
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop import athlete_evidence, cli, privacy_request, product_identity, token_envelope
from garmin_coach_loop.delivery import owned_external_id_for
from garmin_coach_loop.gateway import (
    CoachGateway, GatewayConfig, GatewayConfigError, GatewayError, load_config, run_preflight,
)
from garmin_coach_loop.identity import lookup_or_create_owner, record_token_fingerprint, token_fingerprint
from garmin_coach_loop.plan_init import project_initialization_request
from garmin_coach_loop.store import read_current_plan
from test_gateway import HMAC_KEY, TOKEN_A, NOW, CLIENT_ID_VALUE, CLIENT_SECRET_VALUE, FakeIntervals
from test_plan_init import initialization_request


class IndependentCoachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = GatewayConfig(
            state_root=self.root, token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE,
        )
        self.gateways = {}
        self.owners = {}
        for product in ("hybrid", "training"):
            config = dataclasses.replace(self.config, product=product)
            run_preflight(config)
            gateway = CoachGateway(config, fetch=FakeIntervals(), now=lambda: NOW)
            owner = lookup_or_create_owner(config.identity_db_path, "intervals", "synthetic-athlete")
            record_token_fingerprint(
                config.identity_db_path, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
                owner, "intervals",
            )
            self.gateways[product] = gateway
            self.owners[product] = owner

    def test_same_athlete_has_independent_records_and_plans(self):
        full, training = self.gateways["hybrid"], self.gateways["training"]
        full.route("body_measurement_record", self.owners["hybrid"], TOKEN_A,
                   {"date": "2026-08-13", "weight_kg": 72})
        full_evidence = athlete_evidence.load_evidence(full._state_dir(self.owners["hybrid"]))
        training_evidence = athlete_evidence.load_evidence(training._state_dir(self.owners["training"]))
        self.assertTrue(full_evidence["body_measurements"])
        self.assertFalse(training_evidence["body_measurements"])

        plans = {}
        for product, gateway in self.gateways.items():
            owner = self.owners[product]
            prepared = gateway.prepare_initialization(
                owner, TOKEN_A, {"initialization_request": initialization_request()}
            )
            other = self.gateways["training" if product == "hybrid" else "hybrid"]
            other_owner = self.owners[other.config.product]
            with self.assertRaises(GatewayError):
                other.route("decision_apply", other_owner, TOKEN_A,
                            {"proposal": prepared["proposal"], "confirmed": True})
            gateway.route("decision_apply", owner, TOKEN_A,
                          {"proposal": prepared["proposal"], "confirmed": True})
            plans[product] = read_current_plan(gateway._state_dir(owner))["current_plan"]
        self.assertNotEqual(plans["hybrid"]["plan_id"], plans["training"]["plan_id"])
        self.assertEqual(plans["hybrid"]["goal"], plans["training"]["goal"])
        # Identical prescriptions made at the same instant must not own the same
        # provider event; one coach cannot update/withdraw the other's workout.
        session = plans["hybrid"]["week"]["sessions"][0]["session_id"]
        self.assertNotEqual(owned_external_id_for(plans["hybrid"], session),
                            owned_external_id_for(plans["training"], session))

    def test_even_identical_owner_ids_do_not_share_storage_or_approval_binding(self):
        full, training = self.gateways["hybrid"], self.gateways["training"]
        before = training._state_dir(self.owners["training"])
        # Even an identical owner string cannot cross the configured storage root.
        self.assertNotEqual(full._state_dir(self.owners["hybrid"]), training._state_dir(self.owners["hybrid"]))
        self.assertEqual(before, training._state_dir(self.owners["training"]))
        self.assertNotEqual(full._owner_binding("same-owner"), training._owner_binding("same-owner"))

    def test_operator_export_matches_gateway_and_deletes_only_selected_coach(self):
        training = self.gateways["training"]
        root = training.config.product_state_root
        with mock.patch.dict("os.environ", {product_identity.PRODUCT_ENV_VAR: "training"}):
            args = argparse.Namespace(state_root=str(self.root), state_dir=None,
                                      athlete_id="synthetic-athlete")
            self.assertEqual(root, cli._privacy_request_root(args))
            self.assertEqual(training._state_dir(self.owners["training"]), cli._owner_state_dir(args))
        served = privacy_request.export_request(
            root, training.config.identity_db_path, athlete_id="synthetic-athlete",
            identity_evidence="athlete-id-only", hmac_key=HMAC_KEY, product="training",
        )
        gateway_archive = training.export_owner_data(self.owners["training"], TOKEN_A, {})
        self.assertEqual(gateway_archive["owner_reference"], served["archive"]["owner_reference"])
        scope = privacy_request.deletion_scope(
            root, training.config.identity_db_path, athlete_id="synthetic-athlete",
            identity_evidence="athlete-id-only", hmac_key=HMAC_KEY, product="training",
        )
        erased = privacy_request.apply_deletion(
            root, training.config.identity_db_path, athlete_id="synthetic-athlete",
            identity_evidence="athlete-id-only", hmac_key=HMAC_KEY, product="training",
            now=NOW, scope_digest=scope["scope_digest"], confirmed=True,
        )
        self.assertTrue(erased["deleted"])
        self.assertEqual("verified", erased["verified_after_deletion"]["status"])
        full = self.gateways["hybrid"]
        self.assertEqual(self.owners["hybrid"], full.resolve_owner(TOKEN_A))

    def test_operator_refuses_another_product_marker(self):
        with mock.patch.dict("os.environ", {product_identity.PRODUCT_ENV_VAR: "hybrid"}):
            args = argparse.Namespace(state_root=str(self.gateways["training"].config.product_state_root))
            with self.assertRaises(product_identity.ProductIdentityError):
                cli._privacy_request_root(args)

    def test_access_tokens_are_product_bound_even_with_same_key_and_audience(self):
        base = "https://synthetic.example"
        for issuing in ("hybrid", "training"):
            bearer = token_envelope.seal(
                product_identity.product_claims(
                    {"intervals_token": TOKEN_A, "aud": base + "/mcp", "iat": int(NOW.timestamp())},
                    issuing,
                ), kind=token_envelope.ACCESS_TOKEN, key=HMAC_KEY,
            )
            own = self.gateways[issuing]
            self.assertEqual(self.owners[issuing], own.resolve_mcp_owner(bearer, base_url=base)[0])
            other = self.gateways["training" if issuing == "hybrid" else "hybrid"]
            with self.assertRaises(GatewayError):
                other.resolve_mcp_owner(bearer, base_url=base)

    def test_registrations_cannot_cross_products(self):
        redirect = "http://127.0.0.1:8765/callback"
        for product in ("hybrid", "training"):
            registration = token_envelope.seal(
                product_identity.product_claims(
                    {"redirect_uris": [redirect], "iat": int(NOW.timestamp())}, product,
                ), kind=token_envelope.CLIENT_REGISTRATION, key=HMAC_KEY,
            )
            self.assertEqual([redirect], self.gateways[product]._registered_redirect_uris(registration))
            other = self.gateways["training" if product == "hybrid" else "hybrid"]
            with self.assertRaises(GatewayError):
                other._registered_redirect_uris(registration)

    def test_callback_rejects_other_product_before_provider_exchange(self):
        state = token_envelope.seal(
            {"iat": int(NOW.timestamp()), "client_redirect_uri": "http://127.0.0.1:8765/callback"},
            kind=token_envelope.AUTHORIZE_STATE, key=HMAC_KEY,
        )
        with self.assertRaises(GatewayError):
            self.gateways["training"].complete_authorization(
                {"state": state, "code": "synthetic-code"}, base_url="https://synthetic.example"
            )

    def test_storage_binding_refuses_reusing_training_as_hybrid_before_reaping_locks(self):
        training_root = self.gateways["training"].config.product_state_root
        owner_dir = training_root / "owners" / "synthetic-owner"
        owner_dir.mkdir(parents=True)
        lock = owner_dir / ".lock"
        lock.write_text("synthetic lock")
        wrong = dataclasses.replace(self.config, state_root=training_root)
        with self.assertRaises(GatewayConfigError):
            run_preflight(wrong)
        self.assertEqual("synthetic lock", lock.read_text())

    def test_training_will_not_adopt_an_unmarked_copied_store(self):
        marker = self.gateways["training"].config.product_state_root / product_identity.MARKER_NAME
        marker.unlink()
        with self.assertRaises(GatewayConfigError):
            run_preflight(self.gateways["training"].config)
        self.assertFalse(marker.exists())

    def test_malformed_binding_is_not_treated_as_a_fresh_product(self):
        marker = self.root / product_identity.MARKER_NAME
        marker.write_text("incomplete")
        with self.assertRaises(GatewayConfigError):
            run_preflight(self.config)
        self.assertEqual("incomplete", marker.read_text())

    def test_product_namespace_cannot_alias_legacy_storage(self):
        other_root = self.root / "alias-test"
        (other_root / "products").mkdir(parents=True)
        (other_root / "products" / "training").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(GatewayConfigError):
            dataclasses.replace(self.config, state_root=other_root, product="training")

    def test_configuration_is_explicit_and_health_names_the_product_without_a_path(self):
        env = {
            "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.root),
            "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode(),
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID": CLIENT_ID_VALUE,
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET": CLIENT_SECRET_VALUE,
            product_identity.PRODUCT_ENV_VAR: "training",
        }
        config = load_config(env)
        self.assertEqual(self.root / "products" / "training", config.product_state_root)
        result = CoachGateway(config).health()
        self.assertEqual("training", result["deployment_product"])
        self.assertNotIn(str(self.root), json.dumps(result))
        with self.assertRaises(GatewayConfigError):
            load_config({**env, product_identity.PRODUCT_ENV_VAR: "../hybrid"})

    def test_legacy_first_plan_projection_stays_identical_and_new_product_is_deterministic(self):
        request = initialization_request()
        legacy = project_initialization_request(request, issued_at=NOW)
        self.assertEqual(legacy, project_initialization_request(request, issued_at=NOW, plan_namespace=""))
        training = project_initialization_request(request, issued_at=NOW, plan_namespace="training")
        self.assertEqual(training, project_initialization_request(request, issued_at=NOW, plan_namespace="training"))
        self.assertNotEqual(legacy["plan"]["plan_id"], training["plan"]["plan_id"])
