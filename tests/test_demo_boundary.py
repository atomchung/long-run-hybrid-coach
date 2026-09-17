"""The demo reads, reasons and previews. Everything else is refused, by name.

The refusals here are the reason a public endpoint can be pointed at this product at all,
so they are asserted rather than assumed. Two of them are structural and the test is the
only thing that keeps them so: the product must not import the demo, and the product must
not acquire the demo's model dependency. Both would be easy to break by accident and
invisible in review -- an import added for convenience, a helper moved one directory over --
and both would turn a playground into part of the shipped coaching path.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import unittest
from pathlib import Path

from entrypoints.demo import boundary, fixture


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "garmin_coach_loop"
NOW = dt.datetime(2026, 9, 13, 10, 40, tzinfo=dt.timezone.utc)

CHANGE_REQUEST = {
    "decision_scope": "week",
    "summary": "Three 45-minute sessions next week, running-biased.",
    "reason_codes": ["schedule_or_equipment_changed"],
    "evidence": [
        {
            "field": "constraints.week_constraints",
            "observation": "Three sessions next week, 45 minutes each",
        }
    ],
    "goal_effect": {
        "week": "threshold exposure kept, long-run duration cut",
        "cycle": "the declared measurement still runs",
    },
    "next_review_condition": "If the week opens back up",
    "unknowns": ["whether 45 minutes leaves a usable warm-up"],
    "week": {"start": "2026-09-14", "intent": "Three 45-minute sessions"},
    "sessions": [
        {
            "operation": "add",
            "sport": "running",
            "scheduled_date": "2026-09-15",
            "time_window": "morning",
            "purpose": "Hold the threshold anchor inside 45 minutes",
            "adaptation": "threshold",
            "body_stress": "lower",
            "cost": "hard",
            "priority": "anchor",
            "planned_minutes": 45,
            "plan": {
                "kind": "time_axis",
                "name": "4 x 1km threshold",
                "steps": [
                    {
                        "kind": "work",
                        "name": "Warm up",
                        "duration": {"kind": "time", "seconds": 540},
                        "target": {"kind": "open"},
                    },
                    {
                        "kind": "repeat",
                        "repetitions": 4,
                        "steps": [
                            {
                                "kind": "work",
                                "name": "Threshold",
                                "duration": {"kind": "distance", "meters": 1000},
                                "target": {
                                    "kind": "pace",
                                    "unit": "sec_per_km",
                                    "low_seconds_per_km": 335,
                                    "high_seconds_per_km": 345,
                                },
                            },
                            {
                                "kind": "work",
                                "name": "Jog",
                                "duration": {"kind": "time", "seconds": 120},
                                "target": {"kind": "open"},
                            },
                        ],
                    },
                    {
                        "kind": "work",
                        "name": "Cool down",
                        "duration": {"kind": "time", "seconds": 300},
                        "target": {"kind": "open"},
                    },
                ],
            },
            "fallback": {"action": "reduce", "description": "Drop to three repetitions"},
        }
    ],
}


class ForbiddenWritePathTest(unittest.TestCase):
    def test_every_product_write_operation_is_refused_as_a_playground(self):
        plan = fixture.plan_state()
        for act in boundary.REFUSED_ACTS:
            with self.subTest(act=act):
                result = boundary.dispatch(act, {}, before=plan, issued_at=NOW)
                self.assertEqual("refused", result["status"])
                self.assertEqual("demo_write_forbidden", result["code"])
                self.assertIn("playground", result["playground"])
                self.assertEqual(list(boundary.ALLOWED_ACTS), result["allowed"])

    def test_a_name_the_demo_has_never_heard_of_is_refused_the_same_way(self):
        result = boundary.dispatch(
            "deleteEverything", {}, before=fixture.plan_state(), issued_at=NOW
        )
        self.assertEqual("demo_write_forbidden", result["code"])

    def test_a_refused_act_changes_nothing(self):
        plan = fixture.plan_state()
        boundary.dispatch("applyCoachDecision", {"proposal": "x"}, before=plan, issued_at=NOW)
        self.assertEqual(fixture.plan_state(), plan)

    def test_the_allowed_and_refused_sets_do_not_overlap(self):
        self.assertEqual(set(), set(boundary.ALLOWED_ACTS) & set(boundary.REFUSED_ACTS))

    def test_the_refused_set_is_the_product_s_whole_operation_catalogue(self):
        """A twenty-fourth operation cannot land and be quietly absent from the refusal.

        ``boundary.py`` keeps the names as a literal rather than importing the transport,
        so the demo image stays two directories deep; this is what stops that literal from
        going stale. A new tool fails here until it is named, which is the moment to decide
        whether the demo should say anything about it.
        """
        from garmin_coach_loop.mcp_transport import TOOLS

        self.assertEqual({tool.name for tool in TOOLS}, set(boundary.REFUSED_ACTS))

    def test_no_demo_act_is_spelled_like_a_product_operation(self):
        # Every served operation is camelCase; every demo act is snake_case. One glance at
        # a log line or a transcript tells them apart, and neither can be mistaken for the
        # other in a document the tool-name guard scans.
        for act in boundary.ALLOWED_ACTS:
            with self.subTest(act=act):
                self.assertEqual(act, act.lower())
                self.assertIn("_", act)

    def test_only_the_two_read_and_preview_acts_are_declared_to_the_model(self):
        declared = {tool["name"] for tool in boundary.tool_definitions()}
        self.assertEqual(set(boundary.ALLOWED_ACTS), declared)


class PreviewOnlyTest(unittest.TestCase):
    def test_a_preview_is_projected_by_the_product_and_saved_nowhere(self):
        plan = fixture.plan_state()
        result = boundary.dispatch(
            boundary.PREVIEW_PLAN_CHANGE,
            {"change_request": CHANGE_REQUEST},
            before=plan,
            issued_at=NOW,
        )
        self.assertEqual("preview_only", result["status"])
        self.assertTrue(result["material_change"])
        self.assertEqual(4, result["preview"]["base_version"])
        self.assertEqual(5, result["preview"]["resulting_version"])
        self.assertIn("not_applied", result)
        # The plan handed in, and the fixture underneath it, are exactly as they were.
        self.assertEqual(fixture.plan_state(), plan)

    def test_a_preview_carries_the_context_s_own_unknowns_forward(self):
        result = boundary.dispatch(
            boundary.PREVIEW_PLAN_CHANGE,
            {"change_request": CHANGE_REQUEST},
            before=fixture.plan_state(),
            issued_at=NOW,
        )
        self.assertTrue(
            any("measurement" in line for line in result["unknowns"]),
            "the declared measurement is still unread and a preview must not lose that",
        )

    def test_a_change_request_the_product_would_refuse_is_refused_here_too(self):
        result = boundary.dispatch(
            boundary.PREVIEW_PLAN_CHANGE,
            {"change_request": {"summary": "no evidence, no scope"}},
            before=fixture.plan_state(),
            issued_at=NOW,
        )
        self.assertEqual("invalid", result["status"])
        self.assertEqual("change_request_invalid", result["code"])

    def test_the_preview_renders_in_the_fixture_s_own_language(self):
        result = boundary.dispatch(
            boundary.PREVIEW_PLAN_CHANGE,
            {"change_request": CHANGE_REQUEST},
            before=fixture.plan_state(),
            issued_at=NOW,
        )
        added = result["preview"]["sessions"][0]["after"]["prescription"]
        self.assertIn("Threshold", added)


class ReadEvidenceTest(unittest.TestCase):
    def test_an_evidence_group_comes_back_through_the_product_s_own_projection(self):
        result = boundary.dispatch(
            boundary.READ_EVIDENCE,
            {"read": ["session_detail"]},
            before=fixture.plan_state(),
            issued_at=NOW,
        )
        self.assertEqual("passed", result["status"])
        self.assertIn("segment_execution", result["evidence"])

    def test_a_group_that_is_not_a_group_is_answered_rather_than_raised(self):
        result = boundary.dispatch(
            boundary.READ_EVIDENCE,
            {"read": ["everything"]},
            before=fixture.plan_state(),
            issued_at=NOW,
        )
        self.assertEqual("invalid", result["status"])
        self.assertEqual("unknown_evidence_group", result["code"])

    def test_arguments_that_are_not_an_object_are_answered_rather_than_raised(self):
        result = boundary.dispatch(
            boundary.READ_EVIDENCE, None, before=fixture.plan_state(), issued_at=NOW
        )
        self.assertEqual("arguments_invalid", result["code"])


class FailureDomainTest(unittest.TestCase):
    """The demo depends on the product. The product must not depend on the demo."""

    def _imports(self, path: Path) -> set[str]:
        """Every module this file names, with ``from X import y`` recorded as ``X.y``.

        Recorded both ways so one list of forbidden modules catches both spellings: a
        ``from garmin_coach_loop import store`` hides the module name from a check that
        only reads ``import`` statements.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        return names

    def test_no_product_module_imports_the_demo(self):
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(PACKAGE.glob("*.py"))
            if any(name.split(".")[0] == "entrypoints" for name in self._imports(path))
        ]
        self.assertEqual(
            [],
            offenders,
            "the product must run without the demo: a Product Hunt playground cannot "
            "become part of the shipped coaching path",
        )

    def test_the_product_acquires_no_model_credential_and_no_model_client(self):
        # AGENTS.md scopes the no-LLM-API rule to the product; the demo is the one named
        # exception, and this is what keeps the exception from spreading back.
        pattern = re.compile(r"OPENAI_API_KEY|api\.openai\.com|gpt-5\.6-luna")
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(PACKAGE.glob("*.*"))
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual([], offenders)

    def test_the_demo_imports_no_store_or_provider_module(self):
        # Reading the product's contracts and projectors is the whole point. Reaching its
        # store, its provider client or its delivery path is not, and would give this
        # process a way to touch a real athlete that no refusal above could take back.
        forbidden = {
            "garmin_coach_loop.store",
            "garmin_coach_loop.gateway",
            "garmin_coach_loop.delivery",
            "garmin_coach_loop.decision_delivery",
            "garmin_coach_loop.source_intervals",
            "garmin_coach_loop.identity",
            "garmin_coach_loop.hosted",
            "garmin_coach_loop.owner_data",
            "garmin_coach_loop.privacy_request",
            "garmin_coach_loop.cli",
        }
        demo = ROOT / "entrypoints" / "demo"
        for path in sorted(demo.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(set(), self._imports(path) & forbidden)


if __name__ == "__main__":
    unittest.main()
