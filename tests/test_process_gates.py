"""Keep the development and promotion gates conservative and mechanically testable."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.change_gates import (
    CATALOGUE_EXPORT_PATHS,
    CATALOGUE_MOVED_REASON,
    CLASSIFIED_DEMO_PATHS,
    CLASSIFIED_PACKAGE_PATHS,
    DEMO_ACCEPTANCE_COMMAND,
    DEMO_ACCEPTANCE_FIXTURE_PREFIX,
    DEMO_ACCEPTANCE_PATHS,
    DEMO_ENTRYPOINT_PREFIX,
    DEMO_NO_GATE_PATHS,
    DEPENDENCY_PATHS,
    DEPENDENCY_PIN_MOVED_REASON,
    DEPENDENCY_PIN_UNKNOWN_REASON,
    INTERNAL_PACKAGE_PATHS,
    LIVE_SMOKE_PATHS,
    MODEL_FACING_PATHS,
    PACKAGE,
    PACKAGE_SUFFIXES,
    PROTOCOL_SURFACE_PATH,
    SUBMISSION_ARTIFACT_PATHS,
    classify_changed_paths,
    dependency_pin_at,
    tool_catalogue_sha256_at,
    working_tree_dependency_pin,
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

    # The three states of the digest evidence, and what each one does to the markers.
    # `MARKER_LINE` carries `description=`; `MARKER_FREE` carries none of the markers.
    MARKER_LINE = '+        description="new"'
    MARKER_FREE = '+                        "Send back on applyOwnerDeletion."'

    def _transport(self, diff: str, catalogue_moved):
        return classify_changed_paths(
            ["garmin_coach_loop/mcp_transport.py"],
            diffs_by_path={"garmin_coach_loop/mcp_transport.py": diff},
            catalogue_moved=catalogue_moved,
        )

    def test_a_digest_that_proves_the_catalogue_stood_still_outranks_a_marker(self):
        """`False` is a measurement, and a marker does not overturn one.

        The digest is rebuilt from the same `descriptor()` output `tools/list` serves, so
        two equal digests are two identical catalogues. A changed line that happens to
        mention `inputSchema` says nothing about that, and used to ask for Scan Tools and
        a plugin resubmission anyway.

        Issue #352's transport migration is the case that made it concrete: deleting the
        hand-written protocol layer changed lines containing `inputSchema`, `prompts` and
        `serverInfo`, moved no catalogue byte, and produced a Scan Tools demand against a
        release that was in OpenAI review at the time.
        """
        plan = self._transport(self.MARKER_LINE, False)
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["plugin_resubmission"])
        self.assertEqual([], plan["client_acceptance_reasons"])

    def test_a_moved_digest_asks_for_the_scan_whether_or_not_a_marker_matched(self):
        for label, diff in (("marker line", self.MARKER_LINE), ("no marker", self.MARKER_FREE)):
            with self.subTest(diff=label):
                plan = self._transport(diff, True)
                self.assertTrue(plan["scan_tools"])
                self.assertTrue(plan["client_acceptance"])
                self.assertTrue(plan["plugin_resubmission"])
                # The digest names itself, so an operator can tell which evidence fired.
                self.assertEqual([CATALOGUE_MOVED_REASON], plan["client_acceptance_reasons"])

    def test_the_markers_still_decide_when_the_digest_could_not_be_built(self):
        """`None` is a base that could not be exported, so nothing was measured.

        This is the one state the heuristic is for. Without it an unbuildable base would
        report "no scan needed" for a genuinely changed catalogue, which is the failure
        the markers were added to prevent -- and it stays.
        """
        marked = self._transport(self.MARKER_LINE, None)
        self.assertTrue(marked["scan_tools"])
        self.assertEqual(
            ["garmin_coach_loop/mcp_transport.py"], marked["client_acceptance_reasons"]
        )

        quiet = self._transport(self.MARKER_FREE, None)
        self.assertFalse(quiet["scan_tools"])
        self.assertEqual([], quiet["client_acceptance_reasons"])

    def test_a_model_facing_path_is_never_silenced_by_the_digest(self):
        """The digest binds the tool catalogue and nothing else.

        `instructions` and the served prompts are their own bytes in their own files, and
        a release binds their digests separately. A catalogue that stood still says
        nothing about them, so these paths are classified before the digest is consulted
        and stay classified whatever it answered.
        """
        for path in sorted(MODEL_FACING_PATHS) + [
            ".agents/skills/garmin-coach-loop/SKILL.md",
            "contracts/coach-context.schema.json",
        ]:
            for catalogue_moved in (False, None, True):
                with self.subTest(path=path, catalogue_moved=catalogue_moved):
                    plan = classify_changed_paths([path], catalogue_moved=catalogue_moved)
                    self.assertTrue(plan["client_acceptance"], path)
                    self.assertIn(path, plan["client_acceptance_reasons"])

    def test_a_submission_artifact_is_resubmitted_on_its_own_evidence(self):
        """An edited packet is resubmitted content whatever the catalogue did."""
        for path in sorted(SUBMISSION_ARTIFACT_PATHS):
            with self.subTest(path=path):
                plan = classify_changed_paths([path], catalogue_moved=False)
                self.assertTrue(plan["plugin_resubmission"], path)
                self.assertIn(path, plan["plugin_resubmission_reasons"])

    def test_an_unclassified_package_file_is_not_silenced_by_the_digest(self):
        """A new module is both surfaces until `change_gates.py` names it.

        A digest that stood still must not turn that conservative answer off: the file is
        unnamed precisely because nobody has decided what it is yet.
        """
        plan = classify_changed_paths(
            ["garmin_coach_loop/not_named_anywhere.py"], catalogue_moved=False
        )
        self.assertEqual(["garmin_coach_loop/not_named_anywhere.py"], plan["unclassified_paths"])
        self.assertTrue(plan["live_smoke"])
        self.assertTrue(plan["client_acceptance"])

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


class DemoAcceptanceGateTests(unittest.TestCase):
    """entrypoints/demo/ is outside garmin_coach_loop/, so change_gates.py names it on its
    own rather than folding it into the package lists above -- issue #495. Issue #478 is
    the failure this closes: a changed demo constant, a green (fake-model) unit suite, and
    every visitor turn answering 504 until somebody ran the acceptance command by hand.
    """

    def test_a_demo_model_change_needs_the_acceptance_run(self):
        plan = classify_changed_paths(["entrypoints/demo/model.py"])
        self.assertTrue(plan["demo_acceptance"])
        self.assertEqual(
            [f"entrypoints/demo/model.py ({DEMO_ACCEPTANCE_COMMAND})"],
            plan["demo_acceptance_reasons"],
        )
        # A demo settings change is not a product/MCP surface: none of those gates fire.
        self.assertFalse(plan["live_smoke"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])

    def test_every_named_demo_acceptance_path_needs_the_run(self):
        for path in sorted(DEMO_ACCEPTANCE_PATHS):
            with self.subTest(path=path):
                plan = classify_changed_paths([path])
                self.assertTrue(plan["demo_acceptance"], path)
                self.assertEqual(
                    [f"{path} ({DEMO_ACCEPTANCE_COMMAND})"], plan["demo_acceptance_reasons"]
                )

    def test_a_demo_fixture_change_needs_the_acceptance_run(self):
        # The fixtures directory is a prefix, not a fixed set: the acceptance run replays
        # committed prompts against whatever the fixture holds, so any file inside it moving
        # is a changed answer.
        plan = classify_changed_paths(["entrypoints/demo/fixtures/plan-state.json"])
        self.assertTrue(plan["demo_acceptance"])
        self.assertEqual(
            [f"entrypoints/demo/fixtures/plan-state.json ({DEMO_ACCEPTANCE_COMMAND})"],
            plan["demo_acceptance_reasons"],
        )

    def test_a_readme_only_demo_change_needs_no_live_run(self):
        plan = classify_changed_paths(["entrypoints/demo/README.md"])
        self.assertFalse(plan["demo_acceptance"])
        self.assertEqual([], plan["demo_acceptance_reasons"])
        self.assertEqual([], plan["unclassified_paths"])

    def test_the_no_gate_demo_paths_need_no_live_run(self):
        for path in sorted(DEMO_NO_GATE_PATHS):
            with self.subTest(path=path):
                plan = classify_changed_paths([path])
                self.assertFalse(plan["demo_acceptance"], path)
                self.assertEqual([], plan["unclassified_paths"])

    def test_demo_tests_need_no_live_run(self):
        # tests/ never matches DEMO_ENTRYPOINT_PREFIX -- it is not under entrypoints/demo/
        # at all -- so, unlike acceptance.py or the README, it needs no entry of its own in
        # DEMO_NO_GATE_PATHS to stay quiet.
        plan = classify_changed_paths(
            ["tests/test_demo_service.py", "tests/test_demo_boundary.py"]
        )
        self.assertFalse(plan["demo_acceptance"])

    def test_an_unnamed_demo_file_cannot_read_as_no_gate(self):
        """The demo's analogue of test_an_unclassified_package_file_is_not_reported_as_gate_free.

        A new module under entrypoints/demo/ must not inherit silence by being new: it is
        reported through the *same* unclassified_paths key the package uses (so a no-op diff
        still adds only the two demo_acceptance* keys to the report -- see
        test_a_no_op_diff_adds_only_the_two_demo_keys_to_the_report below) and it flips
        demo_acceptance, the one gate an unrecognised demo file could plausibly need.
        """
        plan = classify_changed_paths(["entrypoints/demo/new_module.py"])
        self.assertEqual(["entrypoints/demo/new_module.py"], plan["unclassified_paths"])
        self.assertTrue(plan["demo_acceptance"])
        self.assertEqual(
            [f"entrypoints/demo/new_module.py ({DEMO_ACCEPTANCE_COMMAND})"],
            plan["demo_acceptance_reasons"],
        )
        # It is not a live MCP boundary or a reviewed tool catalogue -- those stay off,
        # unlike an unclassified *package* file, which conservatively trips both.
        self.assertFalse(plan["live_smoke"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])

    def test_deploy_artifacts_outside_the_directory_still_need_the_run(self):
        # Dockerfile.demo and railway.demo.toml decide the model image and the health
        # check before any test does, exactly like the files inside entrypoints/demo/ --
        # they are just not under that prefix.
        for path in ("Dockerfile.demo", "railway.demo.toml"):
            with self.subTest(path=path):
                plan = classify_changed_paths([path])
                self.assertTrue(plan["demo_acceptance"], path)
                self.assertEqual(
                    [f"{path} ({DEMO_ACCEPTANCE_COMMAND})"], plan["demo_acceptance_reasons"]
                )

    def test_demo_acceptance_keys_are_always_present(self):
        # Both keys appear in every report, false/empty when nothing demo-related changed --
        # the same guarantee every other boolean/reasons pair in this report holds.
        plan = classify_changed_paths(["README.md"])
        self.assertIn("demo_acceptance", plan)
        self.assertIn("demo_acceptance_reasons", plan)
        self.assertFalse(plan["demo_acceptance"])
        self.assertEqual([], plan["demo_acceptance_reasons"])

    def test_package_and_demo_gates_stay_on_their_own_surface(self):
        # A change that touches both a package file and a demo file keeps each gate scoped:
        # the demo does not silence or widen the package's own rows, or vice versa.
        plan = classify_changed_paths(
            ["garmin_coach_loop/gateway.py", "entrypoints/demo/model.py"],
            diffs_by_path={
                "garmin_coach_loop/gateway.py": "+        callback = oauth_callback()"
            },
        )
        self.assertTrue(plan["live_smoke"])
        self.assertEqual(["garmin_coach_loop/gateway.py"], plan["live_smoke_reasons"])
        self.assertFalse(plan["client_acceptance"])
        self.assertTrue(plan["demo_acceptance"])
        self.assertEqual(
            [f"entrypoints/demo/model.py ({DEMO_ACCEPTANCE_COMMAND})"],
            plan["demo_acceptance_reasons"],
        )

    def test_every_demo_file_is_classified_exactly_once(self):
        """The demo's analogue of test_every_package_file_is_classified_exactly_once.

        Every committed file under entrypoints/demo/ is named by DEMO_ACCEPTANCE_PATHS, the
        fixtures prefix, or DEMO_NO_GATE_PATHS. A file none of the three covers fails here
        until change_gates.py is told what it is -- a new demo module cannot inherit "no
        gate" by being new, the same way a new package module cannot.
        """
        tracked = subprocess.run(
            ["git", "ls-files", "entrypoints/demo"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        demo_files = {path for path in tracked if path.startswith(DEMO_ENTRYPOINT_PREFIX)}
        fixture_files = {
            path for path in demo_files if path.startswith(DEMO_ACCEPTANCE_FIXTURE_PREFIX)
        }
        named_files = demo_files - fixture_files
        classified_under_demo = {
            path for path in CLASSIFIED_DEMO_PATHS if path.startswith(DEMO_ENTRYPOINT_PREFIX)
        }

        self.assertEqual(
            set(),
            named_files - classified_under_demo,
            "classify these in scripts/change_gates.py before merging",
        )
        self.assertEqual(
            set(),
            classified_under_demo - named_files,
            "scripts/change_gates.py names demo files that no longer exist",
        )
        self.assertTrue(
            fixture_files,
            "the fixture-prefix coverage this test relies on assumes at least one exists",
        )
        self.assertEqual(set(), DEMO_ACCEPTANCE_PATHS & DEMO_NO_GATE_PATHS)

    def test_a_no_op_diff_adds_only_the_two_demo_keys_to_the_report(self):
        """The report's contract with every other consumer of this gate.

        A diff that touches neither the package nor the demo must add nothing new to look
        at: every existing key keeps its exact shape, and the only new content this change
        introduces is demo_acceptance/demo_acceptance_reasons, both falsy.
        """
        plan = classify_changed_paths(["README.md", "docs/ops/verify-production-status.md"])
        self.assertEqual(
            {
                "changed_paths",
                "unclassified_paths",
                "live_smoke",
                "live_smoke_reasons",
                "client_acceptance",
                "client_acceptance_reasons",
                "protocol_acceptance",
                "protocol_acceptance_reasons",
                "scan_tools",
                "plugin_resubmission",
                "plugin_resubmission_reasons",
                "demo_acceptance",
                "demo_acceptance_reasons",
                "notes",
            },
            set(plan.keys()),
        )
        self.assertFalse(plan["live_smoke"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])
        self.assertFalse(plan["demo_acceptance"])
        self.assertEqual([], plan["demo_acceptance_reasons"])


class DependencyPinGateTests(unittest.TestCase):
    """A moved pin is a transport change with no diff, and only that.

    The protocol implementation behind `/mcp` belongs to the SDK. Which revisions the
    handshake agrees to, what an `initialize` result carries and which JSON-RPC error a
    refused batch returns can all move while every line in this repository stands still --
    measured during the 2.2.0 migration, where all three did. What that needs is the
    dual-era acceptance run. What it must never produce is a Scan Tools demand or an OpenAI
    resubmission: the reviewed catalogue and `instructions` are this repository's own
    bytes, so a dependency cannot move them, and a resubmission triggered by one would put
    a release into review for a change no reviewer can see.
    """

    def test_a_moved_pin_asks_for_the_dual_era_run_and_nothing_else(self):
        for path in sorted(DEPENDENCY_PATHS):
            with self.subTest(path=path):
                plan = classify_changed_paths([path], dependency_pin_moved=True)
                self.assertTrue(plan["protocol_acceptance"])
                self.assertEqual(
                    [DEPENDENCY_PIN_MOVED_REASON], plan["protocol_acceptance_reasons"]
                )
                # The dual-era run is a real-client run, so it is a client acceptance too.
                self.assertTrue(plan["client_acceptance"])
                self.assertEqual(
                    [DEPENDENCY_PIN_MOVED_REASON], plan["client_acceptance_reasons"]
                )
                self.assertFalse(plan["scan_tools"])
                self.assertFalse(plan["plugin_resubmission"])
                self.assertEqual([], plan["plugin_resubmission_reasons"])
                self.assertFalse(plan["live_smoke"])

    def test_a_pin_that_did_not_move_asks_for_nothing(self):
        """Editing the prose in `requirements.txt` is not an upgrade.

        The reason the dependency exists is written in that file, and rewriting it must not
        send anybody to run a manual acceptance -- the same way a marker line does not
        overturn a catalogue digest that proved the bytes stood still.
        """
        plan = classify_changed_paths(sorted(DEPENDENCY_PATHS), dependency_pin_moved=False)
        self.assertFalse(plan["protocol_acceptance"])
        self.assertEqual([], plan["protocol_acceptance_reasons"])
        self.assertFalse(plan["client_acceptance"])
        self.assertFalse(plan["scan_tools"])
        self.assertFalse(plan["plugin_resubmission"])

    def test_a_base_that_cannot_be_read_asks_conservatively(self):
        plan = classify_changed_paths(["requirements.lock"], dependency_pin_moved=None)
        self.assertTrue(plan["protocol_acceptance"])
        self.assertEqual(
            [DEPENDENCY_PIN_UNKNOWN_REASON], plan["protocol_acceptance_reasons"]
        )
        self.assertFalse(plan["scan_tools"])

    def test_an_unrelated_change_never_acquires_the_protocol_run(self):
        for paths, moved in (
            (["README.md"], None),
            (["garmin_coach_loop/store.py"], None),
            (["garmin_coach_loop/mcp_transport.py"], True),
        ):
            with self.subTest(paths=paths):
                plan = classify_changed_paths(
                    paths, catalogue_moved=moved, dependency_pin_moved=True
                )
                self.assertFalse(plan["protocol_acceptance"])
                self.assertEqual([], plan["protocol_acceptance_reasons"])

    def test_the_wire_module_asks_for_the_dual_era_run_whatever_its_diff_says(self):
        """No marker speaks for this file, so the absence of one proves nothing.

        `mcp_sdk_transport.py` serves no catalogue of its own, so the MCP surface markers
        never match it, and the failure it is capable of -- a 2026-07-28 client answered
        `400` and falling back to 2025 -- is invisible from inside the conversation that
        fell back.
        """
        for diff in ("+def _internal_helper():", "+        headers.append((b'accept', value))"):
            with self.subTest(diff=diff):
                plan = classify_changed_paths(
                    [PROTOCOL_SURFACE_PATH], diffs_by_path={PROTOCOL_SURFACE_PATH: diff}
                )
                self.assertTrue(plan["protocol_acceptance"])
                self.assertEqual([PROTOCOL_SURFACE_PATH], plan["protocol_acceptance_reasons"])
                self.assertFalse(plan["scan_tools"])
                self.assertFalse(plan["plugin_resubmission"])

    def test_the_wire_module_is_also_the_live_boundary_it_is_listed_as(self):
        self.assertIn(PROTOCOL_SURFACE_PATH, LIVE_SMOKE_PATHS)

    def test_the_pin_digest_reads_what_installs_and_not_the_prose_around_it(self):
        # Reaching for the private helper deliberately: the public entry points read Git
        # and this checkout, and the property under test is what the digest *ignores*.
        from scripts.change_gates import _pinned_dependency_set

        pinned = {
            "requirements.txt": "# why there is a dependency at all\nmcp==2.2.0\n",
            "requirements.lock": "mcp==2.2.0 \\\n    --hash=sha256:" + "a" * 64 + "\n",
        }
        reworded = dict(pinned, **{"requirements.txt": "# a different explanation\n\nmcp==2.2.0\n"})
        upgraded = dict(pinned, **{"requirements.txt": "mcp==2.2.1\n"})
        rehashed = dict(
            pinned,
            **{"requirements.lock": "mcp==2.2.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"},
        )
        without_lock = dict(pinned, **{"requirements.lock": None})

        self.assertEqual(_pinned_dependency_set(pinned), _pinned_dependency_set(reworded))
        for label, moved in (
            ("version", upgraded),
            ("artifact hash", rehashed),
            ("absent lock", without_lock),
        ):
            with self.subTest(moved=label):
                self.assertNotEqual(_pinned_dependency_set(pinned), _pinned_dependency_set(moved))

    def test_the_pin_is_read_from_a_ref_and_an_unknown_ref_answers_none(self):
        self.assertRegex(dependency_pin_at("HEAD") or "", r"^[0-9a-f]{64}$")
        self.assertIsNone(dependency_pin_at("no-such-ref-for-a-change-gate"))

    def test_a_clean_checkout_installs_what_its_head_commit_pins(self):
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *sorted(DEPENDENCY_PATHS)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            self.skipTest("uncommitted dependency edits: the working tree is not HEAD")
        self.assertEqual(dependency_pin_at("HEAD"), working_tree_dependency_pin())


class AffectedTestSelectionTests(unittest.TestCase):
    def test_docs_only_change_selects_no_product_tests(self):
        plan = select_test_paths(["docs/ops/roll-with-railway-cli.md"])
        self.assertEqual([], plan["test_paths"])
        self.assertFalse(plan["full_suite_required"])

    def test_production_status_runbook_selects_the_observe_ownership_tests(self):
        """The failure this catches: the runbook becoming an untested docs-only file.

        That is how 'Latest recorded release: 1.4.2' would land again without the
        assertion that refuses a present-tense live version in that page.
        """
        plan = select_test_paths(["docs/ops/verify-production-status.md"])
        self.assertEqual(["tests/test_release_bundle.py"], plan["test_paths"])
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
        # Two gates read a workflow: the job/concurrency rules here, and the rule that
        # every job installs the hashed lock rather than an unpinned resolution.
        plan = select_test_paths([".github/workflows/ci.yml"])
        self.assertEqual(
            ["tests/test_dependency_lock.py", "tests/test_process_gates.py"],
            plan["test_paths"],
        )

    def test_a_moved_pin_selects_the_lock_and_the_recorded_wire(self):
        plan = select_test_paths(["requirements.lock"])
        self.assertEqual(
            [
                "tests/test_dependency_lock.py",
                "tests/test_process_gates.py",
                "tests/test_protocol_envelope.py",
            ],
            plan["test_paths"],
        )
        self.assertFalse(plan["full_suite_required"])

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

    def test_every_workflow_job_that_runs_python_installs_the_dependency(self):
        """A gate that cannot import is not a gate, however safely it fails.

        `release_bundle.py` imports the gateway to read the served tool catalogue, and
        the gateway imports the MCP SDK. A job without the install step therefore fails
        every run on `ModuleNotFoundError` rather than judging one -- and it fails
        identically for a release that was fine and one that was not, which is the shape
        of a gate nobody can act on.

        **Every workflow, not one file.** The first version of this test named `ci.yml`
        and passed while the identical bug sat in `publish-mcp-registry.yml`, whose
        retry loop then reported the crash as "production is not serving this source
        yet" -- ten times, against a production that was serving it. A test that names
        its own scope too narrowly is how the same defect ships twice.

        A shallow scan, not a YAML parse: the question is only which jobs run Python.
        """
        workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
        self.assertTrue(workflows)
        checked = 0
        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            if "\njobs:" not in text:
                continue
            current: str | None = None
            jobs: dict[str, list[str]] = {}
            for line in text.split("\njobs:", 1)[1].splitlines():
                header = re.fullmatch(r"  ([A-Za-z][\w-]*):", line)
                if header is not None:
                    current = header.group(1)
                    jobs[current] = []
                elif current is not None:
                    jobs[current].append(line)
            for name, lines in jobs.items():
                block = "\n".join(lines)
                if "python3 " not in block:
                    continue
                checked += 1
                self.assertIn(
                    "pip install --disable-pip-version-check --require-hashes "
                    "-r requirements.lock",
                    block,
                    f"{workflow.name}: the {name!r} job runs Python without "
                    "installing the hashed lock",
                )
        # Four today: ci.yml's two, and publish-mcp-registry.yml's two. A refactor that
        # drops a workflow out of this scan would otherwise pass by checking nothing.
        self.assertEqual(4, checked)


if __name__ == "__main__":
    unittest.main()
