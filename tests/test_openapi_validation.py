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


if __name__ == "__main__":
    unittest.main()
