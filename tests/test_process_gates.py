"""Keep the development and promotion gates conservative and mechanically testable."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.change_gates import (
    CATALOGUE_EXPORT_PATHS,
    CLASSIFIED_PACKAGE_PATHS,
    INTERNAL_PACKAGE_PATHS,
    LIVE_SMOKE_PATHS,
    MODEL_FACING_PATHS,
    PACKAGE,
    PACKAGE_SUFFIXES,
    classify_changed_paths,
    tool_catalogue_sha256_at,
    working_tree_tool_catalogue_sha256,
)
from scripts.test_selection import select_test_paths, unittest_command, unittest_env
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

    def test_a_moved_catalogue_digest_is_a_changed_surface_without_any_marker_line(self):
        # Rewriting the text inside a description string, or the inner lines of an input
        # schema, changes no line carrying `description=` or `input_schema=`. Reading the
        # markers alone answered `scan_tools: false` here while the reviewed bytes had
        # demonstrably moved -- a resubmission that would never have been run.
        plan = classify_changed_paths(
            ["garmin_coach_loop/mcp_transport.py"],
            diffs_by_path={
                "garmin_coach_loop/mcp_transport.py": (
                    '+                        "Send back on applyOwnerDeletion."'
                )
            },
            catalogue_moved=True,
        )
        self.assertTrue(plan["scan_tools"])
        self.assertTrue(plan["client_acceptance"])
        self.assertTrue(plan["plugin_resubmission"])
        # The operator has to be able to tell which evidence fired.
        moved = "garmin_coach_loop/mcp_transport.py (tool_catalogue_sha256 moved)"
        self.assertEqual([moved], plan["client_acceptance_reasons"])
        self.assertEqual([moved], plan["plugin_resubmission_reasons"])

    def test_the_markers_still_decide_when_the_digest_answered_nothing(self):
        # `False` is a compared pair that matched; `None` is a base that could not be
        # built at all. Without the heuristic in the second case an unknown base would
        # silently report "no scan needed" for a genuinely changed catalogue.
        marker_free = '+                        "Send back on applyOwnerDeletion."'
        for catalogue_moved in (False, None):
            with self.subTest(catalogue_moved=catalogue_moved):
                quiet = classify_changed_paths(
                    ["garmin_coach_loop/mcp_transport.py"],
                    diffs_by_path={"garmin_coach_loop/mcp_transport.py": marker_free},
                    catalogue_moved=catalogue_moved,
                )
                self.assertFalse(quiet["scan_tools"])
                self.assertFalse(quiet["client_acceptance"])
                self.assertEqual([], quiet["client_acceptance_reasons"])

                marked = classify_changed_paths(
                    ["garmin_coach_loop/mcp_transport.py"],
                    diffs_by_path={
                        "garmin_coach_loop/mcp_transport.py": '+        description="new"'
                    },
                    catalogue_moved=catalogue_moved,
                )
                self.assertTrue(marked["scan_tools"])
                self.assertEqual(
                    ["garmin_coach_loop/mcp_transport.py"],
                    marked["client_acceptance_reasons"],
                )

    def test_the_catalogue_digest_is_built_from_a_ref_and_an_unknown_ref_answers_none(self):
        # The gate calls this for whatever `--base` names. A ref that cannot be exported,
        # or one predating the module, has to come back as "no evidence" rather than as a
        # traceback, or the whole classification is lost with it.
        self.assertRegex(tool_catalogue_sha256_at("HEAD") or "", r"^[0-9a-f]{64}$")
        self.assertIsNone(tool_catalogue_sha256_at("no-such-ref-for-a-change-gate"))

    def test_a_clean_checkout_serves_the_catalogue_its_head_commit_holds(self):
        # The two digests are computed from different places -- an export of the commit
        # and this working tree -- so a clean checkout agreeing is what shows the export
        # carries everything the import needs.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *CATALOGUE_EXPORT_PATHS],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            self.skipTest("uncommitted package edits: the working tree is not HEAD")
        self.assertEqual(tool_catalogue_sha256_at("HEAD"), working_tree_tool_catalogue_sha256())


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

    def test_full_suite_fallback_still_uses_discovery(self):
        plan = select_test_paths(["new_runtime_component.py"])
        self.assertEqual(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            unittest_command(plan),
        )

    def test_selected_sibling_import_module_loads_under_the_generated_command(self):
        # A gateway edit is the path that made this visible: the selection includes
        # modules that do `from test_gateway import ...`, and those only resolve when
        # the runner matches `unittest discover -s tests`. Asserting the command
        # string is not enough; the generated invocation has to import.
        plan = select_test_paths(["garmin_coach_loop/gateway.py"])
        path = "tests/test_owner_lifecycle.py"
        self.assertIn(path, plan["test_paths"])
        self.assertIn("from test_gateway import", (ROOT / path).read_text(encoding="utf-8"))
        probe = {"full_suite_required": False, "test_paths": [path]}
        command = unittest_command(probe)
        self.assertIn("test_owner_lifecycle", command)
        self.assertNotIn("tests.test_owner_lifecycle", command)
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=unittest_env(probe),
            capture_output=True,
            text=True,
        )
        combined = result.stderr + result.stdout
        self.assertNotIn("ModuleNotFoundError", combined)
        self.assertNotIn("Failed to import test module", combined)
        self.assertEqual(0, result.returncode, result.stderr)


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
