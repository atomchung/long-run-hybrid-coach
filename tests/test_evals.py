"""Keep the coaching behavior cases in ``evals/`` well formed and contract-anchored.

The cases themselves are scored by whichever agent is driving the Coach -- the product
never calls an LLM API (AGENTS.md), so nothing here judges a coaching answer. What it
does judge is whether a case is still about this product: a case naming a field the
contracts no longer have is a case that quietly stopped testing anything, and it would
keep passing review forever because prose does not fail.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


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


# Every committed contract, by file name, so a ``$ref`` can cross from one to another.
DOCUMENTS: dict[str, dict[str, Any]] = {
    path.name: _load(path) for path in sorted(CONTRACTS.glob("*.schema.json"))
}


def _document(name: str) -> dict[str, Any]:
    document = DOCUMENTS.get(name)
    if document is None:
        raise AssertionError(f"{name} is not a committed contract")
    return document


def _pointed_at(document: dict[str, Any], pointer: str) -> Any:
    node: Any = document
    for token in pointer.split("/"):
        if not token:
            continue
        node = node.get(token) if isinstance(node, dict) else None
    return node


Shape = tuple[dict[str, Any], dict[str, Any]]


def _deref(document: dict[str, Any], node: Any) -> list[Shape]:
    """Every object shape ``node`` can be, paired with the document it lives in.

    A union (``anyOf``/``oneOf``) is how the contracts spell "an object or null", so a
    path through one has to be allowed to match the object branch alone.

    A ``$ref`` may name another file in ``contracts/``, which is how the pre-plan
    observations reuse the CoachContext's activity row rather than keeping a second copy
    of it. The document travels with the shape because the shape it lands on refers to
    *its own* ``$defs`` from then on. A ref naming a file that is not a committed
    contract fails loudly: silently resolving it would report the case as anchored to
    something nobody can read.
    """
    if not isinstance(node, dict):
        return []
    reference = node.get("$ref")
    if isinstance(reference, str):
        file_name, _, pointer = reference.partition("#")
        target = _document(file_name) if file_name else document
        return _deref(target, _pointed_at(target, pointer))
    branches = node.get("anyOf") or node.get("oneOf")
    if isinstance(branches, list):
        return [shape for branch in branches for shape in _deref(document, branch)]
    return [(document, node)]


def _resolves(document: dict[str, Any], path: str) -> bool:
    """Whether ``a.b[].c`` names a real field. ``[]`` descends into array items."""
    nodes = _deref(document, document)
    for segment in path.split("."):
        name, _, brackets = segment.partition("[")
        candidates = [
            shape
            for owner, node in nodes
            for shape in _deref(owner, (node.get("properties") or {}).get(name))
        ]
        for _ in range(brackets.count("]")):
            candidates = [
                shape for owner, node in candidates for shape in _deref(owner, node.get("items"))
            ]
        if not candidates:
            return False
        nodes = candidates
    return True


# Which contract answers a case's evidence path, and what is left to resolve inside it.
# Both prefixes are routing tokens rather than field names: `plan.` names the PlanState
# travelling beside the context, and `pre_plan_observations.` the object a `no_plan_state`
# read hands back in place of one -- the first plan's whole evidence base, which is in
# neither of the other two contracts.
ROUTES = (
    ("plan.", "plan-state.schema.json"),
    ("pre_plan_observations.", "pre-plan-observations.schema.json"),
)


def _route(field: str) -> tuple[dict[str, Any], str]:
    for prefix, name in ROUTES:
        if field.startswith(prefix):
            return _document(name), field[len(prefix):]
    return _document("coach-context.schema.json"), field


class BehaviorCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = sorted(CASES.glob("*.json"))
        self.context_schema = _document("coach-context.schema.json")
        self.pre_plan_schema = _document("pre-plan-observations.schema.json")

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
                    self.assertTrue(_resolves(*_route(field)))

    def test_a_renamed_contract_field_fails_its_case(self):
        # The check above only helps if it can fail. Without this, a resolver that
        # silently returns True would report every case as anchored forever.
        self.assertFalse(_resolves(self.context_schema, "goal_context.progress_score"))
        self.assertFalse(_resolves(self.context_schema, "cycle_sessions[].completion_ratio"))
        self.assertTrue(_resolves(self.context_schema, "goal_context.measurement_protocol"))
        self.assertTrue(_resolves(self.context_schema, "cycle_sessions[].activity.average_hr"))
        # The same, one document over. A first-plan case resolves against its own
        # contract, and a path through the row shape that contract borrows has to be
        # able to fail there too -- otherwise the cross-file hop is the hole.
        self.assertFalse(_resolves(self.pre_plan_schema, "recent_training.activities[].date"))
        self.assertFalse(
            _resolves(self.pre_plan_schema, "recent_training.recent_actuals[].pace_sec_per_km")
        )
        self.assertTrue(_resolves(self.pre_plan_schema, "athlete_evidence.long_term_goals[].target"))
        self.assertTrue(
            _resolves(self.pre_plan_schema, "recent_training.recent_actuals[].average_pace_sec_per_km")
        )

    def test_a_rename_in_the_borrowed_row_shape_fails_the_case_that_names_it(self):
        """Which is the whole reason the row is referenced instead of copied.

        A second copy of the activity row here would keep resolving after the real one
        moved, and every first-plan case reading it would report itself anchored to a
        field the product no longer has -- the exact silence these checks exist to break.
        """
        moved = copy.deepcopy(self.context_schema)
        moved["$defs"]["actual"]["properties"].pop("average_pace_sec_per_km")
        with mock.patch.dict(DOCUMENTS, {"coach-context.schema.json": moved}):
            self.assertFalse(
                _resolves(
                    self.pre_plan_schema,
                    "recent_training.recent_actuals[].average_pace_sec_per_km",
                )
            )

    def test_a_ref_to_a_contract_that_is_not_committed_fails_loudly(self):
        """A silent False here would read as a renamed field; a silent True as coverage."""
        with self.assertRaises(AssertionError):
            _resolves({"properties": {"x": {"$ref": "no-such.schema.json#/$defs/x"}}}, "x")


if __name__ == "__main__":
    unittest.main()
