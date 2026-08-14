"""Keep the coaching behavior cases in ``evals/`` well formed and contract-anchored.

The cases themselves are scored by whichever agent is driving the Coach -- the product
never calls an LLM API (AGENTS.md), so nothing here judges a coaching answer. What it
does judge is whether a case is still about this product: a case naming a field the
contracts no longer have is a case that quietly stopped testing anything, and it would
keep passing review forever because prose does not fail.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases"
CONTRACTS = ROOT / "contracts"

CASE_FIELDS = ("case_id", "issues", "mode", "scenario", "given", "evidence_fields", "expected", "fails_if")
EXPECTED_FIELDS = ("conclusion", "must_state", "must_not_state")
MODES = {"plan_cycle", "plan_week", "revisit_today", "review_week", "review_cycle"}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not a JSON object")
    return value


def _deref(root: dict[str, Any], node: Any) -> list[dict[str, Any]]:
    """Every object shape ``node`` can be: refs followed, unions expanded.

    A union (``anyOf``/``oneOf``) is how the contracts spell "an object or null", so a
    path through one has to be allowed to match the object branch alone.
    """
    if not isinstance(node, dict):
        return []
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        return _deref(root, (root.get("$defs") or {}).get(reference.split("/")[-1]))
    branches = node.get("anyOf") or node.get("oneOf")
    if isinstance(branches, list):
        return [shape for branch in branches for shape in _deref(root, branch)]
    return [node]


def _resolves(root: dict[str, Any], path: str) -> bool:
    """Whether ``a.b[].c`` names a real field. ``[]`` descends into array items."""
    nodes = _deref(root, root)
    for segment in path.split("."):
        name, _, brackets = segment.partition("[")
        candidates = [
            shape
            for node in nodes
            for shape in _deref(root, (node.get("properties") or {}).get(name))
        ]
        for _ in range(brackets.count("]")):
            candidates = [
                shape for node in candidates for shape in _deref(root, node.get("items"))
            ]
        if not candidates:
            return False
        nodes = candidates
    return True


class BehaviorCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = sorted(CASES.glob("*.json"))
        self.context_schema = _load(CONTRACTS / "coach-context.schema.json")
        self.plan_schema = _load(CONTRACTS / "plan-state.schema.json")

    def test_there_is_at_least_one_case_to_run(self):
        self.assertTrue(self.paths, f"no behavior cases found in {CASES}")

    def test_every_case_carries_the_whole_shape_a_reviewer_scores_against(self):
        seen: set[str] = set()
        for path in self.paths:
            with self.subTest(case=path.name):
                case = _load(path)
                self.assertEqual(sorted(CASE_FIELDS), sorted(case))
                self.assertEqual(path.stem, case["case_id"])
                self.assertNotIn(case["case_id"], seen)
                seen.add(case["case_id"])
                self.assertIn(case["mode"], MODES)
                self.assertTrue(case["scenario"].strip())
                self.assertTrue(case["issues"])
                for field in ("given", "evidence_fields", "fails_if"):
                    self.assertTrue(case[field], field)
                    self.assertTrue(all(str(item).strip() for item in case[field]), field)
                expected = case["expected"]
                self.assertEqual(sorted(EXPECTED_FIELDS), sorted(expected))
                self.assertTrue(expected["conclusion"].strip())
                # A case that only says what the answer must contain scores a fluent
                # wrong answer as a pass; the forbidden claims are half the test.
                for field in ("must_state", "must_not_state"):
                    self.assertTrue(expected[field], field)

    def test_every_named_evidence_field_still_exists_in_the_contracts(self):
        for path in self.paths:
            case = _load(path)
            for field in case["evidence_fields"]:
                with self.subTest(case=path.name, field=field):
                    if field.startswith("plan."):
                        self.assertTrue(_resolves(self.plan_schema, field[len("plan."):]))
                    else:
                        self.assertTrue(_resolves(self.context_schema, field))

    def test_a_renamed_contract_field_fails_its_case(self):
        # The check above only helps if it can fail. Without this, a resolver that
        # silently returns True would report every case as anchored forever.
        self.assertFalse(_resolves(self.context_schema, "goal_context.progress_score"))
        self.assertFalse(_resolves(self.context_schema, "cycle_sessions[].completion_ratio"))
        self.assertTrue(_resolves(self.context_schema, "goal_context.measurement_protocol"))
        self.assertTrue(_resolves(self.context_schema, "cycle_sessions[].activity.average_hr"))


if __name__ == "__main__":
    unittest.main()
