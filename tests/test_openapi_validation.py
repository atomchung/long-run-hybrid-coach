"""Proves scripts/validate_openapi.py actually rejects a broken document.

test_openapi_contract.py is a line-level text scan and says so in its own
docstring: it would not notice a malformed schema or a dangling $ref. This
file exercises the real parser instead, against documents mutated here, at
test time, in a temp directory -- never a broken fixture committed at rest
(AGENTS.md 2: only anonymous fixtures may be committed, and a YAML file that
only exists to be invalid is not a fixture this repository needs to keep).

Every mutation below targets a failure mode the issue that added this gate
named explicitly: invalid YAML, an invalid $ref, or an invalid component
schema. One of the two $ref mutations specifically breaks a requestBody
schema, because openapi-spec-validator's own traversal does not visit
requestBody at all -- verified by reading its source -- so without
scripts/validate_openapi.py's own whole-document $ref walk, that specific
break would pass silently even though nearly every POST operation in this
file has a requestBody.

openapi-spec-validator and PyYAML are development/CI-only (AGENTS.md: runtime
stays stdlib-only). When neither is installed locally, every test below skips
itself rather than failing the suite; .github/workflows/ci.yml always installs
them, so the real check still runs on every pull request and push to main.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

# The REST entry's own harness -- reused rather than rebuilt, for the same reason
# test_mcp_gateway.py reuses it. Resolved by the documented `unittest discover -s
# tests` run, which puts this directory on the path.
from test_gateway import ONBOARDING, TOKEN_A, GatewayTestCase


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"

_VALIDATOR_AVAILABLE = (
    importlib.util.find_spec("yaml") is not None
    and importlib.util.find_spec("openapi_spec_validator") is not None
    and importlib.util.find_spec("openapi_schema_validator") is not None
)


def _load_validator_module():
    """Import scripts/validate_openapi.py, which is not an installed package."""
    spec = importlib.util.spec_from_file_location(
        "validate_openapi", ROOT / "scripts" / "validate_openapi.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_mutated_copy(tmp_dir: str, needle: str, replacement: str) -> Path:
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    assert needle in text, f"fixture assumption stale -- update the mutation: {needle!r}"
    mutated = text.replace(needle, replacement, 1)
    assert mutated != text
    path = Path(tmp_dir) / "openapi.yaml"
    path.write_text(mutated, encoding="utf-8")
    return path


class OpenApiValidationGateTests(unittest.TestCase):
    def setUp(self):
        if not _VALIDATOR_AVAILABLE:
            self.skipTest(
                "openapi-spec-validator/PyYAML not installed locally; "
                "CI always installs them, see .github/workflows/ci.yml"
            )
        self.validate_openapi = _load_validator_module()

    def _run_main_quietly(self, *args: str) -> int:
        """main() prints its report; keep that off the test runner's own output."""
        with contextlib.redirect_stdout(io.StringIO()):
            return self.validate_openapi.main(list(args))

    def test_the_real_document_passes_every_layer(self):
        problems = self.validate_openapi.validate_document(OPENAPI_PATH)
        self.assertEqual([], problems)

    def test_main_exits_zero_for_the_real_document(self):
        self.assertEqual(0, self._run_main_quietly(str(OPENAPI_PATH)))

    def test_a_dangling_ref_in_a_response_schema_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            broken_path = _write_mutated_copy(
                tmp_dir,
                '$ref: "#/components/schemas/HealthResponse"',
                '$ref: "#/components/schemas/DoesNotExistSchema"',
            )
            problems = self.validate_openapi.validate_document(broken_path)
            exit_code = self._run_main_quietly(str(broken_path))

        self.assertTrue(problems, "a dangling $ref must fail validation")
        self.assertTrue(any("DoesNotExistSchema" in p for p in problems), problems)
        self.assertNotEqual(0, exit_code)

    def test_a_dangling_ref_under_requestbody_fails_validation(self):
        """The gap openapi-spec-validator itself has: it never walks requestBody."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            broken_path = _write_mutated_copy(
                tmp_dir,
                '$ref: "#/components/schemas/SessionRequest"',
                '$ref: "#/components/schemas/DoesNotExistRequestBodySchema"',
            )
            problems = self.validate_openapi.validate_document(broken_path)
            exit_code = self._run_main_quietly(str(broken_path))

        self.assertTrue(
            problems, "a dangling $ref under requestBody must fail validation"
        )
        self.assertTrue(
            any("DoesNotExistRequestBodySchema" in p for p in problems), problems
        )
        self.assertNotEqual(0, exit_code)

    def test_an_invalid_component_schema_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            broken_path = _write_mutated_copy(
                tmp_dir,
                "    HealthResponse:\n      type: object\n",
                "    HealthResponse:\n      type: not_a_real_type\n",
            )
            problems = self.validate_openapi.validate_document(broken_path)
            exit_code = self._run_main_quietly(str(broken_path))

        self.assertTrue(problems, "an invalid component schema must fail validation")
        self.assertTrue(any("HealthResponse" in p for p in problems), problems)
        self.assertNotEqual(0, exit_code)

    def test_invalid_yaml_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            broken_path = Path(tmp_dir) / "openapi.yaml"
            broken_path.write_text(
                "openapi: 3.1.0\ninfo:\n  title: [unterminated\n", encoding="utf-8"
            )
            problems = self.validate_openapi.validate_document(broken_path)

        self.assertTrue(problems, "invalid YAML must fail validation")
        self.assertTrue(any("invalid YAML" in p for p in problems), problems)


class ResponseSchemaConformanceTests(GatewayTestCase):
    """A real gateway response, checked against the schema its own operation documents.

    Every other check in this file and in test_openapi_contract.py proves the *document*
    is well-formed OpenAPI, or that its routes and required property names match
    ``garmin_coach_loop.gateway.ROUTES``; none of them ever construct a real response and
    hold it against a ``components.schemas`` entry. This is deliberately narrow -- one
    record and one retraction per athlete-evidence family, the surface Stage 1 of the
    retraction split just tightened -- rather than a full contract fuzzer: it exists to
    catch exactly the kind of drift the response-required review finding (#157) named, a
    field the schema calls required that the code does not always return.

    Skips locally without openapi-spec-validator/PyYAML, the same as every other test in
    this file; CI always installs them.
    """

    def setUp(self):
        if not _VALIDATOR_AVAILABLE:
            self.skipTest(
                "openapi-spec-validator/PyYAML not installed locally; "
                "CI always installs them, see .github/workflows/ci.yml"
            )
        super().setUp()
        import yaml

        self.schemas = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))[
            "components"
        ]["schemas"]
        self.owner_id = self.seed_owner(TOKEN_A)

    def _assert_conforms(self, payload: dict[str, Any], schema_name: str) -> None:
        """Both directions, because drift happens in both and only one was checked.

        The validator answers "is every field the schema requires actually here", which
        is what the #157 review finding was about. It cannot answer the opposite --
        "does the schema declare every field this actually returns" -- because these
        schemas do not set ``additionalProperties: false``, and setting it would change
        what a real OpenAPI client accepts rather than what this repository promises.

        So the second direction is asserted here instead. A handler that grows a field
        nobody documents is drift the same way a documented field nobody returns is: the
        model reading the contract never learns the field exists, which for
        ``prepareCoachInitialization``'s ``athlete_profile`` meant never showing the
        athlete the timezone their whole first week was computed in.
        """
        from openapi_schema_validator import OAS31Validator

        # Rooted at a document carrying every schema, so a `$ref` to a sibling component
        # resolves. Validating the bare entry silently worked only while the entries
        # under test held no refs, which stopped being true the moment this reached an
        # operation with a real preview in it.
        schema = {**self.schemas[schema_name], "components": {"schemas": self.schemas}}
        OAS31Validator(schema).validate(payload)
        undeclared = sorted(set(payload) - set(self.schemas[schema_name].get("properties") or {}))
        self.assertEqual(
            [],
            undeclared,
            f"{schema_name} does not declare field(s) the gateway returns: "
            f"{', '.join(undeclared)}",
        )

    def test_a_strength_record_and_its_retraction_each_conform(self):
        status, recorded = self.call(
            "POST",
            "/v1/coach/strength-report",
            body={"exercise": "bench press", "sets": [{"weight_kg": 65, "reps": 4}]},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, recorded)
        self._assert_conforms(recorded, "StrengthReportResponse")

        status, retracted = self.call(
            "POST",
            "/v1/coach/record/retract",
            body={"kind": "strength_execution", "exercise": "bench press"},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, retracted)
        self._assert_conforms(retracted, "RetractRecordResponse")

    def test_a_body_measurement_and_its_retraction_each_conform(self):
        status, recorded = self.call(
            "POST", "/v1/coach/body-measurement", body={"weight_kg": 72.5}, token=TOKEN_A
        )
        self.assertEqual(200, status, recorded)
        self._assert_conforms(recorded, "BodyMeasurementResponse")

        status, retracted = self.call(
            "POST",
            "/v1/coach/record/retract",
            body={"kind": "body_measurement"},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, retracted)
        self._assert_conforms(retracted, "RetractRecordResponse")

    def test_the_first_plans_preview_conforms_including_what_it_was_built_on(self):
        """The operation the one-directional check could not see a missing field in.

        `athlete_profile` is returned on every prepare and was declared nowhere, so a
        model reading the contract had no reason to show the athlete the timezone their
        first week's dates came out of.
        """
        status, prepared = self.call(
            "POST",
            "/v1/coach/initialization/prepare",
            body={"initialization_request": ONBOARDING},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, prepared)
        self.assertIn("athlete_profile", prepared)
        self._assert_conforms(prepared, "InitializationPrepareResponse")

    def test_an_upload_and_the_retraction_it_makes_ambiguous_each_conform(self):
        """The import response, and the retraction branch only an upload can reach.

        Two sessions of one sport on one day is a state a conversation cannot produce,
        so `retracted: false` with `candidates` is a shape the three tests above never
        exercise.
        """
        status, imported = self.call(
            "POST",
            "/v1/coach/history/import",
            body={
                "format": "csv",
                "content": (
                    "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance\n"
                    "9001,2026-08-11 06:12:00,Morning,Run,2700,8.1\n"
                    "9002,2026-08-11 18:30:00,Evening,Run,1800,4.0\n"
                ),
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, imported)
        self.assertEqual(2, imported["counts"]["added"])
        self._assert_conforms(imported, "ImportHistoryResponse")

        status, ambiguous = self.call(
            "POST",
            "/v1/coach/record/retract",
            body={"kind": "activity_summary", "sport": "running", "date": "2026-08-11"},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, ambiguous)
        self.assertFalse(ambiguous["retracted"])
        self.assertEqual(2, len(ambiguous["candidates"]))
        self._assert_conforms(ambiguous, "RetractRecordResponse")

    def test_an_activity_summary_and_its_retraction_each_conform(self):
        status, recorded = self.call(
            "POST",
            "/v1/coach/activity-summary",
            body={"sport": "running", "duration_minutes": 40},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, recorded)
        self._assert_conforms(recorded, "ActivitySummaryResponse")

        status, retracted = self.call(
            "POST",
            "/v1/coach/record/retract",
            body={"kind": "activity_summary", "sport": "running"},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, retracted)
        self._assert_conforms(retracted, "RetractRecordResponse")


if __name__ == "__main__":
    unittest.main()
