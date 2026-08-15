from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

from scripts.custom_gpt_deploy_providers import (
    GitHubProviderReader,
    ProviderReadbackError,
    ProviderResponse,
    VercelProviderReader,
    VercelRestProviderReader,
    _NoRedirect,
    load_production_target,
    normalize_vercel_create_attestation,
    production_target_binding,
    validate_github_provider_receipt,
    validate_production_target,
    validate_vercel_create_attestation,
    validate_vercel_provider_receipt,
)


SHA = "a" * 40


def target_value() -> dict:
    value = {
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
            "stable_domain": "long-run-hybrid-coach-gateway.example.vercel.app",
        },
        "custom_gpt": {"gpt_id": "gpt-production-123"},
    }
    value["binding_sha256"] = production_target_binding(value)
    return value


def github_ref(sha: str = SHA) -> dict:
    return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": sha}}


def github_run(**changes) -> dict:
    value = {
        "id": 12345,
        "html_url": "https://github.com/example-org/garmin-coach-loop/actions/runs/12345",
        "path": ".github/workflows/ci.yml",
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "example-org/garmin-coach-loop"},
    }
    value.update(changes)
    return value


class ProductionTargetTests(unittest.TestCase):
    def test_external_target_requires_exact_schema_binding_and_fixed_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "production-target.json"
            path.write_text(json.dumps(target_value()), encoding="utf-8")
            loaded = load_production_target(
                path, expected_repository="example-org/garmin-coach-loop"
            )
            self.assertEqual(target_value(), loaded)
            for mutate in (
                lambda item: item.update(unexpected=True),
                lambda item: item["github"].update(branch="release"),
                lambda item: item["github"].update(workflow_path=".github/workflows/other.yml"),
                lambda item: item["vercel"].update(project_id="wrong"),
                lambda item: item["custom_gpt"].update(gpt_id="not a GPT id"),
                lambda item: item.pop("custom_gpt"),
            ):
                broken = copy.deepcopy(target_value())
                mutate(broken)
                with self.subTest(broken=broken), self.assertRaises(ProviderReadbackError):
                    validate_production_target(broken)

    def test_target_inside_repository_is_rejected(self):
        with self.assertRaisesRegex(ProviderReadbackError, "outside repository"):
            load_production_target(Path(__file__))


class GitHubProviderTests(unittest.TestCase):
    def setUp(self):
        self.target = target_value()

    def _read(self, *, ref=None, runs=None):
        calls = []

        def runner(argv):
            calls.append(argv)
            return (ref or github_ref()) if "/git/ref/" in argv[-1] else {"workflow_runs": runs if runs is not None else [github_run()]}

        receipt = GitHubProviderReader(
            self.target, runner=runner, clock=lambda: "2026-08-15T12:00:00Z"
        ).read(SHA)
        return receipt, calls

    def test_reads_fixed_main_and_ci_success_and_emits_only_normalized_receipt(self):
        receipt, calls = self._read()
        self.assertEqual(SHA, receipt["main_ref_sha"])
        self.assertEqual("success", receipt["workflow_run_conclusion"])
        self.assertEqual(2, len(calls))
        self.assertIn("heads/main", calls[0][-1])
        self.assertIn("actions/workflows/ci.yml/runs", calls[1][-1])
        self.assertIn("event=push", calls[1][-1])
        self.assertNotIn("token", json.dumps(receipt).lower())

    def test_stale_main_is_rejected(self):
        with self.assertRaisesRegex(ProviderReadbackError, "remote main") as caught:
            self._read(ref=github_ref("b" * 40))
        self.assertEqual("stale_main", caught.exception.code)

    def test_wrong_branch_workflow_event_status_or_repository_is_rejected(self):
        mutations = (
            {"head_branch": "release"},
            {"path": ".github/workflows/other.yml"},
            {"event": "workflow_dispatch"},
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"repository": {"full_name": "other/repository"}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                ProviderReadbackError, "no exact successful"
            ):
                self._read(runs=[github_run(**mutation)])

    def test_receipt_tamper_is_rejected(self):
        receipt, _ = self._read()
        for field, replacement in (
            ("commit_sha", "b" * 40),
            ("workflow_run_id", 777),
            ("receipt_sha256", "0" * 64),
        ):
            broken = copy.deepcopy(receipt)
            broken[field] = replacement
            with self.subTest(field=field), self.assertRaises(ProviderReadbackError):
                validate_github_provider_receipt(
                    broken, target=self.target, expected_sha=SHA
                )


class VercelProviderTests(unittest.TestCase):
    def setUp(self):
        self.target = target_value()
        self.metadata = {
            "gclProxyRevision": "gclp-" + "1" * 64,
            "gclRequestSha256": "2" * 64,
            "gclConfigSha256": "3" * 64,
        }
        self.create_raw = {
            "provider": "vercel",
            "target": "production",
            "teamId": "team_example",
            "projectId": "prj_example",
            "projectName": "long-run-hybrid-coach-gateway",
            "deploymentId": "dpl_example",
            "url": "https://long-run-hybrid-coach-gateway-abc.vercel.app",
            "metadata": self.metadata,
            "ignored_provider_field": "hashed-but-not-copied",
        }
        self.create = normalize_vercel_create_attestation(
            self.create_raw, target=self.target
        )
        self.deployment = {
            "id": "dpl_example",
            "projectId": "prj_example",
            "name": "long-run-hybrid-coach-gateway",
            "teamId": "team_example",
            "target": "production",
            "readyState": "READY",
            "url": "long-run-hybrid-coach-gateway-abc.vercel.app",
            "alias": ["long-run-hybrid-coach-gateway.example.vercel.app"],
            "meta": self.metadata,
        }
        self.project = {
            "id": "prj_example",
            "name": "long-run-hybrid-coach-gateway",
            "accountId": "team_example",
            "targets": {"production": {"id": "dpl_example"}},
            "alias": [{"domain": "long-run-hybrid-coach-gateway.example.vercel.app"}],
        }

    def _reader(self, deployment=None, project=None):
        return VercelProviderReader(
            self.target,
            get_deployment=lambda deployment_id, team_id: deployment if deployment is not None else self.deployment,
            get_project=lambda project_id, team_id: project if project is not None else self.project,
            clock=lambda: "2026-08-15T12:01:00Z",
        )

    def test_normalizes_create_then_reads_deployment_and_project(self):
        receipt = self._reader().read(self.create)
        self.assertEqual("READY", receipt["deployment_ready_state"])
        self.assertEqual("dpl_example", receipt["current_production_target"])
        self.assertEqual(
            self.target["vercel"]["stable_domain"], receipt["stable_domain"]
        )
        serialized = json.dumps(receipt)
        self.assertNotIn("ignored_provider_field", serialized)
        self.assertNotIn("token", serialized.lower())

    def test_wrong_ids_preview_not_ready_target_or_domain_is_rejected(self):
        cases = []
        for field, value in (
            ("id", "dpl_wrong"),
            ("projectId", "prj_wrong"),
            ("teamId", "team_wrong"),
            ("name", "wrong-project"),
            ("target", "preview"),
            ("readyState", "BUILDING"),
        ):
            deployment = copy.deepcopy(self.deployment)
            deployment[field] = value
            cases.append((deployment, self.project))
        deployment = copy.deepcopy(self.deployment)
        deployment["meta"]["gclConfigSha256"] = "9" * 64
        cases.append((deployment, self.project))
        project = copy.deepcopy(self.project)
        project["targets"]["production"]["id"] = "dpl_old"
        cases.append((self.deployment, project))
        deployment = copy.deepcopy(self.deployment)
        deployment["alias"] = ["other.example.com"]
        cases.append((deployment, self.project))
        project = copy.deepcopy(self.project)
        project["alias"] = [{"domain": "other.example.com"}]
        cases.append((self.deployment, project))
        for deployment, project in cases:
            with self.subTest(deployment=deployment, project=project), self.assertRaisesRegex(
                ProviderReadbackError, "fixed current production"
            ):
                self._reader(deployment, project).read(self.create)

    def test_create_attestation_wrong_target_identity_and_tamper_are_rejected(self):
        for field, value in (
            ("target", "preview"),
            ("teamId", "team_wrong"),
            ("projectId", "prj_wrong"),
            ("projectName", "wrong-project"),
        ):
            raw = dict(self.create_raw)
            raw[field] = value
            with self.subTest(field=field), self.assertRaises(ProviderReadbackError):
                normalize_vercel_create_attestation(raw, target=self.target)
        broken = dict(self.create)
        broken["deployment_id"] = "dpl_tampered"
        with self.assertRaises(ProviderReadbackError):
            validate_vercel_create_attestation(broken, target=self.target)

    def test_404_is_classified_without_provider_payload(self):
        error_payload = {"error": {"message": "private project details"}}
        reader = self._reader(
            ProviderResponse(404, error_payload), self.project
        )
        with self.assertRaises(ProviderReadbackError) as caught:
            reader.read(self.create)
        self.assertEqual("provider_not_found", caught.exception.code)
        self.assertNotIn("private project details", str(caught.exception))

    def test_receipt_tamper_is_rejected(self):
        receipt = self._reader().read(self.create)
        for field, value in (
            ("project_id", "prj_wrong"),
            ("current_production_target", "dpl_old"),
            ("receipt_sha256", "0" * 64),
        ):
            broken = copy.deepcopy(receipt)
            broken[field] = value
            with self.subTest(field=field), self.assertRaises(ProviderReadbackError):
                validate_vercel_provider_receipt(
                    broken, target=self.target, create_attestation=self.create
                )


class _RestResponse:
    def __init__(self, url: str, body: dict, *, status: int = 200, final_url: str | None = None):
        self.status = status
        self._url = final_url or url
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self):
        return self._body


class VercelRestProviderTests(unittest.TestCase):
    def setUp(self):
        self.target = target_value()
        self.metadata = {
            "gclProxyRevision": "gclp-" + "1" * 64,
            "gclRequestSha256": "2" * 64,
            "gclConfigSha256": "3" * 64,
        }
        self.create = normalize_vercel_create_attestation(
            {
                "provider": "vercel", "target": "production",
                "teamId": "team_example", "projectId": "prj_example",
                "projectName": "long-run-hybrid-coach-gateway",
                "deploymentId": "dpl_example",
                "url": "https://long-run-hybrid-coach-gateway-abc.vercel.app",
                "metadata": self.metadata,
            },
            target=self.target,
        )

    def _responses(self):
        stable = self.target["vercel"]["stable_domain"]
        return [
            {
                "id": "dpl_example", "projectId": "prj_example",
                "name": "long-run-hybrid-coach-gateway", "teamId": "team_example",
                "target": "production", "readyState": "READY",
                "url": "long-run-hybrid-coach-gateway-abc.vercel.app",
                "meta": self.metadata,
            },
            {
                "id": "prj_example", "name": "long-run-hybrid-coach-gateway",
                "accountId": "team_example",
            },
            {"alias": stable, "deploymentId": "dpl_example", "projectId": "prj_example"},
            {"aliases": [{"alias": stable}]},
            {"domains": [{"name": stable}]},
        ]

    def test_reads_exact_rest_resources_and_never_persists_token(self):
        calls = []
        bodies = iter(self._responses())
        token = "vercel-token-1234567890"

        def opener(request, timeout):
            calls.append((request.full_url, request.get_header("Authorization"), timeout))
            return _RestResponse(request.full_url, next(bodies))

        receipt = VercelRestProviderReader(
            self.target, token=token, opener=opener,
            clock=lambda: "2026-08-15T12:02:00Z",
        ).read(self.create)
        self.assertEqual("dpl_example", receipt["deployment_id"])
        self.assertEqual(5, len(calls))
        self.assertIn("/v13/deployments/dpl_example?teamId=team_example", calls[0][0])
        self.assertIn("/v9/projects/prj_example?teamId=team_example", calls[1][0])
        self.assertIn("/v4/aliases/long-run-hybrid-coach-gateway.example.vercel.app?teamId=team_example", calls[2][0])
        self.assertIn("/v2/deployments/dpl_example/aliases?teamId=team_example", calls[3][0])
        self.assertIn("production=true", calls[4][0])
        self.assertTrue(all(auth == "Bearer " + token for _, auth, _ in calls))
        self.assertNotIn("vercel-token", json.dumps(receipt))

    def test_missing_stable_alias_or_redirect_fails_closed(self):
        responses = self._responses()
        responses[3] = {"aliases": [{"alias": "other.example.com"}]}
        bodies = iter(responses)

        def missing_alias(request, timeout):
            return _RestResponse(request.full_url, next(bodies))

        with self.assertRaisesRegex(ProviderReadbackError, "stable production domain"):
            VercelRestProviderReader(
                self.target, token="vercel-token-1234567890", opener=missing_alias,
            ).read(self.create)

        def redirect(request, timeout):
            return _RestResponse(
                request.full_url, {}, final_url="https://other.example/api",
            )

        with self.assertRaises(ProviderReadbackError) as caught:
            VercelRestProviderReader(
                self.target, token="vercel-token-1234567890", opener=redirect,
            ).read(self.create)
        self.assertEqual("provider_error", caught.exception.code)

    def test_http_404_is_redacted_and_fail_closed(self):
        def missing(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 404, "private provider details", {}, None,
            )

        with self.assertRaises(ProviderReadbackError) as caught:
            VercelRestProviderReader(
                self.target, token="vercel-token-1234567890", opener=missing,
            ).read(self.create)
        self.assertEqual("provider_not_found", caught.exception.code)
        self.assertNotIn("private provider details", str(caught.exception))
        self.assertNotIn("vercel-token", str(caught.exception))

    def test_real_redirect_handler_never_sends_authorization_to_second_hop(self):
        calls = []

        class RedirectingTransport(urllib.request.HTTPSHandler):
            handler_order = 100

            def https_open(self, request):
                calls.append((request.full_url, request.get_header("Authorization")))
                headers = Message()
                headers["Location"] = "https://attacker.invalid/collect"
                response = urllib.response.addinfourl(
                    io.BytesIO(b""), headers, request.full_url, 302,
                )
                response.msg = "Found"
                return response

        opener = urllib.request.build_opener(
            _NoRedirect(), RedirectingTransport(),
        ).open
        with self.assertRaises(ProviderReadbackError):
            VercelRestProviderReader(
                self.target, token="vercel-token-1234567890", opener=opener,
            ).read(self.create)
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0][0].startswith("https://api.vercel.com/"))
        self.assertTrue(calls[0][1].startswith("Bearer "))


if __name__ == "__main__":
    unittest.main()
