"""Compare each product's reviewed bytes through both real MCP protocol eras."""

from __future__ import annotations

import copy
import dataclasses
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop import orchestration, product_scope
from scripts import mcp_contract_equivalence as gate


class ProductContractEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hybrid = gate.served_surface()
        cls.training = gate.served_surface("training")

    def test_product_defaults_are_served_in_both_actual_protocol_eras(self):
        for product, readings, count in (
            ("hybrid", self.hybrid, 24), ("training", self.training, 22),
        ):
            expected = product_scope.surface(
                product_scope.health_data_default(product), product=product
            )
            for era in ("legacy", "modern"):
                with self.subTest(product=product, era=era):
                    reading = readings[era]
                    self.assertEqual(count, len(reading["tools"]))
                    self.assertEqual(expected.instructions(), reading["instructions"])
                    self.assertEqual(set(expected.prompts), set(reading["prompt_bodies"]))
                    self.assertEqual(expected.tool_catalogue_sha256(), reading["catalogue_sha256"])
                    for name in ("recordBodyMeasurement", "connectWatch"):
                        self.assertEqual(
                            product == "hybrid", name in {tool["name"] for tool in reading["tools"]}
                        )
            self.assertEqual([], gate.compare(readings["legacy"], readings["modern"], label=product))

    def test_training_base_is_built_from_the_selected_git_ref(self):
        # Poison the checkout's training surface: the base capture must still come from
        # Git, in its own interpreter, rather than inherit this checkout's objects.
        with mock.patch.object(product_scope, "surface", side_effect=AssertionError("checkout leaked")):
            base = gate.base_surface("HEAD", "training")
        self.assertEqual(22, len(base["tools"]))
        self.assertNotIn("recordBodyMeasurement", {tool["name"] for tool in base["tools"]})

    def test_health_override_selects_capability_without_changing_product_default(self):
        readings = gate.served_surface("hybrid", health_data=False)
        for era in ("legacy", "modern"):
            with self.subTest(product="hybrid", health_data=False, era=era):
                self.assertEqual([], gate.compare(self.training[era], readings[era], label=era))
        # Health data on gives the training product the full surface except the watch,
        # which only the full product pairs.
        readings = gate.served_surface("training", health_data=True)
        for era in ("legacy", "modern"):
            with self.subTest(product="training", health_data=True, era=era):
                names = {tool["name"] for tool in readings[era]["tools"]}
                self.assertEqual({tool["name"] for tool in self.hybrid[era]["tools"]} - {"connectWatch"}, names)
                differences = gate.compare(self.hybrid[era], readings[era], label=era)
                self.assertTrue(differences)
                self.assertTrue(all("connectWatch" in item or "catalogue" in item for item in differences), differences)

    def test_training_only_schema_change_is_detected_without_moving_full_catalogue(self):
        original = product_scope.surface(False, product="training")
        tool = original.tools_by_name["getCoachState"]
        changed_schema = copy.deepcopy(tool.input_schema)
        changed_schema.setdefault("properties", {})["contract_gate_probe"] = {"type": "string"}
        changed_tool = dataclasses.replace(tool, input_schema=changed_schema)
        changed = dataclasses.replace(
            original,
            tools=tuple(changed_tool if item.name == tool.name else item for item in original.tools),
        )
        select = product_scope.surface
        with mock.patch.object(
            product_scope, "surface",
            side_effect=lambda enabled, product: select(True, product=product) if enabled else changed,
        ):
            training = gate.served_surface("training")
            hybrid = gate.served_surface("hybrid")
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                differences = gate.compare(self.training[era], training[era], label=era)
                self.assertTrue(any("getCoachState.inputSchema changed" in item for item in differences), differences)
                self.assertEqual(22, len(training[era]["tools"]))
                self.assertEqual([], gate.compare(self.hybrid[era], hybrid[era], label=era))

    def test_training_only_prompt_change_is_detected_even_when_catalogue_is_equal(self):
        original = product_scope.surface(False, product="training")
        changed = dataclasses.replace(
            original, training_judgment=lambda: original.training_judgment() + "\nContract gate probe."
        )
        select = product_scope.surface
        with mock.patch.object(
            product_scope, "surface",
            side_effect=lambda enabled, product: select(True, product=product) if enabled else changed,
        ):
            readings = gate.served_surface("training")
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                self.assertEqual(self.training[era]["catalogue_sha256"], readings[era]["catalogue_sha256"])
                self.assertEqual(self.training[era]["instructions"], readings[era]["instructions"])
                differences = gate.compare(self.training[era], readings[era], label=era)
                self.assertTrue(
                    any(f"prompt {orchestration.TRAINING_PROMPT_NAME} changed" in item for item in differences),
                    differences,
                )

    def test_prompt_names_come_from_the_selected_surface(self):
        original = product_scope.surface(False, product="training")
        changed = dataclasses.replace(original)
        changed.prompts["training_only_probe"] = (
            lambda: {"name": "training_only_probe", "description": "Contract gate test"},
            lambda: {"messages": [{"role": "user", "content": {"type": "text", "text": "Probe"}}]},
        )
        with mock.patch.object(product_scope, "surface", return_value=changed):
            readings = gate.served_surface("training")
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                self.assertIn("training_only_probe", readings[era]["prompt_bodies"])

    def test_old_base_without_training_surface_fails_instead_of_using_full_product(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "garmin_coach_loop"
            package.mkdir()
            for name in ("__init__.py", "mcp_transport.py", "orchestration.py"):
                (package / name).write_text("")
            result = subprocess.run(
                [sys.executable, "-c", gate.BASE_PROGRAM, "training", "default"],
                cwd=directory, capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": directory, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("base ref has no product_scope surface for training", result.stderr)
        self.assertIn("cannot substitute the full-product catalogue", result.stderr)



class DeclaredContractChangeTests(unittest.TestCase):
    """A deliberate change passes only as the profile's pins state it, byte for byte."""

    SERVED = {"catalogue_sha256": "a" * 64, "instructions": "Coach orchestration."}
    INSTRUCTIONS = __import__("hashlib").sha256(b"Coach orchestration.").hexdigest()

    def pins(self, catalogue, instructions):
        return {"TOOL_CATALOGUE_SHA256": catalogue, "INSTRUCTIONS_SHA256": instructions}

    def test_pins_moved_to_exactly_what_is_served_declare_the_change(self):
        base = self.pins("b" * 64, self.INSTRUCTIONS)
        self.assertTrue(gate.declared_change(base, self.pins("a" * 64, self.INSTRUCTIONS), self.SERVED))

    def test_a_difference_the_pins_do_not_state_is_still_refused(self):
        same = self.pins("a" * 64, self.INSTRUCTIONS)
        for base, head in (
            (same, same),  # nothing moved: the difference is an accident
            (self.pins("b" * 64, self.INSTRUCTIONS), self.pins("c" * 64, self.INSTRUCTIONS)),
            (self.pins("b" * 64, self.INSTRUCTIONS), self.pins("a" * 64, "d" * 64)),
            (self.pins("b" * 64, self.INSTRUCTIONS), {"TOOL_CATALOGUE_SHA256": "a" * 64}),
        ):
            with self.subTest(base=base, head=head):
                self.assertFalse(gate.declared_change(base, head, self.SERVED))

    def test_only_differences_the_pins_cover_can_be_declared(self):
        covered = [
            "legacy: tool added: connectWatch",
            "modern: connectWatch.description changed:\n    base: a\n    served: b",
            "legacy: catalogue_sha256 changed:\n    base: a\n    served: b",
            "modern: instructions changed:\n    base: a\n    served: b",
        ]
        self.assertTrue(gate.covered_by_pins(covered))
        for unpinned in (
            "legacy: prompt coach_training_judgment changed:\n    base: a\n    served: b",
            "modern: prompts/list changed:\n    base: a\n    served: b",
            "legacy: server_title changed:\n    base: a\n    served: b",
        ):
            with self.subTest(unpinned=unpinned.split(":")[1]):
                self.assertFalse(gate.covered_by_pins([*covered, unpinned]))

    def test_pins_are_read_from_the_profile_module_text(self):
        text = Path(gate.ROOT / "garmin_coach_loop" / "profile_hybrid.py").read_text(encoding="utf-8")
        pins = gate.profile_pins(text)
        self.assertEqual({"TOOL_CATALOGUE_SHA256", "INSTRUCTIONS_SHA256"}, set(pins))
        self.assertEqual({}, gate.base_profile_pins("HEAD", "no-such-product"))


if __name__ == "__main__":
    unittest.main()
