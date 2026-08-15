#!/usr/bin/env python3
"""Fail-closed external-state orchestrator for Custom GPT production releases."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from garmin_coach_loop.gateway import (  # noqa: E402
    RELEASE_ARTIFACT_SHA_ENV_VAR,
    RELEASE_COMMIT_ENV_VAR,
    RELEASE_DOMAIN_ENV_VAR,
    RELEASE_ID_ENV_VAR,
    RELEASE_INSTRUCTIONS_SHA_ENV_VAR,
    RELEASE_OPENAPI_SHA_ENV_VAR,
)
from garmin_coach_loop.release_identity import (  # noqa: E402
    COMMIT_RE,
    ReleaseIdentityError,
    normalise_gateway_domain,
    release_identity,
    sha256_text,
)
from scripts.custom_gpt_release import bundle, outside_repo, verify_release  # noqa: E402

RUN_RE = re.compile(r"^gcld-[0-9a-f]{64}$")
RELEASE_RE = re.compile(r"^gclr-[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^gclp-[0-9a-f]{64}$")
VERIFICATION_RE = re.compile(r"^gclv-[0-9a-f]{64}$")
ACTIVATION_RE = re.compile(r"^gcla-[0-9a-f]{64}$")
SMOKE_CERTIFIES = "user-visible Custom GPT smoke only"
DEPLOY_CERTIFIES = "vercel production deployment completed exact request"
ROUTE_MARKER_HEADER = "X-GCL-Proxy-Revision"


class DeployError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _id(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _atomic_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, _json_bytes(value))


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"cannot read valid JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise DeployError(f"JSON artifact must be an object: {path}")
    return value


def _home(path: Path) -> Path:
    try:
        return outside_repo(path)
    except ReleaseIdentityError as exc:
        raise DeployError(str(exc)) from exc


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(home / ".deploy.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _git_commit(ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if not COMMIT_RE.fullmatch(result):
        raise DeployError(f"ref did not resolve to a full commit: {ref}")
    return result


def _run_id(release_id: str) -> str:
    return _id("gcld-", release_id)


def _run_dir(home: Path, run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        raise DeployError("run id is malformed")
    return home / "runs" / run_id


def _deployment_identity(value: Any) -> dict[str, str]:
    required = {"environment", "instance_id", "configuration_binding"}
    if (
        not isinstance(value, dict) or set(value) != required
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", str(value.get("environment", "")))
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", str(value.get("instance_id", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("configuration_binding", "")))
    ):
        raise DeployError("expected deployment identity is malformed")
    return {key: value[key] for key in sorted(required)}


def _ci_evidence(value: dict[str, Any], head_sha: str) -> dict[str, Any]:
    required = {"schema_version", "provider", "head_sha", "status", "conclusion", "run_id", "url"}
    if (
        set(value) != required or value.get("schema_version") != "1" or value.get("provider") != "github-actions"
        or value.get("head_sha") != head_sha or value.get("status") != "completed" or value.get("conclusion") != "success"
        or not isinstance(value.get("run_id"), (str, int)) or isinstance(value.get("run_id"), bool) or not str(value["run_id"])
        or not isinstance(value.get("url"), str) or not value["url"].startswith("https://github.com/")
        or f"/actions/runs/{value.get('run_id')}" not in value.get("url", "")
    ):
        raise DeployError("GitHub CI evidence does not certify this exact successful main commit")
    return value


def _manifest(home: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    path = _run_dir(home, run_id) / "manifest.json"
    if not path.is_file():
        raise DeployError(f"unknown deploy run: {run_id}")
    value = _read_object(path)
    if value.get("schema_version") != "2" or value.get("run_id") != run_id:
        raise DeployError("deploy manifest identity does not match its path")
    identity = release_identity(value.get("release_identity", {}))
    if identity != value.get("release_identity"):
        raise DeployError("deploy manifest release identity is not canonical")
    _deployment_identity(value.get("expected_deployment_identity"))
    revisions = value.get("proxy_revisions")
    if not isinstance(revisions, list) or len({item.get("proxy_revision_id") for item in revisions if isinstance(item, dict)}) != len(revisions):
        raise DeployError("deploy manifest proxy revisions are malformed or duplicated")
    return path, value


def _revision(manifest: dict[str, Any], revision_id: str | None = None) -> dict[str, Any]:
    wanted = revision_id or manifest.get("current_proxy_revision_id")
    matches = [item for item in manifest.get("proxy_revisions", []) if item.get("proxy_revision_id") == wanted]
    if len(matches) != 1 or not REVISION_RE.fullmatch(str(wanted or "")):
        raise DeployError("current proxy revision is missing, duplicated, or malformed")
    return matches[0]


def _activation_ref(value: Any, *, allow_none: bool) -> dict[str, str] | None:
    if value is None and allow_none:
        return None
    required = {"run_id", "proxy_revision_id", "activation_id"}
    if not isinstance(value, dict) or set(value) != required or not RUN_RE.fullmatch(str(value.get("run_id", ""))) or not REVISION_RE.fullmatch(str(value.get("proxy_revision_id", ""))) or not ACTIVATION_RE.fullmatch(str(value.get("activation_id", ""))):
        raise DeployError("activation reference is malformed")
    return value


def _active(home: Path) -> dict[str, Any] | None:
    path = home / "active.json"
    if not path.exists():
        return None
    value = _read_object(path)
    required = {"schema_version", "run_id", "release_id", "proxy_revision_id", "activation_id", "verification_id", "activated_at", "previous"}
    if set(value) != required or value.get("schema_version") != "2" or not RUN_RE.fullmatch(str(value.get("run_id", ""))) or not REVISION_RE.fullmatch(str(value.get("proxy_revision_id", ""))) or not ACTIVATION_RE.fullmatch(str(value.get("activation_id", ""))) or not VERIFICATION_RE.fullmatch(str(value.get("verification_id", ""))):
        raise DeployError("active pointer is malformed")
    _activation_ref(value.get("previous"), allow_none=True)
    _, manifest = _manifest(home, value["run_id"])
    revision = _revision(manifest, value["proxy_revision_id"])
    _revision_material((home / "runs" / value["run_id"] / "manifest.json").parent, manifest, revision)
    activation = next((item for item in manifest.get("activations", []) if item.get("activation_id") == value["activation_id"]), None)
    verification = next((item for item in revision.get("verifications", []) if item.get("verification_id") == value["verification_id"]), None)
    if (
        value["release_id"] != manifest["release_identity"]["release_id"] or not activation
        or activation.get("proxy_revision_id") != value["proxy_revision_id"] or activation.get("verification_id") != value["verification_id"]
        or activation.get("previous") != value["previous"] or not verification
        or verification.get("consumed_by_activation_id") != value["activation_id"]
    ):
        raise DeployError("active pointer does not cross-reference its manifest evidence")
    if verification.get("status") == "passed":
        _verification_for_activation(home / "runs" / value["run_id"], manifest, revision)
    return value


def _gateway_environment(identity: dict[str, str]) -> dict[str, str]:
    return {
        RELEASE_ID_ENV_VAR: identity["release_id"], RELEASE_COMMIT_ENV_VAR: identity["git_commit"],
        RELEASE_INSTRUCTIONS_SHA_ENV_VAR: identity["instructions_sha256"], RELEASE_OPENAPI_SHA_ENV_VAR: identity["openapi_sha256"],
        RELEASE_DOMAIN_ENV_VAR: identity["gateway_domain"], RELEASE_ARTIFACT_SHA_ENV_VAR: identity["gateway_artifact_sha256"],
    }


def _proxy_config(upstream: str, revision_id: str) -> dict[str, Any]:
    return {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "headers": [{"source": "/(.*)", "headers": [{"key": ROUTE_MARKER_HEADER, "value": revision_id}]}],
        "redirects": [{"source": "/oauth/intervals/authorize", "destination": "https://intervals.icu/oauth/authorize", "permanent": False}],
        "rewrites": [{"source": "/:path*", "destination": upstream + "/:path*"}],
    }


def _request(manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "2", "provider": "vercel", "target": "production",
        "run_id": manifest["run_id"], "proxy_revision_id": revision["proxy_revision_id"],
        "release_identity": manifest["release_identity"], "git_candidate": manifest["git_candidate"],
        "github_ci_evidence_sha256": manifest.get("github_ci_evidence_sha256"),
        "legacy_proxy_evidence_sha256": manifest.get("legacy_proxy_evidence_sha256"),
        "expected_deployment_identity": manifest["expected_deployment_identity"],
        "expected_deployment_identity_sha256": manifest["expected_deployment_identity_sha256"],
        "gateway_release_environment": _gateway_environment(manifest["release_identity"]),
        "proxy": {"config": revision["config_path"], "config_sha256": revision["config_sha256"], "upstream": revision["upstream"]},
    }


def _write_revision(run_dir: Path, manifest: dict[str, Any], *, upstream: str, kind: str, clock: Callable[[], str], restored_from: dict[str, str] | None = None) -> dict[str, Any]:
    attempt = len(manifest.get("proxy_revisions", [])) + 1
    previous_revision = manifest.get("proxy_revisions", [])[-1] if manifest.get("proxy_revisions") else None
    revision_id = _id("gclp-", manifest["run_id"], attempt, kind, upstream)
    config_path = f"proxy/revisions/{revision_id}.vercel.json"
    request_path = f"deploy-requests/{revision_id}.json"
    config = _proxy_config(upstream, revision_id)
    revision = {
        "proxy_revision_id": revision_id, "attempt_number": attempt, "kind": kind, "upstream": upstream,
        "prepared_at": clock(), "config_path": config_path, "config_sha256": _sha(_json_bytes(config)),
        "request_path": request_path, "request_sha256": None, "restored_from": restored_from,
        "expected_deployment_identity_sha256": manifest["expected_deployment_identity_sha256"],
        "builder": None, "deployment": None, "verifications": [], "current_verification_id": None,
    }
    if kind == "repair" and previous_revision and previous_revision.get("builder"):
        revision["builder"] = {**previous_revision["builder"], "reused_from_proxy_revision_id": previous_revision["proxy_revision_id"]}
    request = _request(manifest, revision)
    revision["request_sha256"] = _sha(_json_bytes(request))
    manifest.setdefault("proxy_revisions", []).append(revision)
    manifest["current_proxy_revision_id"] = revision_id
    manifest["proxy_upstream"] = upstream
    manifest["builder"] = revision["builder"]
    manifest["deployment"] = None
    manifest["verification"] = None
    manifest["deploy_request_sha256"] = revision["request_sha256"]
    manifest["vercel_config_sha256"] = revision["config_sha256"]
    _atomic_json(run_dir / config_path, config)
    _atomic_json(run_dir / request_path, request)
    _atomic_json(run_dir / "proxy" / "vercel.json", config)
    _atomic_json(run_dir / "deploy-request.json", request)
    return revision


def _current_material(manifest_path: Path, manifest: dict[str, Any]) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    revision = _revision(manifest)
    run_dir = manifest_path.parent
    request_path, expected_request = _revision_material(run_dir, manifest, revision)
    config_body = (run_dir / revision["config_path"]).read_bytes()
    request_body = request_path.read_bytes()
    if (
        manifest.get("deploy_request_sha256") != revision.get("request_sha256")
        or manifest.get("vercel_config_sha256") != revision.get("config_sha256")
        or (run_dir / "proxy" / "vercel.json").read_bytes() != config_body
        or (run_dir / "deploy-request.json").read_bytes() != request_body
    ):
        raise DeployError("current deploy request or Vercel config was changed or no longer matches its revision")
    return revision, request_path, expected_request


def _revision_material(run_dir: Path, manifest: dict[str, Any], revision: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    config_path = run_dir / revision["config_path"]
    request_path = run_dir / revision["request_path"]
    config_body = config_path.read_bytes()
    request_body = request_path.read_bytes()
    expected_config = _proxy_config(revision["upstream"], revision["proxy_revision_id"])
    expected_request = _request(manifest, revision)
    if (
        config_body != _json_bytes(expected_config) or _sha(config_body) != revision.get("config_sha256")
        or request_body != _json_bytes(expected_request) or _sha(request_body) != revision.get("request_sha256")
        or revision.get("expected_deployment_identity_sha256") != manifest.get("expected_deployment_identity_sha256")
    ):
        raise DeployError("deploy request or Vercel config was changed or no longer matches its revision")
    identity_path = run_dir / "evidence" / "expected-deployment-identity.json"
    if identity_path.read_bytes() != _json_bytes(manifest["expected_deployment_identity"]) or _sha(identity_path.read_bytes()) != manifest.get("expected_deployment_identity_sha256"):
        raise DeployError("expected deployment identity artifact was changed")
    ci_sha = manifest.get("github_ci_evidence_sha256")
    if ci_sha is not None:
        ci_path = run_dir / "evidence" / "github-ci.json"
        if _sha(ci_path.read_bytes()) != ci_sha or _read_object(ci_path) != manifest.get("github_ci_evidence"):
            raise DeployError("GitHub CI evidence artifact was changed")
    legacy_proxy_sha = manifest.get("legacy_proxy_evidence_sha256")
    if legacy_proxy_sha is not None:
        legacy_proxy_path = run_dir / "evidence" / "legacy-vercel.json"
        if _sha(legacy_proxy_path.read_bytes()) != legacy_proxy_sha:
            raise DeployError("legacy Vercel proxy evidence artifact was changed")
    deployment = revision.get("deployment")
    if deployment and deployment.get("status") == "succeeded":
        receipt = _read_object(run_dir / deployment.get("receipt", ""))
        _validate_deploy_receipt(receipt, manifest=manifest, revision=revision)
        expected_deployment = {**receipt, "receipt": deployment["receipt"], "secret_file_contents_read_by_orchestrator": False}
        if deployment != expected_deployment:
            raise DeployError("deployment manifest does not match its receipt")
    return request_path, expected_request


def _changes(home: Path, active: dict[str, Any] | None, identity: dict[str, str], upstream: str) -> dict[str, bool | None]:
    keys = ("git_commit", "instructions", "openapi", "gateway_artifact", "gateway_domain", "proxy_upstream")
    if active is None:
        return {key: None for key in keys}
    _, old = _manifest(home, active["run_id"])
    old_revision = _revision(old, active["proxy_revision_id"])
    current = old["release_identity"]
    return {
        "git_commit": current["git_commit"] != identity["git_commit"],
        "instructions": current["instructions_sha256"] != identity["instructions_sha256"],
        "openapi": current["openapi_sha256"] != identity["openapi_sha256"],
        "gateway_artifact": current["gateway_artifact_sha256"] != identity["gateway_artifact_sha256"],
        "gateway_domain": current["gateway_domain"] != identity["gateway_domain"],
        "proxy_upstream": old_revision["upstream"] != upstream,
    }


def prepare(*, home_path: Path, git_commit: str, main_ref: str = "origin/main", gateway_domain: str, proxy_upstream: str,
            github_ci_evidence: Path, expected_deployment_identity: dict[str, str], clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    if not COMMIT_RE.fullmatch(git_commit):
        raise DeployError("--git-commit must be one exact 40-character SHA")
    main_commit = _git_commit(main_ref)
    candidate = _git_commit(git_commit)
    if candidate != git_commit or candidate != main_commit:
        raise DeployError("candidate commit must exactly equal the resolved main ref")
    ci_source = _home(github_ci_evidence)
    ci_body = ci_source.read_bytes()
    ci = _ci_evidence(_read_object(ci_source), candidate)
    expected_environment = _deployment_identity(expected_deployment_identity)
    expected_environment_body = _json_bytes(expected_environment)
    public_origin = normalise_gateway_domain(gateway_domain)
    upstream = normalise_gateway_domain(proxy_upstream)
    bundled = bundle(candidate, public_origin)
    identity = release_identity(bundled)
    if not RELEASE_RE.fullmatch(identity["release_id"]):
        raise DeployError("release bundle produced a malformed release id")
    run_id = _run_id(identity["release_id"])
    run_dir = _run_dir(home, run_id)
    with _locked(home):
        active = _active(home)
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = _manifest(home, run_id)[1]
            expected = {
                "release_identity": identity,
                "git_candidate": {"commit": candidate, "main_ref": main_ref, "resolved_main_commit": main_commit},
                "github_ci_evidence": ci, "github_ci_evidence_sha256": _sha(ci_body),
                "expected_deployment_identity": expected_environment,
                "expected_deployment_identity_sha256": _sha(expected_environment_body),
            }
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise DeployError("existing run id is bound to different deployment inputs")
            current = _revision(manifest)
            _current_material(manifest_path, manifest)
            if current["upstream"] == upstream and current["kind"] == "prepare" and current.get("deployment") is None:
                return {"changed": False, "manifest": manifest}
            raise DeployError("use repair-proxy for another route attempt of an existing release")
        created_at = clock()
        manifest = {
            "schema_version": "2", "run_id": run_id, "release_identity": identity,
            "git_candidate": {"commit": candidate, "main_ref": main_ref, "resolved_main_commit": main_commit},
            "github_ci_evidence": ci, "github_ci_evidence_sha256": _sha(ci_body),
            "legacy_proxy_evidence_sha256": None,
            "expected_deployment_identity": expected_environment,
            "expected_deployment_identity_sha256": _sha(expected_environment_body), "prepared_at": created_at,
            "proxy_upstream": upstream, "current_proxy_revision_id": None, "proxy_revisions": [],
            "deploy_request_sha256": None, "vercel_config_sha256": None,
            "changes_from_active": _changes(home, active, identity, upstream),
            "builder": None, "deployment": None, "verification": None,
            "activations": [], "rollback_attempts": [], "adoption": None,
        }
        _write_revision(run_dir, manifest, upstream=upstream, kind="prepare", clock=clock)
        _atomic_json(run_dir / "bundle.json", bundled)
        _atomic_bytes(run_dir / "builder" / "expected-instructions.md", bundled["instructions"].encode())
        _atomic_bytes(run_dir / "builder" / "expected-openapi.yaml", bundled["openapi"].encode())
        _atomic_bytes(run_dir / "evidence" / "github-ci.json", ci_body)
        _atomic_bytes(run_dir / "evidence" / "expected-deployment-identity.json", expected_environment_body)
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def repair_proxy(*, home_path: Path, run_id: str, proxy_upstream: str, kind: str = "repair", restored_from: dict[str, str] | None = None,
                 clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    upstream = normalise_gateway_domain(proxy_upstream)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        _current_material(manifest_path, manifest)
        _write_revision(manifest_path.parent, manifest, upstream=upstream, kind=kind, restored_from=restored_from, clock=clock)
        active = _active(home)
        manifest["changes_from_active"] = _changes(home, active, manifest["release_identity"], upstream)
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def record_builder(*, home_path: Path, run_id: str, instructions_path: Path, openapi_path: Path, builder_evidence: Path,
                   clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    instructions = _home(instructions_path).read_text(encoding="utf-8")
    openapi = _home(openapi_path).read_text(encoding="utf-8")
    evidence_source = _home(builder_evidence)
    evidence_body = evidence_source.read_bytes()
    evidence = _read_object(evidence_source)
    evidence_required = {"schema_version", "producer", "gpt_id", "exported_at", "run_id", "proxy_revision_id", "instructions_sha256", "openapi_sha256"}
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, _, _ = _current_material(manifest_path, manifest)
        if set(evidence) != evidence_required or evidence.get("schema_version") != "1" or evidence.get("run_id") != run_id or evidence.get("proxy_revision_id") != revision["proxy_revision_id"] or any(not isinstance(evidence.get(key), str) or not evidence[key] for key in ("producer", "gpt_id", "exported_at")) or evidence.get("instructions_sha256") != sha256_text(instructions) or evidence.get("openapi_sha256") != sha256_text(openapi):
            raise DeployError("Builder evidence does not attest these exact exports, GPT identity, and proxy revision")
        identity = manifest["release_identity"]
        record = {"recorded_at": clock(), "instructions_sha256": sha256_text(instructions), "openapi_sha256": sha256_text(openapi),
                  "instructions_match": sha256_text(instructions) == identity["instructions_sha256"], "openapi_match": sha256_text(openapi) == identity["openapi_sha256"],
                  "producer": evidence["producer"], "gpt_id": evidence["gpt_id"], "exported_at": evidence["exported_at"],
                  "attestation": "builder-evidence.json", "attestation_sha256": _sha(evidence_body)}
        comparable = {key: value for key, value in record.items() if key != "recorded_at"}
        old = revision.get("builder")
        if old and {key: old.get(key) for key in comparable} == comparable:
            return {"changed": False, "manifest": manifest}
        _atomic_bytes(manifest_path.parent / "builder" / "recorded-instructions.md", instructions.encode())
        _atomic_bytes(manifest_path.parent / "builder" / "recorded-openapi.yaml", openapi.encode())
        _atomic_bytes(manifest_path.parent / "builder" / "builder-evidence.json", evidence_body)
        revision["builder"] = record
        revision["current_verification_id"] = None
        manifest["builder"] = record
        manifest["verification"] = None
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def _secret_file(path: Path) -> Path:
    resolved = _home(path)
    try:
        metadata = resolved.lstat()
    except OSError as exc:
        raise DeployError("secret env file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeployError("secret env file must be one regular, non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DeployError("secret env file must not be accessible by group or other users")
    return resolved


class SubprocessRunner:
    def __init__(self, executable: str): self.executable = executable
    def __call__(self, request: Path, secret_file: Path, receipt: Path) -> None:
        subprocess.run([self.executable, "--request", str(request), "--secret-env-file", str(secret_file), "--receipt", str(receipt)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _validate_deploy_receipt(value: dict[str, Any], *, manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "provider", "target", "run_id", "release_id", "proxy_revision_id", "request_sha256", "config_sha256", "deployment_id", "url", "status", "deployed_at", "certifies"}
    if (
        set(value) != required or value.get("schema_version") != "2" or value.get("provider") != "vercel" or value.get("target") != "production"
        or value.get("run_id") != manifest["run_id"] or value.get("release_id") != manifest["release_identity"]["release_id"]
        or value.get("proxy_revision_id") != revision["proxy_revision_id"] or value.get("request_sha256") != revision["request_sha256"]
        or value.get("config_sha256") != revision["config_sha256"] or value.get("status") != "succeeded" or value.get("certifies") != DEPLOY_CERTIFIES
        or not isinstance(value.get("deployment_id"), str) or not value["deployment_id"]
        or not isinstance(value.get("url"), str) or not value["url"].startswith("https://")
        or not isinstance(value.get("deployed_at"), str) or not value["deployed_at"]
    ):
        raise DeployError("deployment receipt does not certify this exact Vercel production request")
    return value


def _record_receipt(manifest_path: Path, manifest: dict[str, Any], revision: dict[str, Any], receipt: dict[str, Any]) -> None:
    receipt_path = f"deployment-receipts/{revision['proxy_revision_id']}.json"
    _atomic_json(manifest_path.parent / receipt_path, receipt)
    deployment = {**receipt, "receipt": receipt_path, "secret_file_contents_read_by_orchestrator": False}
    revision["deployment"] = deployment
    revision["current_verification_id"] = None
    manifest["deployment"] = deployment
    manifest["verification"] = None
    _atomic_json(manifest_path, manifest)


def deploy_proxy(*, home_path: Path, run_id: str, secret_env_file: Path, runner: Callable[[Path, Path, Path], None]) -> dict[str, Any]:
    home = _home(home_path)
    secret = _secret_file(secret_env_file)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, request_path, _ = _current_material(manifest_path, manifest)
        if (revision.get("deployment") or {}).get("status") == "succeeded":
            return {"changed": False, "manifest": manifest}
        temporary = manifest_path.parent / ".runner-receipt.json"
        if temporary.exists(): temporary.unlink()
        try:
            runner(request_path, secret, temporary)
            receipt = _validate_deploy_receipt(_read_object(temporary), manifest=manifest, revision=revision)
        finally:
            if temporary.exists(): temporary.unlink()
        _record_receipt(manifest_path, manifest, revision, receipt)
        return {"changed": True, "manifest": manifest}


def record_deployment(*, home_path: Path, run_id: str, receipt_path: Path) -> dict[str, Any]:
    home = _home(home_path)
    source = _home(receipt_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, _, _ = _current_material(manifest_path, manifest)
        receipt = _validate_deploy_receipt(_read_object(source), manifest=manifest, revision=revision)
        if revision.get("deployment") == {**receipt, "receipt": f"deployment-receipts/{revision['proxy_revision_id']}.json", "secret_file_contents_read_by_orchestrator": False}:
            return {"changed": False, "manifest": manifest}
        _record_receipt(manifest_path, manifest, revision, receipt)
        return {"changed": True, "manifest": manifest}


def _browser_evidence(path: Path) -> tuple[dict[str, Any], str]:
    source = _home(path)
    body = source.read_bytes()
    value = _read_object(source)
    required = {"schema_version", "producer", "observed_at", "gpt_id", "conversation_ref", "artifact_kind", "status"}
    if set(value) != required or value.get("schema_version") != "1" or value.get("status") != "passed" or value.get("artifact_kind") not in {"browser-receipt", "browser-screenshot-manifest"} or any(not isinstance(value.get(key), str) or not value[key] for key in ("producer", "observed_at", "gpt_id", "conversation_ref")):
        raise DeployError("browser evidence artifact is malformed")
    return value, _sha(body)


def _smoke(path: Path, *, manifest: dict[str, Any], revision: dict[str, Any], browser: dict[str, Any], browser_sha: str) -> tuple[dict[str, Any], str]:
    source = _home(path)
    body = source.read_bytes()
    try: value = json.loads(body)
    except json.JSONDecodeError as exc: raise DeployError("smoke evidence must be valid JSON") from exc
    deployment = revision.get("deployment") or {}
    required = {"schema_version", "run_id", "release_id", "proxy_revision_id", "request_sha256", "deployment_id", "expected_deployment_identity", "producer", "browser_evidence_ref", "browser_evidence_sha256", "status", "observed_at", "certifies"}
    if (
        not isinstance(value, dict) or set(value) != required or value.get("schema_version") != "2"
        or value.get("run_id") != manifest["run_id"] or value.get("release_id") != manifest["release_identity"]["release_id"]
        or value.get("proxy_revision_id") != revision["proxy_revision_id"] or value.get("request_sha256") != revision["request_sha256"]
        or value.get("deployment_id") != deployment.get("deployment_id") or value.get("expected_deployment_identity") != manifest["expected_deployment_identity"]
        or value.get("producer") != browser["producer"] or value.get("browser_evidence_ref") != browser["conversation_ref"]
        or value.get("browser_evidence_sha256") != browser_sha or value.get("observed_at") != browser["observed_at"]
        or value.get("status") != "passed" or value.get("certifies") != SMOKE_CERTIFIES or not isinstance(value.get("observed_at"), str) or not value["observed_at"]
    ):
        raise DeployError("smoke evidence does not certify this exact release, route, request, deployment, and environment")
    return value, _sha(body)


def _public_route(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 - exact HTTPS URL is release-bound
        body = json.loads(response.read())
        return {"status": response.status, "url": response.geturl(), "headers": dict(response.headers.items()), "body": body}


def _verify_builder_parity(verifier: Callable[..., dict[str, Any]], *, run_dir: Path, receipt_path: Path) -> dict[str, Any]:
    kwargs = {
        "bundle_path": run_dir / "bundle.json",
        "builder_instructions_path": run_dir / "builder" / "recorded-instructions.md",
        "builder_openapi_path": run_dir / "builder" / "recorded-openapi.yaml",
        "receipt_path": receipt_path,
    }
    kwargs["expected_deployment_identity_path"] = run_dir / "evidence" / "expected-deployment-identity.json"
    return verifier(**kwargs)


def _route(route_checker: Callable[[str], dict[str, Any]], *, manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    url = manifest["release_identity"]["gateway_domain"] + "/healthz"
    result = route_checker(url)
    headers = result.get("headers") if isinstance(result, dict) else None
    body = result.get("body") if isinstance(result, dict) else None
    marker = next((value for key, value in headers.items() if key.lower() == ROUTE_MARKER_HEADER.lower()), None) if isinstance(headers, dict) else None
    if result.get("status") != 200 or result.get("url") != url or marker != revision["proxy_revision_id"] or not isinstance(body, dict) or body.get("status") != "ok" or body.get("release_identity") != manifest["release_identity"] or body.get("deployment_identity") != manifest["expected_deployment_identity"]:
        raise DeployError("public stable /healthz does not prove the current proxy revision and deployment identity")
    return {"url": url, "marker": marker, "release_identity": body["release_identity"], "deployment_identity": body["deployment_identity"]}


def verify(*, home_path: Path, run_id: str, smoke_evidence: Path, browser_evidence: Path, verifier: Callable[..., dict[str, Any]] = verify_release,
           route_checker: Callable[[str], dict[str, Any]] = _public_route, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, _, _ = _current_material(manifest_path, manifest)
        deployment = revision.get("deployment")
        if not deployment or deployment.get("status") != "succeeded":
            raise DeployError("current proxy revision deployment receipt is required before verification")
        builder = revision.get("builder")
        if not builder or not builder.get("instructions_match") or not builder.get("openapi_match"):
            raise DeployError("fresh or explicitly reusable Builder exports for this proxy revision are required")
        if _sha((manifest_path.parent / "builder" / builder.get("attestation", "")).read_bytes()) != builder.get("attestation_sha256"):
            raise DeployError("Builder evidence attestation changed after recording")
        browser, browser_sha = _browser_evidence(browser_evidence)
        smoke, smoke_sha = _smoke(smoke_evidence, manifest=manifest, revision=revision, browser=browser, browser_sha=browser_sha)
        route = _route(route_checker, manifest=manifest, revision=revision)
        run_dir = manifest_path.parent
        temporary = run_dir / ".parity-receipt.json"
        if temporary.exists(): temporary.unlink()
        try:
            parity = _verify_builder_parity(verifier, run_dir=run_dir, receipt_path=temporary)
        finally:
            if temporary.exists(): temporary.unlink()
        if parity.get("release_identity") != manifest["release_identity"] or parity.get("deployment_identity") != manifest["expected_deployment_identity"]:
            raise DeployError("Builder parity verifier did not certify the exact release and deployment identity")
        parity_sha = _sha(_json_bytes(parity))
        prior = next((item for item in revision["verifications"] if item.get("consumed_by_activation_id") is None and item.get("smoke_evidence_sha256") == smoke_sha and item.get("parity_receipt_sha256") == parity_sha and item.get("route") == route and item.get("deployment_id") == deployment["deployment_id"]), None)
        if prior:
            manifest["verification"] = prior
            revision["current_verification_id"] = prior["verification_id"]
            _verification_for_activation(run_dir, manifest, revision)
            return {"changed": False, "manifest": manifest}
        number = sum(len(item.get("verifications", [])) for item in manifest["proxy_revisions"]) + 1
        verification_id = _id("gclv-", run_id, number, revision["proxy_revision_id"], deployment["deployment_id"], smoke_sha, parity_sha)
        parity_path = f"parity-receipts/{verification_id}.json"
        smoke_path = f"smoke-evidence/{verification_id}.json"
        browser_path = f"browser-evidence/{verification_id}.json"
        _atomic_json(run_dir / parity_path, parity)
        _atomic_bytes(run_dir / smoke_path, _home(smoke_evidence).read_bytes())
        _atomic_bytes(run_dir / browser_path, _home(browser_evidence).read_bytes())
        verification = {
            "verification_id": verification_id, "status": "passed", "verified_at": clock(), "proxy_revision_id": revision["proxy_revision_id"],
            "request_sha256": revision["request_sha256"], "config_sha256": revision["config_sha256"], "deployment_id": deployment["deployment_id"],
            "deployment_receipt_sha256": _sha((run_dir / deployment["receipt"]).read_bytes()), "route": route,
            "builder_hashes": {"instructions_sha256": builder["instructions_sha256"], "openapi_sha256": builder["openapi_sha256"]},
            "parity_receipt": parity_path, "parity_receipt_sha256": parity_sha, "smoke_evidence": smoke_path, "smoke_evidence_sha256": smoke_sha,
            "smoke_observed_at": smoke["observed_at"], "smoke_producer": smoke["producer"], "browser_evidence_ref": smoke["browser_evidence_ref"],
            "browser_evidence": browser_path, "browser_evidence_sha256": browser_sha, "browser_gpt_id": browser["gpt_id"],
            "consumed_by_activation_id": None,
        }
        revision["verifications"].append(verification)
        revision["current_verification_id"] = verification_id
        manifest["verification"] = verification
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def _verification_for_activation(run_dir: Path, manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    verification = next((item for item in revision.get("verifications", []) if item.get("verification_id") == revision.get("current_verification_id")), None)
    deployment = revision.get("deployment") or {}
    builder = revision.get("builder") or {}
    if (
        not verification or verification.get("status") != "passed"
        or not VERIFICATION_RE.fullmatch(str(verification.get("verification_id", "")))
        or verification.get("proxy_revision_id") != revision["proxy_revision_id"]
        or verification.get("request_sha256") != revision["request_sha256"]
        or verification.get("config_sha256") != revision["config_sha256"]
        or verification.get("deployment_id") != deployment.get("deployment_id")
        or verification.get("builder_hashes") != {"instructions_sha256": builder.get("instructions_sha256"), "openapi_sha256": builder.get("openapi_sha256")}
        or verification.get("route") != {"url": manifest["release_identity"]["gateway_domain"] + "/healthz", "marker": revision["proxy_revision_id"], "release_identity": manifest["release_identity"], "deployment_identity": manifest["expected_deployment_identity"]}
        or not isinstance(verification.get("smoke_evidence_sha256"), str)
        or not isinstance(verification.get("browser_evidence_sha256"), str)
        or not isinstance(verification.get("browser_evidence_ref"), str)
        or not verification["browser_evidence_ref"]
    ):
        raise DeployError("current verification is malformed or not bound to current deployment evidence")
    parity_path = run_dir / verification["parity_receipt"]
    if _sha(parity_path.read_bytes()) != verification.get("parity_receipt_sha256"):
        raise DeployError("Builder parity receipt changed after verification")
    if _sha((run_dir / verification["smoke_evidence"]).read_bytes()) != verification.get("smoke_evidence_sha256") or _sha((run_dir / verification["browser_evidence"]).read_bytes()) != verification.get("browser_evidence_sha256"):
        raise DeployError("smoke or browser evidence changed after verification")
    receipt_path = run_dir / deployment["receipt"]
    if _sha(receipt_path.read_bytes()) != verification.get("deployment_receipt_sha256"):
        raise DeployError("deployment receipt changed after verification")
    return verification


def activate(*, home_path: Path, run_id: str, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, _, _ = _current_material(manifest_path, manifest)
        current = _active(home)
        if current and current["run_id"] == run_id and current["proxy_revision_id"] == revision["proxy_revision_id"]:
            return {"changed": False, "manifest": manifest, "active": current}
        verification = _verification_for_activation(manifest_path.parent, manifest, revision)
        if verification.get("consumed_by_activation_id") is not None:
            raise DeployError("activation requires one current, passing, unconsumed verification")
        if current is None and not manifest.get("adoption"):
            raise DeployError("adopt the currently verified production release before the first activation")
        previous = None if current is None else {key: current[key] for key in ("run_id", "proxy_revision_id", "activation_id")}
        number = sum(len(_manifest(home, item.parent.name)[1].get("activations", [])) for item in (home / "runs").glob("*/manifest.json")) + 1
        activation_id = _id("gcla-", run_id, revision["proxy_revision_id"], verification["verification_id"], number)
        activated_at = clock()
        activation = {"activation_id": activation_id, "proxy_revision_id": revision["proxy_revision_id"], "verification_id": verification["verification_id"], "activated_at": activated_at, "previous": previous, "adopted": False}
        verification["consumed_by_activation_id"] = activation_id
        manifest["verification"] = verification
        manifest["activations"].append(activation)
        pointer = {"schema_version": "2", "run_id": run_id, "release_id": manifest["release_identity"]["release_id"], "proxy_revision_id": revision["proxy_revision_id"], "activation_id": activation_id, "verification_id": verification["verification_id"], "activated_at": activated_at, "previous": previous}
        _atomic_json(manifest_path, manifest)
        _atomic_json(home / "active.json", pointer)
        return {"changed": True, "manifest": manifest, "active": pointer}


def rollback(*, home_path: Path, source_run_id: str, record: bool = False, clock: Callable[[], str] = _now) -> dict[str, Any]:
    """Create a fresh restore revision; it still requires deploy, verify, and activate."""
    home = _home(home_path)
    with _locked(home):
        current = _active(home)
        if not current or current["run_id"] != source_run_id:
            raise DeployError("rollback source is not the active run")
        previous = _activation_ref(current["previous"], allow_none=False)
        assert previous is not None
        _, target = _manifest(home, previous["run_id"])
        old_revision = _revision(target, previous["proxy_revision_id"])
        plan = {"schema_version": "2", "source": {key: current[key] for key in ("run_id", "proxy_revision_id", "activation_id")}, "target_previous": previous,
                "target_upstream": old_revision["upstream"], "planned_at": clock(), "live_state_changed": False,
                "next": "create a fresh restore revision, deploy it, verify current public evidence, then activate"}
        if not record:
            return {"changed": False, "recorded": False, "plan": plan, "active": current}
    restored = repair_proxy(home_path=home, run_id=previous["run_id"], proxy_upstream=old_revision["upstream"], kind="restore", restored_from=previous, clock=clock)
    with _locked(home):
        source_path, source = _manifest(home, source_run_id)
        new_revision = _revision(restored["manifest"])
        attempt = {**plan, "restore_proxy_revision_id": new_revision["proxy_revision_id"], "recorded_at": clock()}
        source.setdefault("rollback_attempts", []).append(attempt)
        _atomic_json(source_path, source)
        return {"changed": True, "recorded": True, "plan": attempt, "active": _active(home), "restore_manifest": restored["manifest"]}


def _legacy_smoke(path: Path, identity: dict[str, str]) -> tuple[dict[str, str], str]:
    body = _home(path).read_bytes()
    try: value = json.loads(body)
    except json.JSONDecodeError as exc: raise DeployError("legacy live smoke must be valid JSON") from exc
    checks = value.get("checks") if isinstance(value, dict) else None
    writes = value.get("writes_during_smoke") if isinstance(value, dict) else None
    required_writes = {"plan_modified", "provider_publish_requested", "provider_withdraw_requested"}
    if not isinstance(checks, dict) or not isinstance(writes, dict) or value.get("schema_version") != "1" or value.get("release_id") != identity["release_id"] or value.get("git_commit") != identity["git_commit"] or not value.get("observed_at") or any(checks.get(key) != "passed" for key in ("release_gate", "start_coach_session", "fresh_conversation_today_coaching")) or not required_writes.issubset(writes) or any(writes[key] is not False for key in required_writes):
        raise DeployError("legacy live smoke does not prove a safe passing user-visible check")
    return {"observed_at": value["observed_at"], "certifies": "legacy user-visible Custom GPT smoke only"}, _sha(body)


def _legacy_builder_parity(*, bundle_path: Path, builder_instructions_path: Path, builder_openapi_path: Path, receipt_path: Path) -> dict[str, Any]:
    bundled = _read_object(bundle_path)
    identity = release_identity(bundled)
    if sha256_text(builder_instructions_path.read_text(encoding="utf-8")) != identity["instructions_sha256"] or sha256_text(builder_openapi_path.read_text(encoding="utf-8")) != identity["openapi_sha256"]:
        raise DeployError("legacy Builder exports do not match the exact release identity")
    return {"schema_version": "legacy-bootstrap-1", "release_identity": identity, "certifies": "legacy Builder parity only; no deployment identity"}


def _legacy_route(route_checker: Callable[[str], dict[str, Any]], identity: dict[str, str]) -> dict[str, Any]:
    url = identity["gateway_domain"] + "/healthz"
    result = route_checker(url)
    body = result.get("body") if isinstance(result, dict) else None
    if result.get("status") != 200 or result.get("url") != url or not isinstance(body, dict) or body.get("status") != "ok" or body.get("release_identity") != identity:
        raise DeployError("legacy public /healthz does not prove the exact current release identity")
    return {"url": url, "release_identity": identity, "certifies": "legacy route only; no proxy marker or deployment identity"}


def _legacy_proxy_config(path: Path, expected_upstream: str) -> tuple[dict[str, Any], bytes]:
    source = _home(path)
    body = source.read_bytes()
    value = _read_object(source)
    rewrites = value.get("rewrites")
    if not isinstance(rewrites, list):
        raise DeployError("legacy Vercel config has no exact rewrite evidence")
    candidates = [item for item in rewrites if isinstance(item, dict) and item.get("source") == "/:path*" and isinstance(item.get("destination"), str) and item["destination"].endswith("/:path*")]
    if len(candidates) != 1 or normalise_gateway_domain(candidates[0]["destination"][:-7]) != expected_upstream:
        raise DeployError("legacy Vercel config does not prove the declared current proxy upstream")
    return value, body


def adopt_active(*, home_path: Path, legacy_dir: Path, current_proxy_upstream: str, expected_deployment_identity: dict[str, str],
                 current_proxy_config: Path,
                 verifier: Callable[..., dict[str, Any]] = _legacy_builder_parity,
                 route_checker: Callable[[str], dict[str, Any]] = _public_route, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path); legacy = _home(legacy_dir); upstream = normalise_gateway_domain(current_proxy_upstream)
    legacy_proxy, legacy_proxy_body = _legacy_proxy_config(current_proxy_config, upstream)
    bundled = _read_object(legacy / "builder-bundle.json"); identity = release_identity(bundled)
    if sha256_text(str(bundled.get("instructions", ""))) != identity["instructions_sha256"] or sha256_text(str(bundled.get("openapi", ""))) != identity["openapi_sha256"]:
        raise DeployError("legacy bundle content does not match its release identity")
    if _read_object(legacy / "release-receipt.json") != {"schema_version": "1", "release_identity": identity, "certifies": "gateway artifact and Builder content parity only"}:
        raise DeployError("legacy release receipt does not certify the exact bundle")
    smoke, smoke_sha = _legacy_smoke(legacy / "live-smoke.json", identity)
    legacy_route = _legacy_route(route_checker, identity)
    run_id = _run_id(identity["release_id"]); expected_environment = _deployment_identity(expected_deployment_identity)
    with _locked(home):
        current = _active(home)
        if current:
            if current["run_id"] == run_id: return {"changed": False, "active": current, "manifest": _manifest(home, run_id)[1]}
            raise DeployError("an active release is already adopted")
        run_dir = _run_dir(home, run_id); run_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(run_dir / "evidence" / "expected-deployment-identity.json", expected_environment)
        parity = verifier(bundle_path=legacy / "builder-bundle.json", builder_instructions_path=legacy / "builder-instructions.md", builder_openapi_path=legacy / "builder-openapi.yaml", receipt_path=run_dir / ".adopt-parity-receipt.json")
        adopted_at = clock()
        manifest = {"schema_version": "2", "run_id": run_id, "release_identity": identity,
                    "git_candidate": {"commit": identity["git_commit"], "main_ref": "legacy-adopt", "resolved_main_commit": identity["git_commit"]},
                    "github_ci_evidence": None, "github_ci_evidence_sha256": None, "expected_deployment_identity": expected_environment,
                    "expected_deployment_identity_sha256": _sha(_json_bytes(expected_environment)),
                    "legacy_proxy_evidence_sha256": _sha(legacy_proxy_body),
                    "prepared_at": adopted_at, "proxy_upstream": upstream, "current_proxy_revision_id": None, "proxy_revisions": [],
                    "deploy_request_sha256": None, "vercel_config_sha256": None,
                    "changes_from_active": {key: None for key in ("git_commit", "instructions", "openapi", "gateway_artifact", "gateway_domain", "proxy_upstream")},
                    "builder": {"recorded_at": adopted_at, "instructions_sha256": identity["instructions_sha256"], "openapi_sha256": identity["openapi_sha256"], "instructions_match": True, "openapi_match": True, "producer": "legacy-bootstrap", "gpt_id": "unknown-legacy-gpt", "exported_at": adopted_at, "attestation": "release-receipt.json", "attestation_sha256": _sha((legacy / "release-receipt.json").read_bytes())},
                    "deployment": None, "verification": None, "activations": [], "rollback_attempts": [], "adoption": {"adopted_at": adopted_at, "legacy_layout": True}}
        legacy_builder = manifest["builder"]
        revision = _write_revision(run_dir, manifest, upstream=upstream, kind="adopt", clock=clock)
        revision["legacy_proxy_evidence_sha256"] = _sha(legacy_proxy_body)
        deployment = {"schema_version": "legacy", "provider": "vercel", "target": "production", "run_id": run_id, "release_id": identity["release_id"], "proxy_revision_id": revision["proxy_revision_id"], "request_sha256": revision["request_sha256"], "config_sha256": revision["config_sha256"], "deployment_id": "legacy-adopted-production", "url": identity["gateway_domain"], "status": "legacy-external-verified", "deployed_at": adopted_at, "certifies": "legacy external production observation", "receipt": "release-receipt.json", "secret_file_contents_read_by_orchestrator": False}
        revision["deployment"] = deployment; manifest["deployment"] = deployment
        revision["builder"] = legacy_builder; manifest["builder"] = legacy_builder
        verification_id = _id("gclv-", run_id, "adopt", smoke_sha)
        activation_id = _id("gcla-", run_id, revision["proxy_revision_id"], verification_id, "adopt")
        verification = {"verification_id": verification_id, "status": "legacy-adopted", "verified_at": adopted_at, "proxy_revision_id": revision["proxy_revision_id"], "request_sha256": revision["request_sha256"], "config_sha256": revision["config_sha256"], "deployment_id": deployment["deployment_id"], "deployment_receipt_sha256": _sha((legacy / "release-receipt.json").read_bytes()), "route": legacy_route, "builder_hashes": {"instructions_sha256": identity["instructions_sha256"], "openapi_sha256": identity["openapi_sha256"]}, "parity_receipt": "parity-receipt.json", "parity_receipt_sha256": _sha(_json_bytes(parity)), "smoke_evidence_sha256": smoke_sha, "smoke_observed_at": smoke["observed_at"], "smoke_producer": "legacy-adoption", "browser_evidence_ref": "legacy/live-smoke.json", "browser_evidence_sha256": smoke_sha, "browser_gpt_id": "unknown-legacy-gpt", "consumed_by_activation_id": activation_id}
        revision["verifications"].append(verification); revision["current_verification_id"] = verification_id; manifest["verification"] = verification
        activation = {"activation_id": activation_id, "proxy_revision_id": revision["proxy_revision_id"], "verification_id": verification_id, "activated_at": adopted_at, "previous": None, "adopted": True}
        manifest["activations"].append(activation)
        pointer = {"schema_version": "2", "run_id": run_id, "release_id": identity["release_id"], "proxy_revision_id": revision["proxy_revision_id"], "activation_id": activation_id, "verification_id": verification_id, "activated_at": adopted_at, "previous": None}
        _atomic_json(run_dir / "bundle.json", bundled); _atomic_json(run_dir / "release-receipt.json", _read_object(legacy / "release-receipt.json")); _atomic_json(run_dir / "parity-receipt.json", parity)
        _atomic_bytes(run_dir / "evidence" / "legacy-vercel.json", legacy_proxy_body)
        _atomic_bytes(run_dir / "builder" / "expected-instructions.md", bundled["instructions"].encode()); _atomic_bytes(run_dir / "builder" / "expected-openapi.yaml", bundled["openapi"].encode())
        _atomic_bytes(run_dir / "builder" / "recorded-instructions.md", (legacy / "builder-instructions.md").read_bytes()); _atomic_bytes(run_dir / "builder" / "recorded-openapi.yaml", (legacy / "builder-openapi.yaml").read_bytes())
        _atomic_json(run_dir / "manifest.json", manifest); _atomic_json(home / "active.json", pointer)
        return {"changed": True, "active": pointer, "manifest": manifest}


def status(*, home_path: Path, run_id: str | None = None) -> dict[str, Any]:
    home = _home(home_path)
    if not home.exists(): return {"schema_version": "2", "active": None, "runs": []}
    active = _active(home)
    if run_id:
        manifest_path, manifest = _manifest(home, run_id); _current_material(manifest_path, manifest)
        revision = _revision(manifest)
        current_verification = next((item for item in revision.get("verifications", []) if item.get("verification_id") == revision.get("current_verification_id")), None)
        if current_verification and current_verification.get("status") == "passed":
            _verification_for_activation(manifest_path.parent, manifest, revision)
        return {"schema_version": "2", "active": active, "run": manifest}
    runs = []
    for path in sorted((home / "runs").glob("*/manifest.json")) if (home / "runs").exists() else []:
        manifest_path, value = _manifest(home, path.parent.name); revision, _, _ = _current_material(manifest_path, value)
        runs.append({"run_id": value["run_id"], "release_id": value["release_identity"]["release_id"], "prepared_at": value["prepared_at"], "builder_recorded": revision.get("builder") is not None, "deployed": (revision.get("deployment") or {}).get("status") == "succeeded", "verified": any(item.get("status") == "passed" and item.get("consumed_by_activation_id") is None for item in revision.get("verifications", [])), "active": bool(active and active["run_id"] == value["run_id"] and active["proxy_revision_id"] == revision["proxy_revision_id"])})
    return {"schema_version": "2", "active": active, "runs": runs}


def _load_identity(path: Path) -> dict[str, str]: return _deployment_identity(_read_object(_home(path)))
def _print(value: dict[str, Any]) -> None: print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--home", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status"); show.add_argument("--run-id")
    prep = commands.add_parser("prepare"); prep.add_argument("--git-commit", required=True); prep.add_argument("--gateway-domain", required=True); prep.add_argument("--proxy-upstream", required=True); prep.add_argument("--github-ci-evidence", required=True); prep.add_argument("--expected-deployment-identity", required=True)
    repair = commands.add_parser("repair-proxy"); repair.add_argument("--run-id", required=True); repair.add_argument("--proxy-upstream", required=True)
    adopt = commands.add_parser("adopt-active"); adopt.add_argument("--legacy-dir", required=True); adopt.add_argument("--current-proxy-upstream", required=True); adopt.add_argument("--current-proxy-config", required=True); adopt.add_argument("--expected-deployment-identity", required=True); adopt.add_argument("--confirm-live-check", action="store_true")
    builder = commands.add_parser("record-builder"); builder.add_argument("--run-id", required=True); builder.add_argument("--builder-instructions", required=True); builder.add_argument("--builder-openapi", required=True); builder.add_argument("--builder-evidence", required=True)
    deploy = commands.add_parser("run-deployment-adapter"); deploy.add_argument("--run-id", required=True); deploy.add_argument("--secret-env-file", required=True); deploy.add_argument("--runner", required=True); deploy.add_argument("--confirm", action="store_true")
    recorded = commands.add_parser("record-deployment"); recorded.add_argument("--run-id", required=True); recorded.add_argument("--receipt", required=True)
    check = commands.add_parser("verify"); check.add_argument("--run-id", required=True); check.add_argument("--smoke-evidence", required=True); check.add_argument("--browser-evidence", required=True); check.add_argument("--confirm-live-check", action="store_true")
    active = commands.add_parser("activate"); active.add_argument("--run-id", required=True); active.add_argument("--confirm", action="store_true")
    undo = commands.add_parser("rollback"); undo.add_argument("--run-id", required=True); undo.add_argument("--confirm", action="store_true")
    args = parser.parse_args(); home = Path(args.home)
    try:
        if args.command == "status": result = status(home_path=home, run_id=args.run_id)
        elif args.command == "prepare": result = prepare(home_path=home, git_commit=args.git_commit, main_ref="origin/main", gateway_domain=args.gateway_domain, proxy_upstream=args.proxy_upstream, github_ci_evidence=Path(args.github_ci_evidence), expected_deployment_identity=_load_identity(Path(args.expected_deployment_identity)))
        elif args.command == "repair-proxy": result = repair_proxy(home_path=home, run_id=args.run_id, proxy_upstream=args.proxy_upstream)
        elif args.command == "adopt-active" and not args.confirm_live_check: result = {"plan": "no network request or active-pointer write made; re-run with --confirm-live-check"}
        elif args.command == "adopt-active": result = adopt_active(home_path=home, legacy_dir=Path(args.legacy_dir), current_proxy_upstream=args.current_proxy_upstream, current_proxy_config=Path(args.current_proxy_config), expected_deployment_identity=_load_identity(Path(args.expected_deployment_identity)))
        elif args.command == "record-builder": result = record_builder(home_path=home, run_id=args.run_id, instructions_path=Path(args.builder_instructions), openapi_path=Path(args.builder_openapi), builder_evidence=Path(args.builder_evidence))
        elif args.command == "run-deployment-adapter" and not args.confirm:
            manifest_path, manifest = _manifest(_home(home), args.run_id); _, request_path, _ = _current_material(manifest_path, manifest)
            result = {"plan": "no command executed; re-run with --confirm", "command": [args.runner, "--request", str(request_path), "--secret-env-file", str(Path(args.secret_env_file).expanduser()), "--receipt", str(manifest_path.parent / ".runner-receipt.json")], "secret_contents_read_by_orchestrator": False}
        elif args.command == "run-deployment-adapter": result = deploy_proxy(home_path=home, run_id=args.run_id, secret_env_file=Path(args.secret_env_file), runner=SubprocessRunner(args.runner))
        elif args.command == "record-deployment": result = record_deployment(home_path=home, run_id=args.run_id, receipt_path=Path(args.receipt))
        elif args.command == "verify" and not args.confirm_live_check:
            manifest = status(home_path=home, run_id=args.run_id)["run"]; result = {"plan": "no network request made; re-run with --confirm-live-check", "health_url": manifest["release_identity"]["gateway_domain"] + "/healthz", "run_id": args.run_id}
        elif args.command == "verify": result = verify(home_path=home, run_id=args.run_id, smoke_evidence=Path(args.smoke_evidence), browser_evidence=Path(args.browser_evidence))
        elif args.command == "activate" and not args.confirm: result = {"plan": "active pointer unchanged; re-run with --confirm", "run_id": args.run_id}
        elif args.command == "activate": result = activate(home_path=home, run_id=args.run_id)
        elif args.command == "rollback": result = rollback(home_path=home, source_run_id=args.run_id, record=args.confirm)
        _print(result); return 0
    except (DeployError, ReleaseIdentityError, subprocess.CalledProcessError, OSError, UnicodeError) as exc:
        print(f"deploy orchestrator blocked: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
