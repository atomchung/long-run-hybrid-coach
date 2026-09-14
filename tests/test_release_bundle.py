from __future__ import annotations

import dataclasses
import inspect
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from garmin_coach_loop.gateway import PRODUCT_VERSION
from garmin_coach_loop.mcp_transport import TOOLS, tool_catalogue_sha256
from garmin_coach_loop.release_identity import (
    DEPLOYMENT_ENVIRONMENT_ENV_VAR,
    DEPLOYMENT_INSTANCE_ID_ENV_VAR,
    EXPECTED_DEPLOYMENT_IDENTITY_FILE_ENV_VAR,
    PREDATES_RELEASE_IDENTITY_CHANGE,
    PRODUCTION_OBSERVATION_KIND,
    RELEASE_CONTENT_FIELDS,
    ReleaseIdentityError,
    deployment_identity,
    make_deployment_identity,
    make_release_id,
    normalise_gateway_domain,
    package_artifact_sha256,
    predates_release_identity_change,
    production_observation,
    release_identity,
    sha256_text,
    skill_tree_sha256,
)
from scripts.release_bundle import (
    SKILL,
    expected_deployment_identity_from_env,
    observe_live,
    outside_repo,
    read_private_env,
    verify_release,
)
from scripts import release_bundle as release_bundle_module


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_bundle.py"
SKILL_ROOT = ROOT / SKILL


def _release(**changes: str) -> dict[str, str]:
    identity = {
        "git_commit": "a" * 40,
        "instructions_sha256": "1" * 64,
        "tool_catalogue_sha256": "2" * 64,
        "skill_sha256": "3" * 64,
        "gateway_artifact_sha256": "4" * 64,
        "gateway_domain": "https://gateway.example",
    }
    identity.update(changes)
    return {"release_id": make_release_id(**identity), **identity}


class ReleaseIdentityTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload, url="https://gateway.example/healthz"):
            self.payload = payload
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def geturl(self):
            return self.url

    @staticmethod
    def _deployment(root: Path, **changes: object) -> dict[str, str]:
        values = {
            "resolved_state_root": root.resolve(),
            "intervals_client_id": "client-production",
            "environment": "production",
            "instance_id": "gateway-primary-1",
            "token_hmac_key": b"release-test-token-hmac-key-000000",
        }
        values.update(changes)
        return make_deployment_identity(**values)

    def test_release_identity_binds_every_deployment_input(self):
        """Every input, one at a time -- an id that stopped covering one would pass here
        only if changing that input left the id alone, which is the whole failure."""
        identity = _release()
        self.assertEqual(identity, release_identity(identity))
        for field in (*RELEASE_CONTENT_FIELDS, "git_commit", "gateway_domain"):
            moved = {
                "git_commit": "b" * 40,
                "gateway_domain": "https://other.example",
            }.get(field, "f" * 64)
            with self.subTest(field=field):
                self.assertNotEqual(
                    identity["release_id"], _release(**{field: moved})["release_id"]
                )
                # The same field left at its old value under a moved id: the shape is
                # complete, and the binding is what refuses it.
                with self.assertRaises(ReleaseIdentityError):
                    release_identity({**identity, field: moved})
                with self.assertRaises(ReleaseIdentityError):
                    release_identity({k: v for k, v in identity.items() if k != field})

    def test_a_deployment_older_than_this_change_is_answered_in_words(self):
        """A runtime reporting the shape this repository no longer builds is not a hash
        mismatch, and saying so as one sends the reader looking for the wrong thing."""
        legacy = {
            "release_id": "gclr-" + "0" * 64,
            "git_commit": "a" * 40,
            "instructions_sha256": "1" * 64,
            "openapi_sha256": "2" * 64,
            "gateway_artifact_sha256": "3" * 64,
            "gateway_domain": "https://gateway.example",
        }
        self.assertTrue(predates_release_identity_change(legacy))
        self.assertFalse(predates_release_identity_change(_release()))
        self.assertFalse(predates_release_identity_change(None))
        # A runtime that reports both shapes at once is mid-migration, not old.
        self.assertFalse(
            predates_release_identity_change({**_release(), "openapi_sha256": "2" * 64})
        )
        with self.assertRaisesRegex(
            ReleaseIdentityError, "predates the release-identity change"
        ):
            release_identity(legacy)

    def test_the_release_id_covers_the_tool_catalogue_and_the_canonical_skill(self):
        """Issue #117 item 6: what a release *is* has to be what every entry depends on.

        Three separate ways this stops being true, checked separately: the digests stop
        moving when their artifact moves, the bundle stops feeding the real artifacts in,
        or the id stops binding the fields. The last one is
        ``test_release_identity_binds_every_deployment_input`` above; these are the
        first two.
        """
        catalogue = tool_catalogue_sha256()
        self.assertRegex(catalogue, "^[0-9a-f]{64}$")
        self.assertNotEqual(catalogue, tool_catalogue_sha256(TOOLS[:-1]))
        for change in (
            {"description": "a different description"},
            {"annotations": {**TOOLS[0].annotations, "title": "A different title"}},
            {"annotations": {**TOOLS[0].annotations, "readOnlyHint": True}},
            {"input_schema": {**TOOLS[0].input_schema, "additionalProperties": True}},
            {"name": "renamedTool"},
        ):
            with self.subTest(change=sorted(change)):
                self.assertNotEqual(
                    catalogue,
                    tool_catalogue_sha256(
                        (dataclasses.replace(TOOLS[0], **change), *TOOLS[1:])
                    ),
                )

        skill = skill_tree_sha256(SKILL_ROOT)
        self.assertRegex(skill, "^[0-9a-f]{64}$")
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "skill"
            shutil.copytree(SKILL_ROOT, copy)
            self.assertEqual(skill, skill_tree_sha256(copy))
            (copy / "SKILL.md").write_text("changed", encoding="utf-8")
            self.assertNotEqual(skill, skill_tree_sha256(copy))
            shutil.rmtree(copy)
            shutil.copytree(SKILL_ROOT, copy)
            (copy / "agents" / "anthropic.yaml").write_text("added", encoding="utf-8")
            self.assertNotEqual(skill, skill_tree_sha256(copy))

        # And the bundle binds *those* values, not two constants that happen to be hex.
        # Only meaningful when the checkout is the commit being read out of Git, so a
        # working tree mid-edit skips rather than fails on its own uncommitted change.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "garmin_coach_loop", SKILL],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            self.skipTest(f"working tree differs from HEAD:\n{dirty}")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle.json"
            subprocess.run(
                ["python3", str(SCRIPT), "build", "--gateway-domain", "https://gateway.example", "--output", str(output)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            built = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(catalogue, built["tool_catalogue_sha256"])
        self.assertEqual(skill, built["skill_sha256"])
        self.assertNotIn("openapi_sha256", built)

    def test_placeholder_is_refused(self):
        with self.assertRaises(ReleaseIdentityError): normalise_gateway_domain("https://YOUR-GATEWAY-DOMAIN")
        for value in ("http://gateway.example", "https://" + "user" + "@gateway.example", "https://gateway.example/path", "https://gateway.example?q=x"):
            with self.assertRaises(ReleaseIdentityError): normalise_gateway_domain(value)
        self.assertEqual("https://gateway.example", normalise_gateway_domain("https://Gateway.EXAMPLE:443/"))

    def test_package_digest_changes_for_any_runtime_module(self):
        self.assertNotEqual(package_artifact_sha256([("gateway.py", b"a"), ("store.py", b"b")]), package_artifact_sha256([("gateway.py", b"a"), ("store.py", b"changed")]))

    def test_configuration_binding_changes_for_every_bound_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = self._deployment(root)
            variants = (
                self._deployment(root / "other"),
                self._deployment(root, intervals_client_id="client-staging"),
                self._deployment(root, environment="staging"),
                self._deployment(root, instance_id="gateway-primary-2"),
                self._deployment(
                    root,
                    token_hmac_key=b"different-token-hmac-key-00000000",
                ),
            )
            for variant in variants:
                with self.subTest(variant=variant):
                    self.assertNotEqual(
                        baseline["configuration_binding"],
                        variant["configuration_binding"],
                    )
            self.assertEqual(baseline, deployment_identity(baseline))
            with self.assertRaises(ReleaseIdentityError):
                deployment_identity({**baseline, "unexpected": "field"})
            with self.assertRaises(ReleaseIdentityError):
                deployment_identity({**baseline, "instance_id": 1})
            with self.assertRaises(ReleaseIdentityError):
                deployment_identity({**baseline, "environment": " production "})

    def test_trusted_runner_helper_reads_only_external_0600_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env_file = root / "gateway.env"
            env_file.write_text(
                "\n".join(
                    (
                        f"GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT={root / 'state'}",
                        "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY=release-test-token-hmac-key-000000",
                        "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID=client-production",
                        f"{DEPLOYMENT_ENVIRONMENT_ENV_VAR}=production",
                        f"{DEPLOYMENT_INSTANCE_ID_ENV_VAR}=gateway-primary-1",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            values = read_private_env(env_file)
            self.assertEqual(
                self._deployment(root / "state"),
                expected_deployment_identity_from_env(values),
            )
            output = root / "expected-deployment.json"
            result = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "deployment-identity",
                    "--env-file",
                    str(env_file),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(self._deployment(root / "state"), json.loads(output.read_text()))
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            env_file.chmod(0o644)
            with self.assertRaisesRegex(ReleaseIdentityError, "mode 0600"):
                read_private_env(env_file)

    def test_the_bundle_is_deterministic_and_holds_the_prompt_it_binds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.json"
            result = subprocess.run(["python3", str(SCRIPT), "build", "--gateway-domain", "https://gateway.example", "--output", str(bundle)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            first = bundle.read_text(encoding="utf-8")
            again = root / "again.json"
            self.assertEqual(
                0,
                subprocess.run(
                    ["python3", str(SCRIPT), "build", "--gateway-domain", "https://gateway.example", "--output", str(again)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                ).returncode,
            )
            self.assertEqual(first, again.read_text(encoding="utf-8"))
            data = json.loads(first)
            self.assertEqual("2", data["schema_version"])
            self.assertFalse(data["instructions"].endswith(("\n", "\r")))
            instructions = root / "instructions.md"
            instructions.write_text(data["instructions"], encoding="utf-8")
            # Network verification is exercised by the gateway health contract; this
            # deterministic test verifies the prompt the bundle carries beside its digest.
            self.assertEqual(data["instructions_sha256"], sha256_text(instructions.read_text(encoding="utf-8")))
            instructions.write_text("stale", encoding="utf-8")
            self.assertNotEqual(data["instructions_sha256"], sha256_text(instructions.read_text(encoding="utf-8")))

    def test_verify_fetches_runtime_and_refuses_blocked_or_mismatched_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path = root / "bundle.json"
            subprocess.run(
                ["python3", str(SCRIPT), "build", "--gateway-domain", "https://gateway.example", "--output", str(bundle_path)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            bundled = json.loads(bundle_path.read_text(encoding="utf-8"))
            receipt = root / "receipt.json"
            expected_path = root / "expected-deployment.json"
            expected_deployment = self._deployment(root / "gateway-state")
            expected_path.write_text(json.dumps(expected_deployment), encoding="utf-8")
            calls = []

            def opener(url, *, timeout):
                calls.append((url, timeout))
                return self._Response(
                    {
                        "status": "ok",
                        "release_identity": bundled,
                        "deployment_identity": expected_deployment,
                    }
                )

            verified = verify_release(
                bundle_path=bundle_path,
                receipt_path=receipt,
                expected_deployment_identity_path=expected_path,
                opener=opener,
            )
            self.assertEqual("3", verified["schema_version"])
            self.assertEqual(expected_deployment, verified["deployment_identity"])
            self.assertEqual(
                "gateway artifact and deployment configuration parity only",
                verified["certifies"],
            )
            self.assertEqual([("https://gateway.example/healthz", 15)], calls)
            self.assertEqual(verified, json.loads(receipt.read_text(encoding="utf-8")))

            with mock.patch.dict(
                "os.environ",
                {EXPECTED_DEPLOYMENT_IDENTITY_FILE_ENV_VAR: ""},
            ):
                with self.assertRaisesRegex(
                    ReleaseIdentityError, "expected deployment identity is required"
                ):
                    verify_release(
                        bundle_path=bundle_path,
                        receipt_path=receipt,
                        opener=opener,
                    )
            with mock.patch.dict(
                "os.environ",
                {EXPECTED_DEPLOYMENT_IDENTITY_FILE_ENV_VAR: str(expected_path)},
            ):
                verified_via_deploy_environment = verify_release(
                    bundle_path=bundle_path,
                    receipt_path=receipt,
                    opener=opener,
                )
            self.assertEqual(expected_deployment, verified_via_deploy_environment[
                "deployment_identity"
            ])

            with self.assertRaisesRegex(ReleaseIdentityError, "health is not ready"):
                verify_release(
                    bundle_path=bundle_path,
                    receipt_path=receipt,
                    expected_deployment_identity_path=expected_path,
                    opener=lambda *_args, **_kwargs: self._Response({"status": "blocked"}),
                )
            mismatched = dict(bundled)
            mismatched["gateway_domain"] = "https://other.example"
            mismatched["release_id"] = make_release_id(
                git_commit=mismatched["git_commit"],
                instructions_sha256=mismatched["instructions_sha256"],
                tool_catalogue_sha256=mismatched["tool_catalogue_sha256"],
                skill_sha256=mismatched["skill_sha256"],
                gateway_artifact_sha256=mismatched["gateway_artifact_sha256"],
                gateway_domain=mismatched["gateway_domain"],
            )
            with self.assertRaisesRegex(ReleaseIdentityError, "runtime identity does not match"):
                verify_release(
                    bundle_path=bundle_path,
                    receipt_path=receipt,
                    expected_deployment_identity_path=expected_path,
                    opener=lambda *_args, **_kwargs: self._Response(
                        {
                            "status": "ok",
                            "release_identity": mismatched,
                            "deployment_identity": expected_deployment,
                        }
                    ),
                )
            with self.assertRaisesRegex(ReleaseIdentityError, "redirected away"):
                verify_release(
                    bundle_path=bundle_path,
                    receipt_path=receipt,
                    expected_deployment_identity_path=expected_path,
                    opener=lambda *_args, **_kwargs: self._Response(
                        {
                            "status": "ok",
                            "release_identity": bundled,
                            "deployment_identity": expected_deployment,
                        },
                        url="https://other.example/healthz",
                    ),
                )

            # An older deployment is named as one, before any digest is compared: it
            # reports a shape this checkout no longer builds, so every field of it would
            # "mismatch" and none of that would say what actually happened.
            legacy_runtime = {
                "release_id": bundled["release_id"],
                "git_commit": bundled["git_commit"],
                "instructions_sha256": bundled["instructions_sha256"],
                "openapi_sha256": "2" * 64,
                "gateway_artifact_sha256": bundled["gateway_artifact_sha256"],
                "gateway_domain": bundled["gateway_domain"],
            }
            with self.assertRaisesRegex(
                ReleaseIdentityError, "predates the release-identity change"
            ):
                verify_release(
                    bundle_path=bundle_path,
                    receipt_path=receipt,
                    expected_deployment_identity_path=expected_path,
                    opener=lambda *_args, **_kwargs: self._Response(
                        {
                            "status": "ok",
                            "release_identity": legacy_runtime,
                            "deployment_identity": expected_deployment,
                        }
                    ),
                )
            self.assertIn(
                "predates the release-identity change", PREDATES_RELEASE_IDENTITY_CHANGE
            )

            wrong_deployment = self._deployment(
                root / "gateway-state", environment="staging"
            )
            with self.assertRaisesRegex(
                ReleaseIdentityError, "does not match expected configuration"
            ):
                verify_release(
                    bundle_path=bundle_path,
                    receipt_path=receipt,
                    expected_deployment_identity_path=expected_path,
                    opener=lambda *_args, **_kwargs: self._Response(
                        {
                            "status": "ok",
                            "release_identity": bundled,
                            "deployment_identity": wrong_deployment,
                        }
                    ),
                )

    def test_every_evidence_input_must_resolve_outside_repo(self):
        with self.assertRaises(ReleaseIdentityError):
            outside_repo(ROOT / "garmin_coach_loop" / "orchestration.md")
        with tempfile.TemporaryDirectory() as directory:
            symlink = Path(directory) / "orchestration.md"
            symlink.symlink_to(ROOT / "garmin_coach_loop" / "orchestration.md")
            with self.assertRaises(ReleaseIdentityError):
                outside_repo(symlink)


class ProductionObservationTests(unittest.TestCase):
    """Live production facts are a dated /readyz observation, not a checkout comparison."""

    def test_a_ready_payload_is_stamped_without_matching_this_checkout(self):
        """The failure this catches: treating PRODUCT_VERSION or an issue body as live.

        An observation of 0.0.1 must survive even though this checkout is not 0.0.1.
        Comparing would turn "what is live" into "does live match us", which is verify.
        """
        identity = _release()
        payload = {
            "status": "ok",
            "product_version": "0.0.1",
            "source_git_commit": identity["git_commit"],
            "release_identity": identity,
            "deployment_identity": {
                "environment": "production",
                "instance_id": "gateway-primary-1",
                "configuration_binding": "c" * 64,
            },
            "error": None,
        }
        observed = production_observation(
            payload,
            observed_at="2026-09-14T03:07:18Z",
            endpoint="https://mcp.paceandstaystrong.com/readyz",
        )
        self.assertEqual(PRODUCTION_OBSERVATION_KIND, observed["kind"])
        self.assertEqual("2026-09-14T03:07:18Z", observed["observed_at"])
        self.assertEqual("0.0.1", observed["product_version"])
        self.assertEqual("production", observed["deployment_environment"])
        self.assertEqual(identity, observed["release_identity"])
        self.assertNotEqual(PRODUCT_VERSION, observed["product_version"])
        self.assertIsNone(observed["error"])

    def test_ok_status_with_a_malformed_identity_is_refused_not_quoted(self):
        """A ready endpoint that cannot prove its identity is not a production fact."""
        with self.assertRaises(ReleaseIdentityError):
            production_observation(
                {
                    "status": "ok",
                    "product_version": "1.4.2",
                    "release_identity": {"release_id": "gclr-" + "0" * 64},
                },
                observed_at="2026-09-14T03:07:18Z",
                endpoint="https://mcp.paceandstaystrong.com/readyz",
            )

    def test_blocked_readyz_is_still_an_observation(self):
        """Blocked is what is live. Inventing a release identity for it would be the lie."""
        observed = production_observation(
            {
                "status": "blocked",
                "product_version": "0.0.1",
                "source_git_commit": "b" * 40,
                "error": "missing_or_mismatched_runtime_release_deployment_or_source_identity",
                "release_identity": None,
                "deployment_identity": None,
            },
            observed_at="2026-09-14T03:07:18Z",
            endpoint="https://mcp.paceandstaystrong.com/readyz",
        )
        self.assertEqual("blocked", observed["status"])
        self.assertEqual("0.0.1", observed["product_version"])
        self.assertIsNone(observed["release_identity"])
        self.assertIn("missing_or_mismatched", observed["error"])

    def test_a_legacy_runtime_is_named_rather_than_hash_mismatched(self):
        legacy = {
            "status": "ok",
            "product_version": "1.0.0",
            "release_identity": {
                "release_id": "gclr-" + "0" * 64,
                "git_commit": "a" * 40,
                "instructions_sha256": "1" * 64,
                "openapi_sha256": "2" * 64,
                "gateway_artifact_sha256": "3" * 64,
                "gateway_domain": "https://mcp.paceandstaystrong.com",
            },
        }
        observed = production_observation(
            legacy,
            observed_at="2026-09-14T03:07:18Z",
            endpoint="https://mcp.paceandstaystrong.com/readyz",
        )
        self.assertEqual(PREDATES_RELEASE_IDENTITY_CHANGE, observed["error"])
        self.assertEqual(legacy["release_identity"], observed["release_identity"])


class ObserveLiveTests(unittest.TestCase):
    _Response = ReleaseIdentityTests._Response
    def test_observe_reads_readyz_and_does_not_compare_this_checkout(self):
        identity = _release()
        payload = {
            "status": "ok",
            "product_version": "0.0.1",
            "source_git_commit": identity["git_commit"],
            "release_identity": identity,
            "deployment_identity": {"environment": "staging"},
            "error": None,
        }
        calls = []

        def opener(url, *, timeout):
            calls.append((url, timeout))
            return self._Response(payload, url=url)

        observed = observe_live(
            gateway_domain="https://mcp.paceandstaystrong.com",
            opener=opener,
            now=datetime(2026, 9, 14, 3, 7, 18, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [("https://mcp.paceandstaystrong.com/readyz", 15)], calls
        )
        self.assertEqual("0.0.1", observed["product_version"])
        self.assertEqual("staging", observed["deployment_environment"])
        self.assertEqual("2026-09-14T03:07:18Z", observed["observed_at"])
        self.assertNotEqual(PRODUCT_VERSION, observed["product_version"])
        source = inspect.getsource(observe_live)
        self.assertNotIn("PRODUCT_VERSION", source)
        self.assertNotIn("bundle(", source)

    def test_observe_records_a_blocked_http_error_instead_of_failing_closed(self):
        payload = {
            "status": "blocked",
            "product_version": "0.0.1",
            "source_git_commit": "b" * 40,
            "error": "missing_or_mismatched_runtime_release_deployment_or_source_identity",
            "release_identity": None,
        }

        def opener(url, *, timeout):
            raise urllib.error.HTTPError(
                url,
                503,
                "Service Unavailable",
                None,
                io.BytesIO(json.dumps(payload).encode("utf-8")),
            )

        observed = observe_live(
            gateway_domain="https://mcp.paceandstaystrong.com",
            opener=opener,
            now=datetime(2026, 9, 14, 3, 7, 18, tzinfo=timezone.utc),
        )
        self.assertEqual("blocked", observed["status"])
        self.assertEqual("0.0.1", observed["product_version"])

    def test_observe_refuses_a_redirect(self):
        identity = _release()

        def opener(url, *, timeout):
            return self._Response(
                {"status": "ok", "release_identity": identity},
                url="https://other.example/readyz",
            )

        with self.assertRaisesRegex(ReleaseIdentityError, "redirected"):
            observe_live(gateway_domain="https://mcp.paceandstaystrong.com", opener=opener)

    def test_observe_cli_prints_json_without_a_bundle_or_deployment_file(self):
        identity = _release()
        observation = production_observation(
            {
                "status": "ok",
                "product_version": "0.0.1",
                "source_git_commit": identity["git_commit"],
                "release_identity": identity,
                "deployment_identity": {"environment": "production"},
                "error": None,
            },
            observed_at="2026-09-14T03:07:18Z",
            endpoint="https://mcp.paceandstaystrong.com/readyz",
        )
        with mock.patch.object(
            release_bundle_module, "observe_live", return_value=observation
        ):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                code = release_bundle_module.main(["observe"])
        self.assertEqual(0, code)
        printed = json.loads(stdout.getvalue())
        self.assertEqual("0.0.1", printed["product_version"])
        self.assertEqual(PRODUCTION_OBSERVATION_KIND, printed["kind"])

    def test_observe_cli_will_not_write_an_observation_into_the_repository(self):
        identity = _release()
        observation = production_observation(
            {
                "status": "ok",
                "product_version": "0.0.1",
                "source_git_commit": identity["git_commit"],
                "release_identity": identity,
                "error": None,
            },
            observed_at="2026-09-14T03:07:18Z",
            endpoint="https://mcp.paceandstaystrong.com/readyz",
        )
        inside = ROOT / "docs" / "ops" / "would-be-stale.json"
        with mock.patch.object(
            release_bundle_module, "observe_live", return_value=observation
        ):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    code = release_bundle_module.main(
                        ["observe", "--output", str(inside)]
                    )
        self.assertEqual(2, code)
        self.assertIn("outside the repository", stderr.getvalue())
        self.assertFalse(inside.exists())


class ProductionTruthRunbookTests(unittest.TestCase):
    def test_runbook_names_observe_and_does_not_claim_a_live_version(self):
        """The failure this catches: this file copying 'Production is 1.4.2' again.

        The runbook is allowed to name the command and the receipts directory. A
        present-tense version in it is the same stale-copy bug the issue bodies had.
        """
        text = (ROOT / "docs" / "ops" / "verify-production-status.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 scripts/release_bundle.py observe", text)
        self.assertIn("dated observation", text.lower())
        self.assertNotRegex(text, r"(?i)production is \d+\.\d+")
        self.assertNotRegex(text, r"(?im)^latest recorded release\b")
