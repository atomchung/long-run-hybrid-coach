"""Structural check that entrypoints/custom-gpt/openapi.yaml matches gateway.ROUTES.

This is a line-level, fixed-indentation text scan over the YAML file -- stdlib only, no
PyYAML, no real YAML parser. It reads `paths:` entries, HTTP methods, `operationId:` and
`x-openai-isConsequential:` values by exact indentation (2 spaces per nesting level, the
style the file is hand-written in). It does NOT validate general YAML semantics: it would
not notice a syntactically broken document elsewhere in the file, a malformed schema under
`components:`, or any nesting style other than the one this file uses. Its only job is to
keep the documented routes honest against the one source of truth, garmin_coach_loop.gateway.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.gateway import ROUTES


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"

_PATH_LINE = re.compile(r"^  (/\S+):\s*$")
_METHOD_LINE = re.compile(r"^    (get|post|put|delete|patch|options|head|trace):\s*$")
_OPERATION_ID_LINE = re.compile(r"^      operationId:\s*(\S+)\s*$")
_CONSEQUENTIAL_LINE = re.compile(r"^      x-openai-isConsequential:\s*(\S+)\s*$")

# ROUTES "kind" -> the operationId the plan requires for it. "token" is deliberately
# absent: the OAuth token endpoint must never be a documented Action operation.
EXPECTED_OPERATION_IDS = {
    "health": "healthCheck",
    "session": "startCoachSession",
    "decision_prepare": "prepareCoachDecision",
    "decision_apply": "applyCoachDecision",
    "delivery_prepare": "prepareWorkoutDelivery",
    "delivery_publish": "publishWorkoutDelivery",
}

# Operations that default to OpenAI's "consequential" (write) behavior on purpose, so the
# platform still asks its own confirmation even though the GPT's own instructions already
# ask for one. Every other documented operation must opt out with the literal flag.
CONSEQUENTIAL_OPERATION_IDS = {"applyCoachDecision", "publishWorkoutDelivery"}


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
            if kind == "token":
                continue
            self.assertIn(path, self.documented, f"{path} is a real route but undocumented")

    def test_oauth_token_endpoint_is_not_a_documented_operation(self):
        self.assertNotIn(
            "/oauth/intervals/token",
            self.documented,
            "the OAuth token endpoint is plumbing, not a callable Action operation",
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

    def test_security_scheme_names_the_real_intervals_and_gateway_urls(self):
        self.assertIn("https://intervals.icu/oauth/authorize", self.text)
        self.assertIn("/oauth/intervals/token", self.text)

    def test_server_and_token_url_use_the_placeholder_domain_only(self):
        self.assertIn("YOUR-GATEWAY-DOMAIN", self.text)


if __name__ == "__main__":
    unittest.main()
