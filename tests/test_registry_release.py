"""Publication must follow production, and executable release dependencies are immutable."""
import copy
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

from unittest import mock

from scripts import verify_registry_release
from scripts.verify_registry_release import verify_readyz
from garmin_coach_loop.release_identity import ReleaseIdentityError, make_release_id

ROOT = Path(__file__).resolve().parents[1]


class RegistryReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.identity = dict(git_commit="a" * 40, instructions_sha256="b" * 64,
            tool_catalogue_sha256="c" * 64, skill_sha256="d" * 64,
            gateway_artifact_sha256="e" * 64,
            gateway_domain="https://mcp.paceandstaystrong.com")
        self.identity["release_id"] = make_release_id(**self.identity)
        self.health = dict(status="ok", source_git_commit="a" * 40,
            product_version="1.4.1", release_identity=self.identity,
            deployment_identity={"environment": "production"})

    def test_exact_production_receipt_passes(self):
        verify_readyz(self.health, self.identity, "1.4.1")

    def test_stale_unready_or_wrong_surface_cannot_publish(self):
        for field, value in (("status", "blocked"), ("source_git_commit", "f" * 40),
                             ("product_version", "1.4.0"), ("deployment_identity", {})):
            bad = {**self.health, field: value}
            with self.subTest(field=field), self.assertRaises(ReleaseIdentityError):
                verify_readyz(bad, self.identity, "1.4.1")
        for field in self.identity:
            bad = copy.deepcopy(self.health)
            bad["release_identity"][field] = "f" * 64
            with self.subTest(field=field), self.assertRaises(ReleaseIdentityError):
                verify_readyz(bad, self.identity, "1.4.1")

    def test_wait_keeps_polling_until_production_serves_this_source(self):
        # A push to `production` starts the publish workflow and the deployment together, so
        # the gate has to outlive the old receipt: refusals inside the deadline are retried,
        # the first matching receipt is printed, and no sleep is real.
        receipts = iter([ReleaseIdentityError("old commit"), OSError("connection reset"), self.health])

        def receipt(expected, version):
            outcome = next(receipts)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        sleeps = []
        with mock.patch.object(verify_registry_release, "registry_version", return_value="1.4.1"), \
             mock.patch.object(verify_registry_release, "bundle", return_value=self.identity), \
             mock.patch.object(verify_registry_release, "commit_at_head", return_value="a" * 40), \
             mock.patch.object(verify_registry_release, "production_receipt", side_effect=receipt), \
             mock.patch.object(verify_registry_release.time, "sleep", sleeps.append), \
             mock.patch.object(verify_registry_release.sys, "stderr"), \
             mock.patch.object(verify_registry_release.sys, "stdout"):
            verify_registry_release.main(["--wait-minutes", "5", "--poll-seconds", "7"])
        self.assertEqual([7.0, 7.0], sleeps)

    def test_wait_gives_up_at_the_deadline_and_never_waits_on_a_source_mismatch(self):
        with mock.patch.object(verify_registry_release, "registry_version", return_value="1.4.1"), \
             mock.patch.object(verify_registry_release, "bundle", return_value=self.identity), \
             mock.patch.object(verify_registry_release, "commit_at_head", return_value="a" * 40), \
             mock.patch.object(verify_registry_release, "production_receipt",
                               side_effect=ReleaseIdentityError("old commit")), \
             mock.patch.object(verify_registry_release.time, "sleep"), \
             mock.patch.object(verify_registry_release.sys, "stderr"):
            with self.assertRaises(ReleaseIdentityError):
                verify_registry_release.main(["--wait-minutes", "0"])
        # The checkout's own server.json disagreeing with PRODUCT_VERSION is not a deployment
        # in progress; it fails before the first poll, however long the wait.
        with mock.patch.object(verify_registry_release, "registry_version",
                               side_effect=ReleaseIdentityError("Registry version differs from source version")), \
             mock.patch.object(verify_registry_release, "production_receipt") as receipt:
            with self.assertRaises(ReleaseIdentityError):
                verify_registry_release.main(["--wait-minutes", "30"])
            receipt.assert_not_called()

    def test_verify_step_gives_up_after_ten_failed_gates_and_stops_at_the_first_pass(self):
        # The retry loop is shell in the workflow, not Python, so it is run as shell: the gate
        # is a stub that fails a set number of times, and the sleep is a no-op.
        text = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text()
        step = re.search(r"Verify production serves.*?run: \|\n((?:          [^\n]*\n)+)", text, re.S).group(1)
        script = "\n".join(line[10:] for line in step.splitlines())
        self.assertIn("python3 scripts/verify_registry_release.py", script)
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "gate.py"
            stub.write_text(
                "import os, sys\n"
                "n = int(open('calls').read()) + 1 if os.path.exists('calls') else 1\n"
                "open('calls', 'w').write(str(n))\n"
                "sys.exit(0 if n >= int(os.environ['PASS_ON']) else 1)\n")
            runnable = script.replace("python3 scripts/verify_registry_release.py",
                                      f"python3 {stub}").replace("sleep 30", "true")
            for pass_on, expected_code, expected_calls in ((3, 0, 3), (99, 1, 10)):
                calls = Path(tmp) / "calls"
                if calls.exists():
                    calls.unlink()
                done = subprocess.run(["bash", "-c", runnable], cwd=tmp, capture_output=True,
                                      text=True, env={**os.environ, "PASS_ON": str(pass_on)})
                with self.subTest(pass_on=pass_on):
                    self.assertEqual(expected_code, done.returncode, done.stdout + done.stderr)
                    self.assertEqual(str(expected_calls), calls.read_text())

    def test_workflow_cannot_publish_at_merge_or_execute_unverified_download(self):
        text = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text()
        self.assertIn("workflow_dispatch:", text)
        # It starts on the deployment status Railway posts once the container is up -- never
        # on a push, a merge, a schedule or another workflow, because Railway's Wait for CI
        # holds the deployment until every workflow on the commit finishes, and a workflow
        # waiting for that deployment would hold it forever.
        self.assertIn("  deployment_status:", text)
        self.assertNotRegex(text, r"(?m)^  (push|pull_request|workflow_run|schedule|release|create):")
        self.assertIn("github.event.deployment_status.state == 'success'", text)
        self.assertIn("contains(github.event.deployment.environment, 'production')", text)
        # The first job may ask the gate more than once, but a bounded number of times, and
        # gives up loudly rather than publishing when production never serves the commit.
        self.assertRegex(text, r"for attempt in 1 2 3 4 5 6 7 8 9 10; do")
        self.assertLess(text.index("exit 1"), text.index("  publish:"))
        # Ignored and already-current events never join the publication queue.
        self.assertNotIn("\nconcurrency:", text)
        publish = text.split("\n  publish:", 1)[1]
        self.assertIn("if: needs.verify-production.outputs.publish_required == 'true'", publish)
        self.assertIn("group: mcp-registry-publication", publish)
        self.assertIn("cancel-in-progress: false", publish)
        self.assertNotIn("releases/latest", text)
        self.assertRegex(text, r"releases/download/v[0-9]+\.[0-9]+\.[0-9]+/")
        self.assertRegex(text, r'echo "[a-f0-9]{64}  ')
        self.assertLess(text.index("sha256sum --check --strict"), text.index("tar xzf"))
        self.assertIn("needs: verify-production", text)
        self.assertEqual(3, text.count("python3 scripts/verify_registry_release.py"))
        self.assertEqual(2, text.count('--registry-output "$GITHUB_OUTPUT"'))
        self.assertIn("if: steps.registry.outputs.publish_required == 'true'", publish)
        self.assertEqual(1, text.count("id-token: write"))
        self.assertGreater(text.index("id-token: write"), text.index("  publish:"))
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            for action in re.findall(r"uses: ([^\s]+)", workflow.read_text()):
                self.assertRegex(action, r"@[a-f0-9]{40}$")

    def test_observed_demo_only_deployment_skips_but_promoted_source_is_eligible(self):
        # Read from GitHub on 2026-09-26: deployments 6516206309 (gateway) and
        # 6516179253 (demo). Neither payload/status names a service. The source ref,
        # not an invented service marker, is the distinction this workflow can make.
        gateway = {"sha": "6d55a70baac354c5fb3f2d0e37f76f72d7f31d06",
                   "environment": "adequate-victory / production",
                   "description": "Deployed to Railway",
                   "payload": {"environmentId": "0e76145e-6e80-4234-bf70-fb5bf8972911"}}
        demo = {**gateway, "sha": "5a7e9877ba3fecc69af24c5749029eba04b38a77"}
        text = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text()
        step = re.search(r"Select a deployment.*?run: \|\n((?:          [^\n]*\n)+)", text, re.S).group(1)
        script = "\n".join(line[10:] for line in step.splitlines())
        script = script.replace("$(git rev-parse HEAD)", gateway["sha"])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            for event, deployment, expected in (("deployment_status", gateway, "true"),
                                                ("deployment_status", demo, "false"),
                                                ("workflow_dispatch", demo, "true")):
                output.write_text("")
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                    text=True, env={**os.environ, "EVENT_NAME": event,
                                    "DEPLOYED_SHA": deployment["sha"], "GITHUB_OUTPUT": str(output)})
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(f"eligible={expected}\n", output.read_text())
                if expected == "false":
                    self.assertIn("not the current production ref", result.stdout)
        self.assertIn("if: needs.select-deployment.outputs.eligible == 'true'", text)

    def published_response(self, published=None):
        manifest = json.loads((ROOT / "server.json").read_text())
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = (f"{verify_registry_release.REGISTRY}/"
            f"{quote(manifest['name'], safe='')}/versions/{manifest['version']}")
        response.read.return_value = json.dumps(published if published is not None else {
            "server": manifest,
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
        }).encode()
        return response

    def test_exact_published_version_is_successful_noop_with_reason(self):
        with mock.patch.object(verify_registry_release, "_open_without_redirects",
                               return_value=self.published_response()), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(verify_registry_release.registry_publication_required())
        self.assertIn("Registry already carries version", output.getvalue())
        self.assertIn("entry is current, skipping publication", output.getvalue())

    def test_only_missing_registry_version_requires_publication(self):
        for status in (404, 400, 403, 429, 500):
            error = HTTPError("https://registry.modelcontextprotocol.io", status, "error", {}, None)
            with self.subTest(status=status), \
                 mock.patch.object(verify_registry_release, "_open_without_redirects", side_effect=error), \
                 contextlib.redirect_stdout(io.StringIO()):
                if status == 404:
                    self.assertTrue(verify_registry_release.registry_publication_required())
                else:
                    with self.assertRaises(HTTPError):
                        verify_registry_release.registry_publication_required()

    def test_conflicting_or_inactive_registry_entry_fails_instead_of_noop(self):
        manifest = json.loads((ROOT / "server.json").read_text())
        for field, value in (("name", "other"), ("version", "0.0.0"),
                             ("remotes", [{"type": "streamable-http", "url": "https://other.example/mcp"}])):
            published = {"server": {**manifest, field: value}}
            with self.subTest(field=field), \
                 mock.patch.object(verify_registry_release, "_open_without_redirects",
                                   return_value=self.published_response(published)), \
                 self.assertRaises(ReleaseIdentityError):
                verify_registry_release.registry_publication_required()
        with mock.patch.object(verify_registry_release, "_open_without_redirects",
                               return_value=self.published_response({"server": manifest})), \
             self.assertRaisesRegex(ReleaseIdentityError, "not active"):
            verify_registry_release.registry_publication_required()

    def test_registry_output_is_written_only_after_production_and_registry_pass(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(verify_registry_release, "registry_version", return_value="1.4.1"), \
             mock.patch.object(verify_registry_release, "bundle", return_value=self.identity), \
             mock.patch.object(verify_registry_release, "commit_at_head", return_value="a" * 40), \
             mock.patch.object(verify_registry_release, "production_receipt", return_value=self.health) as receipt, \
             mock.patch.object(verify_registry_release, "registry_publication_required", return_value=False) as registry, \
             contextlib.redirect_stdout(io.StringIO()):
            output = Path(tmp) / "github-output"
            verify_registry_release.main(["--registry-output", str(output)])
            self.assertEqual("publish_required=false\n", output.read_text())
            output.unlink()
            registry.side_effect = ReleaseIdentityError("different remote")
            with self.assertRaises(ReleaseIdentityError):
                verify_registry_release.main(["--registry-output", str(output)])
            self.assertFalse(output.exists())
            registry.reset_mock()
            receipt.side_effect = ReleaseIdentityError("old commit")
            with self.assertRaises(ReleaseIdentityError):
                verify_registry_release.main(["--registry-output", str(output)])
            registry.assert_not_called()

    def test_publisher_failure_remains_a_failure(self):
        text = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text()
        step = re.search(r"Authenticate with GitHub OIDC.*?run: \|\n((?:          [^\n]*\n)+)", text, re.S).group(1)
        script = "\n".join(line[10:] for line in step.splitlines())
        with tempfile.TemporaryDirectory() as tmp:
            publisher = Path(tmp) / "mcp-publisher"
            publisher.write_text('#!/bin/sh\n[ "$1" = login ] && exit 0\necho "400 genuine failure" >&2\nexit 1\n')
            publisher.chmod(0o755)
            result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                text=True, env={**os.environ, "RUNNER_TEMP": tmp})
        self.assertEqual(1, result.returncode)
        self.assertIn("400 genuine failure", result.stderr)
