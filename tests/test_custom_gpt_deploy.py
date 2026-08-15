from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.custom_gpt_deploy import (
    DEPLOY_CERTIFIES,
    ROUTE_MARKER_HEADER,
    SMOKE_CERTIFIES,
    DeployError,
    activate,
    adopt_active,
    deploy_proxy,
    prepare,
    record_builder,
    repair_proxy,
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
        self.commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        self.identity = {"environment": "production", "instance_id": "gateway-prod-1", "configuration_binding": "a" * 64}
        self.ci = self.external / "github-ci.json"
        self.ci.write_text(json.dumps({"schema_version": "1", "provider": "github-actions", "head_sha": self.commit, "status": "completed", "conclusion": "success", "run_id": 12345, "url": "https://github.com/example-org/garmin-coach-loop/actions/runs/12345"}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _prepare(self, upstream: str = "https://tunnel-one.example", gateway: str = "https://gateway.example") -> dict:
        return prepare(home_path=self.home, git_commit=self.commit, main_ref="HEAD", gateway_domain=gateway,
                       proxy_upstream=upstream, github_ci_evidence=self.ci, expected_deployment_identity=self.identity,
                       clock=lambda: "2026-08-15T01:00:00Z")

    def _record_builder(self, run_id: str) -> None:
        run_dir = self.home / "runs" / run_id
        instructions = (run_dir / "builder" / "expected-instructions.md").read_text()
        openapi = (run_dir / "builder" / "expected-openapi.yaml").read_text()
        revision_id = status(home_path=self.home, run_id=run_id)["run"]["current_proxy_revision_id"]
        evidence = self.external / f"builder-evidence-{run_id}.json"
        evidence.write_text(json.dumps({"schema_version": "1", "producer": "custom-gpt-builder-export", "gpt_id": "gpt-production-123", "exported_at": "2026-08-15T01:00:30Z", "run_id": run_id, "proxy_revision_id": revision_id, "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(), "openapi_sha256": hashlib.sha256(openapi.encode()).hexdigest()}), encoding="utf-8")
        record_builder(home_path=self.home, run_id=run_id,
                       instructions_path=run_dir / "builder" / "expected-instructions.md",
                       openapi_path=run_dir / "builder" / "expected-openapi.yaml",
                       builder_evidence=evidence,
                       clock=lambda: "2026-08-15T01:01:00Z")

    def _deploy(self, run_id: str, calls: list | None = None, mutate: dict | None = None) -> None:
        secret = self.external / "gateway.env"
        if not secret.exists():
            secret.write_text("SECRET_MUST_NOT_BE_READ=sentinel\n", encoding="utf-8")
            secret.chmod(0o600)

        def runner(request: Path, secret_file: Path, receipt: Path) -> None:
            request_value = json.loads(request.read_text(encoding="utf-8"))
            if calls is not None:
                calls.append((request_value, secret_file))
            value = {
                "schema_version": "2", "provider": "vercel", "target": "production",
                "run_id": run_id, "release_id": request_value["release_identity"]["release_id"],
                "proxy_revision_id": request_value["proxy_revision_id"],
                "request_sha256": status(home_path=self.home, run_id=run_id)["run"]["deploy_request_sha256"],
                "config_sha256": request_value["proxy"]["config_sha256"],
                "deployment_id": "dpl_" + request_value["proxy_revision_id"][-12:],
                "url": "https://gcl-production.vercel.app", "status": "succeeded",
                "deployed_at": "2026-08-15T01:02:00Z", "certifies": DEPLOY_CERTIFIES,
            }
            value.update(mutate or {})
            receipt.write_text(json.dumps(value), encoding="utf-8")

        deploy_proxy(home_path=self.home, run_id=run_id, secret_env_file=secret, runner=runner)
        self.assertEqual("SECRET_MUST_NOT_BE_READ=sentinel\n", secret.read_text(encoding="utf-8"))

    def _smoke(self, run_id: str, *, revision_id: str | None = None, mutate: dict | None = None) -> Path:
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        revision = next(item for item in manifest["proxy_revisions"] if item["proxy_revision_id"] == (revision_id or manifest["current_proxy_revision_id"]))
        browser = self.external / f"browser-{revision['proxy_revision_id']}.json"
        browser.write_text(json.dumps({"schema_version": "1", "producer": "codex-browser-smoke-v1", "observed_at": "2026-08-15T01:03:00Z", "gpt_id": "gpt-production-123", "conversation_ref": "browser://custom-gpt/preview/receipt-123", "artifact_kind": "browser-receipt", "status": "passed"}), encoding="utf-8")
        value = {
            "schema_version": "2", "run_id": run_id, "release_id": manifest["release_identity"]["release_id"],
            "proxy_revision_id": revision["proxy_revision_id"], "request_sha256": revision["request_sha256"],
            "deployment_id": (revision.get("deployment") or {}).get("deployment_id"),
            "expected_deployment_identity": self.identity, "producer": "codex-browser-smoke-v1",
            "browser_evidence_ref": "browser://custom-gpt/preview/receipt-123", "browser_evidence_sha256": hashlib.sha256(browser.read_bytes()).hexdigest(), "status": "passed",
            "observed_at": "2026-08-15T01:03:00Z", "certifies": SMOKE_CERTIFIES,
        }
        value.update(mutate or {})
        path = self.external / f"smoke-{revision['proxy_revision_id']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _browser(self, run_id: str) -> Path:
        revision_id = status(home_path=self.home, run_id=run_id)["run"]["current_proxy_revision_id"]
        return self.external / f"browser-{revision_id}.json"

    def _browser_for_smoke(self, smoke: Path) -> Path:
        revision_id = json.loads(smoke.read_text())["proxy_revision_id"]
        return self.external / f"browser-{revision_id}.json"

    def _route(self, run_id: str):
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        revision_id = manifest["current_proxy_revision_id"]

        def checker(url: str) -> dict:
            return {"status": 200, "url": url, "headers": {ROUTE_MARKER_HEADER: revision_id}, "body": {"status": "ok", "release_identity": manifest["release_identity"], "deployment_identity": self.identity}}
        return checker

    def _verify(self, run_id: str) -> dict:
        manifest = status(home_path=self.home, run_id=run_id)["run"]

        def verifier(**_kwargs):
            return {"schema_version": "2", "release_identity": manifest["release_identity"], "deployment_identity": self.identity, "certifies": "gateway artifact, Builder content and deployment configuration parity only"}

        smoke = self._smoke(run_id)
        return verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser(run_id), verifier=verifier,
                      route_checker=self._route(run_id), clock=lambda: "2026-08-15T01:04:00Z")

    def _ready(self, upstream: str = "https://tunnel-one.example", gateway: str = "https://gateway.example") -> str:
        run_id = self._prepare(upstream, gateway)["manifest"]["run_id"]
        self._record_builder(run_id)
        self._deploy(run_id)
        self._verify(run_id)
        return run_id

    def _adopt(self) -> str:
        legacy = self.external / "legacy"
        legacy.mkdir(exist_ok=True)
        bundled = bundle(self.commit, "https://legacy-gateway.example")
        identity = {key: bundled[key] for key in ("release_id", "git_commit", "instructions_sha256", "openapi_sha256", "gateway_artifact_sha256", "gateway_domain")}
        (legacy / "builder-bundle.json").write_text(json.dumps(bundled), encoding="utf-8")
        (legacy / "builder-instructions.md").write_text(bundled["instructions"], encoding="utf-8")
        (legacy / "builder-openapi.yaml").write_text(bundled["openapi"], encoding="utf-8")
        (legacy / "release-receipt.json").write_text(json.dumps({"schema_version": "1", "release_identity": identity, "certifies": "gateway artifact and Builder content parity only"}), encoding="utf-8")
        (legacy / "live-smoke.json").write_text(json.dumps({"schema_version": "1", "release_id": bundled["release_id"], "git_commit": bundled["git_commit"], "observed_at": "2026-08-15T00:00:00Z", "checks": {"release_gate": "passed", "start_coach_session": "passed", "fresh_conversation_today_coaching": "passed"}, "writes_during_smoke": {"plan_modified": False, "provider_publish_requested": False, "provider_withdraw_requested": False}}), encoding="utf-8")

        def verifier(**_kwargs):
            return {"schema_version": "1", "release_identity": identity, "certifies": "gateway artifact and Builder content parity only"}

        legacy_proxy = self.external / "legacy-vercel.json"
        legacy_proxy.write_text(json.dumps({"rewrites": [{"source": "/:path*", "destination": "https://legacy-tunnel.example/:path*"}]}), encoding="utf-8")
        return adopt_active(home_path=self.home, legacy_dir=legacy, current_proxy_upstream="https://legacy-tunnel.example",
                            current_proxy_config=legacy_proxy, expected_deployment_identity=self.identity, verifier=verifier,
                            route_checker=lambda url: {"status": 200, "url": url, "headers": {}, "body": {"status": "ok", "release_identity": identity}},
                            clock=lambda: "2026-08-15T00:01:00Z")["active"]["run_id"]

    def test_prepare_binds_ci_environment_request_config_and_public_revision_header(self):
        manifest = self._prepare()["manifest"]
        run_dir = self.home / "runs" / manifest["run_id"]
        revision = manifest["proxy_revisions"][0]
        config = json.loads((run_dir / revision["config_path"]).read_text())
        self.assertEqual(revision["proxy_revision_id"], config["headers"][0]["headers"][0]["value"])
        self.assertEqual(ROUTE_MARKER_HEADER, config["headers"][0]["headers"][0]["key"])
        request = json.loads((run_dir / revision["request_path"]).read_text())
        self.assertEqual("production", request["target"])
        self.assertEqual(manifest["github_ci_evidence_sha256"], request["github_ci_evidence_sha256"])
        self.assertEqual(manifest["expected_deployment_identity_sha256"], request["expected_deployment_identity_sha256"])
        self.assertEqual(revision["request_sha256"], manifest["deploy_request_sha256"])
        self.assertEqual(revision["config_sha256"], manifest["vercel_config_sha256"])

    def test_proxy_attempt_ids_never_repeat_when_upstream_returns_to_old_value(self):
        first = self._prepare()["manifest"]
        run_id = first["run_id"]
        second = repair_proxy(home_path=self.home, run_id=run_id, proxy_upstream="https://tunnel-two.example")["manifest"]
        third = repair_proxy(home_path=self.home, run_id=run_id, proxy_upstream="https://tunnel-one.example")["manifest"]
        ids = [item["proxy_revision_id"] for item in third["proxy_revisions"]]
        self.assertEqual(3, len(set(ids)))
        self.assertEqual([1, 2, 3], [item["attempt_number"] for item in third["proxy_revisions"]])
        self.assertNotEqual(ids[0], ids[2])
        self.assertEqual(second["current_proxy_revision_id"], ids[1])

    def test_request_or_config_tampering_blocks_record_and_deploy(self):
        manifest = self._prepare()["manifest"]
        run_id = manifest["run_id"]
        run_dir = self.home / "runs" / run_id
        request = run_dir / manifest["proxy_revisions"][0]["request_path"]
        request.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(DeployError, "request or Vercel config"):
            self._record_builder(run_id)
        self.home = self.external / "second-home"
        manifest = self._prepare()["manifest"]; run_id = manifest["run_id"]; run_dir = self.home / "runs" / run_id
        config = run_dir / manifest["proxy_revisions"][0]["config_path"]
        config.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(DeployError, "request or Vercel config"):
            self._deploy(run_id)

    def test_deployment_receipt_binds_vercel_production_ids_urls_and_hashes(self):
        run_id = self._prepare()["manifest"]["run_id"]
        for mutation in ({"provider": "other"}, {"target": "preview"}, {"request_sha256": "0" * 64}, {"config_sha256": "0" * 64}, {"deployment_id": ""}, {"url": "http://not-secure"}):
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(DeployError, "Vercel production"):
                    self._deploy(run_id, mutate=mutation)
        self._deploy(run_id)
        receipt_path = self.home / "runs" / run_id / status(home_path=self.home, run_id=run_id)["run"]["deployment"]["receipt"]
        receipt = json.loads(receipt_path.read_text()); receipt["deployment_id"] = "tampered"; receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(DeployError, "does not certify|does not match"):
            status(home_path=self.home, run_id=run_id)

    def test_verify_requires_exact_public_route_marker_and_environment(self):
        run_id = self._prepare()["manifest"]["run_id"]; self._record_builder(run_id); self._deploy(run_id)
        smoke = self._smoke(run_id)
        verifier = lambda **_: {}
        url = "https://gateway.example/healthz"
        with self.assertRaisesRegex(DeployError, "stable /healthz"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=verifier,
                   route_checker=lambda _: {"status": 200, "url": url, "headers": {ROUTE_MARKER_HEADER: "old"}, "body": {"deployment_identity": self.identity}})
        with self.assertRaisesRegex(DeployError, "stable /healthz"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=verifier,
                   route_checker=lambda _: {"status": 200, "url": url, "headers": {ROUTE_MARKER_HEADER: status(home_path=self.home, run_id=run_id)["run"]["current_proxy_revision_id"]}, "body": {"deployment_identity": {**self.identity, "instance_id": "other"}}})

    def test_verify_passes_bound_expected_identity_artifact_to_real_verifier_seam(self):
        run_id = self._prepare()["manifest"]["run_id"]; self._record_builder(run_id); self._deploy(run_id)
        seen = {}

        def verifier(**kwargs):
            seen.update(kwargs)
            manifest = status(home_path=self.home, run_id=run_id)["run"]
            return {"schema_version": "2", "release_identity": manifest["release_identity"], "deployment_identity": self.identity, "certifies": "test parity"}

        smoke = self._smoke(run_id)
        verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=verifier,
               route_checker=self._route(run_id))
        identity_path = seen["expected_deployment_identity_path"]
        self.assertEqual(self.identity, json.loads(identity_path.read_text()))
        self.assertEqual((self.home / "runs" / run_id / "evidence" / "expected-deployment-identity.json").resolve(), identity_path.resolve())

    def test_builder_and_browser_evidence_artifacts_are_hash_bound(self):
        run_id = self._prepare()["manifest"]["run_id"]; self._record_builder(run_id); self._deploy(run_id)
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        builder_evidence = self.home / "runs" / run_id / "builder" / manifest["builder"]["attestation"]
        builder_evidence.write_text("{}", encoding="utf-8")
        smoke = self._smoke(run_id)
        with self.assertRaisesRegex(DeployError, "Builder evidence attestation"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        self.home = self.external / "browser-home"
        run_id = self._prepare()["manifest"]["run_id"]; self._record_builder(run_id); self._deploy(run_id)
        smoke = self._smoke(run_id); browser = self._browser_for_smoke(smoke)
        browser.write_text(json.dumps({"schema_version": "1", "producer": "tampered"}), encoding="utf-8")
        with self.assertRaisesRegex(DeployError, "browser evidence artifact"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=browser, verifier=lambda **_: {}, route_checker=self._route(run_id))

    def test_smoke_rejects_old_revision_wrong_request_or_missing_browser_provenance(self):
        run_id = self._prepare()["manifest"]["run_id"]; old = status(home_path=self.home, run_id=run_id)["run"]["current_proxy_revision_id"]
        repair_proxy(home_path=self.home, run_id=run_id, proxy_upstream="https://tunnel-two.example")
        self._record_builder(run_id); self._deploy(run_id)
        with self.assertRaisesRegex(DeployError, "exact release"):
            smoke = self._smoke(run_id, revision_id=old)
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        with self.assertRaisesRegex(DeployError, "exact release"):
            smoke = self._smoke(run_id, mutate={"request_sha256": "0" * 64})
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        with self.assertRaisesRegex(DeployError, "exact release"):
            smoke = self._smoke(run_id, mutate={"browser_evidence_ref": ""})
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))

    def test_verification_is_unique_bound_and_single_use(self):
        self._adopt(); run_id = self._ready()
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        verification_id = manifest["verification"]["verification_id"]
        activated = activate(home_path=self.home, run_id=run_id)
        self.assertEqual(verification_id, activated["active"]["verification_id"])
        self.assertEqual(activated["active"]["activation_id"], status(home_path=self.home, run_id=run_id)["run"]["verification"]["consumed_by_activation_id"])
        self.assertFalse(activate(home_path=self.home, run_id=run_id)["changed"])
        repair_proxy(home_path=self.home, run_id=run_id, proxy_upstream="https://tunnel-one.example")
        with self.assertRaisesRegex(DeployError, "current proxy revision deployment"):
            smoke = self._smoke(run_id)
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        with self.assertRaisesRegex(DeployError, "verification is malformed|current, passing, unconsumed"):
            activate(home_path=self.home, run_id=run_id)

    def test_rollback_creates_fresh_restore_and_cannot_activate_old_evidence(self):
        self._adopt(); first = self._ready(); activate(home_path=self.home, run_id=first)
        second = self._ready(upstream="https://tunnel-two.example", gateway="https://gateway-two.example")
        activate(home_path=self.home, run_id=second)
        before = status(home_path=self.home, run_id=first)["run"]["current_proxy_revision_id"]
        result = rollback(home_path=self.home, source_run_id=second, record=True)
        restore = result["restore_manifest"]
        self.assertEqual("restore", restore["proxy_revisions"][-1]["kind"])
        self.assertNotEqual(before, restore["current_proxy_revision_id"])
        self.assertEqual(second, result["active"]["run_id"])
        with self.assertRaisesRegex(DeployError, "verification is malformed|current, passing, unconsumed"):
            activate(home_path=self.home, run_id=first)
        self._deploy(first)
        smoke = self._smoke(first)
        with self.assertRaisesRegex(DeployError, "Builder exports"):
            verify(home_path=self.home, run_id=first, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(first))
        self._record_builder(first); self._verify(first)
        self.assertTrue(activate(home_path=self.home, run_id=first)["changed"])

    def test_legacy_adoption_requires_upstream_and_creates_recoverable_consumed_revision(self):
        run_id = self._adopt()
        result = status(home_path=self.home, run_id=run_id)
        revision = result["run"]["proxy_revisions"][0]
        self.assertEqual("https://legacy-tunnel.example", revision["upstream"])
        self.assertTrue((self.home / "runs" / run_id / revision["request_path"]).is_file())
        self.assertEqual(result["active"]["activation_id"], result["run"]["verification"]["consumed_by_activation_id"])

    def test_prepare_rejects_bad_ci_and_cli_does_not_offer_main_ref(self):
        bad = json.loads(self.ci.read_text()); bad["head_sha"] = "0" * 40; self.ci.write_text(json.dumps(bad))
        with self.assertRaisesRegex(DeployError, "GitHub CI evidence"):
            self._prepare()
        help_result = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "prepare", "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, help_result.returncode)
        self.assertNotIn("--main-ref", help_result.stdout)

    def test_active_pointer_cross_reference_tampering_fails_closed(self):
        self._adopt(); run_id = self._ready(); activate(home_path=self.home, run_id=run_id)
        active_path = self.home / "active.json"; active = json.loads(active_path.read_text()); active["verification_id"] = "gclv-" + "0" * 64; active_path.write_text(json.dumps(active))
        with self.assertRaisesRegex(DeployError, "cross-reference"):
            status(home_path=self.home)

    def test_changes_compare_active_revision_not_pending_top_level(self):
        self._adopt(); first = self._ready(); activate(home_path=self.home, run_id=first)
        repair_proxy(home_path=self.home, run_id=first, proxy_upstream="https://pending-tunnel.example")
        second = self._prepare(upstream="https://tunnel-one.example", gateway="https://gateway-new.example")["manifest"]
        self.assertFalse(second["changes_from_active"]["proxy_upstream"])

    def test_cli_live_commands_default_to_plans(self):
        run_id = self._prepare()["manifest"]["run_id"]
        result = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "run-deployment-adapter", "--run-id", run_id, "--secret-env-file", str(self.external / "absent.env"), "--runner", "/bin/false"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no command executed", result.stdout)
        verify_plan = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "verify", "--run-id", run_id, "--smoke-evidence", str(self.external / "absent.json"), "--browser-evidence", str(self.external / "absent-browser.json")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, verify_plan.returncode, verify_plan.stderr)
        self.assertIn("no network request made", verify_plan.stdout)


if __name__ == "__main__":
    unittest.main()
