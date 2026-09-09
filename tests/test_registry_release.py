"""Publication must follow production, and executable release dependencies are immutable."""
import copy
import re
import unittest
from pathlib import Path

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

    def test_workflow_cannot_publish_at_merge_or_execute_unverified_download(self):
        text = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertNotRegex(text, r"(?m)^  (push|pull_request|workflow_run):")
        self.assertNotIn("releases/latest", text)
        self.assertRegex(text, r"releases/download/v[0-9]+\.[0-9]+\.[0-9]+/")
        self.assertRegex(text, r'echo "[a-f0-9]{64}  ')
        self.assertLess(text.index("sha256sum --check --strict"), text.index("tar xzf"))
        self.assertIn("needs: verify-production", text)
        self.assertEqual(2, text.count("run: python3 scripts/verify_registry_release.py"))
        self.assertEqual(1, text.count("id-token: write"))
        self.assertGreater(text.index("id-token: write"), text.index("  publish:"))
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            for action in re.findall(r"uses: ([^\s]+)", workflow.read_text()):
                self.assertRegex(action, r"@[a-f0-9]{40}$")
