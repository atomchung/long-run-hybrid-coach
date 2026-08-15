from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.custom_gpt_deploy import (
    DEPLOY_CERTIFIES,
    SMOKE_CERTIFIES,
    DeployError,
    activate,
    adopt_active,
    deploy_proxy,
    prepare,
    record_builder,
    rollback,
    status,
    verify,
)
from scripts.custom_gpt_release import bundle


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "custom_gpt_deploy.py"


class CustomGptDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.external = Path(self.temporary.name)
        self.home = self.external / "release-home"
        self.commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()

    def tearDown(self):
        self.temporary.cleanup()

    def _prepare(self, upstream: str = "https://tunnel-one.example", gateway: str = "https://gateway.example") -> dict:
        return prepare(
            home_path=self.home,
            git_commit=self.commit,
            main_ref="HEAD",
            gateway_domain=gateway,
            proxy_upstream=upstream,
            clock=lambda: "2026-08-15T01:00:00Z",
        )

    def _record_expected_builder(self, run_id: str) -> None:
        run_dir = self.home / "runs" / run_id
        record_builder(
            home_path=self.home,
            run_id=run_id,
            instructions_path=run_dir / "builder" / "expected-instructions.md",
            openapi_path=run_dir / "builder" / "expected-openapi.yaml",
            clock=lambda: "2026-08-15T01:01:00Z",
        )

    def _deploy(self, run_id: str, calls: list | None = None) -> None:
        secret = self.external / "gateway.env"
        if not secret.exists():
            secret.write_text("SECRET_MUST_NOT_BE_READ=sentinel\n", encoding="utf-8")
            secret.chmod(0o600)

        def runner(request: Path, secret_file: Path, receipt: Path) -> None:
            if calls is not None:
                calls.append((request, secret_file, receipt))
            request_value = json.loads(request.read_text(encoding="utf-8"))
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "run_id": run_id,
                        "release_id": request_value["release_identity"]["release_id"],
                        "proxy_revision_id": request_value["proxy_revision_id"],
                        "status": "succeeded",
                        "deployed_at": "2026-08-15T01:02:00Z",
                        "certifies": DEPLOY_CERTIFIES,
                    }
                ),
                encoding="utf-8",
            )

        deploy_proxy(home_path=self.home, run_id=run_id, secret_env_file=secret, runner=runner)
        self.assertEqual("SECRET_MUST_NOT_BE_READ=sentinel\n", secret.read_text(encoding="utf-8"))

    def _smoke(self, run_id: str, release_id: str) -> Path:
        path = self.external / f"{run_id}-smoke.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "run_id": run_id,
                    "release_id": release_id,
                    "status": "passed",
                    "observed_at": "2026-08-15T01:03:00Z",
                    "certifies": SMOKE_CERTIFIES,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _verify(self, run_id: str, calls: list | None = None) -> None:
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        release_id = manifest["release_identity"]["release_id"]

        def verifier(**kwargs):
            if calls is not None:
                calls.append(kwargs)
            return {
                "schema_version": "1",
                "release_identity": manifest["release_identity"],
                "certifies": "gateway artifact and Builder content parity only",
            }

        verify(
            home_path=self.home,
            run_id=run_id,
            smoke_evidence=self._smoke(run_id, release_id),
            verifier=verifier,
            clock=lambda: "2026-08-15T01:04:00Z",
        )

    def _ready(self, upstream: str = "https://tunnel-one.example") -> str:
        result = self._prepare(upstream)
        run_id = result["manifest"]["run_id"]
        self._record_expected_builder(run_id)
        self._deploy(run_id)
        self._verify(run_id)
        return run_id

    def _adopt_legacy(self) -> str:
        legacy = self.external / "legacy"
        legacy.mkdir()
        bundled = bundle(self.commit, "https://legacy-gateway.example")
        (legacy / "builder-bundle.json").write_text(json.dumps(bundled), encoding="utf-8")
        (legacy / "builder-instructions.md").write_text(bundled["instructions"], encoding="utf-8")
        (legacy / "builder-openapi.yaml").write_text(bundled["openapi"], encoding="utf-8")
        (legacy / "release-receipt.json").write_text(
            json.dumps({"schema_version": "1", "release_identity": {key: bundled[key] for key in ("release_id", "git_commit", "instructions_sha256", "openapi_sha256", "gateway_artifact_sha256", "gateway_domain")}, "certifies": "gateway artifact and Builder content parity only"}),
            encoding="utf-8",
        )
        (legacy / "live-smoke.json").write_text(
            json.dumps({"schema_version": "1", "release_id": bundled["release_id"], "git_commit": bundled["git_commit"], "observed_at": "2026-08-15T00:00:00Z", "checks": {"release_gate": "passed", "start_coach_session": "passed", "fresh_conversation_today_coaching": "passed"}, "writes_during_smoke": {"plan_modified": False, "provider_publish_requested": False, "provider_withdraw_requested": False}}),
            encoding="utf-8",
        )

        def verifier(**_kwargs):
            return {"schema_version": "1", "release_identity": {key: bundled[key] for key in ("release_id", "git_commit", "instructions_sha256", "openapi_sha256", "gateway_artifact_sha256", "gateway_domain")}, "certifies": "gateway artifact and Builder content parity only"}

        result = adopt_active(home_path=self.home, legacy_dir=legacy, verifier=verifier, clock=lambda: "2026-08-15T00:01:00Z")
        return result["active"]["run_id"]

    def test_prepare_binds_exact_main_commit_and_generates_external_payloads(self):
        first = self._prepare()
        manifest = first["manifest"]
        run_id = manifest["run_id"]
        run_dir = self.home / "runs" / run_id
        self.assertTrue(first["changed"])
        self.assertEqual(self.commit, manifest["git_candidate"]["resolved_main_commit"])
        self.assertEqual(
            {"git_commit": None, "instructions": None, "openapi": None, "gateway_artifact": None, "gateway_domain": None, "proxy_upstream": None},
            manifest["changes_from_active"],
        )
        proxy = json.loads((run_dir / "proxy" / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual("https://tunnel-one.example/:path*", proxy["rewrites"][0]["destination"])
        request = json.loads((run_dir / "deploy-request.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_identity"], request["release_identity"])
        self.assertEqual(0o600, (run_dir / "manifest.json").stat().st_mode & 0o777)
        again = self._prepare()
        self.assertFalse(again["changed"])
        self.assertEqual(manifest, again["manifest"])

    def test_prepare_refuses_a_candidate_other_than_main(self):
        previous = subprocess.run(
            ["git", "rev-parse", "HEAD^"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()
        with self.assertRaisesRegex(DeployError, "exactly equal"):
            prepare(
                home_path=self.home,
                git_commit=previous,
                main_ref="HEAD",
                gateway_domain="https://gateway.example",
                proxy_upstream="https://tunnel.example",
            )

    def test_proxy_repair_is_a_revision_of_the_same_release_run(self):
        first = self._prepare("https://tunnel-one.example")
        repaired = self._prepare("https://tunnel-two.example")
        self.assertEqual(first["manifest"]["run_id"], repaired["manifest"]["run_id"])
        self.assertEqual(first["manifest"]["release_identity"]["release_id"], repaired["manifest"]["release_identity"]["release_id"])
        self.assertEqual(2, len(repaired["manifest"]["proxy_revisions"]))
        self.assertEqual("https://tunnel-two.example", repaired["manifest"]["proxy_upstream"])
        self.assertIsNone(repaired["manifest"]["deployment"])
        self.assertIsNone(repaired["manifest"]["verification"])

    def test_pending_proxy_repair_preserves_the_active_revision_evidence(self):
        self._adopt_legacy()
        run_id = self._ready("https://tunnel-one.example")
        activated = activate(home_path=self.home, run_id=run_id)
        active_revision = activated["active"]["proxy_revision_id"]
        repaired = self._prepare("https://tunnel-two.example")
        prior = next(item for item in repaired["manifest"]["proxy_revisions"] if item["proxy_revision_id"] == active_revision)
        self.assertEqual("passed", prior["verification"]["status"])
        self.assertEqual(active_revision, status(home_path=self.home)["active"]["proxy_revision_id"])
        self.assertNotEqual(active_revision, repaired["manifest"]["current_proxy_revision_id"])

    def test_legacy_adoption_is_required_before_first_activation(self):
        run_id = self._ready()
        with self.assertRaisesRegex(DeployError, "adopt the currently verified"):
            activate(home_path=self.home, run_id=run_id)
        adopted_home = self.external / "adopted-home"
        original_home = self.home
        self.home = adopted_home
        try:
            adopted = self._adopt_legacy()
            current = status(home_path=self.home)["active"]
            self.assertEqual(adopted, current["run_id"])
            self.assertIsNone(current["previous_run_id"])
            self.assertTrue(status(home_path=self.home, run_id=adopted)["run"]["adoption"]["legacy_layout"])
        finally:
            self.home = original_home

    def test_builder_deploy_verify_and_activate_are_resumable(self):
        self._adopt_legacy()
        prepared = self._prepare()
        run_id = prepared["manifest"]["run_id"]
        self._record_expected_builder(run_id)
        builder_again = record_builder(
            home_path=self.home,
            run_id=run_id,
            instructions_path=self.home / "runs" / run_id / "builder" / "expected-instructions.md",
            openapi_path=self.home / "runs" / run_id / "builder" / "expected-openapi.yaml",
        )
        self.assertFalse(builder_again["changed"])

        deploy_calls: list = []
        self._deploy(run_id, deploy_calls)
        self._deploy(run_id, deploy_calls)
        self.assertEqual(1, len(deploy_calls))
        self.assertFalse(status(home_path=self.home, run_id=run_id)["run"]["deployment"]["secret_file_contents_read_by_orchestrator"])

        verify_calls: list = []
        self._verify(run_id, verify_calls)
        self._verify(run_id, verify_calls)
        self.assertEqual(1, len(verify_calls))
        activated = activate(home_path=self.home, run_id=run_id, clock=lambda: "2026-08-15T01:05:00Z")
        self.assertTrue(activated["changed"])
        self.assertFalse(activate(home_path=self.home, run_id=run_id)["changed"])
        self.assertEqual(run_id, status(home_path=self.home)["active"]["run_id"])

    def test_mismatched_builder_and_missing_deployment_block_verification(self):
        prepared = self._prepare()
        run_id = prepared["manifest"]["run_id"]
        release_id = prepared["manifest"]["release_identity"]["release_id"]
        with self.assertRaisesRegex(DeployError, "deployment receipt"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=self._smoke(run_id, release_id), verifier=lambda **_: {})
        stale = self.external / "stale.md"
        stale.write_text("stale", encoding="utf-8")
        expected_openapi = self.home / "runs" / run_id / "builder" / "expected-openapi.yaml"
        record_builder(home_path=self.home, run_id=run_id, instructions_path=stale, openapi_path=expected_openapi)
        self._deploy(run_id)
        with self.assertRaisesRegex(DeployError, "do not match"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=self._smoke(run_id, release_id), verifier=lambda **_: {})
        with self.assertRaisesRegex(DeployError, "parity-verified"):
            activate(home_path=self.home, run_id=run_id)

    def test_secret_file_is_metadata_only_and_receipt_is_strict(self):
        prepared = self._prepare()
        run_id = prepared["manifest"]["run_id"]
        secret = self.external / "gateway.env"
        secret.write_text("do-not-read", encoding="utf-8")
        secret.chmod(0o644)
        with self.assertRaisesRegex(DeployError, "group or other"):
            deploy_proxy(home_path=self.home, run_id=run_id, secret_env_file=secret, runner=lambda *_: None)
        secret.chmod(0o600)

        def bad_runner(_request, _secret, receipt):
            receipt.write_text(json.dumps({"status": "succeeded", "secret": "leak"}), encoding="utf-8")

        with self.assertRaisesRegex(DeployError, "does not certify"):
            deploy_proxy(home_path=self.home, run_id=run_id, secret_env_file=secret, runner=bad_runner)
        self.assertEqual("do-not-read", secret.read_text(encoding="utf-8"))

    def test_rollback_targets_the_previous_verified_run_and_is_idempotent(self):
        legacy = self._adopt_legacy()
        first = self._ready("https://tunnel-one.example")
        activate(home_path=self.home, run_id=first, clock=lambda: "2026-08-15T01:05:00Z")
        prepared = self._prepare("https://tunnel-two.example", gateway="https://gateway-two.example")
        second = prepared["manifest"]["run_id"]
        self._record_expected_builder(second)
        self._deploy(second)
        self._verify(second)
        activate(home_path=self.home, run_id=second, clock=lambda: "2026-08-15T02:05:00Z")
        rolled_back = rollback(home_path=self.home, source_run_id=second, record=True, clock=lambda: "2026-08-15T02:06:00Z")
        self.assertTrue(rolled_back["changed"])
        self.assertEqual(first, rolled_back["plan"]["target_run_id"])
        self.assertEqual(second, rolled_back["active"]["run_id"])
        self.assertFalse(rolled_back["plan"]["live_state_changed"])
        self.assertFalse(rollback(home_path=self.home, source_run_id=second, record=True)["changed"])
        self.assertNotEqual(legacy, second)

    def test_cli_live_commands_default_to_plans(self):
        prepared = self._prepare()
        run_id = prepared["manifest"]["run_id"]
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--home",
                str(self.home),
                "run-deployment-adapter",
                "--run-id",
                run_id,
                "--secret-env-file",
                str(self.external / "absent.env"),
                "--runner",
                "/bin/false",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no command executed", result.stdout)
        self.assertFalse((self.home / "runs" / run_id / ".runner-receipt.json").exists())
        verify_plan = subprocess.run(
            ["python3", str(SCRIPT), "--home", str(self.home), "verify", "--run-id", run_id, "--smoke-evidence", str(self.external / "absent.json")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, verify_plan.returncode, verify_plan.stderr)
        self.assertIn("no network request made", verify_plan.stdout)

    def test_release_home_and_evidence_cannot_resolve_inside_repo(self):
        with self.assertRaisesRegex(DeployError, "outside"):
            prepare(
                home_path=ROOT / "release-home",
                git_commit=self.commit,
                main_ref="HEAD",
                gateway_domain="https://gateway.example",
                proxy_upstream="https://tunnel.example",
            )


if __name__ == "__main__":
    unittest.main()
