"""The model-facing output contract: outputSchema, structuredContent, and the projection.

Three claims, each pinned against the real loopback server:

1. **Conformity.** Every tool result carries `structuredContent`, it is exactly the text
   block parsed, and it conforms to the tool's own declared `outputSchema` -- the
   2025-06-18 obligation a declared output schema creates.
2. **Projection.** No workflow tool result carries the store's audit material -- receipt
   ids, content-hash record ids, provider event ids on apply responses, response
   envelopes -- while the REST entry, asked the same thing, still does. The store keeps
   its record; the model reads the projection.
3. **Sufficiency.** The projected payloads alone drive every multi-call workflow:
   prepare/apply for decisions and deliveries, a partial delivery retried to convergence
   with the same opaque set, and an open reservation read back and cleared. Nothing the
   projection removed is anything a following call needs.

The validator below is deliberately a subset of JSON Schema -- `type`, `properties`,
`required`, `items`, `enum` -- because that is the whole vocabulary the output schemas
use, and this suite runs on a bare Python 3.11 (AGENTS.md: no installed dependencies).
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from garmin_coach_loop.mcp_transport import TOOLS, TOOLS_BY_NAME, _redact

from test_gateway import (
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    TOKEN_B,
    WEEKLY_CHANGE,
    publishable_plan,
)
from test_mcp_gateway import McpTestCase

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def schema_violations(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Every way `instance` fails `schema`, in the subset the output schemas use."""
    problems: list[str] = []
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS[name](instance) for name in names):
            problems.append(f"{path}: {type(instance).__name__} is not {declared}")
            return problems
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, dict):
        for name in schema.get("required", ()):
            if name not in instance:
                problems.append(f"{path}: missing required {name!r}")
        for name, subschema in schema.get("properties", {}).items():
            if name in instance:
                problems.extend(schema_violations(instance[name], subschema, f"{path}.{name}"))
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            problems.extend(schema_violations(item, schema["items"], f"{path}[{index}]"))
    return problems


# What no workflow tool's projected result may carry, anywhere the projection reaches.
# `external_id` is deliberately absent: on the session and state views it is delivery
# evidence the orchestration prompt reads (`superseded_external_id` decides withdrawals),
# so its removal is asserted per-path below, not globally.
FORBIDDEN_KEYS = frozenset(
    {
        "api_version",
        "generated_at",
        "athlete_evidence_version",
        "receipt_id",
        "proposal_id",
        "import_id",
        "event_id",
        "readback_hash",
        "owned_external_id",
        "report_id",
        "measurement_id",
        "summary_id",
        "state_id",
        "dedup_keys",
    }
)

# Subtrees the scan must not enter. Each is either bytes an approval hash covers
# (`context`, `delivery_set`), a contract owned elsewhere and served whole
# (`plan_state` and the views assembled from it), or the athlete's own archive
# (`athlete_evidence`, `decision_history` on the export).
OPAQUE_SUBTREES = frozenset(
    {"context", "plan_state", "delivery_set", "athlete_evidence", "decision_history"}
)

# The account-lifecycle tools serve records, not workflow results: an archive's own
# timestamps, version tags and deletion receipt are content the athlete is owed.
LIFECYCLE_TOOLS = frozenset({"exportOwnerData", "prepareOwnerDeletion", "applyOwnerDeletion"})
LIFECYCLE_ALLOWED = frozenset({"api_version", "generated_at", "archive_version", "receipt_id"})


def forbidden_hits(node: Any, *, allowed: frozenset[str] = frozenset(), path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS and key not in allowed:
                hits.append(f"{path}.{key}")
            if key in OPAQUE_SUBTREES:
                continue
            hits.extend(forbidden_hits(value, allowed=allowed, path=f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            hits.extend(forbidden_hits(item, allowed=allowed, path=f"{path}[{index}]"))
    return hits


class RedactionMechanicsTests(unittest.TestCase):
    """`_redact` itself: path semantics the projection tables rely on."""

    def test_a_named_path_is_removed_and_nothing_beside_it(self):
        projected = _redact(
            {"keep": 1, "drop": 2, "nest": {"drop": 3, "keep": 4}},
            (("drop",), ("nest", "drop")),
        )
        self.assertEqual({"keep": 1, "nest": {"keep": 4}}, projected)

    def test_a_star_steps_into_every_list_element(self):
        projected = _redact(
            {"rows": [{"id": "a", "keep": 1}, {"id": "b"}, {"keep": 2}]},
            (("rows", "*", "id"),),
        )
        self.assertEqual({"rows": [{"keep": 1}, {}, {"keep": 2}]}, projected)

    def test_a_path_absent_from_this_branch_is_not_an_error(self):
        payload = {"status": "passed"}
        self.assertEqual(payload, _redact(payload, (("delivered", "*", "external_id"),)))

    def test_an_unnamed_subtree_is_the_same_object_not_a_copy(self):
        opaque = {"items": [{"owned_external_id": "gcl:keep"}]}
        payload = {"delivery_set": opaque, "receipt_id": "r"}
        projected = _redact(payload, (("receipt_id",),))
        self.assertIs(opaque, projected["delivery_set"])

    def test_the_input_payload_is_never_mutated(self):
        payload = {"drop": 1, "rows": [{"id": "a"}]}
        _redact(payload, (("drop",), ("rows", "*", "id")))
        self.assertEqual({"drop": 1, "rows": [{"id": "a"}]}, payload)


class CatalogueOutputSchemaTests(unittest.TestCase):
    """What the served catalogue now declares about every tool's result."""

    def test_every_tool_declares_an_object_output_schema(self):
        for tool in TOOLS:
            descriptor = tool.descriptor()
            self.assertIn("outputSchema", descriptor, tool.name)
            self.assertEqual("object", descriptor["outputSchema"].get("type"), tool.name)
            self.assertIn("status", descriptor["outputSchema"]["properties"], tool.name)

    def test_required_never_claims_more_than_every_branch_serves(self):
        # `status` is the one key every branch of every tool carries; anything more in
        # `required` would turn a legitimate branch into a client-side validation failure.
        for tool in TOOLS:
            self.assertEqual(["status"], tool.output_schema["required"], tool.name)

    def test_no_output_schema_advertises_a_redacted_key(self):
        # The schema describes the projection, so a schema naming a key the projection
        # removes would promise the model something it never receives.
        for tool in TOOLS:
            allowed = LIFECYCLE_ALLOWED if tool.name in LIFECYCLE_TOOLS else frozenset()
            advertised = set(tool.output_schema["properties"])
            leaked = {
                key for key in advertised if key in FORBIDDEN_KEYS and key not in allowed
            }
            self.assertEqual(set(), leaked, tool.name)

    def test_the_lifecycle_tools_are_the_only_ones_left_unprojected(self):
        for tool in TOOLS:
            if tool.name in LIFECYCLE_TOOLS:
                self.assertEqual((), tool.redactions, tool.name)
            else:
                self.assertIn(("api_version",), tool.redactions, tool.name)
                self.assertIn(("generated_at",), tool.redactions, tool.name)


class OutputContractCase(McpTestCase):
    """One helper: call a tool over MCP and hold its result to the whole contract."""

    def setUp(self):
        super().setUp()
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def checked(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.tool_result(name, arguments)
        self.assertNotEqual(True, result.get("isError"), result)
        self.assertIn("structuredContent", result, name)
        structured = result["structuredContent"]
        self.assertEqual(self.tool_payload(result), structured, name)
        self.assertEqual(
            [], schema_violations(structured, TOOLS_BY_NAME[name].output_schema), name
        )
        allowed = LIFECYCLE_ALLOWED if name in LIFECYCLE_TOOLS else frozenset()
        self.assertEqual([], forbidden_hits(structured, allowed=allowed), name)
        return structured

    def prepare_delivery(self, session_ids: list[str]) -> dict[str, Any]:
        current = self.checked("startCoachSession", {"all_clear": True})
        return self.checked(
            "prepareWorkoutDelivery",
            {
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": session_ids,
            },
        )


class EveryToolMeetsTheContractTests(OutputContractCase):
    """Each of the 22 tools, exercised for real, held to conformity and projection."""

    def test_the_session_state_and_permissions_reads(self):
        self.checked("startCoachSession", {"all_clear": True})
        self.checked("getCoachState")
        self.checked("inspectIntervalsPermissions")

    def test_the_conversational_record_tools(self):
        self.checked("recordAthleteProfile", {"timezone": "Asia/Taipei"})
        self.checked("recordAthleteAvailability", {"recurring": {"available_days": ["mon", "wed", "sat"]}})
        self.checked(
            "recordLongTermGoal",
            {"metric": "half marathon", "target": "sub 2:00", "target_date": "2026-12-31"},
        )
        self.checked(
            "recordTrainingPreference",
            {"topic": "long run day", "statement": "long runs feel best on Saturday"},
        )
        self.checked(
            "recordStrengthExecution",
            {"exercise": "bench press", "sets": [{"weight_kg": 65, "reps": 4}]},
        )
        self.checked("recordBodyMeasurement", {"weight_kg": 72.5})
        self.checked(
            "recordActivitySummary", {"sport": "running", "duration_minutes": 40}
        )
        self.checked("recordSubjectiveState", {"note": "這幾天覺得很累"})
        replaced = self.checked("recordBodyMeasurement", {"weight_kg": 72.3})
        # The displaced row survives the projection -- what was said is coaching
        # evidence; only the store's content hash stays behind.
        self.assertEqual(72.5, replaced["replaced"]["weight_kg"])

    def test_the_import_and_retraction_pair(self):
        imported = self.checked(
            "importAthleteHistory",
            {
                "format": "records",
                "records": [
                    {"date": "2026-08-10", "sport": "running", "duration_minutes": 35}
                ],
            },
        )
        self.assertEqual(1, imported["counts"]["added"])
        self.checked("recordSubjectiveState", {"note": "record to take back"})
        retracted = self.checked("retractAthleteRecord", {"kind": "subjective_state"})
        self.assertTrue(retracted["retracted"])
        self.assertEqual("record to take back", retracted["removed"]["note"])

    def test_the_prescribed_strength_confirmation(self):
        confirmed = self.checked(
            "confirmPrescribedStrength", {"session_id": "strength-full-01"}
        )
        self.assertTrue(confirmed["movements"])

    def test_the_decision_pair_runs_on_projected_payloads_alone(self):
        session = self.checked("startCoachSession", {"all_clear": True})
        shared = {
            "plan_id": session["plan_state"]["plan_id"],
            "plan_version": session["plan_state"]["plan_version"],
            "context": session["context"],
            "change_request": WEEKLY_CHANGE,
        }
        prepared = self.checked("prepareCoachDecision", shared)
        applied = self.checked(
            "applyCoachDecision",
            {**shared, "proposal": prepared["proposal"], "confirmed": True},
        )
        self.assertEqual(prepared["resulting_version"], applied["plan_version"])

    def test_the_delivery_pair_runs_on_projected_payloads_alone(self):
        prepared = self.prepare_delivery(["run-quality-01"])
        published = self.checked(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        self.assertEqual("passed", published["status"])
        self.assertEqual("intervals_accepted", published["delivery_state"])
        self.assertEqual(
            ["run-quality-01"],
            [item["session_id"] for item in published["delivered"]],
        )

    def test_a_partial_delivery_retries_to_convergence_on_the_same_projection(self):
        prepared = self.prepare_delivery(["run-quality-01", "run-long-01"])
        # The injection key comes off the opaque set itself: the preview no longer
        # carries `owned_external_id`, and the set must -- its bytes are what the
        # confirmed hash covers.
        self.fake.corrupt_external_ids.add(
            prepared["delivery_set"]["items"][1]["owned_external_id"]
        )
        partial = self.checked(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        self.assertEqual("partial", partial["status"])
        self.assertEqual(
            ["run-long-01"], [item["session_id"] for item in partial["unresolved"]]
        )
        self.assertTrue(partial["attempt_open"])

        self.fake.corrupt_external_ids.clear()
        converged = self.checked(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        self.assertEqual("passed", converged["status"])
        self.assertEqual([], converged["unresolved"])
        self.assertFalse(converged["attempt_open"])

    def test_an_open_reservation_is_read_back_and_cleared_from_projected_reads(self):
        prepared = self.prepare_delivery(["run-quality-01", "run-long-01"])
        self.fake.corrupt_external_ids.add(
            prepared["delivery_set"]["items"][1]["owned_external_id"]
        )
        self.checked(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        # The apply response never carries `attempt_id`; the projected state read is
        # where a model finds the one value `clearDeliveryAttempt` takes.
        state = self.checked("getCoachState")
        attempt_id = state["pending_delivery_attempt_id"]
        self.assertIsInstance(attempt_id, str)
        cleared = self.checked(
            "clearDeliveryAttempt", {"attempt_id": attempt_id, "confirmed": True}
        )
        self.assertTrue(cleared["cleared"])
        self.assertEqual(attempt_id, cleared["attempt_id"])

    def test_the_account_lifecycle_tools_serve_their_records_whole(self):
        self.checked("recordBodyMeasurement", {"weight_kg": 72.5})
        exported = self.checked("exportOwnerData")
        # The archive keeps what the projection elsewhere removes: its timestamps, its
        # version tag, and the athlete's own stored ids inside `athlete_evidence`.
        self.assertIn("generated_at", exported)
        self.assertTrue(exported["owner_reference"])
        prepared = self.checked("prepareOwnerDeletion")
        deleted = self.checked(
            "applyOwnerDeletion", {"proposal": prepared["proposal"], "confirmed": True}
        )
        self.assertTrue(deleted["deleted"])
        self.assertIn("receipt_id", deleted)


class ProjectionAgainstRestTests(OutputContractCase):
    """The same call on both entries: REST keeps the record, MCP serves the projection."""

    def test_the_rest_entry_still_serves_what_the_projection_removes(self):
        # Two owners, one identical delivery each: what separates the two responses is
        # the audience, never the operation.
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        prepared = self.prepare_delivery(["run-quality-01"])
        mcp_published = self.checked(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        status, rest_session = self.call(
            "POST", "/v1/coach/session", body={"all_clear": True}, token=TOKEN_B
        )
        self.assertEqual(200, status, rest_session)
        status, rest_prepared = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": rest_session["plan_state"]["plan_id"],
                "plan_version": rest_session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_B,
        )
        self.assertEqual(200, status, rest_prepared)
        status, rest_published = self.call(
            "POST",
            "/v1/coach/delivery/apply",
            body={
                "delivery_set": rest_prepared["delivery_set"],
                "proposal_hash": rest_prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_B,
        )
        self.assertEqual(200, status, rest_published)
        # The REST body still carries every audit key the model-facing copy does not.
        for key in ("api_version", "generated_at", "receipt_id"):
            self.assertIn(key, rest_published)
            self.assertNotIn(key, mcp_published)
        self.assertIn("external_id", rest_published["delivered"][0])
        self.assertNotIn("external_id", mcp_published["delivered"][0])
        self.assertEqual(
            rest_published["delivered"][0]["session_id"],
            mcp_published["delivered"][0]["session_id"],
        )

    def test_a_record_echo_differs_from_rest_only_by_the_store_hash(self):
        mcp_state = self.checked("recordSubjectiveState", {"note": "累"})
        status, rest_state = self.call(
            "POST",
            "/v1/coach/subjective-state",
            body={"note": "累"},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, rest_state)
        self.assertTrue(rest_state["idempotent_replay"])
        self.assertIn("state_id", rest_state["state"])
        self.assertEqual(
            {k: v for k, v in rest_state["state"].items() if k != "state_id"},
            mcp_state["state"],
        )

    def test_a_refusal_is_projected_but_still_the_gateways_own_refusal(self):
        result = self.tool_result(
            "clearDeliveryAttempt", {"attempt_id": "attempt-x", "confirmed": False}
        )
        self.assertTrue(result["isError"])
        # A refusal is not a tool result, so it carries no structuredContent for a
        # validating client to hold against the outputSchema.
        self.assertNotIn("structuredContent", result)
        payload = self.tool_payload(result)
        self.assertEqual("confirmation_required", payload["error"])
        self.assertNotIn("api_version", payload)
        self.assertNotIn("generated_at", payload)
