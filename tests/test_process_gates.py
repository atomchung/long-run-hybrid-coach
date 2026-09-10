"""Keep the development and promotion gates conservative and mechanically testable."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.change_gates import (
    CLASSIFIED_PACKAGE_PATHS,
    INTERNAL_PACKAGE_PATHS,
    LIVE_SMOKE_PATHS,
    MODEL_FACING_PATHS,
    PACKAGE,
    PACKAGE_SUFFIXES,
    classify_changed_paths,
)
from scripts.test_selection import select_test_paths
from scripts.verify_production_promotion import (
    PromotionGateError,
    verify_main_green,
    verify_release_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class ChangeGateTests(unittest.TestCase):
    def test_internal_documentation_does_not_trigger_expensive_live_gates(self):
        plan = classify_changed_paths(["README.md", "docs/ops/verify-production-status.md"])
        self.assertFalse(plan["live_smoke"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])

    def test_oauth_gateway_and_delivery_changes_need_live_smoke_only(self):
        plan = classify_changed_paths(
            ["garmin_coach_loop/gateway.py", "garmin_coach_loop/delivery.py"],
            diffs_by_path={
                "garmin_coach_loop/gateway.py": "+        callback = oauth_callback()",
                "garmin_coach_loop/delivery.py": "+        return verify_readback(event)",
            },
        )
        self.assertTrue(plan["live_smoke"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])

    def test_internal_gateway_or_delivery_helper_change_does_not_trigger_live_smoke(self):
        plan = classify_changed_paths(
            ["garmin_coach_loop/gateway.py", "garmin_coach_loop/delivery.py"],
            diffs_by_path={
                "garmin_coach_loop/gateway.py": "+def internal_format_helper(value):",
                "garmin_coach_loop/delivery.py": "+def internal_format_helper(value):",
            },
        )
        self.assertFalse(plan["live_smoke"])

    def test_tool_surface_change_needs_client_acceptance_scan_and_resubmission(self):
        plan = classify_changed_paths(
            ["garmin_coach_loop/mcp_transport.py"],
            diffs_by_path={"garmin_coach_loop/mcp_transport.py": "+        description=\"new\""},
        )
        self.assertFalse(plan["live_smoke"])
        self.assertTrue(plan["client_acceptance"])
        self.assertTrue(plan["scan_tools"])
        self.assertTrue(plan["plugin_resubmission"])

    def test_mcp_internal_helper_change_does_not_trigger_client_ceremony(self):
        plan = classify_changed_paths(
            ["garmin_coach_loop/mcp_transport.py"],
            diffs_by_path={"garmin_coach_loop/mcp_transport.py": "+def internal_helper():"},
        )
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])

    def test_skill_change_needs_client_acceptance_but_not_current_mcp_resubmission(self):
        plan = classify_changed_paths([".agents/skills/garmin-coach-loop/SKILL.md"])
        self.assertTrue(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])

    def test_every_package_file_is_classified_exactly_once(self):
        # A new module that no list names would otherwise be reported as needing no
        # gate at all. This is where that decision is forced: adding a file to the
        # package fails here until change_gates.py says which surface it is.
        tracked = subprocess.run(
            ["git", "ls-files", "garmin_coach_loop"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        package_files = {
            path for path in tracked if path.endswith(PACKAGE_SUFFIXES) and path.startswith(PACKAGE)
        }

        self.assertEqual(
            set(),
            package_files - set(CLASSIFIED_PACKAGE_PATHS),
            "classify these in scripts/change_gates.py before merging",
        )
        self.assertEqual(
            set(),
            set(CLASSIFIED_PACKAGE_PATHS) - package_files,
            "scripts/change_gates.py names package files that no longer exist",
        )
        for first, second in (
            (LIVE_SMOKE_PATHS, MODEL_FACING_PATHS),
            (LIVE_SMOKE_PATHS, INTERNAL_PACKAGE_PATHS),
            (MODEL_FACING_PATHS, INTERNAL_PACKAGE_PATHS),
        ):
            self.assertEqual(set(), first & second)

    def test_an_unclassified_package_file_is_not_reported_as_gate_free(self):
        plan = classify_changed_paths(["garmin_coach_loop/source_garmin.py"])
        self.assertEqual(["garmin_coach_loop/source_garmin.py"], plan["unclassified_paths"])
        self.assertTrue(plan["live_smoke"])
        self.assertTrue(plan["client_acceptance"])
        # The reviewed tool catalogue cannot move without mcp_transport.py changing too.
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])

    def test_the_local_mcp_client_is_a_live_boundary(self):
        plan = classify_changed_paths(
            ["garmin_coach_loop/hosted.py"],
            diffs_by_path={"garmin_coach_loop/hosted.py": "+        token = self.redeem(code)"},
        )
        self.assertTrue(plan["live_smoke"])
        self.assertFalse(plan["client_acceptance"])

        internal = classify_changed_paths(
            ["garmin_coach_loop/hosted.py"],
            diffs_by_path={"garmin_coach_loop/hosted.py": "+def _format_row(value):"},
        )
        self.assertFalse(internal["live_smoke"])

    def test_edited_submission_artifacts_are_resubmitted_without_a_new_scan(self):
        # The packet, the registry entry and the plugin manifest are the bytes a
        # reviewer or registry receives. Editing one changes what is submitted next
        # even though the served tool catalogue is untouched.
        for path in (
            "chatgpt-app-submission.json",
            "server.json",
            "plugins/long-run-hybrid-coach/.codex-plugin/plugin.json",
        ):
            with self.subTest(path=path):
                plan = classify_changed_paths([path])
                self.assertTrue(plan["plugin_resubmission"])
                self.assertEqual([path], plan["plugin_resubmission_reasons"])
                self.assertFalse(plan["scan_tools"])
                self.assertFalse(plan["client_acceptance"])
                self.assertFalse(plan["live_smoke"])

    def test_a_tool_surface_change_still_reports_why_it_is_resubmitted(self):
        plan = classify_changed_paths(
            ["garmin_coach_loop/mcp_transport.py"],
            diffs_by_path={"garmin_coach_loop/mcp_transport.py": "+        description=\"new\""},
        )
        self.assertTrue(plan["plugin_resubmission"])
        self.assertEqual(
            ["garmin_coach_loop/mcp_transport.py"], plan["plugin_resubmission_reasons"]
        )


class AffectedTestSelectionTests(unittest.TestCase):
    def test_docs_only_change_selects_no_product_tests(self):
        plan = select_test_paths(["docs/ops/roll-with-railway-cli.md"])
        self.assertEqual([], plan["test_paths"])
        self.assertFalse(plan["full_suite_required"])

    def test_runtime_change_selects_direct_and_importing_controls(self):
        plan = select_test_paths(["garmin_coach_loop/delivery.py"])
        self.assertIn("tests/test_delivery.py", plan["test_paths"])
        self.assertFalse(plan["full_suite_required"])

    def test_unknown_executable_change_falls_back_to_full_suite(self):
        plan = select_test_paths(["new_runtime_component.py"])
        self.assertTrue(plan["full_suite_required"])
        self.assertEqual([], plan["test_paths"])

    def test_workflow_change_tests_the_gate_itself(self):
        plan = select_test_paths([".github/workflows/ci.yml"])
        self.assertEqual(["tests/test_process_gates.py"], plan["test_paths"])


class ProductionPromotionGateTests(unittest.TestCase):
    SHA = "a" * 40

    def test_exact_main_head_with_a_successful_main_push_passes(self):
        calls = []

        def api(url, token):
            calls.append((url, token))
            if url.endswith("/git/ref/heads/main"):
                return {"object": {"sha": self.SHA}}
            return {
                "workflow_runs": [
                    {
                        "id": 123,
                        "head_sha": self.SHA,
                        "head_branch": "main",
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }

        result = verify_main_green(
            repository="owner/repo",
            sha=self.SHA,
            token="opaque-test-token",
            api=api,
        )
        self.assertEqual(self.SHA, result["main_sha"])
        self.assertEqual([123], result["successful_main_runs"])
        self.assertEqual("opaque-test-token", calls[0][1])
        self.assertIn("head_sha=", calls[1][0])

    def test_stale_main_or_missing_success_cannot_promote(self):
        def stale_api(_url, _token):
            return {"object": {"sha": "b" * 40}}

        with self.assertRaisesRegex(PromotionGateError, "not current main"):
            verify_main_green(
                repository="owner/repo", sha=self.SHA, token="token", api=stale_api
            )

        def no_success_api(url, _token):
            if url.endswith("/git/ref/heads/main"):
                return {"object": {"sha": self.SHA}}
            return {"workflow_runs": []}

        with self.assertRaisesRegex(PromotionGateError, "no successful main push CI"):
            verify_main_green(
                repository="owner/repo", sha=self.SHA, token="token", api=no_success_api
            )

    def test_release_bundle_is_built_and_validated_for_exact_sha(self):
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        identity = verify_release_bundle(
            sha=sha,
            gateway_domain="https://mcp.paceandstaystrong.com",
        )
        self.assertEqual(sha, identity["git_commit"])
        self.assertTrue(identity["release_id"].startswith("gclr-"))

    def test_the_promotion_gate_runs_the_way_ci_invokes_it(self):
        # CI runs `python3 scripts/verify_production_promotion.py` from the repository
        # root, where `scripts` and `garmin_coach_loop` are not importable unless the
        # script puts the root on the path itself.
        result = subprocess.run(
            [sys.executable, "scripts/verify_production_promotion.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in {"GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_TOKEN", "PYTHONPATH"}
            },
        )
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("production promotion gate blocked", result.stderr)

    def test_ci_keeps_full_boundary_on_pr_and_main_but_not_production(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("concurrency:", text)
        # Only a pull request cancels. A cancelled `main` run leaves a commit that can
        # never satisfy the promotion gate's "successful main push run for this SHA".
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text
        )
        self.assertNotIn("cancel-in-progress: true", text)
        self.assertIn("if: github.event_name == 'pull_request' || github.ref == 'refs/heads/main'", text)
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/production'", text)
        self.assertEqual(2, text.count("          fi\n"))
        promotion = text.split("\n  promotion:", 1)[1]
        self.assertIn("verify_production_promotion.py", promotion)
        self.assertNotIn("unittest discover", promotion)


if __name__ == "__main__":
    unittest.main()
