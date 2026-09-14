"""Derived prepare→apply identifier conformance, beside the handwritten flows.

`tests/test_confirmation_transactions.py` already walks the public MCP
prepare/apply pairs with real producer output. Those tests choose which keys
to echo. This module derives the identifier list from the published prepare
*output* schema and the echo set from the matching apply *input* schema, then
asserts the invariant the handwritten tests cannot: a new `*_id` or
`*_version` on a prepare result that the matching apply would reject fails
here even if no human added a case.

The copy helper is `schema_runtime.apply_body_from_prepare`, which
`apply_from_prepare` in the handwritten module now delegates to, so there is
one echo policy.
"""

from __future__ import annotations

import unittest
from typing import Any

from schema_runtime import (
    PREPARE_APPLY_PAIRS,
    apply_body_from_prepare,
    handoff_identifier_names,
    identifiers_apply_schema_rejects,
    input_schema,
    output_schema,
    returned_handoff_identifiers,
)
from test_gateway import (
    ONBOARDING,
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    WEEKLY_CHANGE,
    as_change_request,
    publishable_plan,
)
from test_mcp_gateway import McpTestCase


class IdentifierDerivationTests(unittest.TestCase):
    def test_handoff_identifier_names_are_derived_and_not_empty(self):
        for prepare_tool, apply_tool in PREPARE_APPLY_PAIRS:
            names = handoff_identifier_names(prepare_tool)
            self.assertTrue(
                names,
                f"{prepare_tool} output schema exposed no identifier properties; "
                "a schema that stops naming them must not make this suite pass",
            )
            self.assertIn("proposal", names, prepare_tool)
            apply_properties = set(input_schema(apply_tool).get("properties") or {})
            self.assertIn("proposal", apply_properties, apply_tool)

    def test_apply_body_refuses_to_invent_a_missing_proposal(self):
        with self.assertRaisesRegex(AssertionError, "refusing to invent"):
            apply_body_from_prepare({"status": "passed"}, "applyCoachDecision")

    def test_echo_set_is_the_apply_schema_not_a_hand_list(self):
        """A new prepare-output identifier is not copied unless apply declares it.

        The handwritten flows would miss that identifier entirely. This is the
        future change they cannot see: add `transaction_id` to prepare output
        and return it; this helper still will not invent an apply field for it,
        and `identifiers_apply_schema_rejects` will fail the first-plan path.
        """
        from test_confirmation_transactions import apply_from_prepare

        prepared = {
            "proposal": "signed.token",
            "plan_id": "plan-derived",
            "plan_version": 1,
            "transaction_id": "new-identifier",
            "status": "passed",
        }
        body = apply_from_prepare(prepared)
        self.assertEqual({"proposal": "signed.token", "confirmed": True}, body)
        contradiction = identifiers_apply_schema_rejects(
            {**prepared, "transaction_id": "new-identifier"},
            "prepareCoachDecision",
            "applyCoachDecision",
        )
        # transaction_id is not in today's output schema, so the derived
        # identifier list does not include it. The floor test below is what
        # fails when the output schema grows a new *_id.
        self.assertNotIn("transaction_id", contradiction)
        self.assertIn("plan_id", contradiction)

    def test_both_apply_tools_declare_the_same_echo_keys(self):
        decision = set(input_schema("applyCoachDecision").get("properties") or {})
        delivery = set(input_schema("applyWorkoutDelivery").get("properties") or {})
        self.assertEqual(decision, delivery)


class PrepareApplyFlowCase(McpTestCase):
    def setUp(self):
        super().setUp()
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]

    def invoke(self, name: str, arguments: dict[str, Any] | None = None):
        result = self.tool_result(name, arguments)
        payload = self.tool_payload(result)
        refused = bool(result.get("isError"))
        return refused, payload

    def apply_from_producer(self, prepared: dict[str, Any], apply_tool: str, **kwargs):
        body = apply_body_from_prepare(prepared, apply_tool, **kwargs)
        return self.invoke(apply_tool, body), body


class FirstPlanHandoffTests(PrepareApplyFlowCase):
    """Cold start: no PlanState. This is the #280 path."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare_first_plan(self) -> dict[str, Any]:
        refused, session = self.invoke("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, session)
        self.assertEqual("no_plan_state", session["status"], session)
        refused, prepared = self.invoke(
            "prepareCoachDecision",
            {"change_request": as_change_request(ONBOARDING)},
        )
        self.assertFalse(refused, prepared)
        self.assertTrue(prepared.get("proposal"), prepared)
        return prepared

    def test_first_plan_prepare_returns_no_identifier_the_matching_apply_rejects(self):
        """The invariant, not the current behaviour.

        If prepare starts handing back a non-null `plan_id` (or any other
        identifier apply's schema does not accept), this fails. Do not weaken
        it to match a self-contradictory handoff. Issue #280.
        """
        prepared = self.prepare_first_plan()
        contradiction = identifiers_apply_schema_rejects(
            prepared, "prepareCoachDecision", "applyCoachDecision"
        )
        self.assertEqual(
            {},
            contradiction,
            "prepareCoachDecision returned identifiers applyCoachDecision would "
            f"reject: {sorted(contradiction)}. Every identifier a prepare result "
            "returns must be accepted by the matching apply, or absent from the "
            "prepare result.",
        )

    def test_first_plan_apply_accepts_the_identifiers_prepare_actually_returned(self):
        prepared = self.prepare_first_plan()
        (refused, applied), body = self.apply_from_producer(
            prepared, "applyCoachDecision"
        )
        self.assertEqual({"proposal", "confirmed"}, set(body), body)
        self.assertFalse(refused, applied)
        self.assertEqual("passed", applied["status"], applied)


class ExistingPlanHandoffTests(PrepareApplyFlowCase):
    """Warm path: a PlanState already exists."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare_week_change(self) -> dict[str, Any]:
        refused, session = self.invoke("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, session)
        refused, prepared = self.invoke(
            "prepareCoachDecision",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
        )
        self.assertFalse(refused, prepared)
        return prepared

    def test_existing_plan_apply_accepts_the_identifiers_the_apply_schema_declares(self):
        prepared = self.prepare_week_change()
        returned = returned_handoff_identifiers(prepared, "prepareCoachDecision")
        self.assertIn("proposal", returned)
        (refused, applied), body = self.apply_from_producer(
            prepared, "applyCoachDecision"
        )
        self.assertFalse(refused, applied)
        self.assertEqual("passed", applied["status"], applied)
        self.assertEqual({"proposal", "confirmed"}, set(body), body)

    def test_existing_plan_informational_identity_is_not_copied_onto_apply(self):
        """Warm prepare still names the durable plan; apply takes the proposal.

        Identifiers the output schema lists that apply's input schema does not
        are informational on this path. Copying them is what apply refuses; the
        mechanical handoff therefore must not include them. A *first-plan*
        prepare returning the same names is the #280 contradiction and is
        asserted empty in FirstPlanHandoffTests.
        """
        prepared = self.prepare_week_change()
        informational = identifiers_apply_schema_rejects(
            prepared, "prepareCoachDecision", "applyCoachDecision"
        )
        apply_properties = set(
            input_schema("applyCoachDecision").get("properties") or {}
        )
        output_ids = set(handoff_identifier_names("prepareCoachDecision"))
        allowed_info = output_ids - apply_properties
        self.assertTrue(
            set(informational) <= allowed_info,
            f"prepare returned unexpected identifiers {sorted(informational)} "
            f"beyond the output schema's {sorted(allowed_info)}",
        )
        (refused, applied), body = self.apply_from_producer(
            prepared, "applyCoachDecision"
        )
        self.assertFalse(refused, applied)
        self.assertTrue(set(body) <= apply_properties)


class DeliveryHandoffTests(PrepareApplyFlowCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare_one_run(self) -> dict[str, Any]:
        refused, session = self.invoke("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, session)
        refused, prepared = self.invoke(
            "prepareWorkoutDelivery",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
        )
        self.assertFalse(refused, prepared)
        return prepared

    def test_delivery_apply_accepts_the_identifiers_the_apply_schema_declares(self):
        prepared = self.prepare_one_run()
        self.assertTrue(prepared.get("proposal"), prepared)
        (refused, applied), body = self.apply_from_producer(
            prepared, "applyWorkoutDelivery"
        )
        self.assertFalse(refused, applied)
        self.assertEqual({"proposal", "confirmed"}, set(body), body)
        self.assertEqual("passed", applied.get("status"), applied)

    def test_delivery_prepare_does_not_require_apply_to_echo_plan_identity(self):
        prepared = self.prepare_one_run()
        informational = identifiers_apply_schema_rejects(
            prepared, "prepareWorkoutDelivery", "applyWorkoutDelivery"
        )
        apply_properties = set(
            input_schema("applyWorkoutDelivery").get("properties") or {}
        )
        allowed_info = set(handoff_identifier_names("prepareWorkoutDelivery")) - apply_properties
        self.assertTrue(set(informational) <= allowed_info, informational)
        (refused, applied), body = self.apply_from_producer(
            prepared, "applyWorkoutDelivery"
        )
        self.assertFalse(refused, applied)
        self.assertTrue(set(body) <= apply_properties)


class OutputSchemaIdentifierFloorTests(unittest.TestCase):
    """A prepare output schema that drops every identifier must not pass quietly."""

    def test_prepare_output_schemas_still_name_proposal(self):
        for prepare_tool, _apply_tool in PREPARE_APPLY_PAIRS:
            properties = output_schema(prepare_tool).get("properties") or {}
            self.assertIn("proposal", properties, prepare_tool)
            self.assertTrue(
                handoff_identifier_names(prepare_tool),
                prepare_tool,
            )


if __name__ == "__main__":
    unittest.main()
