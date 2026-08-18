"""Structural check that entrypoints/custom-gpt/openapi.yaml matches gateway.ROUTES.

This is a line-level, fixed-indentation text scan over the YAML file -- stdlib only, no
PyYAML, no real YAML parser. It reads `paths:` entries, HTTP methods, `operationId:` and
`x-openai-isConsequential:` values by exact indentation (2 spaces per nesting level, the
style the file is hand-written in). It does NOT validate general YAML semantics: it would
not notice a syntactically broken document elsewhere in the file, a malformed schema under
`components:`, or any nesting style other than the one this file uses. Its only job is to
keep the documented routes honest against the one source of truth, garmin_coach_loop.gateway.

One test here reaches past the schema into the setup README next to it: the OAuth scope set
is a single fact that fails at authorization time if the two disagree. Another reaches into
`contracts/coach-context.schema.json` (real JSON, `json.loads` -- still no YAML parser) to
check that the hosted `context` description has not drifted from the contract it restates.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop import orchestration
from garmin_coach_loop.gateway import ROUTES, gateway_artifact_sha256
from garmin_coach_loop.release_identity import package_artifact_sha256
from scripts import release_bundle


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"
COACH_CONTEXT_SCHEMA_PATH = ROOT / "contracts" / "coach-context.schema.json"
# Where the scope string an operator copies by hand actually lives. It was the Custom
# GPT setup README while that was the only entry with a setup; every entry now
# authorizes through one Intervals application, so the entry-agnostic gateway runbook
# is the one place that string can be right for all of them.
SETUP_README_PATH = ROOT / "docs" / "deploy-gateway.md"
INSTRUCTIONS_PATH = ROOT / "garmin_coach_loop" / "orchestration.md"
# Builder saves fail above the observed <8000-character boundary.  Keep a useful margin
# rather than treating the platform's undocumented maximum as a target.
# The orchestration prompt's ceiling. It arrived as one client's paste limit and stays
# for a reason that outlived it: every MCP client is handed this file at connect time
# and carries it for the whole conversation, so a paragraph here is a paragraph of
# every future turn. Unbounded growth is also how an orchestration layer becomes a
# shadow coach (AGENTS.md 11) -- one reasonable-sounding sentence at a time. Raising
# it is a decision, not a way to fit a new paragraph; a new one costs an old one.
MAX_ORCHESTRATION_CHARACTERS = 7600

_PATH_LINE = re.compile(r"^  (/\S+):\s*$")
_METHOD_LINE = re.compile(r"^    (get|post|put|delete|patch|options|head|trace):\s*$")
_OPERATION_ID_LINE = re.compile(r"^      operationId:\s*(\S+)\s*$")
_DESCRIPTION_LINE = re.compile(r"^      description:\s*(.*)$")
_CONSEQUENTIAL_LINE = re.compile(r"^      x-openai-isConsequential:\s*(\S+)\s*$")
_SCHEMA_LINE = re.compile(r"^    (\w+):\s*$")
_SCHEMA_PROPERTY_LINE = re.compile(r"^        (\w+):\s*$")
_REQUESTED_SCOPE_LINE = re.compile(r'^      - "([A-Z]+:[A-Z]+)"\s*$')
_DEFINED_SCOPE_LINE = re.compile(r'^            "([A-Z]+:[A-Z]+)":\s+\S')
# A backticked, comma-joined scope list in prose -- the form the operator copies. One
# scope named alone is prose about a scope, not an instruction to request it.
_SCOPE_LIST_IN_PROSE = re.compile(r"`([A-Z]+:[A-Z]+(?:,[A-Z]+:[A-Z]+)+)`")

# The exact scopes every authorize query requests. The Intervals application-registration
# page has no scope field; authorization chooses them per request. CALENDAR:WRITE carries
# calendar read access there, so a separate CALENDAR:READ would ask for a nonexistent
# extra permission, and SETTINGS:WRITE likewise includes Settings read access.
#
# Confirmed against the provider's own OAuth announcement, which lists six scopes --
# ACTIVITY, WELLNESS, CALENDAR, CHATS, LIBRARY, SETTINGS -- and one modifier rule: "For
# each scope specify READ or WRITE (to update, implies READ access)". There is no
# CALENDAR:READ to ask for (forum.intervals.icu/t/intervals-icu-oauth-support/2759).
#
# What the same page also says, and what this list cannot promise, is that intervals.icu
# "will ask the user to login and display a confirmation dialog with options to choose
# which scopes to grant" -- each permission its own checkbox. So this is what the
# application *requests*; a token can come back holding less, and on 2026-08-18 one did:
# calendar reads were refused while Settings reads succeeded, which is why the diagnostic
# now performs a live calendar read instead of reporting a recorded scope (issue #162).
REGISTERED_SCOPES = ["ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE"]

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

# Routes that are platform plumbing rather than callable Action operations: the OAuth
# pair the GPT editor's auth config points at, and the MCP endpoint plus the discovery
# and registration documents an MCP client reads for itself. A Custom GPT never calls any
# of them, so this schema must not document them.
NON_ACTION_ROUTE_KINDS = {
    "token",
    "authorize",
    "gateway_authorize",
    "gateway_callback",
    "gateway_token",
    "client_registration",
    "protected_resource_metadata",
    "authorization_server_metadata",
    "openai_apps_challenge",
    "mcp",
}

# ROUTES "kind" -> the operationId the plan requires for it. Every kind in
# NON_ACTION_ROUTE_KINDS is deliberately absent.
EXPECTED_OPERATION_IDS = {
    "health": "healthCheck",
    "readiness": "readinessCheck",
    "session": "startCoachSession",
    "state": "getCoachState",
    "permissions": "inspectIntervalsPermissions",
    "profile_record": "recordAthleteProfile",
    "availability_record": "recordAthleteAvailability",
    "long_term_goal_record": "recordLongTermGoal",
    "training_preference_record": "recordTrainingPreference",
    "strength_report": "recordStrengthExecution",
    "strength_prescribed_confirm": "confirmPrescribedStrength",
    "body_measurement_record": "recordBodyMeasurement",
    "activity_summary_record": "recordActivitySummary",
    "history_import": "importAthleteHistory",
    "athlete_record_retract": "retractAthleteRecord",
    "initialization_prepare": "prepareCoachInitialization",
    "initialization_apply": "initializeCoachPlan",
    "decision_prepare": "prepareCoachDecision",
    "decision_apply": "applyCoachDecision",
    "delivery_prepare": "prepareWorkoutDelivery",
    "delivery_apply": "applyWorkoutDelivery",
    "delivery_attempt_clear": "clearDeliveryAttempt",
    "data_export": "exportOwnerData",
    "deletion_prepare": "prepareOwnerDeletion",
    "deletion_apply": "applyOwnerDeletion",
}

# Operations that default to OpenAI's "consequential" (write) behavior on purpose, so the
# platform still asks its own confirmation even though the GPT's own instructions already
# ask for one. Every other documented operation must opt out with the literal flag.
CONSEQUENTIAL_OPERATION_IDS = {
    "initializeCoachPlan",
    "applyCoachDecision",
    # Publishing and withdrawing are both outward: replacing a workout already on the
    # athlete's calendar, or removing one from it, is the same weight either direction.
    "applyWorkoutDelivery",
    # Nothing leaves the gateway here, but it ends the product's own tracking of writes
    # that may be sitting on the athlete's calendar. That is not a read (issue #16).
    "clearDeliveryAttempt",
    # The one operation this product cannot undo. The platform asking again on top of the
    # product's own confirmation is exactly right here (issue #6).
    "applyOwnerDeletion",
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
    """Map every documented path to its method, operationId, description, and flag."""
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

        description_matches = [
            (index, m.group(1))
            for index, line in enumerate(block)
            if (m := _DESCRIPTION_LINE.match(line))
        ]
        assert len(description_matches) == 1, (
            f"{path}: expected exactly one operation description, found {description_matches}"
        )
        description_index, description_value = description_matches[0]
        if description_value in {">", ">-", ">+", "|", "|-", "|+"}:
            continuation = []
            for line in block[description_index + 1 :]:
                if line.strip() and _indent(line) <= 6:
                    break
                continuation.append(line.strip())
            if description_value.startswith(">"):
                description_value = " ".join(part for part in continuation if part)
            else:
                description_value = "\n".join(continuation).rstrip("\n")

        consequential_false = any(
            (m := _CONSEQUENTIAL_LINE.match(line)) and m.group(1) == "false" for line in block
        )

        result[path] = {
            "method": methods[0],
            "operationId": operation_ids[0],
            "description": description_value,
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


def _context_field_description(lines: list[str]) -> str:
    """The flattened prose of ``SessionResponse.properties.context.description``.

    This is the one description a hosted client -- a Custom GPT, or any other entry that
    cannot read this repository -- receives for `context`; the schema otherwise declares
    it opaque (`additionalProperties: true`, no nested properties) on purpose, per issue
    #83. Folds the `description: >-` block scalar the same way `_extract_paths` folds an
    operation description, just at the indentation schema properties nest at (8) instead
    of the fixed 6-space route level.
    """
    block = _schema_block(lines, "SessionResponse")
    start = next(i for i, line in enumerate(block) if line == "        context:") + 1
    end = len(block)
    for i in range(start, len(block)):
        if block[i].strip() and _indent(block[i]) <= 8:
            end = i
            break
    field_lines = [line.strip() for line in block[start:end]]
    marker_index = next(i for i, line in enumerate(field_lines) if line.startswith("description:"))
    return " ".join(field_lines[marker_index + 1 :])


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
            if kind in NON_ACTION_ROUTE_KINDS:
                continue
            self.assertIn(path, self.documented, f"{path} is a real route but undocumented")

    def test_transport_and_oauth_endpoints_are_not_documented_operations(self):
        for path, (_, kind) in ROUTES.items():
            if kind not in NON_ACTION_ROUTE_KINDS:
                continue
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

    def test_every_operation_description_exists_and_fits_builder_limit(self):
        for path, entry in self.documented.items():
            description = entry["description"]
            self.assertTrue(description.strip(), f"{path}: operation description is empty")
            self.assertLessEqual(
                len(description),
                300,
                f"{entry['operationId']} description is {len(description)} characters",
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
                "source_git_commit",
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
            "source_git_commit",
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

    def test_the_hosted_context_description_covers_the_contracts_execution_evidence(self):
        """Issue #84: the hosted `context` description is a hand-written restatement of
        `contracts/coach-context.schema.json` for a client that cannot read this
        repository (issue #83), and nothing keeps the two in step -- issue #69 was found
        only by comparing them by hand. Not a full mirror and not a schema-diff
        framework: scoped to exactly the evidence a coach reads to judge execution --
        `cycle_sessions.activity_evidence`, `match_status`, and the whole-activity
        average caveat -- the set issue #84 names, read from the contract itself so a
        value or a caveat it adds later fails here instead of staying invisible.
        """
        contract = json.loads(COACH_CONTEXT_SCHEMA_PATH.read_text(encoding="utf-8"))
        cycle_session = contract["$defs"]["cycle_session"]["properties"]
        hosted = _context_field_description(self.lines)

        # Every activity_evidence and match_status value the contract can report must be
        # nameable, verbatim, in the hosted prose -- not just the common ones. Backtick-
        # quoted because that is how this same paragraph already names every field and
        # enum value it documents; an unquoted "missed" or "completed" would just be
        # ordinary English elsewhere in the same paragraph, not a description of the
        # value, so it would not count.
        for field in ("activity_evidence", "match_status"):
            for value in cycle_session[field]["enum"]:
                with self.subTest(field=field, value=value):
                    self.assertIn(
                        f"`{value}`",
                        hosted,
                        f"contract's {field} enum names `{value}`; "
                        "the hosted context description does not mention it",
                    )

        # The whole-activity average caveat: cycle_session_activity's average_hr and
        # average_pace_sec_per_km carry the identical contract sentence -- check that
        # instead of picking one, so the two cannot silently diverge from each other.
        activity_properties = contract["$defs"]["cycle_session_activity"]["properties"]
        caveat = activity_properties["average_hr"]["description"]
        self.assertEqual(
            caveat, activity_properties["average_pace_sec_per_km"]["description"]
        )

        def _hyphen_insensitive(text: str) -> str:
            return text.replace("-", " ")

        for phrase in ("whole activity", "warm up", "recoveries", "not a reading of the work"):
            with self.subTest(phrase=phrase):
                # Guards the phrase list itself: if the contract's own wording moves,
                # this fails on the contract side first, rather than quietly checking
                # the hosted text against a caveat the contract no longer states.
                self.assertIn(
                    phrase, _hyphen_insensitive(caveat), "contract wording moved; update this test"
                )
                self.assertIn(phrase, _hyphen_insensitive(hosted))

    def test_requested_scopes_are_exactly_the_publicly_documented_authorize_set(self):
        """Asking for one scope too many costs the whole authorization (issue #97)."""
        self.assertEqual(REGISTERED_SCOPES, _requested_scopes(self.lines))
        self.assertEqual(REGISTERED_SCOPES, _defined_scopes(self.lines))

        # The runbook gives the operator one copyable statement of the authorize set. Only
        # copyable scope lists are checked; prose is free to name a scope to explain it.
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
        for schema in ("DeliveryApplyRequest", "DeliveryPrepareResponse"):
            self.assertIn("proposal_hash", _schema_properties(self.lines, schema), schema)

    def test_both_entries_read_the_one_orchestration_file(self):
        """Issue #125: the anti-drift property, held by there being nothing to sync.

        The MCP entry serves this text as a prompt and the Custom GPT entry has it
        pasted into the Builder. Two hand-maintained copies would drift the way field
        descriptions drift, so there is one file and two readers -- which is only true
        while the release path and the runtime path name the same one.
        """
        self.assertEqual(INSTRUCTIONS_PATH, ROOT / release_bundle.INSTRUCTIONS)
        self.assertEqual(
            INSTRUCTIONS_PATH.read_text(encoding="utf-8").rstrip("\r\n"),
            orchestration.instructions(),
        )

    def test_the_gateway_artifact_digest_covers_the_text_the_gateway_serves(self):
        """A prose-only change has to move the deployed artifact identity.

        The gateway serves this file verbatim to every MCP client that fetches the
        prompt, so a digest that skipped it would call two deployments identical while
        they told two different stories about when a confirmation is required.
        """
        package = INSTRUCTIONS_PATH.parent
        without_the_prompt = package_artifact_sha256(
            [
                (path.name, path.read_bytes())
                for path in package.iterdir()
                if path.is_file()
                and path.suffix in {".py", ".md"}
                and path != INSTRUCTIONS_PATH
            ]
        )
        self.assertNotEqual(without_the_prompt, gateway_artifact_sha256())

    def test_the_orchestration_prompt_fits_its_budget_and_keeps_the_contract(self):
        instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(
            len(instructions),
            MAX_ORCHESTRATION_CHARACTERS,
            "the orchestration prompt is over budget; a new paragraph costs an old one",
        )
        # Flattened, because the file is hard-wrapped and where a line happens to break is
        # not a fact about the contract. Rewrapping a paragraph is the most ordinary edit
        # there is, and it should not be able to fail this test or, worse, pass it by
        # moving a phrase back onto one line.
        instructions = " ".join(instructions.split())
        required = (
            "`startCoachSession`",
            "only source of truth",
            "not chat memory",
            "`inspectIntervalsPermissions`",
            "no PlanState or coaching-session",
            # The two live classifications, not the recorded scope list: a diagnostic the
            # model explains from a stored value is what issue #162 cost a day to.
            "`settings_read` and `calendar_read`",
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
            "`applyWorkoutDelivery`",
            "`withdraw: true`",
            "`intervals_accepted`",
            "Garmin Connect or the watch",
            "`status: \"partial\"`",
            "`attempt_open: true`",
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
