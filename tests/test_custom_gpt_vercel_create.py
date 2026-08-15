from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.parse
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

from scripts.custom_gpt_vercel_create import (
    CreateError,
    VercelCreateClient,
    _NoRedirect,
    _id,
    _request_material,
    _write_private,
    run_create,
)


class _Response:
    def __init__(self, url: str, value: dict, status: int = 200):
        self.status = status
        self._url = url
        self._body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self):
        return self._body


class VercelCreateAdapterTests(unittest.TestCase):
    def _adapter_fixture(self, root: Path) -> dict:
        revision = "gclp-" + "1" * 64
        run_id = "gcld-" + "2" * 64
        release_id = "gclr-" + "3" * 64
        binding = "4" * 64
        config = b'{"rewrites":[]}\n'
        config_path = root / "proxy" / "revisions" / f"{revision}.vercel.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_bytes(config)
        target = {
            "team_id": "team_example",
            "project_id": "prj_example",
            "project_name": "long-run-hybrid-coach-gateway",
            "stable_domain": "gateway.example",
        }
        request = {
            "schema_version": "2",
            "provider": "vercel",
            "target": "production",
            "run_id": run_id,
            "proxy_revision_id": revision,
            "release_identity": {"release_id": release_id},
            "production_target_binding_sha256": binding,
            "vercel_target": target,
            "proxy": {
                "config": f"proxy/revisions/{revision}.vercel.json",
                "config_sha256": hashlib.sha256(config).hexdigest(),
                "upstream": "https://tunnel.example",
            },
        }
        request_body = (
            json.dumps(request, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        request_path = root / "deploy-requests" / f"{revision}.json"
        request_path.parent.mkdir()
        request_path.write_bytes(request_body)
        metadata = {
            "gclProxyRevision": revision,
            "gclRequestSha256": hashlib.sha256(request_body).hexdigest(),
            "gclConfigSha256": hashlib.sha256(config).hexdigest(),
        }
        attempt = {
            "schema_version": "1",
            "state": "prepared",
            "attempt_id": _id(
                "gclc-", run_id, release_id, revision,
                metadata["gclRequestSha256"], metadata["gclConfigSha256"],
                binding,
            ),
            "run_id": run_id,
            "release_id": release_id,
            "proxy_revision_id": revision,
            "request_sha256": metadata["gclRequestSha256"],
            "config_sha256": metadata["gclConfigSha256"],
            "target_binding_sha256": binding,
            "team_id": target["team_id"],
            "project_id": target["project_id"],
            "project_name": target["project_name"],
            "metadata": metadata,
            "prepared_at": "2026-08-15T01:00:00Z",
            "submission_started_at": None,
            "attestation_path": None,
            "attestation_sha256": None,
        }
        attempt_path = root / "create-attempts" / f"{revision}.json"
        attempt_path.parent.mkdir()
        attempt_path.write_text(
            json.dumps(attempt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        attempt_path.chmod(0o600)
        secret = root / "vercel.env"
        secret.write_text("VERCEL_TOKEN=vercel-token-1234567890\n", encoding="utf-8")
        secret.chmod(0o600)
        evidence = root / "evidence.json"
        evidence.touch(mode=0o600)
        evidence.chmod(0o600)
        return {
            "request": request,
            "request_path": request_path,
            "attempt_path": attempt_path,
            "secret": secret,
            "evidence": evidence,
            "metadata": metadata,
            "target": target,
        }

    @staticmethod
    def _deployment(fixture: dict, suffix: str = "one") -> dict:
        return {
            "id": "dpl_" + suffix,
            "url": f"{suffix}.example.vercel.app",
            "projectId": fixture["target"]["project_id"],
            "name": fixture["target"]["project_name"],
            "target": "production",
            "meta": fixture["metadata"],
        }

    @staticmethod
    def _mark_pending(fixture: dict) -> None:
        attempt = json.loads(fixture["attempt_path"].read_text(encoding="utf-8"))
        attempt["state"] = "submission_started"
        attempt["submission_started_at"] = "2026-08-15T01:01:00Z"
        fixture["attempt_path"].write_text(
            json.dumps(attempt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        fixture["attempt_path"].chmod(0o600)

    def test_uploads_exact_config_then_creates_fixed_production_attestation(self):
        calls = []
        token = "vercel-token-1234567890"

        def opener(request, timeout):
            calls.append(request)
            if "/v2/files?" in request.full_url:
                return _Response(request.full_url, {})
            return _Response(
                request.full_url,
                {"id": "dpl_created", "url": "created.example.vercel.app"},
                201,
            )

        request_value = {
            "vercel_target": {
                "team_id": "team_example", "project_id": "prj_example",
                "project_name": "long-run-hybrid-coach-gateway",
                "stable_domain": "gateway.example",
            },
        }
        config = b'{"rewrites":[]}\n'
        metadata = {
            "gclProxyRevision": "gclp-" + "1" * 64,
            "gclRequestSha256": "2" * 64,
            "gclConfigSha256": hashlib.sha256(config).hexdigest(),
        }
        evidence = VercelCreateClient(token, opener=opener).create(
            request_value, config, metadata,
        )
        self.assertEqual(2, len(calls))
        self.assertIn("/v2/files?teamId=team_example", calls[0].full_url)
        self.assertEqual(
            hashlib.sha1(config).hexdigest(),  # noqa: S324 - provider contract
            calls[0].get_header("X-vercel-digest"),
        )
        payload = json.loads(calls[1].data)
        self.assertEqual("production", payload["target"])
        self.assertNotIn("alias", payload)
        self.assertEqual(metadata, payload["meta"])
        self.assertEqual("prj_example", payload["project"])
        self.assertEqual("dpl_created", evidence["create_response"]["deploymentId"])
        self.assertEqual(metadata, evidence["create_response"]["metadata"])
        self.assertNotIn(token, json.dumps(evidence))

    def test_lost_create_response_reconciles_by_exact_metadata_without_second_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            calls = []
            created = self._deployment(fixture)

            def ambiguous_opener(request, timeout):
                calls.append((request.get_method(), request.full_url))
                if "/v2/files?" in request.full_url:
                    return _Response(request.full_url, {})
                self.assertEqual("POST", request.get_method())
                payload = json.loads(request.data)
                self.assertEqual(fixture["metadata"], payload["meta"])
                raise OSError("accepted response was lost")

            with self.assertRaisesRegex(CreateError, "provider request failed"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"],
                    opener=ambiguous_opener,
                    clock=lambda: "2026-08-15T01:01:00Z",
                )
            pending = json.loads(fixture["attempt_path"].read_text())
            self.assertEqual("submission_started", pending["state"])
            self.assertEqual(0o600, fixture["attempt_path"].stat().st_mode & 0o777)

            retry_calls = []

            def reconcile_opener(request, timeout):
                retry_calls.append((request.get_method(), request.full_url))
                self.assertEqual("GET", request.get_method())
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
                self.assertEqual([fixture["target"]["project_id"]], query["projectId"])
                return _Response(request.full_url, {"deployments": [created]})

            evidence = run_create(
                request_path=fixture["request_path"],
                secret_env_file=fixture["secret"],
                evidence_output=fixture["evidence"],
                attempt_state=fixture["attempt_path"],
                opener=reconcile_opener,
                clock=lambda: "2026-08-15T01:02:00Z",
            )
            self.assertEqual(1, sum(method == "POST" and "/v13/deployments" in url for method, url in calls))
            self.assertEqual(["GET"], [method for method, _ in retry_calls])
            attempt = json.loads(fixture["attempt_path"].read_text())
            self.assertEqual("attested", attempt["state"])
            attestation = Path(temporary) / attempt["attestation_path"]
            self.assertEqual(0o600, attestation.stat().st_mode & 0o777)
            self.assertEqual(evidence, json.loads(attestation.read_text()))
            self.assertEqual(evidence, json.loads(fixture["evidence"].read_text()))
            self.assertNotIn(
                "vercel-token-1234567890",
                fixture["attempt_path"].read_text() + attestation.read_text(),
            )

    def test_successful_create_is_durable_before_ephemeral_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            fixture["evidence"].chmod(0o644)
            calls = []

            def opener(request, timeout):
                calls.append(request.get_method())
                if "/v2/files?" in request.full_url:
                    return _Response(request.full_url, {})
                return _Response(
                    request.full_url,
                    {"id": "dpl_direct", "url": "direct.example.vercel.app"},
                    201,
                )

            with self.assertRaisesRegex(CreateError, "output must be one 0600"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"], opener=opener,
                    clock=lambda: "2026-08-15T01:01:00Z",
                )
            self.assertEqual(["POST", "POST"], calls)
            attempt = json.loads(fixture["attempt_path"].read_text())
            self.assertEqual("attested", attempt["state"])
            attestation = Path(temporary) / attempt["attestation_path"]
            self.assertTrue(attestation.is_file())
            self.assertEqual(
                attempt["attestation_sha256"],
                hashlib.sha256(attestation.read_bytes()).hexdigest(),
            )

            fixture["evidence"].chmod(0o600)

            def no_network(request, timeout):
                raise AssertionError("attested retry must not call Vercel")

            evidence = run_create(
                request_path=fixture["request_path"],
                secret_env_file=fixture["secret"],
                evidence_output=fixture["evidence"],
                attempt_state=fixture["attempt_path"], opener=no_network,
            )
            self.assertEqual("dpl_direct", evidence["create_response"]["deploymentId"])

    def test_pending_zero_match_fails_closed_without_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            self._mark_pending(fixture)
            calls = []

            def opener(request, timeout):
                calls.append(request.get_method())
                return _Response(request.full_url, {"deployments": []})

            with self.assertRaisesRegex(CreateError, "no exact deployment match"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"], opener=opener,
                )
            self.assertEqual(["GET"], calls)
            self.assertEqual(
                "submission_started",
                json.loads(fixture["attempt_path"].read_text())["state"],
            )

    def test_pending_multiple_matches_fail_closed_without_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            self._mark_pending(fixture)
            calls = []

            def opener(request, timeout):
                calls.append(request.get_method())
                return _Response(request.full_url, {"deployments": [
                    self._deployment(fixture, "one"),
                    self._deployment(fixture, "two"),
                ]})

            with self.assertRaisesRegex(CreateError, "multiple exact"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"], opener=opener,
                )
            self.assertEqual(["GET"], calls)

    def test_pending_same_revision_different_hashes_is_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            self._mark_pending(fixture)
            collision = self._deployment(fixture)
            collision["meta"] = {
                **fixture["metadata"], "gclRequestSha256": "9" * 64,
            }
            calls = []

            def opener(request, timeout):
                calls.append(request.get_method())
                return _Response(request.full_url, {"deployments": [collision]})

            with self.assertRaisesRegex(CreateError, "metadata collision"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"], opener=opener,
                )
            self.assertEqual(["GET"], calls)

    def test_attempt_must_be_exact_0600_and_request_bound_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            fixture["attempt_path"].chmod(0o644)
            called = False

            def opener(request, timeout):
                nonlocal called
                called = True
                raise AssertionError("network must not be called")

            with self.assertRaisesRegex(CreateError, "0600"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"], opener=opener,
                )
            self.assertFalse(called)

    def test_secret_final_symlink_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._adapter_fixture(Path(temporary))
            real_secret = Path(temporary) / "real-vercel.env"
            fixture["secret"].replace(real_secret)
            fixture["secret"].symlink_to(real_secret)
            called = False

            def opener(request, timeout):
                nonlocal called
                called = True
                raise AssertionError("network must not be called")

            with self.assertRaisesRegex(CreateError, "non-symlink"):
                run_create(
                    request_path=fixture["request_path"],
                    secret_env_file=fixture["secret"],
                    evidence_output=fixture["evidence"],
                    attempt_state=fixture["attempt_path"], opener=opener,
                )
            self.assertFalse(called)

    def test_request_material_is_hash_bound_and_output_stays_0600(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "proxy" / "revisions" / "revision.vercel.json"
            config_path.parent.mkdir(parents=True)
            config = b'{"rewrites":[]}\n'
            config_path.write_bytes(config)
            request = {
                "schema_version": "2", "provider": "vercel",
                "target": "production", "production_target_binding_sha256": "a" * 64,
                "vercel_target": {
                    "team_id": "team_example", "project_id": "prj_example",
                    "project_name": "long-run-hybrid-coach-gateway",
                    "stable_domain": "gateway.example",
                },
                "proxy": {
                    "config": "proxy/revisions/revision.vercel.json",
                    "config_sha256": hashlib.sha256(config).hexdigest(),
                    "upstream": "https://tunnel.example",
                },
            }
            request_path = root / "deploy-requests" / "revision.json"
            request_path.parent.mkdir()
            request_path.write_text(json.dumps(request), encoding="utf-8")
            loaded, body = _request_material(request_path)
            self.assertEqual(request, loaded)
            self.assertEqual(config, body)
            output = root / "evidence.json"
            output.touch(mode=0o600)
            _write_private(output, {"status": "bounded"})
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            self.assertEqual({"status": "bounded"}, json.loads(output.read_text()))
            request["proxy"]["config_sha256"] = "0" * 64
            request_path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaisesRegex(CreateError, "does not match"):
                _request_material(request_path)

    def test_request_material_rejects_symlink_inside_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.json"
            config = b'{"rewrites":[]}\n'
            outside.write_bytes(config)
            proxy = root / "proxy"
            proxy.mkdir()
            (proxy / "revisions").symlink_to(root)
            request = {
                "schema_version": "2", "provider": "vercel",
                "target": "production", "production_target_binding_sha256": "a" * 64,
                "vercel_target": {
                    "team_id": "team_example", "project_id": "prj_example",
                    "project_name": "long-run-hybrid-coach-gateway",
                    "stable_domain": "gateway.example",
                },
                "proxy": {
                    "config": "proxy/revisions/outside.json",
                    "config_sha256": hashlib.sha256(config).hexdigest(),
                    "upstream": "https://tunnel.example",
                },
            }
            request_path = root / "deploy-requests" / "revision.json"
            request_path.parent.mkdir()
            request_path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaisesRegex(CreateError, "must not contain symlinks"):
                _request_material(request_path)

    def test_create_redirect_is_not_followed(self):
        calls = []

        class RedirectingTransport(urllib.request.HTTPSHandler):
            handler_order = 100

            def https_open(self, request):
                calls.append(request.full_url)
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
        with self.assertRaises(CreateError):
            VercelCreateClient(
                "vercel-token-1234567890", opener=opener,
            ).create(
                {"vercel_target": {
                    "team_id": "team_example", "project_id": "prj_example",
                    "project_name": "long-run-hybrid-coach-gateway",
                    "stable_domain": "gateway.example",
                }},
                b"{}",
                {
                    "gclProxyRevision": "gclp-" + "1" * 64,
                    "gclRequestSha256": "2" * 64,
                    "gclConfigSha256": "3" * 64,
                },
            )
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0].startswith("https://api.vercel.com/"))


if __name__ == "__main__":
    unittest.main()
