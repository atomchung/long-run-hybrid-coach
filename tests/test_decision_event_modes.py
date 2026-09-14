"""Live DecisionEvent mode producers, and the shape of a rule that never runs.

Issue #315: three schema modes have no producer, and mode-conditional policy keyed
only on those modes reads as live while running on zero turns. These tests fail if
that shape comes back, and they exercise the surviving invariants on the path a
real turn takes.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.plan_change import project_change_request
from garmin_coach_loop.store import StateStoreError, apply_decision, doctor_store, init_store
from garmin_coach_loop.validation import MODE_ACTIONS, validate_decision_event


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"
FROZEN = Path(__file__).resolve().parent / "fixtures" / "frozen_stores"
PACKAGE = ROOT / "garmin_coach_loop"
CONTRACTS = ROOT / "contracts"
ISSUED_AT = dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc)

# Modes stored events may carry that nothing emits. Adding one requires proving
# doctor-store history can hold it. Removing one requires proving no stored event
# carries it. Gaining a producer means moving the value out of this set.
HISTORICAL_DECISION_MODES = frozenset({"plan_cycle", "plan_week", "revisit_today"})


def load(name: str) -> dict:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def schema_modes() -> frozenset[str]:
    schema = json.loads((CONTRACTS / "decision-event.schema.json").read_text(encoding="utf-8"))
    return frozenset(schema["properties"]["mode"]["enum"])


def _is_mode_read(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "mode":
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "mode"
    ):
        return True
    return False


def _constant_modes(node: ast.AST, allowed: frozenset[str]) -> frozenset[str]:
    if isinstance(node, ast.Constant) and node.value in allowed:
        return frozenset({node.value})
    if isinstance(node, (ast.Set, ast.Tuple, ast.List, ast.Dict)):
        values = node.keys if isinstance(node, ast.Dict) else node.elts
        found = {
            element.value
            for element in values
            if isinstance(element, ast.Constant) and element.value in allowed
        }
        return frozenset(found)
    return frozenset()


def live_produced_modes() -> set[str]:
    """Modes running package code actually writes onto a DecisionEvent."""
    allowed = schema_modes()
    found: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "mode"
                        and isinstance(value, ast.Constant)
                        and value.value in allowed
                    ):
                        found.add(value.value)
            if isinstance(node, ast.FunctionDef) and node.name == "_derive_mode":
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Return)
                        and isinstance(child.value, ast.Constant)
                        and child.value.value in allowed
                    ):
                        found.add(child.value.value)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DECISION_SCOPE_MODES":
                        found.update(_constant_modes(node.value, allowed))
                        if isinstance(node.value, ast.Dict):
                            for value in node.value.values:
                                if isinstance(value, ast.Constant) and value.value in allowed:
                                    found.add(value.value)
    return found


def distinct_dead_mode_policy(tree: ast.AST, live: frozenset[str]) -> list[str]:
    """If-tests whose mode set has no live producer: a rule that never runs."""
    allowed = schema_modes()
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(_is_mode_read(operand) for operand in (node.left, *node.comparators)):
            continue
        modes: set[str] = set()
        for operand in (node.left, *node.comparators):
            modes.update(_constant_modes(operand, allowed))
        if modes and modes.isdisjoint(live):
            offenders.append(
                f"line {node.lineno}: {sorted(modes)} has no live producer"
            )
    return offenders


def coaching_request(**overrides):
    request = {
        "summary": "調整本週安排",
        "reason_codes": ["schedule_or_equipment_changed"],
        "evidence": [{"field": "constraints", "observation": "本週行程改變"}],
        "goal_effect": {"week": "本週安排調整", "cycle": "28 天方向不變"},
        "next_review_condition": "下一次 anchor 前重新評估",
        "sessions": [
            {"operation": "move", "session_id": "rest-01", "scheduled_date": "2026-08-14"}
        ],
    }
    request.update(overrides)
    return request


class LiveModeProducerMapTests(unittest.TestCase):
    def test_schema_modes_are_exactly_live_producers_plus_historical(self):
        live = frozenset(live_produced_modes())
        schema = schema_modes()
        self.assertTrue(live, "producer scan found no DecisionEvent modes")
        self.assertEqual(live, frozenset({"review_week", "review_cycle", "record_delivery"}))
        self.assertEqual(schema, live | HISTORICAL_DECISION_MODES)
        self.assertTrue(live.isdisjoint(HISTORICAL_DECISION_MODES))
        self.assertEqual(set(MODE_ACTIONS), schema)

    def test_validate_bundle_has_no_mode_conditional_policy_without_a_producer(self):
        source = (PACKAGE / "validation.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        live = frozenset(live_produced_modes())
        self.assertEqual([], distinct_dead_mode_policy(tree, live))

    def test_the_guard_catches_a_reintroduced_dead_mode_branch(self):
        reintroduced = ast.parse(
            "def validate_bundle(event):\n"
            "    if event.get('mode') == 'revisit_today':\n"
            "        return 'daily policy'\n"
        )
        live = frozenset({"review_week", "review_cycle", "record_delivery"})
        self.assertEqual(
            ["line 2: ['revisit_today'] has no live producer"],
            distinct_dead_mode_policy(reintroduced, live),
        )

    def test_a_mode_that_loses_its_producer_while_keeping_policy_fails(self):
        tree = ast.parse(
            "def validate_bundle(event):\n"
            "    if event.get('mode') == 'review_week':\n"
            "        return 'week freeze'\n"
        )
        live_without_week = frozenset({"review_cycle", "record_delivery"})
        self.assertEqual(
            ["line 2: ['review_week'] has no live producer"],
            distinct_dead_mode_policy(tree, live_without_week),
        )


class LivePathStoreInvariantTests(unittest.TestCase):
    def setUp(self):
        self.before = load("plan-state-v1.json")
        self.context = load("coach-context-day-4.json")

    def _project(self):
        return project_change_request(
            self.before, coaching_request(), context=self.context, issued_at=ISSUED_AT
        )

    def test_apply_decision_refuses_a_version_that_does_not_match_the_change(self):
        projection = self._project()
        self.assertTrue(projection["material_change"])
        after = copy.deepcopy(projection["after_plan"])
        event = copy.deepcopy(projection["decision_event"])
        after["version"] = self.before["version"]
        event["plan_version_after"] = self.before["version"]
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            with self.assertRaisesRegex(StateStoreError, "version does not match exact change"):
                apply_decision(
                    state_dir, context=self.context, after=after, event=event
                )

    def test_review_week_refuses_the_deleted_daily_action_vocabulary(self):
        event = self._project()["decision_event"]
        event = copy.deepcopy(event)
        event["action"] = "replace"
        report = validate_decision_event(event)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("event.action must be one of" in error for error in report["errors"]),
            report["errors"],
        )


class HistoricalModeDoctorStoreTests(unittest.TestCase):
    def test_doctor_store_revalidates_a_store_that_already_carries_revisit_today(self):
        """Narrowing the schema enum would make this history unopenable (issue #315)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = Path(tmp.name) / "coach-state"
        shutil.copytree(FROZEN / "current_contract", state_dir)
        event = json.loads(
            next((state_dir / "commits").glob("*/event.json")).read_text(encoding="utf-8")
        )
        self.assertEqual("revisit_today", event["mode"])

        report = doctor_store(state_dir)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual([], report["errors"])
        self.assertEqual(1, report["event_count"])

        env = {**os.environ, "GARMIN_COACH_LOOP_HOME": str(state_dir)}
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "garmin_coach_loop.cli",
                "doctor-store",
                "--state-dir",
                str(state_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertEqual("passed", payload["status"])
        self.assertEqual([], payload["errors"])

    def test_doctor_store_revalidates_a_live_projected_review_week_event(self):
        before = load("plan-state-v1.json")
        context = load("coach-context-day-4.json")
        projection = project_change_request(
            before, coaching_request(), context=context, issued_at=ISSUED_AT
        )
        self.assertEqual("review_week", projection["decision_event"]["mode"])
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, before)
            apply_decision(
                state_dir,
                context=context,
                after=projection["after_plan"],
                event=projection["decision_event"],
            )
            report = doctor_store(state_dir)
            self.assertEqual("passed", report["status"], report)
            self.assertEqual(1, report["event_count"])


if __name__ == "__main__":
    unittest.main()
