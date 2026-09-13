"""Publication must follow production, and executable release dependencies are immutable."""
import copy
import re
import unittest
from pathlib import Path

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
        self.assertNotIn("releases/latest", text)
        self.assertRegex(text, r"releases/download/v[0-9]+\.[0-9]+\.[0-9]+/")
        self.assertRegex(text, r'echo "[a-f0-9]{64}  ')
        self.assertLess(text.index("sha256sum --check --strict"), text.index("tar xzf"))
        self.assertIn("needs: verify-production", text)
        self.assertEqual(2, text.count("python3 scripts/verify_registry_release.py"))
        self.assertEqual(1, text.count("run: python3 scripts/verify_registry_release.py"))
        self.assertEqual(1, text.count("id-token: write"))
        self.assertGreater(text.index("id-token: write"), text.index("  publish:"))
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            for action in re.findall(r"uses: ([^\s]+)", workflow.read_text()):
                self.assertRegex(action, r"@[a-f0-9]{40}$")
