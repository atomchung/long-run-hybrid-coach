"""Structural check that entrypoints/custom-gpt/openapi.yaml matches gateway.ROUTES.

This is a line-level, fixed-indentation text scan over the YAML file -- stdlib only, no
PyYAML, no real YAML parser. It reads `paths:` entries, HTTP methods, `operationId:` and
`x-openai-isConsequential:` values by exact indentation (2 spaces per nesting level, the
style the file is hand-written in). It does NOT validate general YAML semantics: it would
not notice a syntactically broken document elsewhere in the file, a malformed schema under
`components:`, or any nesting style other than the one this file uses. Its only job is to
keep the documented routes honest against the one source of truth, garmin_coach_loop.gateway.

One test here reaches past the schema into the setup README next to it: the OAuth scope set
is a single fact that fails at authorization time if the two disagree.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.gateway import ROUTES


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"
SETUP_README_PATH = ROOT / "entrypoints" / "custom-gpt" / "README.md"
INSTRUCTIONS_PATH = ROOT / "entrypoints" / "custom-gpt" / "instructions.md"
# Builder saves fail above the observed <8000-character boundary.  Keep a useful margin
# rather than treating the platform's undocumented maximum as a target.
MAX_CUSTOM_GPT_INSTRUCTION_CHARACTERS = 7600

_PATH_LINE = re.compile(r"^  (/\S+):\s*$")
_METHOD_LINE = re.compile(r"^    (get|post|put|delete|patch|options|head|trace):\s*$")
_OPERATION_ID_LINE = re.compile(r"^      operationId:\s*(\S+)\s*$")
_CONSEQUENTIAL_LINE = re.compile(r"^      x-openai-isConsequential:\s*(\S+)\s*$")
_SCHEMA_LINE = re.compile(r"^    (\w+):\s*$")
_SCHEMA_PROPERTY_LINE = re.compile(r"^        (\w+):\s*$")
_REQUESTED_SCOPE_LINE = re.compile(r'^      - "([A-Z]+:[A-Z]+)"\s*$')
_DEFINED_SCOPE_LINE = re.compile(r'^            "([A-Z]+:[A-Z]+)":\s+\S')
# A backticked, comma-joined scope list in prose -- the form the operator copies. One
# scope named alone is prose about a scope, not an instruction to request it.
_SCOPE_LIST_IN_PROSE = re.compile(r"`([A-Z]+:[A-Z]+(?:,[A-Z]+:[A-Z]+)+)`")

# What the registered Intervals.icu application actually holds (issue #41). CALENDAR:WRITE
# carries calendar read access there, so a separate CALENDAR:READ would ask for a scope the
# registration never granted -- and the provider refuses the whole authorization rather
# than the surplus scope alone, so the consent page never appears at all.
REGISTERED_SCOPES = ["ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:READ"]

# What the model may never be asked for on a plan change (issue #71): the product's own
# artifacts, and the mechanical fields inside them. Every one of these is derived by the
# gateway from the current PlanState, so a schema that names one has handed it back.
FORBIDDEN_CHANGE_REQUEST_PROPERTIES = {
    "after_plan",
    "decision_event",
    "plan_id",
    "plan_version",
    "plan_version_before",
    "plan_version_after",
    "version",
    "schema_version",
    "event_id",
    "created_at",
    "mode",
    "inputs_used",
    "hard",
    "execution",
    "delivery_state",
    "external_id",
    "publish_supported",
    "match_status",
    "proposal",
    "proposal_hash",
    # Prose is an output (issue #93): the gateway renders it from `plan`, so a request
    # schema naming it would be handing the model back the one field it must not write.
    "prescription",
    "structured_workout",
    "strength_movements",
}

# The same rule on the first plan (issue #86), plus the three a first plan alone could
# leak: the whole artifact, the status only the store sets, and the session ids the
# gateway names. A change request legitimately carries session_id -- it points at a
# session that already exists -- but an initialization has nothing to point at.
FORBIDDEN_INITIALIZATION_PROPERTIES = FORBIDDEN_CHANGE_REQUEST_PROPERTIES | {
    "initial_plan",
    "status",
    "session_id",
}

# ROUTES "kind" -> the operationId the plan requires for it. "token" is deliberately
# absent: the OAuth token endpoint must never be a documented Action operation.
EXPECTED_OPERATION_IDS = {
    "health": "healthCheck",
    "session": "startCoachSession",
    "permissions": "inspectIntervalsPermissions",
    "availability_record": "recordAthleteAvailability",
    "strength_report": "recordStrengthExecution",
    "initialization_prepare": "prepareCoachInitialization",
    "initialization_apply": "initializeCoachPlan",
    "decision_prepare": "prepareCoachDecision",
    "decision_apply": "applyCoachDecision",
    "delivery_prepare": "prepareWorkoutDelivery",
    "delivery_publish": "publishWorkoutDelivery",
    "withdrawal_prepare": "prepareDeliveryWithdrawal",
    "withdrawal_apply": "applyDeliveryWithdrawal",
    "delivery_attempt_clear": "clearDeliveryAttempt",
}

# Operations that default to OpenAI's "consequential" (write) behavior on purpose, so the
# platform still asks its own confirmation even though the GPT's own instructions already
# ask for one. Every other documented operation must opt out with the literal flag.
CONSEQUENTIAL_OPERATION_IDS = {
    "initializeCoachPlan",
    "applyCoachDecision",
    "publishWorkoutDelivery",
    # Removing a workout from the athlete's calendar is as outward as putting one there.
    "applyDeliveryWithdrawal",
    # Nothing leaves the gateway here, but it ends the product's own tracking of writes
    # that may be sitting on the athlete's calendar. That is not a read (issue #16).
    "clearDeliveryAttempt",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _top_level_block(lines: list[str], key: str) -> tuple[int, int]:
    """Return the (start, end) line-index range of a top-level ``key:`` block.

    ``start`` is the index right after the ``key:`` line; ``end`` is the index of the next
    zero-indent, non-blank line (or len(lines) if the key's block runs to EOF).
    """
    key_line = f"{key}:"
    start = next(i for i, line in enumerate(lines) if line == key_line) + 1
    end = len(lines)
    for i in range(start, len(lines)):
        line = lines[i]
        if line.strip() and _indent(line) == 0:
            end = i
            break
    return start, end


def _extract_paths(lines: list[str]) -> dict[str, dict[str, Any]]:
    """Map every documented path to its method, operationId, and consequential flag."""
    paths_start, paths_end = _top_level_block(lines, "paths")

    path_starts: list[tuple[str, int]] = []
    for i in range(paths_start, paths_end):
        match = _PATH_LINE.match(lines[i])
        if match:
            path_starts.append((match.group(1), i))

    result: dict[str, dict[str, Any]] = {}
    for index, (path, start) in enumerate(path_starts):
        block_end = path_starts[index + 1][1] if index + 1 < len(path_starts) else paths_end
        block = lines[start + 1 : block_end]

        methods = [m.group(1).upper() for line in block if (m := _METHOD_LINE.match(line))]
        assert len(methods) == 1, f"{path}: expected exactly one HTTP method, found {methods}"

        operation_ids = [
            m.group(1) for line in block if (m := _OPERATION_ID_LINE.match(line))
        ]
        assert len(operation_ids) == 1, f"{path}: expected exactly one operationId, found {operation_ids}"

        consequential_false = any(
            (m := _CONSEQUENTIAL_LINE.match(line)) and m.group(1) == "false" for line in block
        )

        result[path] = {
            "method": methods[0],
            "operationId": operation_ids[0],
            "isConsequentialFalse": consequential_false,
        }
    return result


def _requested_scopes(lines: list[str]) -> list[str]:
    """The scopes the top-level ``security`` block asks for, in file order."""
    start, end = _top_level_block(lines, "security")
    return [
        match.group(1)
        for line in lines[start:end]
        if (match := _REQUESTED_SCOPE_LINE.match(line))
    ]


def _defined_scopes(lines: list[str]) -> list[str]:
    """The scopes the OAuth security scheme documents, in file order."""
    return [match.group(1) for line in lines if (match := _DEFINED_SCOPE_LINE.match(line))]


def _schema_block(lines: list[str], name: str) -> list[str]:
    """The lines of one ``components.schemas`` entry, by the same indentation rule."""
    start = next(
        index
        for index, line in enumerate(lines)
        if (match := _SCHEMA_LINE.match(line)) and match.group(1) == name
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and _indent(line) <= 4:
            end = index
            break
    return lines[start + 1 : end]


def _schema_properties(lines: list[str], name: str) -> set[str]:
    """The schema's own top-level property names; nested objects keep their own."""
    return {
        match.group(1)
        for line in _schema_block(lines, name)
        if (match := _SCHEMA_PROPERTY_LINE.match(line))
    }


class OpenApiContractTests(unittest.TestCase):
    def setUp(self):
        self.text = OPENAPI_PATH.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()
        self.documented = _extract_paths(self.lines)

    def test_every_documented_path_and_method_matches_a_real_route(self):
        for path, entry in self.documented.items():
            self.assertIn(path, ROUTES, f"{path} is documented but not a real gateway route")
            expected_method, _ = ROUTES[path]
            self.assertEqual(
                expected_method, entry["method"], f"{path} documents the wrong HTTP method"
            )

    def test_every_coach_and_health_route_is_documented(self):
        for path, (_, kind) in ROUTES.items():
            if kind in ("token", "authorize"):
                continue
            self.assertIn(path, self.documented, f"{path} is a real route but undocumented")

    def test_oauth_endpoints_are_not_documented_operations(self):
        # Both OAuth endpoints are plumbing the GPT editor's auth config points at,
        # not callable Action operations.
        for path in ("/oauth/intervals/token", "/oauth/intervals/authorize"):
            self.assertNotIn(
                path,
                self.documented,
                f"{path} is plumbing, not a callable Action operation",
            )

    def test_operation_ids_match_exactly_what_each_route_requires(self):
        for path, entry in self.documented.items():
            _, kind = ROUTES[path]
            self.assertEqual(EXPECTED_OPERATION_IDS[kind], entry["operationId"])

    def test_consequential_write_operations_omit_the_false_override(self):
        for entry in self.documented.values():
            if entry["operationId"] in CONSEQUENTIAL_OPERATION_IDS:
                self.assertFalse(
                    entry["isConsequentialFalse"],
                    f"{entry['operationId']} must default to consequential (no override)",
                )

    def test_read_only_operations_declare_themselves_non_consequential(self):
        for entry in self.documented.values():
            if entry["operationId"] not in CONSEQUENTIAL_OPERATION_IDS:
                self.assertTrue(
                    entry["isConsequentialFalse"],
                    f"{entry['operationId']} must carry x-openai-isConsequential: false",
                )

    def test_health_schema_strictly_requires_both_runtime_identities(self):
        block = _schema_block(self.lines, "HealthResponse")
        text = "\n".join(block)
        self.assertEqual(
            {
                "status",
                "error",
                "api_version",
                "release_identity",
                "deployment_identity",
            },
            _schema_properties(self.lines, "HealthResponse"),
        )
        self.assertIn("      additionalProperties: false", block)
        for field in (
            "status",
            "error",
            "api_version",
            "release_identity",
            "deployment_identity",
        ):
            self.assertIn(f"        - {field}", block)
        for field in ("environment", "instance_id", "configuration_binding"):
            self.assertIn(f"            {field}:", text)
        for forbidden in (
            "state_root",
            "client_id",
            "client_secret",
            "token",
            "owner",
        ):
            self.assertNotIn(f"            {forbidden}:", text)

    def test_requested_scopes_are_exactly_the_ones_the_registration_grants(self):
        """Asking for one scope too many costs the whole authorization (issue #97)."""
        self.assertEqual(REGISTERED_SCOPES, _requested_scopes(self.lines))
        self.assertEqual(REGISTERED_SCOPES, _defined_scopes(self.lines))

        # The operator copies this string into the GPT editor by hand, so a README that
        # drifted from the schema fails the connection just as completely. Only the
        # copyable scope lists are checked; prose is free to name a scope to warn about it.
        setup = SETUP_README_PATH.read_text(encoding="utf-8")
        offered = _SCOPE_LIST_IN_PROSE.findall(setup)
        self.assertEqual([",".join(REGISTERED_SCOPES)], offered)

    def test_security_scheme_names_the_real_intervals_and_gateway_urls(self):
        self.assertIn("https://intervals.icu/oauth/authorize", self.text)
        self.assertIn("/oauth/intervals/token", self.text)

    def test_server_and_token_url_use_the_placeholder_domain_only(self):
        self.assertIn("YOUR-GATEWAY-DOMAIN", self.text)

    def test_the_decision_actions_ask_for_a_change_request_not_product_artifacts(self):
        """The plan-write contract the model is actually able to satisfy (issue #71)."""
        for schema in ("DecisionPrepareRequest", "DecisionApplyRequest"):
            properties = _schema_properties(self.lines, schema)
            self.assertIn("change_request", properties, schema)
            self.assertNotIn("after_plan", properties, schema)
            self.assertNotIn("decision_event", properties, schema)

    def test_the_change_request_schema_names_nothing_mechanical(self):
        for schema in ("CoachChangeRequest", "SessionChange"):
            forbidden = _schema_properties(self.lines, schema) & FORBIDDEN_CHANGE_REQUEST_PROPERTIES
            self.assertEqual(set(), forbidden, f"{schema} asks the model for {forbidden}")

    def test_the_initialization_actions_ask_for_a_request_not_a_plan_state(self):
        """The first-plan contract the model is actually able to satisfy (issue #86)."""
        for schema in ("InitializationPrepareRequest", "InitializationApplyRequest"):
            properties = _schema_properties(self.lines, schema)
            self.assertIn("initialization_request", properties, schema)
            self.assertNotIn("initial_plan", properties, schema)
        # Nor handed back one to round-trip: the apply route re-derives the candidate.
        self.assertNotIn(
            "initial_plan", _schema_properties(self.lines, "InitializationPrepareResponse")
        )
        self.assertNotIn("initial_plan", self.text)

    def test_the_initialization_request_schema_names_nothing_mechanical(self):
        for schema in ("CoachInitializationRequest", "InitialSession"):
            forbidden = (
                _schema_properties(self.lines, schema) & FORBIDDEN_INITIALIZATION_PROPERTIES
            )
            self.assertEqual(set(), forbidden, f"{schema} asks the model for {forbidden}")

    def test_every_session_shape_carries_one_plan_and_no_prose(self):
        """The Coach has nowhere to author a prescription (issue #93).

        Not "must not" but "cannot": a request schema that named the field would let the
        sentence and the structure disagree again, and a rule saying not to is exactly
        what this repository already had, five repairs running. The sentence is rendered
        from `plan` and comes back in the preview.
        """
        for schema in ("InitialSession", "SessionChange"):
            properties = _schema_properties(self.lines, schema)
            self.assertIn("plan", properties, schema)
            self.assertNotIn("prescription", properties, schema)
            self.assertNotIn("structured_workout", properties, schema)
            self.assertNotIn("strength_movements", properties, schema)

    def test_the_three_execution_models_are_the_only_ones_documented(self):
        # Adding a sport must be one `sport` enum value reusing one of these, not a
        # fourth shape -- so the union stays closed at three.
        self.assertEqual(
            ["TimeAxisPlan", "MovementListPlan", "UnstructuredPlan"],
            [
                line.split("/")[-1].strip('"')
                for line in _schema_block(self.lines, "SessionPlan")
                if "$ref:" in line
            ],
        )

    def test_a_change_can_carry_the_structure_a_birth_can(self):
        """Issue #92/#100: what a session gets at birth it can also get through a change.

        Not two lists of fields kept in step, which is what let the change path fall a
        repair behind the initialization path in the first place -- one `$ref`, to the
        same union, from both. A model added to `SessionPlan` reaches both shapes or
        neither.
        """
        refs = {
            schema: [
                line.split(":", 1)[1].strip().strip('"')
                for line in _schema_block(self.lines, schema)
                if line.strip().startswith("$ref:") and "SessionPlan" in line
            ]
            for schema in ("InitialSession", "SessionChange")
        }
        self.assertEqual(
            {
                "InitialSession": ["#/components/schemas/SessionPlan"],
                "SessionChange": ["#/components/schemas/SessionPlan"],
            },
            refs,
        )

    def test_both_confirmation_routes_bind_the_same_kind_of_proposal(self):
        for schema in ("DecisionApplyRequest", "InitializationApplyRequest"):
            properties = _schema_properties(self.lines, schema)
            self.assertIn("proposal", properties, schema)
            self.assertNotIn("proposal_hash", properties, schema)
        for schema in ("DecisionPrepareResponse", "InitializationPrepareResponse"):
            properties = _schema_properties(self.lines, schema)
            self.assertIn("proposal", properties, schema)
            self.assertIn("expires_at", properties, schema)

    def test_the_delivery_confirmation_contract_is_left_alone(self):
        for schema in ("DeliveryPublishRequest", "DeliveryPrepareResponse"):
            self.assertIn("proposal_hash", _schema_properties(self.lines, schema), schema)

    def test_custom_gpt_instructions_fit_builder_budget_and_keep_the_contract(self):
        instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(
            len(instructions),
            MAX_CUSTOM_GPT_INSTRUCTION_CHARACTERS,
            "Builder rejects instructions near 8000 characters; retain the release buffer",
        )
        required = (
            "`startCoachSession`",
            "only source of truth",
            "not chat memory",
            "`inspectIntervalsPermissions`",
            "no PlanState or coaching-session",
            "`granted_scopes`",
            "`readable` = 200",
            "`denied` = 403",
            "`invalid_or_expired` = 401",
            "Settings values, tokens, fingerprints",
            "athlete ids, or owner",
            "`prepareCoachInitialization`",
            "`initializeCoachPlan`",
            "identical `initialization_request`",
            "ONE confirmation",
            "`prepareCoachDecision`",
            "`applyCoachDecision`",
            "identical `context`, `change_request`",
            "`goal_context.measurement_protocol`",
            "Monday-Sunday",
            "`prepareWorkoutDelivery`",
            "`publishWorkoutDelivery`",
            "`intervals_accepted`",
            "Garmin Connect or the watch",
            "`status: \"partial\"`",
            "`attempt_open: true`",
            "`prepareDeliveryWithdrawal`",
            "`applyDeliveryWithdrawal`",
            "never withdraw a past workout",
            # The recovery path only works if the model asks first and clears second.
            "`clearDeliveryAttempt`",
            "Never clear on your own initiative",
            "`stale_plan_version`",
            "reconnect Intervals",
        )
        for phrase in required:
            self.assertIn(phrase, instructions, phrase)


if __name__ == "__main__":
    unittest.main()
