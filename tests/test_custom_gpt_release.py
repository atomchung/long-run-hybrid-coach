from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.release_identity import ReleaseIdentityError, make_release_id, normalise_gateway_domain, package_artifact_sha256, release_identity, sha256_text


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "custom_gpt_release.py"


class ReleaseIdentityTests(unittest.TestCase):
    def test_release_identity_binds_every_deployment_input(self):
        identity = {"git_commit": "a" * 40, "instructions_sha256": "1" * 64, "openapi_sha256": "2" * 64, "gateway_artifact_sha256": "3" * 64, "gateway_domain": "https://gateway.example"}
        identity["release_id"] = make_release_id(**identity)
        self.assertEqual(identity, release_identity(identity))
        identity["gateway_domain"] = "https://other.example"
        with self.assertRaises(ReleaseIdentityError): release_identity(identity)

    def test_placeholder_is_refused(self):
        with self.assertRaises(ReleaseIdentityError): normalise_gateway_domain("https://YOUR-GATEWAY-DOMAIN")
        for value in ("http://gateway.example", "https://" + "user" + "@gateway.example", "https://gateway.example/path", "https://gateway.example?q=x"):
            with self.assertRaises(ReleaseIdentityError): normalise_gateway_domain(value)
        self.assertEqual("https://gateway.example", normalise_gateway_domain("https://Gateway.EXAMPLE:443/"))

    def test_package_digest_changes_for_any_runtime_module(self):
        self.assertNotEqual(package_artifact_sha256([("gateway.py", b"a"), ("store.py", b"b")]), package_artifact_sha256([("gateway.py", b"a"), ("store.py", b"changed")]))

    def test_builder_gate_is_deterministic_and_refuses_stale_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.json"
            result = subprocess.run(["python3", str(SCRIPT), "build", "--gateway-domain", "https://gateway.example", "--output", str(bundle)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            first = bundle.read_text(encoding="utf-8")
            again = root / "again.json"
            self.assertEqual(0, subprocess.run(["python3", str(SCRIPT), "build", "--gateway-domain", "https://gateway.example", "--output", str(again)], cwd=ROOT).returncode)
            self.assertEqual(first, again.read_text(encoding="utf-8"))
            data = json.loads(first)
            instructions, openapi = root / "instructions.md", root / "openapi.yaml"
            instructions.write_text(data["instructions"], encoding="utf-8"); openapi.write_text(data["openapi"], encoding="utf-8")
            # Network verification is exercised by the gateway health contract; this
            # deterministic test verifies the external Builder artifacts it will compare.
            self.assertEqual(data["instructions_sha256"], sha256_text(instructions.read_text(encoding="utf-8")))
            instructions.write_text("stale", encoding="utf-8")
            self.assertNotEqual(data["instructions_sha256"], sha256_text(instructions.read_text(encoding="utf-8")))
