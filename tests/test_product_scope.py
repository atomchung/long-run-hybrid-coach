"""With health data off, a deployment never reads, accepts or keeps the excluded categories.

Harmful cases: a wellness request, a recovery or symptom field, a body measurement, a
health record out of an upload, or an excluded name in what the model is handed. False-
positive controls: the ordinary training flow -- first plan, a session read with how the
athlete feels, a subjective note, an activity upload -- still works, the full surface is
byte-for-byte what it was, authorization asks for the same scopes either way, and turning
the switch on later serves the same athlete's plan with health data.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop import (
    athlete_evidence,
    evidence_import,
    mcp_sdk_transport,
    mcp_transport,
    orchestration,
    product_scope,
)
from garmin_coach_loop.gateway import (
    INTERVALS_OAUTH_SCOPES,
    STATE_READ_UNKNOWNS,
    CoachGateway,
    GatewayConfig,
    GatewayConfigError,
    GatewayError,
    _intervals_scope,
    authorization_server_metadata,
    load_config,
    protected_resource_metadata,
    run_preflight,
)
from garmin_coach_loop.identity import lookup_or_create_owner, record_token_fingerprint, token_fingerprint
from garmin_coach_loop.source_intervals import RECOVERY_NOT_COLLECTED_NOTE
from test_evidence_import import APPLE_XML
from test_gateway import CLIENT_ID_VALUE, CLIENT_SECRET_VALUE, HMAC_KEY, NOW, TOKEN_A, FakeIntervals
from test_plan_init import initialization_request
from scripts.release_bundle import expected_deployment_identity_from_env


NO_HEALTH = product_scope.surface(False)
FULL = product_scope.surface(True)

# Names the training surface must not hand the model. A guard on what the model is told,
# not proof of what the code processes -- the gateway tests below hold that.
EXCLUDED_NAMES = (
    "HRV",
    "hrv",
    "sleep",
    "睡",
    "resting heart rate",
    "resting_hr",
    "body fat",
    "body_fat",
    "body-mass",
    "recordBodyMeasurement",
    "recovery_signals",
    "red_flags",
    "recovery_reading",
)


def _served_text(surface: product_scope.Surface) -> str:
    return json.dumps([tool.descriptor() for tool in surface.tools], ensure_ascii=False)


class TrainingSurfaceTests(unittest.TestCase):
    def test_hybrid_surface_is_the_reviewed_one_unchanged(self):
        self.assertIs(mcp_transport.TOOLS, FULL.tools)
        self.assertEqual(mcp_transport.tool_catalogue_sha256(), FULL.tool_catalogue_sha256())
        self.assertEqual(orchestration.instructions(), FULL.instructions())
        self.assertEqual(orchestration.training_judgment(), FULL.training_judgment())
        self.assertIs(orchestration.PROMPTS, FULL.prompts)

    def test_training_catalogue_drops_the_excluded_tool_fields_and_codes(self):
        tools = NO_HEALTH.tools_by_name
        self.assertNotIn("recordBodyMeasurement", tools)
        self.assertEqual(len(mcp_transport.TOOLS) - 1, len(NO_HEALTH.tools))
        session = tools["startCoachSession"]
        for field in ("red_flags", "all_clear", "recovery_signals"):
            self.assertNotIn(field, session.input_schema["properties"])
        for field in ("leg_fatigue", "soreness", "read", "available_days"):
            self.assertIn(field, session.input_schema["properties"])
        self.assertNotIn("recovery_recording", session.output_schema["properties"])
        self.assertFalse(session.annotations["destructiveHint"])
        prepare = tools["prepareCoachDecision"].input_schema["properties"]
        self.assertNotIn("red_flags", prepare)
        codes = prepare["change_request"]["properties"]["reason_codes"]["items"]["enum"]
        self.assertTrue({"multi_signal_recovery_down", "recovery_signal_mixed", "pain_or_illness_flag"}.isdisjoint(codes))
        self.assertIn("actual_load_above_plan", codes)
        kinds = tools["retractAthleteRecord"].input_schema["properties"]["kind"]["enum"]
        self.assertNotIn("body_measurement", kinds)
        self.assertNotIn("recovery_reading", kinds)
        self.assertIn("subjective_state", kinds)
        received = tools["importAthleteHistory"].output_schema["properties"]
        for key in ("measurements_added", "measurements_skipped", "recovery_added", "recovery_skipped"):
            self.assertNotIn(key, received)
        self.assertIn("not_kept", received)

    def test_the_hybrid_tools_themselves_are_not_mutated(self):
        self.assertIn("red_flags", mcp_transport.TOOLS_BY_NAME["startCoachSession"].input_schema["properties"])
        self.assertIn("recordBodyMeasurement", mcp_transport.TOOLS_BY_NAME)
        product_scope.tools_without_health(mcp_transport.TOOLS)
        self.assertEqual(mcp_transport.tool_catalogue_sha256(), FULL.tool_catalogue_sha256())

    def test_no_excluded_name_reaches_the_training_model_surface(self):
        served = "\n".join(
            (_served_text(NO_HEALTH), NO_HEALTH.instructions(), NO_HEALTH.training_judgment())
        )
        # The instructions name the categories once, to say they are not stored; the
        # catalogue and the guidance name none of them.
        instructions = NO_HEALTH.instructions()
        boundary = (
            "Sleep, HRV, resting heart rate, body measurements and symptoms are not stored\n"
            "  here."
        )
        self.assertEqual(1, instructions.count(boundary))
        served = served.replace(boundary, "")
        for name in EXCLUDED_NAMES:
            if name == "sleep":
                # One sentence lists sleep among the many causes of a missed session.
                served = served.replace("equipment, sleep,", "equipment,")
            self.assertNotIn(name, served, name)
        self.assertNotIn("Body composition", NO_HEALTH.training_judgment())
        self.assertIn("Body composition", FULL.training_judgment())

    def test_every_training_tool_routes_to_a_gateway_act(self):
        self.assertLessEqual({tool.kind for tool in NO_HEALTH.tools}, CoachGateway.route_kinds())

    def test_a_changed_hybrid_text_fails_the_derivation_instead_of_serving_it(self):
        with self.assertRaises(product_scope.ProductScopeError):
            product_scope.instructions_without_health(orchestration.instructions().replace("red_flags", "symptom_flags"))
        with self.assertRaises(product_scope.ProductScopeError):
            product_scope.judgment_without_health(orchestration.training_judgment() + "\n\n## A later section\n\nText.")
        session = mcp_transport.TOOLS_BY_NAME["startCoachSession"]
        reworded = dataclasses.replace(
            session,
            input_schema={
                **session.input_schema,
                "properties": {
                    key: value
                    for key, value in session.input_schema["properties"].items()
                    if key != "all_clear"
                },
            },
        )
        tools = tuple(reworded if tool is session else tool for tool in mcp_transport.TOOLS)
        with self.assertRaises(product_scope.ProductScopeError):
            product_scope.tools_without_health(tools)

    def test_training_prompts_serve_the_training_text(self):
        prompts = NO_HEALTH.prompts
        self.assertEqual(set(orchestration.PROMPTS), set(prompts))
        text = prompts[orchestration.TRAINING_PROMPT_NAME][1]()["messages"][0]["content"]["text"]
        self.assertEqual(NO_HEALTH.training_judgment(), text)
        text = prompts[orchestration.PROMPT_NAME][1]()["messages"][0]["content"]["text"]
        self.assertEqual(NO_HEALTH.instructions(), text)

    def test_sdk_server_lists_the_training_catalogue(self):
        listed = [tool.name for tool in mcp_sdk_transport._tool_descriptors(NO_HEALTH)]
        self.assertEqual([tool.name for tool in NO_HEALTH.tools], listed)
        self.assertNotIn("recordBodyMeasurement", listed)

    def test_authorization_asks_for_the_same_scopes_whatever_the_switch_says(self):
        # The switch decides what this server reads, never what the athlete consents to,
        # so turning health data on later needs no second consent.
        self.assertIn("WELLNESS:READ", INTERVALS_OAUTH_SCOPES)
        self.assertEqual(",".join(INTERVALS_OAUTH_SCOPES), _intervals_scope(None))
        base = "https://openai-mcp.example"
        self.assertEqual(list(INTERVALS_OAUTH_SCOPES), protected_resource_metadata(base)["scopes_supported"])
        self.assertEqual(list(INTERVALS_OAUTH_SCOPES), authorization_server_metadata(base)["scopes_supported"])

    def test_state_read_unknowns_stop_asking_for_red_flags(self):
        without = product_scope.state_read_unknowns(False, STATE_READ_UNKNOWNS)
        self.assertFalse(any("red flags" in entry for entry in without))
        self.assertEqual(STATE_READ_UNKNOWNS, product_scope.state_read_unknowns(True, STATE_READ_UNKNOWNS))


class RefusalTests(unittest.TestCase):
    def test_hybrid_refuses_nothing(self):
        for kind, body in (
            ("body_measurement_record", {"weight_kg": 72}),
            ("session", {"red_flags": {"pain": True}, "recovery_signals": {"days": []}}),
        ):
            self.assertIsNone(product_scope.refusal(True, kind, body))

    def test_training_refuses_each_excluded_category(self):
        cases = (
            ("body_measurement_record", {"date": "2026-08-13", "weight_kg": 72}),
            ("session", {"red_flags": {"pain": False}}),
            ("session", {"all_clear": True}),
            ("session", {"recovery_signals": {"source": "watch", "days": [{"date": "2026-08-13"}]}}),
            ("decision_prepare", {"red_flags": {"illness": True}}),
            ("decision_prepare", {"change_request": {"reason_codes": ["recovery_signal_mixed"]}}),
            ("athlete_record_retract", {"kind": "recovery_reading", "date": "2026-08-13"}),
            ("athlete_record_retract", {"kind": "body_measurement", "date": "2026-08-13"}),
        )
        for kind, body in cases:
            with self.subTest(kind=kind, body=body):
                self.assertIsNotNone(product_scope.refusal(False, kind, body))

    def test_training_accepts_ordinary_training_statements(self):
        cases = (
            ("session", {"read": ["today"], "leg_fatigue": "elevated", "soreness": "normal"}),
            ("session", {"red_flags": None, "recovery_signals": None}),
            ("subjective_state_record", {"note": "我覺得很累"}),
            ("decision_prepare", {"change_request": {"reason_codes": ["actual_load_above_plan"]}}),
            ("athlete_record_retract", {"kind": "subjective_state", "date": "2026-08-13"}),
            ("history_import", {"format": "apple_health_xml", "content": APPLE_XML}),
        )
        for kind, body in cases:
            with self.subTest(kind=kind):
                self.assertIsNone(product_scope.refusal(False, kind, body))

    def test_a_symptom_refusal_says_only_what_is_not_collected(self):
        refused = product_scope.refusal(False, "session", {"red_flags": {"chest_pain": True}})
        self.assertEqual(f"red_flags is not accepted. {product_scope._NOT_COLLECTED}", refused)

    def test_the_boundary_is_data_and_the_response_is_the_models_own(self):
        # Only what is read and stored is withheld. How to answer an athlete who feels
        # unwell -- whether to ask, rest the day or change the plan -- is the client
        # model's judgment, so nothing here instructs it or forbids a change.
        apply = NO_HEALTH.tools_by_name["applyCoachDecision"].description
        self.assertNotIn("symptom", apply)
        subjective = NO_HEALTH.tools_by_name["recordSubjectiveState"].description
        self.assertIn("Symptoms are not stored here.", subjective)
        for guidance in ("human decision", "lower-risk", "adjust the plan"):
            self.assertNotIn(guidance, subjective)
        self.assertNotIn("never ask for or record", NO_HEALTH.instructions())


class TrainingImportTests(unittest.TestCase):
    def test_health_records_are_counted_before_any_value_is_read(self):
        kept = evidence_import.read_payload(format_name="apple_health_xml", content=APPLE_XML)
        self.assertTrue(kept["measurements"])
        self.assertTrue(kept["recovery"])
        withheld = evidence_import.read_payload(
            format_name="apple_health_xml", content=APPLE_XML, keeps_health=False
        )
        self.assertEqual([], withheld["measurements"])
        self.assertEqual([], withheld["recovery"])
        # Two body masses, body fat, resting heart rate, HRV and one sleep record.
        self.assertEqual(6, withheld["ignored"][evidence_import.HEALTH_NOT_COLLECTED])
        self.assertEqual(kept["activities"], withheld["activities"])
        self.assertEqual([], [row for row in withheld["unreadable"] if row not in kept["unreadable"]])


class TrainingGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.gateways: dict[str, CoachGateway] = {}
        self.owners: dict[str, str] = {}
        for product in ("hybrid", "training"):
            config = GatewayConfig(
                state_root=root, token_hmac_key=HMAC_KEY, product=product,
                intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE,
            )
            run_preflight(config)
            fake = FakeIntervals(
                wellness=[{"id": "2026-08-12", "hrv": 61, "restingHR": 48, "sleepScore": 80, "sleepSecs": 27000}]
            )
            gateway = CoachGateway(config, fetch=fake, now=lambda: NOW)
            owner = lookup_or_create_owner(config.identity_db_path, "intervals", "synthetic-athlete")
            record_token_fingerprint(
                config.identity_db_path, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY), owner, "intervals",
            )
            self.gateways[product] = gateway
            self.owners[product] = owner

    def _with_first_plan(self, product: str) -> CoachGateway:
        gateway, owner = self.gateways[product], self.owners[product]
        prepared = gateway.prepare_initialization(
            owner, TOKEN_A, {"initialization_request": initialization_request()}
        )
        gateway.route("decision_apply", owner, TOKEN_A, {"proposal": prepared["proposal"], "confirmed": True})
        return gateway

    def _wellness_requests(self, product: str) -> list[str]:
        return [url for _, url in self.gateways[product].fetch.calls if "/wellness" in url]

    def test_a_training_session_never_requests_wellness_and_shows_no_recovery_field(self):
        for product in ("hybrid", "training"):
            self._with_first_plan(product)
        hybrid = self.gateways["hybrid"].route("session", self.owners["hybrid"], TOKEN_A, {"read": ["all"]})
        answer = self.gateways["training"].route("session", self.owners["training"], TOKEN_A, {"read": ["all"]})
        self.assertTrue(self._wellness_requests("hybrid"))
        self.assertEqual([], self._wellness_requests("training"))
        self.assertIn("recovery_trends", hybrid["context"])
        context = answer["context"]
        for field in ("coverage", "recovery_trends", "recovery_signals", "reported_recovery", "body_measurements"):
            self.assertNotIn(field, context)
        self.assertNotIn("red_flags", context["constraints"])
        self.assertNotIn("recovery", context["freshness"])
        self.assertIn(RECOVERY_NOT_COLLECTED_NOTE, context["unknowns"])
        self.assertNotIn("sleep_data_unavailable", context["unknowns"])
        self.assertNotIn("red_flags_not_confirmed", context["unknowns"])
        self.assertNotIn("Body composition", answer["coaching_guidance"])
        self.assertIn("Body composition", hybrid["coaching_guidance"])
        expanded = self.gateways["training"].route(
            "evidence_read", self.owners["training"], TOKEN_A,
            {"context_id": context["context_id"], "read": ["recovery", "records"]},
        )
        for field in ("recovery_signals", "reported_recovery", "body_measurements"):
            self.assertNotIn(field, expanded["evidence"])
        self.assertNotIn("recovery_signals", json.dumps(expanded["evidence_index"]))

    def test_excluded_calls_are_refused_before_any_provider_request(self):
        gateway, owner = self._with_first_plan("training"), self.owners["training"]
        before = list(gateway.fetch.calls)
        for kind, body in (
            ("body_measurement_record", {"date": "2026-08-13", "weight_kg": 72}),
            ("session", {"red_flags": {"chest_pain": True}}),
            ("session", {"recovery_signals": {"source": "watch", "days": [{"date": "2026-08-13", "resting_hr_bpm": 48}]}}),
        ):
            with self.subTest(kind=kind), self.assertRaises(GatewayError) as caught:
                gateway.route(kind, owner, TOKEN_A, body)
            self.assertEqual(product_scope.OUTSIDE_SCOPE_ERROR, caught.exception.code)
        self.assertEqual(before, gateway.fetch.calls)
        evidence = athlete_evidence.load_evidence(gateway._state_dir(owner))
        self.assertFalse(evidence["body_measurements"])
        self.assertFalse(evidence["reported_recovery"])

    def test_how_the_athlete_feels_is_still_heard(self):
        gateway, owner = self._with_first_plan("training"), self.owners["training"]
        answer = gateway.route(
            "session", owner, TOKEN_A,
            {"read": ["today"], "leg_fatigue": "elevated", "red_flags": None},
        )
        self.assertEqual("passed", answer["status"])
        self.assertEqual("elevated", answer["context"]["constraints"]["leg_fatigue"])
        recorded = gateway.route("subjective_state_record", owner, TOKEN_A, {"note": "我覺得很累"})
        self.assertEqual("passed", recorded["status"])

    def test_an_upload_keeps_its_sessions_and_counts_its_health_records(self):
        gateway, owner = self.gateways["training"], self.owners["training"]
        answer = gateway.route("history_import", owner, TOKEN_A, {"format": "apple_health_xml", "content": APPLE_XML})
        self.assertTrue(answer["added"]["items"] if isinstance(answer["added"], dict) else answer["added"])
        self.assertEqual(6, answer["not_kept"][evidence_import.HEALTH_NOT_COLLECTED])
        for key in ("measurements_added", "measurements_skipped", "recovery_added", "recovery_skipped"):
            self.assertNotIn(key, answer)
        evidence = athlete_evidence.load_evidence(gateway._state_dir(owner))
        self.assertTrue(evidence["reported_activities"])
        self.assertFalse(evidence["body_measurements"])
        self.assertFalse(evidence["reported_recovery"])
        hybrid = self.gateways["hybrid"].route(
            "history_import", self.owners["hybrid"], TOKEN_A, {"format": "apple_health_xml", "content": APPLE_XML}
        )
        self.assertIn("measurements_added", hybrid)

    def test_readiness_compares_the_catalogue_this_product_serves(self):
        self.assertEqual(NO_HEALTH.tool_catalogue_sha256(), self.gateways["training"].surface.tool_catalogue_sha256())
        self.assertNotEqual(FULL.tool_catalogue_sha256(), NO_HEALTH.tool_catalogue_sha256())
        self.assertEqual("off", self.gateways["training"].health()["health_data"])
        self.assertEqual("on", self.gateways["hybrid"].health()["health_data"])

    def test_no_answer_carries_a_health_note_or_a_record_kept_while_on(self):
        # A deployment flipped from on to off still holds what it kept while on; nothing
        # the model is handed may carry it, including the pre-plan observations, and no
        # validation note or unknown may ask for a reading this deployment refuses.
        gateway, owner = self.gateways["hybrid"], self.owners["hybrid"]
        imported = gateway.route("history_import", owner, TOKEN_A, {"format": "apple_health_xml", "content": APPLE_XML})
        self.assertTrue(imported["measurements_added"])
        switched = CoachGateway(
            dataclasses.replace(gateway.config, health_data=False), fetch=gateway.fetch, now=lambda: NOW
        )
        before_plan = switched.route("session", owner, TOKEN_A, {"read": ["all"]})
        self.assertIn("pre_plan_observations", before_plan)
        self.assertNotIn("body_measurements", json.dumps(before_plan))
        planned = self._with_first_plan("training")
        answer = planned.route("session", self.owners["training"], TOKEN_A, {"read": ["all"]})
        notes = json.dumps([answer["unknowns"], answer["validation"]], ensure_ascii=False)
        for hidden in ("red flag", "red_flags_not_confirmed", "sleep_data_unavailable", "recovery freshness"):
            self.assertNotIn(hidden, notes)
        self.assertIn("red flag", json.dumps(gateway.route("session", owner, TOKEN_A, {"read": ["all"]})))

    def test_turning_health_data_on_serves_the_same_plan_with_health_data(self):
        gateway, owner = self._with_first_plan("training"), self.owners["training"]
        before = gateway.route("session", owner, TOKEN_A, {"read": ["all"]})
        self.assertEqual([], self._wellness_requests("training"))
        switched = CoachGateway(
            dataclasses.replace(gateway.config, health_data=True), fetch=gateway.fetch, now=lambda: NOW
        )
        after = switched.route("session", owner, TOKEN_A, {"read": ["all"]})
        self.assertEqual(before["plan_state"], after["plan_state"])
        self.assertTrue(self._wellness_requests("training"))
        self.assertIn("recovery_trends", after["context"])
        self.assertIs(FULL, switched.surface)
        self.assertEqual("on", switched.health()["health_data"])


class HealthDataSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_unset_follows_the_product_and_an_explicit_value_wins(self):
        def config(**extra) -> GatewayConfig:
            return GatewayConfig(
                state_root=self.root, token_hmac_key=HMAC_KEY,
                intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE, **extra,
            )

        self.assertTrue(config().health_data)
        self.assertFalse(config(product="training").health_data)
        self.assertTrue(config(product="training", health_data=True).health_data)
        self.assertFalse(config(health_data=False).health_data)

    def test_the_environment_takes_on_or_off_and_nothing_else(self):
        env = {
            "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.root),
            "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode(),
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID": CLIENT_ID_VALUE,
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET": CLIENT_SECRET_VALUE,
            "GARMIN_COACH_LOOP_PRODUCT": "training",
        }
        switch = product_scope.HEALTH_DATA_ENV_VAR
        self.assertFalse(load_config(env).health_data)
        self.assertTrue(load_config({**env, switch: "on"}).health_data)
        self.assertFalse(load_config({**env, switch: " OFF "}).health_data)
        for value in ("yes", "1", "true"):
            with self.subTest(value=value), self.assertRaises(GatewayConfigError):
                load_config({**env, switch: value})

    def test_the_trusted_runner_expects_the_identity_each_product_reports(self):
        # The gateway binds its deployment identity to the product's own root; the release
        # check has to name the same one, or a training deployment can never verify.
        env = {
            "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.root),
            "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode(),
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID": CLIENT_ID_VALUE,
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET": CLIENT_SECRET_VALUE,
            "GARMIN_COACH_LOOP_DEPLOYMENT_ENVIRONMENT": "production",
            "GARMIN_COACH_LOOP_DEPLOYMENT_INSTANCE_ID": "coach-1",
        }
        for product in ("hybrid", "training"):
            with self.subTest(product=product):
                configured = {**env, "GARMIN_COACH_LOOP_PRODUCT": product}
                self.assertEqual(
                    load_config(configured).deployment_identity,
                    expected_deployment_identity_from_env(configured),
                )


if __name__ == "__main__":
    unittest.main()
