"""Hold profile extraction to the existing product, grant and release boundaries."""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop import gateway, profile_hybrid, profile_training, product_profiles
from garmin_coach_loop.product_identity import ProductIdentityError
from scripts import change_gates, check_product_profiles, release_bundle

ROOT = Path(__file__).resolve().parents[1]
FULL_SCOPES = ("ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")
TRAINING_SCOPES = ("ACTIVITY:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")


class ProductProfileTests(unittest.TestCase):
    def test_existing_product_health_matrix_and_store_roots_remain_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for product, health, scopes, count in (
                ("hybrid", True, FULL_SCOPES, 24),
                ("hybrid", False, FULL_SCOPES, 22),
                ("training", False, TRAINING_SCOPES, 22),
                ("training", True, FULL_SCOPES, 23),
            ):
                with self.subTest(product=product, health=health):
                    config = gateway.GatewayConfig(
                        root, b"k" * 32, "client", "secret", product=product,
                        health_data=health,
                    )
                    self.assertEqual(scopes, config.intervals_oauth_scopes)
                    self.assertEqual(count, len(product_profiles.get_profile(product).surface(health).tools))
                    self.assertEqual(root if product == "hybrid" else root / "products/training",
                                     config.product_state_root)
            for product, expected in (("hybrid", True), ("training", False)):
                config = gateway.GatewayConfig(root, b"k" * 32, "client", "secret", product=product)
                self.assertIs(expected, config.health_data)

    def test_unknown_product_cannot_select_a_profile(self):
        for value in ("claude", "chatgpt", "../hybrid", ""):
            with self.subTest(value=value), self.assertRaises(ProductIdentityError):
                product_profiles.get_profile(value)

    def test_each_reviewed_profile_matches_its_actual_surface_and_packet(self):
        for product in product_profiles.PRODUCTS:
            with self.subTest(product=product):
                self.assertEqual([], check_product_profiles.check_profile(product_profiles.get_profile(product)))

    def test_selecting_full_packet_for_restricted_product_is_refused(self):
        training = product_profiles.get_profile("training")
        wrong = dataclasses.replace(training, submission_packet="chatgpt-app-submission.json")
        problems = check_product_profiles.check_profile(wrong)
        self.assertIn("submission packet and served tool names differ", problems)
        self.assertIn("submission annotations differ for startCoachSession", problems)

    def test_changed_reviewed_surface_cannot_pass_using_only_a_tool_count(self):
        profile = product_profiles.get_profile("training")
        with mock.patch.object(profile_training, "TOOL_CATALOGUE_SHA256", "0" * 64):
            problems = check_product_profiles.check_profile(product_profiles.get_profile("training"))
        self.assertIn("tool catalogue differs from the reviewed fingerprint", problems)
        changed = dataclasses.replace(profile, instructions_sha256="0" * 64)
        self.assertIn("instructions differ from the reviewed fingerprint", check_product_profiles.check_profile(changed))

    def test_release_profile_uses_target_package_not_current_imports(self):
        package = [(p.name, p.read_bytes()) for p in (ROOT / "garmin_coach_loop").iterdir()
                   if p.is_file() and p.suffix in {".py", ".md"}]
        package = [(name, body + b'\nVERSION = "8.7.6"\n'
                    if name == "profile_training.py" else body) for name, body in package]
        target = release_bundle.profile_at_package(package, "training", None)
        self.assertEqual("8.7.6", target["version"])
        self.assertNotEqual(profile_training.VERSION, target["version"])
        self.assertEqual("off", target["health_data"])
        self.assertEqual(list(TRAINING_SCOPES), target["oauth_scopes"])
        override = release_bundle.profile_at_package(package, "hybrid", False)
        self.assertEqual("hybrid", override["product"])
        self.assertEqual("off", override["health_data"])
        self.assertEqual(list(FULL_SCOPES), override["oauth_scopes"])

    def test_missing_target_profile_does_not_fall_back_to_current_checkout(self):
        with self.assertRaisesRegex(release_bundle.ReleaseIdentityError, "before profiles"):
            release_bundle.profile_at_package([("__init__.py", b"")], "training", None)

    def test_profile_refactor_requires_smoke_but_not_resubmission_when_bytes_match(self):
        paths = ["garmin_coach_loop/profile_training.py", "garmin_coach_loop/product_scope.py"]
        equal = change_gates.classify_changed_paths(paths, profile_surface_moved=False, training_metadata_moved=False)
        self.assertTrue(equal["live_smoke"])
        self.assertFalse(equal["plugin_resubmission"])
        self.assertFalse(equal["client_acceptance"])
        for state in (True, None):
            plan = change_gates.classify_changed_paths(
                paths, profile_surface_moved=state, training_metadata_moved=False)
            self.assertTrue(plan["scan_tools"])
            self.assertTrue(plan["plugin_resubmission"])
            self.assertTrue(plan["client_acceptance"])

    def test_scope_or_version_change_requires_review_without_a_tool_scan(self):
        for moved in (True, None):
            decision = change_gates.classify_changed_paths(
                ["garmin_coach_loop/profile_training.py"], profile_surface_moved=False,
                training_metadata_moved=moved)
            self.assertTrue(decision["plugin_resubmission"])
            self.assertTrue(decision["client_acceptance"])
            self.assertFalse(decision["scan_tools"])

    def test_scope_only_change_is_visible_to_the_training_metadata_gate(self):
        package = ROOT / "garmin_coach_loop"
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "garmin_coach_loop"
            copied.mkdir()
            for path in package.iterdir():
                if path.is_file() and path.suffix in {".py", ".md"}:
                    (copied / path.name).write_bytes(path.read_bytes())
            program = change_gates.TRAINING_METADATA_PROGRAM
            before = change_gates._catalogue_digest_in(directory, program)
            self.assertIsNotNone(before)
            config = copied / "profile_training.py"
            config.write_text(config.read_text() + "\nOAUTH_SCOPES_OFF = OAUTH_SCOPES_ON\n")
            after = change_gates._catalogue_digest_in(directory, program)
            self.assertIsNotNone(after)
            self.assertNotEqual(before, after)

    def test_changed_training_prompt_moves_profile_gate_with_same_catalogue(self):
        package = ROOT / "garmin_coach_loop"
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "garmin_coach_loop"
            copied.mkdir()
            for path in package.iterdir():
                if path.is_file() and path.suffix in {".py", ".md"}:
                    (copied / path.name).write_bytes(path.read_bytes())
            before = change_gates._catalogue_digest_in(directory, change_gates.PROFILE_SURFACE_PROGRAM)
            source = copied / "product_scope.py"
            source.write_text(source.read_text().replace(
                "lambda: judgment_without_health(orchestration.training_judgment()),",
                'lambda: judgment_without_health(orchestration.training_judgment()) + "Profile gate probe.",',
            ))
            after = change_gates._catalogue_digest_in(directory, change_gates.PROFILE_SURFACE_PROGRAM)
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            self.assertNotEqual(before, after)

    def test_profile_changes_are_part_of_the_gateway_artifact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "gateway.py").write_text("# synthetic gateway\n")
            for name in ("profile_hybrid.py", "profile_training.py", "product_profiles.py"):
                (path / name).write_text("# synthetic config\n")
            with mock.patch.object(gateway, "__file__", str(path / "gateway.py")):
                original = gateway.gateway_artifact_sha256()
                for name in ("profile_hybrid.py", "profile_training.py", "product_profiles.py"):
                    (path / name).write_text("# changed synthetic config\n")
                    self.assertNotEqual(original, gateway.gateway_artifact_sha256(), name)
                    (path / name).write_text("# synthetic config\n")


if __name__ == "__main__":
    unittest.main()
