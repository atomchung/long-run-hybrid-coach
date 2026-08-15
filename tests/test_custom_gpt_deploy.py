from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.custom_gpt_deploy import (
    ROUTE_MARKER_HEADER,
    SMOKE_CERTIFIES,
    DeployError,
    activate,
    adopt_active,
    deploy_proxy,
    init_production_target,
    prepare,
    record_deployment,
    record_builder,
    repair_proxy,
    rollback,
    status,
    verify,
)
from scripts.custom_gpt_deploy_providers import (
    GitHubProviderReader,
    ProviderReadbackError,
    VercelProviderReader,
    production_target_binding,
)
from scripts.custom_gpt_release import bundle
from scripts.custom_gpt_vercel_create import _request_material

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "custom_gpt_deploy.py"


class CustomGptDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.external = Path(self.temporary.name)
        self.home = self.external / "release-home"
        self.commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        self.previous_commit = subprocess.run(["git", "rev-parse", "HEAD^"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        self.second_previous_commit = subprocess.run(["git", "rev-parse", "HEAD~2"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        self.identity = {"environment": "production", "instance_id": "gateway-prod-1", "configuration_binding": "a" * 64}
        self.target_path = self.external / "production-target.json"
        self.target = {}

    def tearDown(self):
        self.temporary.cleanup()

    def _prepare(
        self, upstream: str = "https://tunnel-one.example",
        gateway: str = "https://gateway.example", *,
        git_commit: str | None = None, main_ref: str = "HEAD",
    ) -> dict:
        candidate = git_commit or self.commit
        self.target = {
            "schema_version": "1",
            "environment": "production",
            "github": {
                "repository": "example-org/garmin-coach-loop",
                "branch": "main",
                "workflow_path": ".github/workflows/ci.yml",
            },
            "vercel": {
                "team_id": "team_example",
                "project_id": "prj_example",
                "project_name": "long-run-hybrid-coach-gateway",
                "stable_domain": gateway.removeprefix("https://"),
            },
            "custom_gpt": {"gpt_id": "gpt-production-123"},
        }
        self.target["binding_sha256"] = production_target_binding(self.target)
        self.target_path.write_text(json.dumps(self.target), encoding="utf-8")

        def github_runner(argv):
            if "/git/ref/" in argv[-1]:
                return {"object": {"sha": candidate}}
            return {"workflow_runs": [{
                "id": 12345,
                "html_url": "https://github.com/example-org/garmin-coach-loop/actions/runs/12345",
                "path": ".github/workflows/ci.yml",
                "head_sha": candidate,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": "example-org/garmin-coach-loop"},
            }]}

        github_reader = GitHubProviderReader(
            self.target, runner=github_runner,
            clock=lambda: "2026-08-15T00:59:00Z",
        ).read
        return prepare(home_path=self.home, git_commit=candidate, main_ref=main_ref, gateway_domain=gateway,
                       proxy_upstream=upstream, production_target=self.target_path,
                       github_reader=github_reader, expected_deployment_identity=self.identity,
                       expected_repository="example-org/garmin-coach-loop",
                       clock=lambda: "2026-08-15T01:00:00Z")

    def test_builtin_create_adapter_reads_real_orchestrator_request_layout(self):
        prepared = self._prepare()
        run_id = prepared["manifest"]["run_id"]
        run_dir = self.home / "runs" / run_id
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        revision = next(
            item for item in manifest["proxy_revisions"]
            if item["proxy_revision_id"] == manifest["current_proxy_revision_id"]
        )
        request_path = run_dir / revision["request_path"]
        request, config = _request_material(request_path)
        self.assertEqual(revision["proxy_revision_id"], request["proxy_revision_id"])
        self.assertEqual(
            revision["config_sha256"], hashlib.sha256(config).hexdigest(),
        )

    def _record_builder(
        self, run_id: str, *, producer: str = "custom-gpt-builder-export",
        gpt_id: str = "gpt-production-123",
    ) -> None:
        run_dir = self.home / "runs" / run_id
        instructions = (run_dir / "builder" / "expected-instructions.md").read_text()
        openapi = (run_dir / "builder" / "expected-openapi.yaml").read_text()
        revision_id = status(home_path=self.home, run_id=run_id)["run"]["current_proxy_revision_id"]
        evidence = self.external / f"builder-evidence-{run_id}.json"
        evidence.write_text(json.dumps({"schema_version": "1", "producer": producer, "gpt_id": gpt_id, "exported_at": "2026-08-15T01:00:30Z", "run_id": run_id, "proxy_revision_id": revision_id, "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(), "openapi_sha256": hashlib.sha256(openapi.encode()).hexdigest()}), encoding="utf-8")
        record_builder(home_path=self.home, run_id=run_id,
                       instructions_path=run_dir / "builder" / "expected-instructions.md",
                       openapi_path=run_dir / "builder" / "expected-openapi.yaml",
                       builder_evidence=evidence,
                       clock=lambda: "2026-08-15T01:01:00Z")

    def _provider_reader(self, run_id: str, *, checked_at: str):
        def provider_reader(create):
            deploy_manifest = status(home_path=self.home, run_id=run_id)["run"]
            deploy_revision = next(
                item for item in deploy_manifest["proxy_revisions"]
                if item["proxy_revision_id"] == deploy_manifest["current_proxy_revision_id"]
            )
            deployment_target = deploy_revision.get("production_target") or deploy_manifest.get("production_target")
            vercel_target = deployment_target["vercel"]
            deployment_id = create["deployment_id"]
            deployment_url = create["deployment_url"]
            deployment = {
                "id": deployment_id,
                "projectId": vercel_target["project_id"],
                "name": vercel_target["project_name"],
                "teamId": vercel_target["team_id"],
                "target": "production", "readyState": "READY",
                "url": deployment_url,
                "alias": [vercel_target["stable_domain"]],
                "meta": create["metadata"],
            }
            project = {
                "id": vercel_target["project_id"],
                "name": vercel_target["project_name"],
                "teamId": vercel_target["team_id"],
                "targets": {"production": {"id": deployment_id}},
                "alias": [{"domain": vercel_target["stable_domain"]}],
            }
            return VercelProviderReader(
                deployment_target,
                get_deployment=lambda _deployment_id, _team_id: deployment,
                get_project=lambda _project_id, _team_id: project,
                clock=lambda: checked_at,
            ).read(create)

        return provider_reader

    def _deploy(
        self, run_id: str, calls: list | None = None,
        mutate: dict | None = None, provider_reader=None,
    ) -> None:
        secret = self.external / "gateway.env"
        if not secret.exists():
            secret.write_text("SECRET_MUST_NOT_BE_READ=sentinel\n", encoding="utf-8")
            secret.chmod(0o600)

        def runner(
            request: Path, secret_file: Path, receipt: Path,
            attempt_path: Path,
        ) -> None:
            request_value = json.loads(request.read_text(encoding="utf-8"))
            deploy_manifest = status(home_path=self.home, run_id=run_id)["run"]
            deploy_revision = next(
                item for item in deploy_manifest["proxy_revisions"]
                if item["proxy_revision_id"] == deploy_manifest["current_proxy_revision_id"]
            )
            deployment_target = deploy_revision.get("production_target") or deploy_manifest.get("production_target")
            vercel_target = deployment_target["vercel"]
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            if calls is not None:
                calls.append((request_value, secret_file, receipt, receipt.stat().st_mode & 0o777))
            deployment_id = "dpl_" + request_value["proxy_revision_id"][-12:]
            deployment_url = "https://gcl-production.vercel.app"
            value = {
                "schema_version": "1",
                "producer": "vercel-create-attestation-v1",
                "create_response": {
                    "provider": "vercel", "target": "production",
                    "teamId": vercel_target["team_id"],
                    "projectId": vercel_target["project_id"],
                    "projectName": vercel_target["project_name"],
                    "deploymentId": deployment_id, "url": deployment_url,
                    "metadata": attempt["metadata"],
                },
            }
            value.update(mutate or {})
            attempt["state"] = "submission_started"
            attempt["submission_started_at"] = "2026-08-15T01:01:30Z"
            attempt_path.write_text(
                json.dumps(attempt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            attestation_path = (
                attempt_path.parent.parent / "create-attestations"
                / f"{request_value['proxy_revision_id']}.json"
            )
            attestation_path.parent.mkdir(exist_ok=True)
            attestation_body = (
                json.dumps(value, indent=2, sort_keys=True) + "\n"
            ).encode()
            attestation_path.write_bytes(attestation_body)
            attestation_path.chmod(0o600)
            attempt["state"] = "attested"
            attempt["attestation_path"] = str(
                attestation_path.relative_to(attempt_path.parent.parent)
            )
            attempt["attestation_sha256"] = hashlib.sha256(
                attestation_body
            ).hexdigest()
            attempt_path.write_text(
                json.dumps(attempt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            receipt.write_bytes(attestation_body)

        deploy_proxy(
            home_path=self.home, run_id=run_id, secret_env_file=secret,
            runner=runner,
            provider_reader=provider_reader or self._provider_reader(
                run_id, checked_at="2026-08-15T01:02:00Z",
            ),
            clock=lambda: "2026-08-15T01:01:00Z",
        )
        self.assertEqual("SECRET_MUST_NOT_BE_READ=sentinel\n", secret.read_text(encoding="utf-8"))

    def _smoke(self, run_id: str, *, revision_id: str | None = None, mutate: dict | None = None,
               browser_mutate: dict | None = None) -> Path:
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        revision = next(item for item in manifest["proxy_revisions"] if item["proxy_revision_id"] == (revision_id or manifest["current_proxy_revision_id"]))
        browser = self.external / f"browser-{revision['proxy_revision_id']}.json"
        browser_value = {"schema_version": "1", "producer": "codex-browser-smoke-v1", "observed_at": "2026-08-15T01:03:00Z", "gpt_id": "gpt-production-123", "conversation_ref": "browser://custom-gpt/preview/receipt-123", "artifact_kind": "browser-receipt", "status": "passed"}
        browser_value.update(browser_mutate or {})
        browser.write_text(json.dumps(browser_value), encoding="utf-8")
        value = {
            "schema_version": "2", "run_id": run_id, "release_id": manifest["release_identity"]["release_id"],
            "proxy_revision_id": revision["proxy_revision_id"], "request_sha256": revision["request_sha256"],
            "deployment_id": (revision.get("deployment") or {}).get("deployment_id"),
            "expected_deployment_identity": self.identity, "producer": "codex-browser-smoke-v1",
            "gpt_id": "gpt-production-123",
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

    def _activate(self, run_id: str, *, route_checker=None, checked_at: str = "2026-08-15T01:05:00Z") -> dict:
        return activate(
            home_path=self.home,
            run_id=run_id,
            provider_reader=self._provider_reader(run_id, checked_at=checked_at),
            route_checker=route_checker or self._route(run_id),
            clock=lambda: "2026-08-15T01:05:30Z",
        )

    def _ready(
        self, upstream: str = "https://tunnel-one.example",
        gateway: str = "https://gateway.example", *,
        git_commit: str | None = None, main_ref: str = "HEAD",
    ) -> str:
        run_id = self._prepare(
            upstream, gateway, git_commit=git_commit, main_ref=main_ref,
        )["manifest"]["run_id"]
        self._record_builder(run_id)
        self._deploy(run_id)
        self._verify(run_id)
        return run_id

    def _adopt(self) -> str:
        legacy = self.external / "legacy"
        legacy.mkdir(exist_ok=True)
        bundled = bundle(self.previous_commit, "https://gateway.example")
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
                            production_gpt_id="gpt-production-123",
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
        self.assertEqual(manifest["production_target_binding_sha256"], request["production_target_binding_sha256"])
        self.assertEqual(manifest["github_provider_receipt_sha256"], request["github_provider_receipt_sha256"])
        self.assertEqual(manifest["expected_deployment_identity_sha256"], request["expected_deployment_identity_sha256"])
        self.assertEqual(revision["request_sha256"], manifest["deploy_request_sha256"])
        self.assertEqual(revision["config_sha256"], manifest["vercel_config_sha256"])

    def test_init_production_target_computes_binding_and_refuses_silent_migration(self):
        output = self.external / "generated-production-target.json"
        arguments = {
            "output": output,
            "repository": "example-org/garmin-coach-loop",
            "team_id": "team_example",
            "project_id": "prj_example",
            "project_name": "long-run-hybrid-coach-gateway",
            "stable_domain": "gateway.example",
            "production_gpt_id": "gpt-production-123",
        }
        created = init_production_target(**arguments)
        self.assertTrue(created["changed"])
        self.assertEqual(
            production_target_binding(created["target"]),
            created["target"]["binding_sha256"],
        )
        self.assertFalse(init_production_target(**arguments)["changed"])
        with self.assertRaisesRegex(DeployError, "migration"):
            init_production_target(**{**arguments, "project_id": "prj_other"})

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
        mutations = ({"provider": "other"}, {"target": "preview"}, {"request_sha256": "0" * 64}, {"config_sha256": "0" * 64}, {"deployment_id": ""}, {"url": "http://not-secure"})
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                run_id = self._prepare(
                    gateway=f"https://gateway-{index}.example",
                )["manifest"]["run_id"]
                with self.assertRaisesRegex(
                    DeployError, "Vercel adapter|Vercel production|Vercel create|vercel",
                ):
                    self._deploy(run_id, mutate=mutation)
        run_id = self._prepare(
            gateway="https://gateway-valid.example",
        )["manifest"]["run_id"]
        self._deploy(run_id)
        receipt_path = self.home / "runs" / run_id / status(home_path=self.home, run_id=run_id)["run"]["deployment"]["receipt"]
        receipt = json.loads(receipt_path.read_text()); receipt["deployment_id"] = "tampered"; receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(DeployError, "does not certify|does not match"):
            status(home_path=self.home, run_id=run_id)

    def test_deployment_adapter_uses_private_unique_ephemeral_create_evidence(self):
        run_id = self._prepare()["manifest"]["run_id"]
        calls = []
        self._deploy(run_id, calls=calls)
        self.assertEqual(1, len(calls))
        _, _, evidence_path, mode = calls[0]
        self.assertEqual(0o600, mode)
        self.assertFalse(evidence_path.exists())
        self.assertEqual([], list((self.home / "runs" / run_id).glob(".runner-provider-evidence-*.json")))

    def test_deploy_proxy_rejects_final_secret_symlink_before_runner(self):
        run_id = self._prepare(
            gateway="https://gateway-secret-link.example",
        )["manifest"]["run_id"]
        secret = self.external / "real-vercel.env"
        secret.write_text(
            "VERCEL_TOKEN=vercel-token-1234567890\n", encoding="utf-8",
        )
        secret.chmod(0o600)
        link = self.external / "linked-vercel.env"
        link.symlink_to(secret)
        called = False

        def runner(*_args):
            nonlocal called
            called = True

        with self.assertRaisesRegex(DeployError, "non-symlink"):
            deploy_proxy(
                home_path=self.home, run_id=run_id,
                secret_env_file=link, runner=runner,
            )
        self.assertFalse(called)

    def test_provider_readback_retry_reuses_durable_create_attestation(self):
        run_id = self._prepare()["manifest"]["run_id"]
        calls = []

        def unavailable(_create):
            raise ProviderReadbackError(
                "provider_error", "vercel", "read-back",
                "temporarily unavailable",
            )

        with self.assertRaisesRegex(DeployError, "temporarily unavailable"):
            self._deploy(run_id, calls=calls, provider_reader=unavailable)
        failed = status(home_path=self.home, run_id=run_id)["run"]
        revision = next(
            item for item in failed["proxy_revisions"]
            if item["proxy_revision_id"] == failed["current_proxy_revision_id"]
        )
        attempt_path = (
            self.home / "runs" / run_id / revision["create_attempt"]["path"]
        )
        self.assertEqual(
            "attested", json.loads(attempt_path.read_text())["state"],
        )
        self.assertIsNone(revision["deployment"])
        self.assertEqual(1, len(calls))

        self._deploy(run_id, calls=calls)
        recovered = status(home_path=self.home, run_id=run_id)["run"]
        recovered_revision = next(
            item for item in recovered["proxy_revisions"]
            if item["proxy_revision_id"] == recovered["current_proxy_revision_id"]
        )
        self.assertEqual(
            "provider-verified", recovered_revision["deployment"]["status"],
        )
        self.assertEqual(
            "provider_verified", json.loads(attempt_path.read_text())["state"],
        )
        self.assertEqual(1, len(calls))

    def test_external_create_attestation_metadata_must_bind_exact_revision(self):
        run_id = self._prepare(
            gateway="https://gateway-record.example",
        )["manifest"]["run_id"]
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        revision = next(
            item for item in manifest["proxy_revisions"]
            if item["proxy_revision_id"] == manifest["current_proxy_revision_id"]
        )
        target = manifest["production_target"]["vercel"]
        evidence = self.external / "wrong-metadata-create.json"
        evidence.write_text(json.dumps({
            "schema_version": "1",
            "producer": "vercel-create-attestation-v1",
            "create_response": {
                "provider": "vercel", "target": "production",
                "teamId": target["team_id"],
                "projectId": target["project_id"],
                "projectName": target["project_name"],
                "deploymentId": "dpl_wrong_metadata",
                "url": "https://wrong-metadata.vercel.app",
                "metadata": {
                    "gclProxyRevision": revision["proxy_revision_id"],
                    "gclRequestSha256": "9" * 64,
                    "gclConfigSha256": revision["config_sha256"],
                },
            },
        }), encoding="utf-8")
        called = False

        def provider_reader(_create):
            nonlocal called
            called = True
            raise AssertionError("mismatched metadata must block before read-back")

        with self.assertRaisesRegex(DeployError, "exact release request"):
            record_deployment(
                home_path=self.home, run_id=run_id, evidence_path=evidence,
                provider_reader=provider_reader,
            )
        self.assertFalse(called)

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
        builder_evidence = self.home / "runs" / run_id / manifest["builder"]["attestation"]
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
        activated = self._activate(run_id)
        self.assertEqual(verification_id, activated["active"]["verification_id"])
        self.assertEqual(activated["active"]["activation_id"], status(home_path=self.home, run_id=run_id)["run"]["verification"]["consumed_by_activation_id"])
        self.assertFalse(self._activate(run_id)["changed"])
        repair_proxy(home_path=self.home, run_id=run_id, proxy_upstream="https://tunnel-one.example")
        with self.assertRaisesRegex(DeployError, "current proxy revision provider-verified deployment"):
            smoke = self._smoke(run_id)
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        with self.assertRaisesRegex(DeployError, "verification is malformed|current, passing, unconsumed"):
            self._activate(run_id)

    def test_rollback_creates_fresh_restore_and_cannot_activate_old_evidence(self):
        self._adopt(); first = self._ready(); self._activate(first)
        second = self._ready(
            upstream="https://tunnel-two.example",
            git_commit=self.second_previous_commit, main_ref="HEAD~2",
        )
        self._activate(second)
        before = status(home_path=self.home, run_id=first)["run"]["current_proxy_revision_id"]
        result = rollback(home_path=self.home, source_run_id=second, record=True)
        restore = result["restore_manifest"]
        self.assertEqual("restore", restore["proxy_revisions"][-1]["kind"])
        self.assertNotEqual(before, restore["current_proxy_revision_id"])
        self.assertEqual(second, result["active"]["run_id"])
        with self.assertRaisesRegex(DeployError, "verification is malformed|current, passing, unconsumed"):
            self._activate(first)
        self._deploy(first)
        smoke = self._smoke(first)
        with self.assertRaisesRegex(DeployError, "Builder exports"):
            verify(home_path=self.home, run_id=first, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(first))
        self._record_builder(first); self._verify(first)
        self.assertTrue(self._activate(first)["changed"])

    def test_first_modern_release_can_restore_the_legacy_target_only_with_fresh_provider_evidence(self):
        legacy = self._adopt()
        modern = self._ready()
        self._activate(modern)
        with self.assertRaisesRegex(DeployError, "production-target"):
            rollback(home_path=self.home, source_run_id=modern, record=True)
        restored = rollback(
            home_path=self.home, source_run_id=modern, record=True,
            production_target=self.target_path,
            expected_repository="example-org/garmin-coach-loop",
        )["restore_manifest"]
        revision = restored["proxy_revisions"][-1]
        self.assertEqual("restore", revision["kind"])
        self.assertEqual(self.target["binding_sha256"], revision["production_target_binding_sha256"])
        self.assertEqual(
            restored["activations"][0]["activation_id"],
            revision["activation_authority"]["activation_id"],
        )
        with self.assertRaisesRegex(DeployError, "provider-verified deployment"):
            self._verify(legacy)
        self._deploy(legacy)
        self._record_builder(legacy)
        self._verify(legacy)
        self.assertTrue(self._activate(legacy)["changed"])

    def test_rollback_cannot_use_a_valid_alternate_production_target(self):
        self._adopt()
        modern = self._ready()
        self._activate(modern)
        alternate = json.loads(json.dumps(self.target))
        alternate["vercel"]["project_id"] = "prj_alternate"
        alternate["binding_sha256"] = production_target_binding(alternate)
        alternate_path = self.external / "alternate-production-target.json"
        alternate_path.write_text(json.dumps(alternate), encoding="utf-8")
        with self.assertRaisesRegex(DeployError, "rollback cannot migrate"):
            rollback(
                home_path=self.home, source_run_id=modern, record=True,
                production_target=alternate_path,
                expected_repository="example-org/garmin-coach-loop",
            )

    def test_provider_target_and_nested_readback_tampering_fail_closed_after_activation(self):
        for case in ("target", "github", "create", "provider"):
            with self.subTest(case=case):
                self.home = self.external / f"provider-durability-{case}"
                self._adopt()
                run_id = self._ready()
                self._activate(run_id)
                manifest_path = self.home / "runs" / run_id / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                revision = next(
                    item for item in manifest["proxy_revisions"]
                    if item["proxy_revision_id"] == manifest["current_proxy_revision_id"]
                )
                if case == "target":
                    target_path = manifest_path.parent / "evidence" / "production-target.json"
                    target = json.loads(target_path.read_text())
                    target["vercel"]["project_id"] = "prj_changed"
                    target_path.write_text(json.dumps(target))
                elif case == "github":
                    github_path = manifest_path.parent / "evidence" / "github-provider-readback.json"
                    github = json.loads(github_path.read_text())
                    github["commit_sha"] = "0" * 40
                    github_path.write_text(json.dumps(github))
                else:
                    receipt_path = manifest_path.parent / revision["deployment"]["receipt"]
                    receipt = json.loads(receipt_path.read_text())
                    key = "create_attestation" if case == "create" else "provider_receipt"
                    receipt[key]["deployment_id"] = "dpl_tampered"
                    receipt_path.write_text(json.dumps(receipt))
                with self.assertRaises(DeployError):
                    status(home_path=self.home)

    def test_legacy_adoption_requires_upstream_and_creates_recoverable_consumed_revision(self):
        run_id = self._adopt()
        result = status(home_path=self.home, run_id=run_id)
        revision = result["run"]["proxy_revisions"][0]
        self.assertEqual("https://legacy-tunnel.example", revision["upstream"])
        self.assertTrue((self.home / "runs" / run_id / revision["request_path"]).is_file())
        self.assertEqual(result["active"]["activation_id"], result["run"]["verification"]["consumed_by_activation_id"])

    def test_prepare_rejects_bad_ci_and_cli_does_not_offer_main_ref(self):
        self._prepare()
        self.home = self.external / "bad-ci-home"
        with self.assertRaisesRegex(DeployError, "github|GitHub|receipt"):
            prepare(
                home_path=self.home, git_commit=self.commit, main_ref="HEAD",
                gateway_domain="https://gateway.example",
                proxy_upstream="https://tunnel-one.example",
                production_target=self.target_path,
                github_reader=lambda _sha: {},
                expected_repository="example-org/garmin-coach-loop",
                expected_deployment_identity=self.identity,
            )
        help_result = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "prepare", "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, help_result.returncode)
        self.assertNotIn("--main-ref", help_result.stdout)

    def test_active_pointer_cross_reference_tampering_fails_closed(self):
        self._adopt(); run_id = self._ready(); self._activate(run_id)
        active_path = self.home / "active.json"; active = json.loads(active_path.read_text()); active["verification_id"] = "gclv-" + "0" * 64; active_path.write_text(json.dumps(active))
        with self.assertRaisesRegex(DeployError, "cross-reference"):
            status(home_path=self.home)

    def test_verify_archives_exact_external_smoke_and_browser_bytes(self):
        self._adopt(); run_id = self._ready(); self._activate(run_id)
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        revision = next(item for item in manifest["proxy_revisions"] if item["proxy_revision_id"] == manifest["current_proxy_revision_id"])
        verification = manifest["verification"]
        for key in ("smoke_evidence", "browser_evidence", "parity_receipt"):
            self.assertIn(revision["proxy_revision_id"], verification[key])
            self.assertTrue((self.home / "runs" / run_id / verification[key]).is_file())
        self._browser(run_id).unlink()
        (self.external / f"smoke-{revision['proxy_revision_id']}.json").unlink()
        self.assertEqual(run_id, status(home_path=self.home)["active"]["run_id"])

    def test_active_status_revalidates_every_durable_evidence_artifact_and_verification_binding(self):
        cases = ("parity", "builder_attestation", "builder_export", "browser", "smoke", "deployment", "verification")
        for case in cases:
            with self.subTest(case=case):
                self.home = self.external / f"durability-{case}"
                self._adopt(); run_id = self._ready(); self._activate(run_id)
                manifest_path = self.home / "runs" / run_id / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                revision = next(item for item in manifest["proxy_revisions"] if item["proxy_revision_id"] == manifest["current_proxy_revision_id"])
                verification = next(item for item in revision["verifications"] if item["verification_id"] == manifest["verification"]["verification_id"])
                if case == "parity":
                    (manifest_path.parent / verification["parity_receipt"]).unlink()
                elif case == "builder_attestation":
                    (manifest_path.parent / verification["builder_attestation"]).write_text("{}", encoding="utf-8")
                elif case == "builder_export":
                    (manifest_path.parent / verification["builder_instructions"]).write_text("tampered", encoding="utf-8")
                elif case == "browser":
                    (manifest_path.parent / verification["browser_evidence"]).write_text("{}", encoding="utf-8")
                elif case == "smoke":
                    (manifest_path.parent / verification["smoke_evidence"]).write_text("{}", encoding="utf-8")
                elif case == "deployment":
                    (manifest_path.parent / verification["deployment_receipt"]).unlink()
                else:
                    verification["smoke_observed_at"] = "2099-01-01T00:00:00Z"
                    manifest["verification"]["smoke_observed_at"] = verification["smoke_observed_at"]
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(DeployError):
                    status(home_path=self.home)

    def test_builder_browser_and_smoke_require_allowlisted_producers_and_one_gpt(self):
        run_id = self._prepare()["manifest"]["run_id"]
        with self.assertRaisesRegex(DeployError, "Builder evidence"):
            self._record_builder(run_id, producer="arbitrary-script")
        with self.assertRaisesRegex(DeployError, "Builder evidence"):
            self._record_builder(run_id, gpt_id="gpt-consistent-but-not-production")
        self._record_builder(run_id); self._deploy(run_id)
        smoke = self._smoke(run_id, browser_mutate={"producer": "arbitrary-script"}, mutate={"producer": "arbitrary-script"})
        with self.assertRaisesRegex(DeployError, "browser evidence artifact"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        smoke = self._smoke(run_id, browser_mutate={"gpt_id": "gpt-other"}, mutate={"gpt_id": "gpt-other"})
        with self.assertRaisesRegex(DeployError, "production GPT"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))
        smoke = self._smoke(run_id, mutate={"gpt_id": "gpt-other"})
        with self.assertRaisesRegex(DeployError, "exact release"):
            verify(home_path=self.home, run_id=run_id, smoke_evidence=smoke, browser_evidence=self._browser_for_smoke(smoke), verifier=lambda **_: {}, route_checker=self._route(run_id))

    def test_activate_rechecks_provider_and_public_route_immediately_before_pointer_write(self):
        self._adopt()
        run_id = self._ready()
        manifest = status(home_path=self.home, run_id=run_id)["run"]
        old_marker = "gclp-" + "0" * 64

        def stale_route(url: str) -> dict:
            return {
                "status": 200, "url": url,
                "headers": {ROUTE_MARKER_HEADER: old_marker},
                "body": {
                    "status": "ok",
                    "release_identity": manifest["release_identity"],
                    "deployment_identity": self.identity,
                },
            }

        with self.assertRaisesRegex(DeployError, "stable /healthz"):
            self._activate(run_id, route_checker=stale_route)
        self.assertNotEqual(run_id, status(home_path=self.home)["active"]["run_id"])
        with self.assertRaisesRegex(DeployError, "provider read-back is stale"):
            self._activate(run_id, checked_at="2026-08-15T00:00:00Z")
        self.assertTrue(self._activate(run_id)["changed"])

    def test_active_status_revalidates_final_provider_and_route_receipts(self):
        for artifact in ("provider_receipt", "route_receipt"):
            with self.subTest(artifact=artifact):
                self.home = self.external / f"activation-final-gate-{artifact}"
                self._adopt()
                run_id = self._ready()
                activated = self._activate(run_id)
                manifest = activated["manifest"]
                activation = manifest["activations"][-1]
                path = self.home / "runs" / run_id / activation["final_gate"][artifact]
                path.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(DeployError, "activation final gate"):
                    status(home_path=self.home)

    def test_activate_recovers_after_active_pointer_write_failure(self):
        legacy = self._adopt()
        run_id = self._ready()

        def fail_pointer(_path: Path, _value: dict) -> None:
            raise OSError("injected active pointer write failure")

        with self.assertRaisesRegex(OSError, "injected"):
            activate(
                home_path=self.home, run_id=run_id,
                provider_reader=self._provider_reader(
                    run_id, checked_at="2026-08-15T01:05:00Z",
                ),
                route_checker=self._route(run_id),
                pointer_writer=fail_pointer,
                clock=lambda: "2026-08-15T01:05:30Z",
            )
        self.assertTrue((self.home / "pending-activation.json").is_file())
        self.assertEqual(legacy, status(home_path=self.home)["active"]["run_id"])
        recovered = self._activate(run_id)
        self.assertTrue(recovered["recovered"])
        self.assertFalse((self.home / "pending-activation.json").exists())
        self.assertEqual(run_id, status(home_path=self.home)["active"]["run_id"])
        self.assertIn("pointer_recovery", recovered["manifest"]["activations"][-1])

    def test_strict_deployment_identity_rejects_noncanonical_or_non_sha_values(self):
        for identity in (
            {**self.identity, "environment": "Production"},
            {**self.identity, "instance_id": "-gateway"},
            {**self.identity, "configuration_binding": "A" * 64},
        ):
            with self.subTest(identity=identity):
                self.identity = identity
                with self.assertRaisesRegex(DeployError, "deployment identity"):
                    self._prepare()

    def test_fake_previous_activation_reference_fails_even_when_shape_is_valid(self):
        self._adopt(); first = self._ready(); self._activate(first)
        second = self._ready(
            upstream="https://tunnel-two.example",
            git_commit=self.second_previous_commit, main_ref="HEAD~2",
        )
        self._activate(second)
        active_path = self.home / "active.json"
        active = json.loads(active_path.read_text())
        fake_id = "gcla-" + "f" * 64
        active["previous"]["activation_id"] = fake_id
        active_path.write_text(json.dumps(active), encoding="utf-8")
        manifest_path = self.home / "runs" / second / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        activation = next(item for item in manifest["activations"] if item["activation_id"] == active["activation_id"])
        activation["previous"]["activation_id"] = fake_id
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(DeployError, "recorded activation"):
            status(home_path=self.home)

    def test_legacy_active_status_revalidates_all_copied_bootstrap_artifacts(self):
        cases = ("smoke", "builder_export", "builder_attestation", "parity", "proxy")
        for case in cases:
            with self.subTest(case=case):
                self.home = self.external / f"legacy-durability-{case}"
                run_id = self._adopt()
                manifest = status(home_path=self.home, run_id=run_id)["run"]
                run_dir = self.home / "runs" / run_id
                verification = manifest["verification"]
                if case == "smoke": path = run_dir / verification["smoke_evidence"]
                elif case == "builder_export": path = run_dir / verification["builder_openapi"]
                elif case == "builder_attestation": path = run_dir / verification["builder_attestation"]
                elif case == "parity": path = run_dir / verification["parity_receipt"]
                else: path = run_dir / "evidence" / "legacy-vercel.json"
                path.unlink()
                with self.assertRaises(DeployError):
                    status(home_path=self.home)

    def test_changes_compare_active_revision_not_pending_top_level(self):
        self._adopt(); first = self._ready(); self._activate(first)
        repair_proxy(home_path=self.home, run_id=first, proxy_upstream="https://pending-tunnel.example")
        second = self._prepare(
            upstream="https://tunnel-one.example",
            git_commit=self.second_previous_commit, main_ref="HEAD~2",
        )["manifest"]
        self.assertFalse(second["changes_from_active"]["proxy_upstream"])

    def test_prepare_blocks_implicit_production_target_migration(self):
        self._adopt()
        first = self._ready()
        self._activate(first)
        with self.assertRaisesRegex(DeployError, "production target migration"):
            self._prepare(gateway="https://gateway-new.example")

    def test_cli_live_commands_default_to_plans(self):
        run_id = self._prepare()["manifest"]["run_id"]
        result = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "run-deployment-adapter", "--run-id", run_id, "--secret-env-file", str(self.external / "absent.env"), "--runner", "/bin/false"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no command executed", result.stdout)
        default_result = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "run-deployment-adapter", "--run-id", run_id, "--secret-env-file", str(self.external / "absent.env")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, default_result.returncode, default_result.stderr)
        self.assertIn("custom_gpt_vercel_create.py", default_result.stdout)
        self.assertIn("--attempt-state", default_result.stdout)
        verify_plan = subprocess.run(["python3", str(SCRIPT), "--home", str(self.home), "verify", "--run-id", run_id, "--smoke-evidence", str(self.external / "absent.json"), "--browser-evidence", str(self.external / "absent-browser.json")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, verify_plan.returncode, verify_plan.stderr)
        self.assertIn("no network request made", verify_plan.stdout)


if __name__ == "__main__":
    unittest.main()
