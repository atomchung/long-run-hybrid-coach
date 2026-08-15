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
import time
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
    deployment_identity as strict_deployment_identity,
    normalise_gateway_domain,
    release_identity,
    sha256_text,
)
from scripts.custom_gpt_release import bundle, outside_repo, verify_release  # noqa: E402
from scripts.custom_gpt_deploy_providers import (  # noqa: E402
    GitHubProviderReader,
    ProviderReadbackError,
    VercelRestProviderReader,
    canonical_sha256,
    load_production_target,
    normalize_vercel_create_attestation,
    production_target_binding,
    validate_github_provider_receipt,
    validate_production_target,
    validate_vercel_create_attestation,
    validate_vercel_provider_receipt,
)

RUN_RE = re.compile(r"^gcld-[0-9a-f]{64}$")
RELEASE_RE = re.compile(r"^gclr-[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^gclp-[0-9a-f]{64}$")
CREATE_ATTEMPT_RE = re.compile(r"^gclc-[0-9a-f]{64}$")
VERIFICATION_RE = re.compile(r"^gclv-[0-9a-f]{64}$")
ACTIVATION_RE = re.compile(r"^gcla-[0-9a-f]{64}$")
SMOKE_CERTIFIES = "user-visible Custom GPT smoke only"
DEPLOY_CERTIFIES = "Vercel provider read-back and exact local release request binding"
ROUTE_MARKER_HEADER = "X-GCL-Proxy-Revision"
BROWSER_EVIDENCE_PRODUCERS = frozenset({
    "codex-browser-client-v1",
    "codex-browser-smoke-v1",
    "chrome-manual-attestation-v1",
})
BUILDER_EVIDENCE_PRODUCERS = frozenset({
    "custom-gpt-builder-export",
    "codex-browser-client-v1",
    "chrome-manual-attestation-v1",
})


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


def _origin_repository() -> str:
    try:
        value = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"], cwd=ROOT,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise DeployError("cannot resolve the canonical GitHub origin") from exc
    match = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", value)
    if match is None:
        raise DeployError("origin is not one canonical GitHub repository")
    return match.group(1)


def _run_id(release_id: str) -> str:
    return _id("gcld-", release_id)


def _run_dir(home: Path, run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        raise DeployError("run id is malformed")
    return home / "runs" / run_id


def _deployment_identity(value: Any) -> dict[str, str]:
    try:
        return strict_deployment_identity(value)
    except ReleaseIdentityError as exc:
        raise DeployError("expected deployment identity is malformed") from exc


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
    if value.get("production_target") is not None:
        try:
            target = validate_production_target(value["production_target"])
        except ProviderReadbackError as exc:
            raise DeployError("deploy manifest production target is invalid") from exc
        if value.get("production_target_binding_sha256") != target["binding_sha256"]:
            raise DeployError("deploy manifest production target binding changed")
    elif value.get("adoption") is not None:
        gpt_id = value.get("production_gpt_id")
        gpt_path = path.parent / "evidence" / "production-gpt.json"
        gpt_body = gpt_path.read_bytes()
        if (
            not isinstance(gpt_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", gpt_id)
            or _read_object(gpt_path) != {"schema_version": "1", "gpt_id": gpt_id}
            or _sha(gpt_body) != value.get("production_gpt_artifact_sha256")
        ):
            raise DeployError("legacy production GPT identity artifact changed")
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


def _target_for(manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any] | None:
    value = revision.get("production_target")
    if value is None:
        value = manifest.get("production_target")
    if value is None:
        return None
    try:
        return validate_production_target(value)
    except ProviderReadbackError as exc:
        raise DeployError("revision production target is invalid") from exc


def _target_binding(manifest: dict[str, Any], revision: dict[str, Any]) -> str | None:
    target = _target_for(manifest, revision)
    return None if target is None else target["binding_sha256"]


def _production_gpt_id(manifest: dict[str, Any], revision: dict[str, Any]) -> str:
    target = _target_for(manifest, revision)
    value = (
        target["custom_gpt"]["gpt_id"]
        if target is not None else manifest.get("production_gpt_id")
    )
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", value):
        raise DeployError("canonical production GPT id is missing or malformed")
    return value


def _activation_ref(value: Any, *, allow_none: bool) -> dict[str, str] | None:
    if value is None and allow_none:
        return None
    required = {"run_id", "proxy_revision_id", "activation_id"}
    if not isinstance(value, dict) or set(value) != required or not RUN_RE.fullmatch(str(value.get("run_id", ""))) or not REVISION_RE.fullmatch(str(value.get("proxy_revision_id", ""))) or not ACTIVATION_RE.fullmatch(str(value.get("activation_id", ""))):
        raise DeployError("activation reference is malformed")
    return value


def _validate_activation_reference(home: Path, reference: dict[str, str], *, seen: set[tuple[str, str]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    key = (reference["run_id"], reference["activation_id"])
    if key in seen:
        raise DeployError("activation history contains a cycle")
    seen.add(key)
    manifest_path, manifest = _manifest(home, reference["run_id"])
    revision = _revision(manifest, reference["proxy_revision_id"])
    _revision_material(manifest_path.parent, manifest, revision)
    matches = [item for item in manifest.get("activations", []) if isinstance(item, dict) and item.get("activation_id") == reference["activation_id"]]
    if len(matches) != 1:
        raise DeployError("activation reference does not identify one recorded activation")
    activation = matches[0]
    required = {"activation_id", "proxy_revision_id", "verification_id", "activated_at", "previous", "adopted"}
    allowed = set(required)
    if activation.get("adopted") is False:
        required.add("final_gate")
        allowed.update({"final_gate", "pointer_recovery"})
    if (
        required - set(activation) or set(activation) - allowed
        or activation.get("proxy_revision_id") != reference["proxy_revision_id"]
        or not VERIFICATION_RE.fullmatch(str(activation.get("verification_id", "")))
        or not isinstance(activation.get("activated_at"), str) or not activation["activated_at"]
        or not isinstance(activation.get("adopted"), bool)
    ):
        raise DeployError("activation reference is not bound to its recorded revision")
    verification = _verification_for_activation(manifest_path.parent, manifest, revision, activation["verification_id"])
    if verification.get("consumed_by_activation_id") != activation["activation_id"]:
        raise DeployError("activation verification is not consumed by this activation")
    if not activation["adopted"]:
        _activation_gate_material(manifest_path.parent, manifest, revision, activation)
    previous = _activation_ref(activation.get("previous"), allow_none=True)
    if previous is not None:
        _validate_activation_reference(home, previous, seen=seen)
    return manifest, revision, activation


def _active(home: Path) -> dict[str, Any] | None:
    path = home / "active.json"
    if not path.exists():
        return None
    value = _read_object(path)
    required = {"schema_version", "run_id", "release_id", "proxy_revision_id", "activation_id", "verification_id", "activated_at", "previous"}
    if set(value) != required or value.get("schema_version") != "2" or not RUN_RE.fullmatch(str(value.get("run_id", ""))) or not REVISION_RE.fullmatch(str(value.get("proxy_revision_id", ""))) or not ACTIVATION_RE.fullmatch(str(value.get("activation_id", ""))) or not VERIFICATION_RE.fullmatch(str(value.get("verification_id", ""))):
        raise DeployError("active pointer is malformed")
    _activation_ref(value.get("previous"), allow_none=True)
    reference = {key: value[key] for key in ("run_id", "proxy_revision_id", "activation_id")}
    manifest, _, activation = _validate_activation_reference(home, reference, seen=set())
    if (
        value["release_id"] != manifest["release_identity"]["release_id"]
        or activation.get("verification_id") != value["verification_id"]
        or activation.get("activated_at") != value["activated_at"]
        or activation.get("previous") != value["previous"]
    ):
        raise DeployError("active pointer does not cross-reference its manifest evidence")
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
    target = _target_for(manifest, revision)
    request = {
        "schema_version": "2", "provider": "vercel", "target": "production",
        "run_id": manifest["run_id"], "proxy_revision_id": revision["proxy_revision_id"],
        "release_identity": manifest["release_identity"], "git_candidate": manifest["git_candidate"],
        "legacy_proxy_evidence_sha256": manifest.get("legacy_proxy_evidence_sha256"),
        "expected_deployment_identity": manifest["expected_deployment_identity"],
        "expected_deployment_identity_sha256": manifest["expected_deployment_identity_sha256"],
        "gateway_release_environment": _gateway_environment(manifest["release_identity"]),
        "proxy": {"config": revision["config_path"], "config_sha256": revision["config_sha256"], "upstream": revision["upstream"]},
    }
    if target is None:
        request["github_ci_evidence_sha256"] = manifest.get("github_ci_evidence_sha256")
    else:
        request["production_target_binding_sha256"] = target["binding_sha256"]
        request["vercel_target"] = target["vercel"]
        if manifest.get("github_provider_receipt_sha256") is not None and revision.get("production_target") is None:
            request["github_provider_receipt_sha256"] = manifest["github_provider_receipt_sha256"]
        else:
            request["activation_authority"] = revision.get("activation_authority")
    return request


def _write_revision(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    upstream: str,
    kind: str,
    clock: Callable[[], str],
    restored_from: dict[str, str] | None = None,
    production_target: dict[str, Any] | None = None,
    activation_authority: dict[str, str] | None = None,
) -> dict[str, Any]:
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
        "production_target": production_target,
        "production_target_binding_sha256": (
            production_target or manifest.get("production_target") or {}
        ).get("binding_sha256"),
        "production_target_artifact": None,
        "production_target_artifact_sha256": None,
        "activation_authority": activation_authority,
        "expected_deployment_identity_sha256": manifest["expected_deployment_identity_sha256"],
        "builder": None, "deployment": None, "create_attempt": None,
        "verifications": [], "current_verification_id": None,
    }
    if production_target is not None:
        target_path = f"evidence/providers/{revision_id}/production-target.json"
        target_body = _json_bytes(production_target)
        revision["production_target_artifact"] = target_path
        revision["production_target_artifact_sha256"] = _sha(target_body)
        _atomic_bytes(run_dir / target_path, target_body)
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
        or revision.get("production_target_binding_sha256") != _target_binding(manifest, revision)
        or revision.get("expected_deployment_identity_sha256") != manifest.get("expected_deployment_identity_sha256")
    ):
        raise DeployError("deploy request or Vercel config was changed or no longer matches its revision")
    override = revision.get("production_target")
    if override is not None:
        relative = revision.get("production_target_artifact")
        if not isinstance(relative, str) or not relative:
            raise DeployError("revision production target artifact is missing")
        target_path, target_body = _bound_artifact(
            run_dir, relative, label="revision production target",
        )
        try:
            target = validate_production_target(_read_object(target_path))
        except ProviderReadbackError as exc:
            raise DeployError("revision production target artifact is invalid") from exc
        authority = _activation_ref(revision.get("activation_authority"), allow_none=False)
        assert authority is not None
        if (
            target != override
            or _sha(target_body) != revision.get("production_target_artifact_sha256")
            or target["binding_sha256"] != revision.get("production_target_binding_sha256")
        ):
            raise DeployError("revision production target artifact was changed")
        _validate_activation_reference(run_dir.parents[1], authority, seen=set())
    elif any(revision.get(key) is not None for key in (
        "production_target_artifact", "production_target_artifact_sha256",
        "activation_authority",
    )):
        raise DeployError("revision provider authority fields are inconsistent")
    identity_path = run_dir / "evidence" / "expected-deployment-identity.json"
    if identity_path.read_bytes() != _json_bytes(manifest["expected_deployment_identity"]) or _sha(identity_path.read_bytes()) != manifest.get("expected_deployment_identity_sha256"):
        raise DeployError("expected deployment identity artifact was changed")
    if manifest.get("production_target") is not None:
        target_path = run_dir / "evidence" / "production-target.json"
        github_path = run_dir / "evidence" / "github-provider-readback.json"
        target_body = target_path.read_bytes()
        github_body = github_path.read_bytes()
        try:
            target = validate_production_target(_read_object(target_path))
            github_receipt = validate_github_provider_receipt(
                _read_object(github_path), target=target,
                expected_sha=manifest["git_candidate"]["commit"],
            )
        except (OSError, ProviderReadbackError) as exc:
            raise DeployError("provider target or GitHub read-back evidence is invalid") from exc
        if (
            target != manifest.get("production_target")
            or target["binding_sha256"] != manifest.get("production_target_binding_sha256")
            or _sha(target_body) != manifest.get("production_target_artifact_sha256")
            or github_receipt != manifest.get("github_provider_receipt")
            or _sha(github_body) != manifest.get("github_provider_receipt_sha256")
        ):
            raise DeployError("provider target or GitHub read-back evidence was changed")
    else:
        ci_sha = manifest.get("github_ci_evidence_sha256")
        if ci_sha is not None:
            ci_path = run_dir / "evidence" / "github-ci.json"
            if _sha(ci_path.read_bytes()) != ci_sha or _read_object(ci_path) != manifest.get("github_ci_evidence"):
                raise DeployError("GitHub CI evidence artifact was changed")
    legacy_proxy_sha = manifest.get("legacy_proxy_evidence_sha256")
    if legacy_proxy_sha is not None:
        legacy_proxy_path = run_dir / "evidence" / "legacy-vercel.json"
        try:
            legacy_proxy_body = legacy_proxy_path.read_bytes()
        except OSError as exc:
            raise DeployError("legacy Vercel proxy evidence artifact is unavailable") from exc
        if _sha(legacy_proxy_body) != legacy_proxy_sha:
            raise DeployError("legacy Vercel proxy evidence artifact was changed")
        _legacy_proxy_config(legacy_proxy_path, revision["upstream"])
    deployment = revision.get("deployment")
    if deployment and deployment.get("status") == "provider-verified":
        receipt = _read_object(run_dir / deployment.get("receipt", ""))
        _validate_deploy_receipt(receipt, manifest=manifest, revision=revision)
        expected_deployment = {**receipt, "receipt": deployment["receipt"], "provider_secret_persisted": False}
        if deployment != expected_deployment:
            raise DeployError("deployment manifest does not match its receipt")
    attempt_reference = revision.get("create_attempt")
    if attempt_reference is not None:
        _validate_create_attempt(
            run_dir, manifest, revision, attempt_reference=attempt_reference,
        )
    return request_path, expected_request


def _changes(
    home: Path, active: dict[str, Any] | None, identity: dict[str, str],
    upstream: str, target: dict[str, Any] | None,
) -> dict[str, bool | None]:
    keys = (
        "git_commit", "instructions", "openapi", "gateway_artifact",
        "gateway_domain", "proxy_upstream", "production_target",
    )
    if active is None:
        return {key: None for key in keys}
    _, old = _manifest(home, active["run_id"])
    old_revision = _revision(old, active["proxy_revision_id"])
    current = old["release_identity"]
    old_target = _target_for(old, old_revision)
    return {
        "git_commit": current["git_commit"] != identity["git_commit"],
        "instructions": current["instructions_sha256"] != identity["instructions_sha256"],
        "openapi": current["openapi_sha256"] != identity["openapi_sha256"],
        "gateway_artifact": current["gateway_artifact_sha256"] != identity["gateway_artifact_sha256"],
        "gateway_domain": current["gateway_domain"] != identity["gateway_domain"],
        "proxy_upstream": old_revision["upstream"] != upstream,
        "production_target": (
            None if old_target is None and target is None
            else old_target is None or target is None
            or old_target["binding_sha256"] != target["binding_sha256"]
        ),
    }


def init_production_target(
    *, output: Path, repository: str, team_id: str, project_id: str,
    project_name: str, stable_domain: str, production_gpt_id: str,
) -> dict[str, Any]:
    destination = outside_repo(output)
    target = {
        "schema_version": "1",
        "environment": "production",
        "github": {
            "repository": repository,
            "branch": "main",
            "workflow_path": ".github/workflows/ci.yml",
        },
        "vercel": {
            "team_id": team_id,
            "project_id": project_id,
            "project_name": project_name,
            "stable_domain": stable_domain,
        },
        "custom_gpt": {"gpt_id": production_gpt_id},
    }
    target["binding_sha256"] = production_target_binding(target)
    try:
        target = validate_production_target(
            target, expected_repository=repository,
        )
    except ProviderReadbackError as exc:
        raise DeployError("production target inputs are invalid") from exc
    if destination.exists():
        try:
            existing = load_production_target(
                destination, expected_repository=repository,
            )
        except ProviderReadbackError as exc:
            raise DeployError("existing production target is invalid") from exc
        if existing != target:
            raise DeployError(
                "existing production target differs; migration requires a separate explicit process"
            )
        return {"changed": False, "path": str(destination), "target": target}
    _atomic_json(destination, target)
    return {"changed": True, "path": str(destination), "target": target}


def prepare(*, home_path: Path, git_commit: str, main_ref: str = "origin/main", gateway_domain: str, proxy_upstream: str,
            production_target: Path, expected_deployment_identity: dict[str, str],
            github_reader: Callable[[str], dict[str, Any]] | None = None,
            expected_repository: str | None = None,
            clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    if not COMMIT_RE.fullmatch(git_commit):
        raise DeployError("--git-commit must be one exact 40-character SHA")
    main_commit = _git_commit(main_ref)
    candidate = _git_commit(git_commit)
    if candidate != git_commit or candidate != main_commit:
        raise DeployError("candidate commit must exactly equal the resolved main ref")
    try:
        target = load_production_target(
            production_target,
            expected_repository=expected_repository or _origin_repository(),
        )
    except ProviderReadbackError as exc:
        raise DeployError(str(exc)) from exc
    public_origin = normalise_gateway_domain(gateway_domain)
    if public_origin != "https://" + target["vercel"]["stable_domain"]:
        raise DeployError("gateway domain must exactly equal the fixed Vercel production target")
    try:
        github_receipt = (github_reader or GitHubProviderReader(target).read)(candidate)
        github_receipt = validate_github_provider_receipt(
            github_receipt, target=target, expected_sha=candidate,
        )
    except ProviderReadbackError as exc:
        raise DeployError(str(exc)) from exc
    target_body = _json_bytes(target)
    github_body = _json_bytes(github_receipt)
    expected_environment = _deployment_identity(expected_deployment_identity)
    expected_environment_body = _json_bytes(expected_environment)
    upstream = normalise_gateway_domain(proxy_upstream)
    bundled = bundle(candidate, public_origin)
    identity = release_identity(bundled)
    if not RELEASE_RE.fullmatch(identity["release_id"]):
        raise DeployError("release bundle produced a malformed release id")
    run_id = _run_id(identity["release_id"])
    run_dir = _run_dir(home, run_id)
    with _locked(home):
        active = _active(home)
        if active is not None:
            _, active_manifest = _manifest(home, active["run_id"])
            active_revision = _revision(
                active_manifest, active["proxy_revision_id"],
            )
            active_target = _target_for(active_manifest, active_revision)
            if _production_gpt_id(active_manifest, active_revision) != target["custom_gpt"]["gpt_id"]:
                raise DeployError(
                    "production GPT identity migration requires a separate explicit process"
                )
            if (
                active_target is not None
                and active_target["binding_sha256"] != target["binding_sha256"]
            ):
                raise DeployError(
                    "production target migration requires a separate explicit process"
                )
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = _manifest(home, run_id)[1]
            expected = {
                "release_identity": identity,
                "git_candidate": {"commit": candidate, "main_ref": main_ref, "resolved_main_commit": main_commit},
                "production_target": target,
                "production_target_binding_sha256": target["binding_sha256"],
                "production_target_artifact_sha256": _sha(target_body),
                "github_provider_receipt": github_receipt,
                "github_provider_receipt_sha256": _sha(github_body),
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
            "production_target": target,
            "production_target_binding_sha256": target["binding_sha256"],
            "production_target_artifact_sha256": _sha(target_body),
            "github_provider_receipt": github_receipt,
            "github_provider_receipt_sha256": _sha(github_body),
            "github_ci_evidence": None, "github_ci_evidence_sha256": None,
            "legacy_proxy_evidence_sha256": None,
            "expected_deployment_identity": expected_environment,
            "expected_deployment_identity_sha256": _sha(expected_environment_body), "prepared_at": created_at,
            "proxy_upstream": upstream, "current_proxy_revision_id": None, "proxy_revisions": [],
            "deploy_request_sha256": None, "vercel_config_sha256": None,
            "changes_from_active": _changes(
                home, active, identity, upstream, target,
            ),
            "builder": None, "deployment": None, "verification": None,
            "activations": [], "rollback_attempts": [], "adoption": None,
        }
        _write_revision(run_dir, manifest, upstream=upstream, kind="prepare", clock=clock)
        _atomic_json(run_dir / "bundle.json", bundled)
        _atomic_bytes(run_dir / "builder" / "expected-instructions.md", bundled["instructions"].encode())
        _atomic_bytes(run_dir / "builder" / "expected-openapi.yaml", bundled["openapi"].encode())
        _atomic_bytes(run_dir / "evidence" / "production-target.json", target_body)
        _atomic_bytes(run_dir / "evidence" / "github-provider-readback.json", github_body)
        _atomic_bytes(run_dir / "evidence" / "expected-deployment-identity.json", expected_environment_body)
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def repair_proxy(
    *,
    home_path: Path,
    run_id: str,
    proxy_upstream: str,
    kind: str = "repair",
    restored_from: dict[str, str] | None = None,
    production_target: Path | None = None,
    activation_authority: dict[str, str] | None = None,
    expected_repository: str | None = None,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    home = _home(home_path)
    upstream = normalise_gateway_domain(proxy_upstream)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        _current_material(manifest_path, manifest)
        target_override = None
        if manifest.get("production_target") is None:
            if production_target is None:
                raise DeployError("repairing a legacy release requires --production-target")
            try:
                target_override = load_production_target(
                    production_target,
                    expected_repository=expected_repository or _origin_repository(),
                )
            except ProviderReadbackError as exc:
                raise DeployError(str(exc)) from exc
            legacy_revision = _revision(manifest)
            if (
                target_override["custom_gpt"]["gpt_id"]
                != _production_gpt_id(manifest, legacy_revision)
                or "https://" + target_override["vercel"]["stable_domain"]
                != manifest["release_identity"]["gateway_domain"]
            ):
                raise DeployError(
                    "legacy repair target must preserve the canonical production GPT and stable domain"
                )
            current_active = _active(home)
            if current_active is not None:
                _, current_manifest = _manifest(home, current_active["run_id"])
                current_revision = _revision(
                    current_manifest, current_active["proxy_revision_id"],
                )
                current_target = _target_for(current_manifest, current_revision)
                if (
                    current_target is not None
                    and current_target["binding_sha256"]
                    != target_override["binding_sha256"]
                ):
                    raise DeployError(
                        "rollback cannot migrate the fixed production target"
                    )
            if activation_authority is None:
                if current_active is None or current_active["run_id"] != run_id:
                    raise DeployError("legacy repair requires the active release as authority")
                activation_authority = {
                    key: current_active[key] for key in (
                        "run_id", "proxy_revision_id", "activation_id"
                    )
                }
        _write_revision(
            manifest_path.parent, manifest, upstream=upstream, kind=kind,
            restored_from=restored_from, production_target=target_override,
            activation_authority=activation_authority if target_override is not None else None,
            clock=clock,
        )
        active = _active(home)
        manifest["changes_from_active"] = _changes(
            home, active, manifest["release_identity"], upstream,
            _target_for(manifest, _revision(manifest)),
        )
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
        if set(evidence) != evidence_required or evidence.get("schema_version") != "1" or evidence.get("run_id") != run_id or evidence.get("proxy_revision_id") != revision["proxy_revision_id"] or evidence.get("producer") not in BUILDER_EVIDENCE_PRODUCERS or evidence.get("gpt_id") != _production_gpt_id(manifest, revision) or not isinstance(evidence.get("exported_at"), str) or not evidence["exported_at"] or evidence.get("instructions_sha256") != sha256_text(instructions) or evidence.get("openapi_sha256") != sha256_text(openapi):
            raise DeployError("Builder evidence does not attest these exact exports, GPT identity, and proxy revision")
        identity = manifest["release_identity"]
        builder_root = f"builder/revisions/{revision['proxy_revision_id']}"
        record = {"recorded_at": clock(), "instructions_sha256": sha256_text(instructions), "openapi_sha256": sha256_text(openapi),
                  "instructions_match": sha256_text(instructions) == identity["instructions_sha256"], "openapi_match": sha256_text(openapi) == identity["openapi_sha256"],
                  "producer": evidence["producer"], "gpt_id": evidence["gpt_id"], "exported_at": evidence["exported_at"],
                  "attested_proxy_revision_id": revision["proxy_revision_id"],
                  "instructions_path": f"{builder_root}/recorded-instructions.md",
                  "openapi_path": f"{builder_root}/recorded-openapi.yaml",
                  "attestation": f"{builder_root}/builder-evidence.json", "attestation_sha256": _sha(evidence_body)}
        comparable = {key: value for key, value in record.items() if key != "recorded_at"}
        old = revision.get("builder")
        if old and {key: old.get(key) for key in comparable} == comparable:
            return {"changed": False, "manifest": manifest}
        _atomic_bytes(manifest_path.parent / record["instructions_path"], instructions.encode())
        _atomic_bytes(manifest_path.parent / record["openapi_path"], openapi.encode())
        _atomic_bytes(manifest_path.parent / record["attestation"], evidence_body)
        revision["builder"] = record
        revision["current_verification_id"] = None
        manifest["builder"] = record
        manifest["verification"] = None
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def _secret_file(path: Path) -> Path:
    supplied = path.expanduser()
    try:
        supplied_metadata = supplied.lstat()
    except OSError as exc:
        raise DeployError("secret env file is unavailable") from exc
    if (
        stat.S_ISLNK(supplied_metadata.st_mode)
        or not stat.S_ISREG(supplied_metadata.st_mode)
    ):
        raise DeployError("secret env file must be one regular, non-symlink file")
    resolved = _home(supplied)
    metadata = resolved.lstat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DeployError("secret env file must not be accessible by group or other users")
    return resolved


def _secret_value(path: Path, name: str) -> str:
    source = _secret_file(path)
    try:
        body = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeployError("secret env file is unavailable") from exc
    values: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            raise DeployError(f"secret env file may contain only {name}")
        values.append(value.strip())
    if len(values) != 1 or not values[0]:
        raise DeployError(f"secret env file must contain exactly one {name}")
    return values[0]


def _vercel_rest_reader(
    secret_env_file: Path, target: dict[str, Any]
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    token = _secret_value(secret_env_file, "VERCEL_TOKEN")
    return VercelRestProviderReader(
        target, token=token, attempts=20, retry_delay_seconds=3,
        sleeper=time.sleep,
    ).read


def _create_attempt_material(
    manifest: dict[str, Any], revision: dict[str, Any], *, prepared_at: str,
) -> dict[str, Any]:
    target = _target_for(manifest, revision)
    if target is None:
        raise DeployError("Vercel create attempt requires a fixed production target")
    vercel = target["vercel"]
    metadata = {
        "gclProxyRevision": revision["proxy_revision_id"],
        "gclRequestSha256": revision["request_sha256"],
        "gclConfigSha256": revision["config_sha256"],
    }
    attempt_id = _id(
        "gclc-", manifest["run_id"], manifest["release_identity"]["release_id"],
        revision["proxy_revision_id"], revision["request_sha256"],
        revision["config_sha256"], target["binding_sha256"],
    )
    return {
        "schema_version": "1",
        "state": "prepared",
        "attempt_id": attempt_id,
        "run_id": manifest["run_id"],
        "release_id": manifest["release_identity"]["release_id"],
        "proxy_revision_id": revision["proxy_revision_id"],
        "request_sha256": revision["request_sha256"],
        "config_sha256": revision["config_sha256"],
        "target_binding_sha256": target["binding_sha256"],
        "team_id": vercel["team_id"],
        "project_id": vercel["project_id"],
        "project_name": vercel["project_name"],
        "metadata": metadata,
        "prepared_at": prepared_at,
        "submission_started_at": None,
        "attestation_path": None,
        "attestation_sha256": None,
    }


def _validate_create_attempt(
    run_dir: Path,
    manifest: dict[str, Any],
    revision: dict[str, Any],
    *,
    attempt_reference: dict[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    if set(attempt_reference) != {"path", "attempt_id"}:
        raise DeployError("Vercel create attempt reference is malformed")
    relative = attempt_reference.get("path")
    if not isinstance(relative, str) or not relative:
        raise DeployError("Vercel create attempt path is malformed")
    attempt_path, attempt_body = _bound_artifact(
        run_dir, relative, label="Vercel create attempt",
    )
    if stat.S_IMODE(attempt_path.lstat().st_mode) != 0o600:
        raise DeployError("Vercel create attempt must remain private")
    attempt = _read_object(attempt_path)
    expected = _create_attempt_material(
        manifest, revision, prepared_at=str(attempt.get("prepared_at", "")),
    )
    required = set(expected)
    state = attempt.get("state")
    if (
        set(attempt) != required
        or state not in {
            "prepared", "submission_started", "attested", "provider_verified",
        }
        or attempt_reference.get("attempt_id") != expected["attempt_id"]
        or not CREATE_ATTEMPT_RE.fullmatch(str(attempt.get("attempt_id", "")))
        or any(
            attempt.get(key) != value
            for key, value in expected.items()
            if key not in {
                "state", "submission_started_at", "attestation_path",
                "attestation_sha256",
            }
        )
        or not isinstance(attempt.get("prepared_at"), str)
        or not attempt["prepared_at"]
    ):
        raise DeployError("Vercel create attempt binding changed")
    attestation: dict[str, Any] | None = None
    if state == "prepared":
        if any(attempt.get(key) is not None for key in (
            "submission_started_at", "attestation_path", "attestation_sha256",
        )):
            raise DeployError("Vercel create attempt state transition is malformed")
    elif state == "submission_started":
        if (
            not isinstance(attempt.get("submission_started_at"), str)
            or not attempt["submission_started_at"]
            or attempt.get("attestation_path") is not None
            or attempt.get("attestation_sha256") is not None
        ):
            raise DeployError("Vercel create attempt state transition is malformed")
    else:
        if (
            not isinstance(attempt.get("submission_started_at"), str)
            or not attempt["submission_started_at"]
            or not isinstance(attempt.get("attestation_path"), str)
            or not isinstance(attempt.get("attestation_sha256"), str)
        ):
            raise DeployError("Vercel create attempt attestation is missing")
        attestation_path, attestation_body = _bound_artifact(
            run_dir, attempt["attestation_path"],
            label="Vercel create attestation",
        )
        if stat.S_IMODE(attestation_path.lstat().st_mode) != 0o600:
            raise DeployError("Vercel create attestation must remain private")
        if _sha(attestation_body) != attempt["attestation_sha256"]:
            raise DeployError("Vercel create attestation changed")
        attestation = _read_object(attestation_path)
        if (
            set(attestation) != {"schema_version", "producer", "create_response"}
            or attestation.get("schema_version") != "1"
            or attestation.get("producer") != "vercel-create-attestation-v1"
        ):
            raise DeployError("Vercel create attestation is malformed")
        target = _target_for(manifest, revision)
        try:
            normalized = normalize_vercel_create_attestation(
                attestation.get("create_response"), target=target,
            )
        except ProviderReadbackError as exc:
            raise DeployError("Vercel create attestation is malformed") from exc
        if normalized.get("metadata") != attempt["metadata"]:
            raise DeployError("Vercel create attestation metadata changed")
    if _sha(attempt_body) != _sha(_json_bytes(attempt)):
        raise DeployError("Vercel create attempt is not canonical JSON")
    return attempt_path, attempt, attestation


def _ensure_create_attempt(
    manifest_path: Path,
    manifest: dict[str, Any],
    revision: dict[str, Any],
    *,
    clock: Callable[[], str],
) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    reference = revision.get("create_attempt")
    if reference is None:
        attempt = _create_attempt_material(manifest, revision, prepared_at=clock())
        relative = f"create-attempts/{revision['proxy_revision_id']}.json"
        path = manifest_path.parent / relative
        _atomic_json(path, attempt)
        revision["create_attempt"] = {
            "path": relative, "attempt_id": attempt["attempt_id"],
        }
        _atomic_json(manifest_path, manifest)
        reference = revision["create_attempt"]
    return _validate_create_attempt(
        manifest_path.parent, manifest, revision,
        attempt_reference=reference,
    )


def _mark_create_attempt_provider_verified(
    path: Path, attempt: dict[str, Any],
) -> None:
    if attempt.get("state") == "provider_verified":
        return
    if attempt.get("state") != "attested":
        raise DeployError("Vercel create attempt was not attested before provider verification")
    _atomic_json(path, {**attempt, "state": "provider_verified"})


class SubprocessRunner:
    def __init__(self, executable: str | None = None):
        self.command = (
            [executable] if executable
            else [sys.executable, str(ROOT / "scripts" / "custom_gpt_vercel_create.py")]
        )

    def __call__(
        self, request: Path, secret_file: Path, evidence: Path,
        attempt_state: Path,
    ) -> None:
        subprocess.run([*self.command, "--request", str(request), "--secret-env-file", str(secret_file), "--evidence-output", str(evidence), "--attempt-state", str(attempt_state)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _provider_deployment_record(
    value: dict[str, Any], *, manifest: dict[str, Any], revision: dict[str, Any],
    provider_reader: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    target = _target_for(manifest, revision)
    if target is None:
        raise DeployError("provider read-back requires a fixed production target")
    required = {
        "schema_version", "producer", "create_response",
    }
    if (
        set(value) != required
        or value.get("schema_version") != "1"
        or value.get("producer") != "vercel-create-attestation-v1"
        or not isinstance(value.get("create_response"), dict)
    ):
        raise DeployError("Vercel adapter evidence is malformed")
    try:
        create = normalize_vercel_create_attestation(
            value["create_response"], target=target,
        )
        if create.get("metadata") != {
            "gclProxyRevision": revision["proxy_revision_id"],
            "gclRequestSha256": revision["request_sha256"],
            "gclConfigSha256": revision["config_sha256"],
        }:
            raise ProviderReadbackError(
                "target_mismatch", "vercel", "create attestation",
                "deployment metadata does not bind the exact release request",
            )
        provider = validate_vercel_provider_receipt(
            provider_reader(create), target=target, create_attestation=create,
        )
    except ProviderReadbackError as exc:
        raise DeployError(str(exc)) from exc
    return {
        "schema_version": "3",
        "provider": "vercel",
        "target": "production",
        "run_id": manifest["run_id"],
        "release_id": manifest["release_identity"]["release_id"],
        "proxy_revision_id": revision["proxy_revision_id"],
        "request_sha256": revision["request_sha256"],
        "config_sha256": revision["config_sha256"],
        "target_binding_sha256": target["binding_sha256"],
        "deployment_id": provider["deployment_id"],
        "url": provider["deployment_url"],
        "status": "provider-verified",
        "checked_at": provider["checked_at"],
        "certifies": DEPLOY_CERTIFIES,
        "create_attestation": create,
        "provider_receipt": provider,
    }


def _validate_deploy_receipt(value: dict[str, Any], *, manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "provider", "target", "run_id", "release_id",
        "proxy_revision_id", "request_sha256", "config_sha256",
        "target_binding_sha256", "deployment_id", "url", "status",
        "checked_at", "certifies", "create_attestation", "provider_receipt",
    }
    target = _target_for(manifest, revision)
    if target is None:
        raise DeployError("deployment receipt has no fixed production target")
    try:
        create = validate_vercel_create_attestation(
            value.get("create_attestation"), target=target,
        )
        provider = validate_vercel_provider_receipt(
            value.get("provider_receipt"), target=target,
            create_attestation=create,
        )
    except (ProviderReadbackError, TypeError) as exc:
        raise DeployError("deployment receipt provider read-back is invalid") from exc
    if (
        set(value) != required or value.get("schema_version") != "3"
        or value.get("provider") != "vercel" or value.get("target") != "production"
        or value.get("run_id") != manifest["run_id"]
        or value.get("release_id") != manifest["release_identity"]["release_id"]
        or value.get("proxy_revision_id") != revision["proxy_revision_id"]
        or value.get("request_sha256") != revision["request_sha256"]
        or value.get("config_sha256") != revision["config_sha256"]
        or value.get("target_binding_sha256") != _target_binding(manifest, revision)
        or value.get("deployment_id") != provider["deployment_id"]
        or value.get("url") != provider["deployment_url"]
        or value.get("status") != "provider-verified"
        or value.get("checked_at") != provider["checked_at"]
        or value.get("certifies") != DEPLOY_CERTIFIES
        or value.get("create_attestation") != create
        or value.get("provider_receipt") != provider
    ):
        raise DeployError("deployment receipt does not certify this exact provider-verified Vercel production request")
    return value


def _record_receipt(manifest_path: Path, manifest: dict[str, Any], revision: dict[str, Any], receipt: dict[str, Any]) -> None:
    receipt_path = f"deployment-receipts/{revision['proxy_revision_id']}.json"
    _atomic_json(manifest_path.parent / receipt_path, receipt)
    deployment = {**receipt, "receipt": receipt_path, "provider_secret_persisted": False}
    revision["deployment"] = deployment
    revision["current_verification_id"] = None
    manifest["deployment"] = deployment
    manifest["verification"] = None
    _atomic_json(manifest_path, manifest)


def deploy_proxy(
    *, home_path: Path, run_id: str, secret_env_file: Path,
    runner: Callable[[Path, Path, Path, Path], None],
    provider_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    home = _home(home_path)
    secret = _secret_file(secret_env_file)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, request_path, _ = _current_material(manifest_path, manifest)
        if (revision.get("deployment") or {}).get("status") == "provider-verified":
            reference = revision.get("create_attempt")
            if reference is not None:
                attempt_path, attempt, _ = _validate_create_attempt(
                    manifest_path.parent, manifest, revision,
                    attempt_reference=reference,
                )
                _mark_create_attempt_provider_verified(attempt_path, attempt)
            return {"changed": False, "manifest": manifest}
        attempt_path, attempt, durable_attestation = _ensure_create_attempt(
            manifest_path, manifest, revision, clock=clock,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".runner-provider-evidence-", suffix=".json",
            dir=manifest_path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.chmod(0o600)
        try:
            if durable_attestation is None:
                runner(request_path, secret, temporary, attempt_path)
                metadata = temporary.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise DeployError("Vercel adapter evidence must remain one private regular file")
                attempt_path, attempt, durable_attestation = _validate_create_attempt(
                    manifest_path.parent, manifest, revision,
                    attempt_reference=revision["create_attempt"],
                )
                if durable_attestation is None or _read_object(temporary) != durable_attestation:
                    raise DeployError(
                        "Vercel adapter output does not match its durable create attestation"
                    )
            reader = provider_reader or _vercel_rest_reader(
                secret, _target_for(manifest, revision) or {},
            )
            receipt = _provider_deployment_record(
                durable_attestation, manifest=manifest, revision=revision,
                provider_reader=reader,
            )
        finally:
            if temporary.exists(): temporary.unlink()
        _record_receipt(manifest_path, manifest, revision, receipt)
        _mark_create_attempt_provider_verified(attempt_path, attempt)
        return {"changed": True, "manifest": manifest}


def record_deployment(
    *, home_path: Path, run_id: str, evidence_path: Path,
    secret_env_file: Path | None = None,
    provider_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    home = _home(home_path)
    source = _home(evidence_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, _, _ = _current_material(manifest_path, manifest)
        if provider_reader is None:
            if secret_env_file is None:
                raise DeployError("Vercel REST read-back requires --secret-env-file")
            provider_reader = _vercel_rest_reader(
                secret_env_file, _target_for(manifest, revision) or {},
            )
        receipt = _provider_deployment_record(
            _read_object(source), manifest=manifest, revision=revision,
            provider_reader=provider_reader,
        )
        if revision.get("deployment") == {**receipt, "receipt": f"deployment-receipts/{revision['proxy_revision_id']}.json", "provider_secret_persisted": False}:
            return {"changed": False, "manifest": manifest}
        _record_receipt(manifest_path, manifest, revision, receipt)
        return {"changed": True, "manifest": manifest}


def _browser_evidence(path: Path) -> tuple[dict[str, Any], str, bytes]:
    source = _home(path)
    body = source.read_bytes()
    value = _read_object(source)
    required = {"schema_version", "producer", "observed_at", "gpt_id", "conversation_ref", "artifact_kind", "status"}
    if set(value) != required or value.get("schema_version") != "1" or value.get("status") != "passed" or value.get("artifact_kind") not in {"browser-receipt", "browser-screenshot-manifest"} or value.get("producer") not in BROWSER_EVIDENCE_PRODUCERS or any(not isinstance(value.get(key), str) or not value[key] for key in ("observed_at", "gpt_id", "conversation_ref")):
        raise DeployError("browser evidence artifact is malformed")
    return value, _sha(body), body


def _smoke(path: Path, *, manifest: dict[str, Any], revision: dict[str, Any], browser: dict[str, Any], browser_sha: str) -> tuple[dict[str, Any], str, bytes]:
    source = _home(path)
    body = source.read_bytes()
    try: value = json.loads(body)
    except json.JSONDecodeError as exc: raise DeployError("smoke evidence must be valid JSON") from exc
    deployment = revision.get("deployment") or {}
    required = {"schema_version", "run_id", "release_id", "proxy_revision_id", "request_sha256", "deployment_id", "expected_deployment_identity", "producer", "gpt_id", "browser_evidence_ref", "browser_evidence_sha256", "status", "observed_at", "certifies"}
    if (
        not isinstance(value, dict) or set(value) != required or value.get("schema_version") != "2"
        or value.get("run_id") != manifest["run_id"] or value.get("release_id") != manifest["release_identity"]["release_id"]
        or value.get("proxy_revision_id") != revision["proxy_revision_id"] or value.get("request_sha256") != revision["request_sha256"]
        or value.get("deployment_id") != deployment.get("deployment_id") or value.get("expected_deployment_identity") != manifest["expected_deployment_identity"]
        or value.get("producer") != browser["producer"] or value.get("gpt_id") != browser["gpt_id"]
        or value.get("browser_evidence_ref") != browser["conversation_ref"]
        or value.get("browser_evidence_sha256") != browser_sha or value.get("observed_at") != browser["observed_at"]
        or value.get("status") != "passed" or value.get("certifies") != SMOKE_CERTIFIES or not isinstance(value.get("observed_at"), str) or not value["observed_at"]
    ):
        raise DeployError("smoke evidence does not certify this exact release, route, request, deployment, and environment")
    return value, _sha(body), body


def _public_route(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 - exact HTTPS URL is release-bound
        body = json.loads(response.read())
        return {"status": response.status, "url": response.geturl(), "headers": dict(response.headers.items()), "body": body}


def _bound_artifact(run_dir: Path, relative: Any, *, label: str) -> tuple[Path, bytes]:
    if not isinstance(relative, str) or not relative:
        raise DeployError(f"{label} path is missing")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise DeployError(f"{label} path escapes the deploy run")
    resolved = run_dir / path
    try:
        metadata = resolved.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DeployError(f"{label} artifact must be one durable regular file")
        return resolved, resolved.read_bytes()
    except OSError as exc:
        raise DeployError(f"{label} artifact is unavailable") from exc


def _builder_material(run_dir: Path, manifest: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    builder = revision.get("builder")
    if not isinstance(builder, dict):
        raise DeployError("Builder evidence is missing")
    attested_revision = builder.get("attested_proxy_revision_id")
    expected_root = f"builder/revisions/{attested_revision}"
    required = {
        "recorded_at", "instructions_sha256", "openapi_sha256", "instructions_match", "openapi_match",
        "producer", "gpt_id", "exported_at", "attested_proxy_revision_id", "instructions_path",
        "openapi_path", "attestation", "attestation_sha256",
    }
    allowed = required | {"reused_from_proxy_revision_id"}
    is_legacy = builder.get("producer") == "legacy-bootstrap"
    attestation_name = "legacy-release-receipt.json" if is_legacy else "builder-evidence.json"
    if (
        set(builder) - allowed or required - set(builder)
        or not REVISION_RE.fullmatch(str(attested_revision or ""))
        or builder.get("instructions_path") != f"{expected_root}/recorded-instructions.md"
        or builder.get("openapi_path") != f"{expected_root}/recorded-openapi.yaml"
        or builder.get("attestation") != f"{expected_root}/{attestation_name}"
        or (not is_legacy and builder.get("producer") not in BUILDER_EVIDENCE_PRODUCERS)
        or builder.get("gpt_id") != _production_gpt_id(manifest, revision)
        or builder.get("instructions_sha256") != manifest["release_identity"]["instructions_sha256"]
        or builder.get("openapi_sha256") != manifest["release_identity"]["openapi_sha256"]
        or builder.get("instructions_match") is not True or builder.get("openapi_match") is not True
    ):
        raise DeployError("Builder evidence binding is malformed")
    if "reused_from_proxy_revision_id" not in builder and attested_revision != revision["proxy_revision_id"]:
        raise DeployError("Builder evidence is not attested for this proxy revision")
    _, instructions_body = _bound_artifact(run_dir, builder["instructions_path"], label="Builder instructions export")
    _, openapi_body = _bound_artifact(run_dir, builder["openapi_path"], label="Builder OpenAPI export")
    _, evidence_body = _bound_artifact(run_dir, builder["attestation"], label="Builder evidence attestation")
    if (
        sha256_text(instructions_body.decode("utf-8")) != builder["instructions_sha256"]
        or sha256_text(openapi_body.decode("utf-8")) != builder["openapi_sha256"]
        or _sha(evidence_body) != builder.get("attestation_sha256")
    ):
        raise DeployError("Builder evidence attestation or exports changed after recording")
    try: evidence = json.loads(evidence_body)
    except json.JSONDecodeError as exc: raise DeployError("Builder evidence attestation is malformed") from exc
    if is_legacy:
        expected_receipt = {"schema_version": "1", "release_identity": manifest["release_identity"], "certifies": "gateway artifact and Builder content parity only"}
        if evidence != expected_receipt:
            raise DeployError("legacy Builder attestation does not certify its recorded exports")
        return builder
    required_evidence = {"schema_version", "producer", "gpt_id", "exported_at", "run_id", "proxy_revision_id", "instructions_sha256", "openapi_sha256"}
    if (
        not isinstance(evidence, dict) or set(evidence) != required_evidence
        or evidence.get("schema_version") != "1" or evidence.get("run_id") != manifest["run_id"]
        or evidence.get("proxy_revision_id") != attested_revision
        or evidence.get("producer") != builder.get("producer") or evidence.get("gpt_id") != builder.get("gpt_id")
        or evidence.get("exported_at") != builder.get("exported_at")
        or evidence.get("instructions_sha256") != builder["instructions_sha256"]
        or evidence.get("openapi_sha256") != builder["openapi_sha256"]
    ):
        raise DeployError("Builder evidence attestation does not match its recorded exports")
    return builder


def _verify_builder_parity(verifier: Callable[..., dict[str, Any]], *, run_dir: Path, builder: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
    kwargs = {
        "bundle_path": run_dir / "bundle.json",
        "builder_instructions_path": run_dir / builder["instructions_path"],
        "builder_openapi_path": run_dir / builder["openapi_path"],
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


def _verification_id(value: dict[str, Any]) -> str:
    binding = {key: item for key, item in value.items() if key not in {"verification_id", "consumed_by_activation_id"}}
    return _id("gclv-", _sha(_json_bytes(binding)))


def verify(*, home_path: Path, run_id: str, smoke_evidence: Path, browser_evidence: Path, verifier: Callable[..., dict[str, Any]] = verify_release,
           route_checker: Callable[[str], dict[str, Any]] = _public_route, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        revision, _, _ = _current_material(manifest_path, manifest)
        deployment = revision.get("deployment")
        if not deployment or deployment.get("status") != "provider-verified":
            raise DeployError("current proxy revision provider-verified deployment receipt is required before verification")
        builder = revision.get("builder")
        if not builder or not builder.get("instructions_match") or not builder.get("openapi_match"):
            raise DeployError("fresh or explicitly reusable Builder exports for this proxy revision are required")
        run_dir = manifest_path.parent
        builder = _builder_material(run_dir, manifest, revision)
        browser, browser_sha, browser_body = _browser_evidence(browser_evidence)
        if browser["gpt_id"] != builder["gpt_id"]:
            raise DeployError("browser evidence does not identify the current production GPT")
        smoke, smoke_sha, smoke_body = _smoke(smoke_evidence, manifest=manifest, revision=revision, browser=browser, browser_sha=browser_sha)
        route = _route(route_checker, manifest=manifest, revision=revision)
        temporary = run_dir / ".parity-receipt.json"
        if temporary.exists(): temporary.unlink()
        try:
            parity = _verify_builder_parity(verifier, run_dir=run_dir, builder=builder, receipt_path=temporary)
        finally:
            if temporary.exists(): temporary.unlink()
        if parity.get("release_identity") != manifest["release_identity"] or parity.get("deployment_identity") != manifest["expected_deployment_identity"]:
            raise DeployError("Builder parity verifier did not certify the exact release and deployment identity")
        parity_sha = _sha(_json_bytes(parity))
        prior = next((item for item in revision["verifications"] if item.get("consumed_by_activation_id") is None and item.get("smoke_evidence_sha256") == smoke_sha and item.get("browser_evidence_sha256") == browser_sha and item.get("parity_receipt_sha256") == parity_sha and item.get("route") == route and item.get("deployment_id") == deployment["deployment_id"]), None)
        if prior:
            manifest["verification"] = prior
            revision["current_verification_id"] = prior["verification_id"]
            _verification_for_activation(run_dir, manifest, revision)
            return {"changed": False, "manifest": manifest}
        evidence_set = _sha(_json_bytes({"smoke": smoke_sha, "browser": browser_sha, "parity": parity_sha}))
        evidence_root = f"evidence/verifications/{revision['proxy_revision_id']}/{evidence_set}"
        parity_path = f"{evidence_root}/parity.json"
        smoke_path = f"{evidence_root}/smoke.json"
        browser_path = f"{evidence_root}/browser.json"
        _atomic_json(run_dir / parity_path, parity)
        _atomic_bytes(run_dir / smoke_path, smoke_body)
        _atomic_bytes(run_dir / browser_path, browser_body)
        verified_at = clock()
        verification = {
            "status": "passed", "verified_at": verified_at, "proxy_revision_id": revision["proxy_revision_id"],
            "request_sha256": revision["request_sha256"], "config_sha256": revision["config_sha256"], "deployment_id": deployment["deployment_id"],
            "production_target_binding_sha256": _target_binding(manifest, revision),
            "provider_create_attestation_sha256": canonical_sha256(deployment["create_attestation"]),
            "provider_readback_receipt_sha256": canonical_sha256(deployment["provider_receipt"]),
            "deployment_receipt": deployment["receipt"], "deployment_receipt_sha256": _sha((run_dir / deployment["receipt"]).read_bytes()), "route": route,
            "builder_hashes": {"instructions_sha256": builder["instructions_sha256"], "openapi_sha256": builder["openapi_sha256"]},
            "builder_attestation": builder["attestation"], "builder_attestation_sha256": builder["attestation_sha256"],
            "builder_instructions": builder["instructions_path"], "builder_openapi": builder["openapi_path"],
            "parity_receipt": parity_path, "parity_receipt_sha256": parity_sha, "smoke_evidence": smoke_path, "smoke_evidence_sha256": smoke_sha,
            "smoke_observed_at": smoke["observed_at"], "smoke_producer": smoke["producer"], "browser_evidence_ref": smoke["browser_evidence_ref"],
            "browser_evidence": browser_path, "browser_evidence_sha256": browser_sha, "browser_gpt_id": browser["gpt_id"],
            "consumed_by_activation_id": None,
        }
        verification["verification_id"] = _verification_id(verification)
        verification_id = verification["verification_id"]
        revision["verifications"].append(verification)
        revision["current_verification_id"] = verification_id
        manifest["verification"] = verification
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def _verification_for_activation(
    run_dir: Path,
    manifest: dict[str, Any],
    revision: dict[str, Any],
    verification_id: str | None = None,
) -> dict[str, Any]:
    wanted = verification_id or revision.get("current_verification_id")
    matches = [item for item in revision.get("verifications", []) if isinstance(item, dict) and item.get("verification_id") == wanted]
    if len(matches) != 1:
        raise DeployError("current verification is malformed, missing, or duplicated")
    verification = matches[0]
    required = {
        "verification_id", "status", "verified_at", "proxy_revision_id", "request_sha256", "config_sha256",
        "deployment_id", "deployment_receipt", "deployment_receipt_sha256", "route", "builder_hashes",
        "builder_attestation", "builder_attestation_sha256", "builder_instructions", "builder_openapi",
        "parity_receipt", "parity_receipt_sha256", "smoke_evidence", "smoke_evidence_sha256",
        "smoke_observed_at", "smoke_producer", "browser_evidence_ref", "browser_evidence",
        "browser_evidence_sha256", "browser_gpt_id", "consumed_by_activation_id",
    }
    deployment = revision.get("deployment") or {}
    builder = _builder_material(run_dir, manifest, revision)
    is_legacy = verification.get("status") == "legacy-adopted"
    if not is_legacy:
        required |= {
            "production_target_binding_sha256",
            "provider_create_attestation_sha256",
            "provider_readback_receipt_sha256",
        }
    expected_route = (
        {"url": manifest["release_identity"]["gateway_domain"] + "/healthz", "release_identity": manifest["release_identity"], "certifies": "legacy route only; no proxy marker or deployment identity"}
        if is_legacy
        else {"url": manifest["release_identity"]["gateway_domain"] + "/healthz", "marker": revision["proxy_revision_id"], "release_identity": manifest["release_identity"], "deployment_identity": manifest["expected_deployment_identity"]}
    )
    if (
        set(verification) != required or verification.get("status") not in {"passed", "legacy-adopted"}
        or not VERIFICATION_RE.fullmatch(str(verification.get("verification_id", "")))
        or verification.get("verification_id") != _verification_id(verification)
        or verification.get("proxy_revision_id") != revision["proxy_revision_id"]
        or verification.get("request_sha256") != revision["request_sha256"]
        or verification.get("config_sha256") != revision["config_sha256"]
        or (not is_legacy and verification.get("production_target_binding_sha256") != _target_binding(manifest, revision))
        or (not is_legacy and verification.get("provider_create_attestation_sha256") != canonical_sha256(deployment.get("create_attestation")))
        or (not is_legacy and verification.get("provider_readback_receipt_sha256") != canonical_sha256(deployment.get("provider_receipt")))
        or verification.get("deployment_id") != deployment.get("deployment_id")
        or verification.get("deployment_receipt") != deployment.get("receipt")
        or verification.get("builder_hashes") != {"instructions_sha256": builder.get("instructions_sha256"), "openapi_sha256": builder.get("openapi_sha256")}
        or verification.get("builder_attestation") != builder.get("attestation")
        or verification.get("builder_attestation_sha256") != builder.get("attestation_sha256")
        or verification.get("builder_instructions") != builder.get("instructions_path")
        or verification.get("builder_openapi") != builder.get("openapi_path")
        or verification.get("route") != expected_route
        or not all(isinstance(verification.get(key), str) and verification[key] for key in (
            "verified_at", "parity_receipt", "parity_receipt_sha256", "smoke_evidence", "smoke_evidence_sha256",
            "smoke_observed_at", "smoke_producer", "browser_evidence_ref", "browser_evidence",
            "browser_evidence_sha256", "browser_gpt_id", "deployment_receipt_sha256",
        ))
    ):
        raise DeployError("current verification is malformed or not bound to current deployment evidence")

    receipt_path, receipt_body = _bound_artifact(run_dir, verification["deployment_receipt"], label="deployment receipt")
    if _sha(receipt_body) != verification["deployment_receipt_sha256"]:
        raise DeployError("deployment receipt changed after verification")
    receipt = _read_object(receipt_path)
    if is_legacy:
        expected_receipt = {"schema_version": "1", "release_identity": manifest["release_identity"], "certifies": "gateway artifact and Builder content parity only"}
        if receipt != expected_receipt or deployment.get("status") != "legacy-external-verified":
            raise DeployError("legacy deployment receipt no longer certifies the adopted release")
    else:
        _validate_deploy_receipt(receipt, manifest=manifest, revision=revision)
        if deployment != {**receipt, "receipt": verification["deployment_receipt"], "provider_secret_persisted": False}:
            raise DeployError("deployment manifest does not match its verified receipt")

    parity_path, parity_body = _bound_artifact(run_dir, verification["parity_receipt"], label="Builder parity receipt")
    if _sha(parity_body) != verification["parity_receipt_sha256"]:
        raise DeployError("Builder parity receipt changed after verification")
    parity = _read_object(parity_path)
    if parity.get("release_identity") != manifest["release_identity"] or (not is_legacy and parity.get("deployment_identity") != manifest["expected_deployment_identity"]):
        raise DeployError("Builder parity receipt no longer certifies this release")

    smoke_path, smoke_body = _bound_artifact(run_dir, verification["smoke_evidence"], label="smoke evidence")
    browser_path, browser_body = _bound_artifact(run_dir, verification["browser_evidence"], label="browser evidence")
    if _sha(smoke_body) != verification["smoke_evidence_sha256"] or _sha(browser_body) != verification["browser_evidence_sha256"]:
        raise DeployError("smoke or browser evidence changed after verification")
    if is_legacy:
        smoke, smoke_sha = _legacy_smoke(smoke_path, manifest["release_identity"])
        if browser_body != smoke_body or smoke_sha != verification["smoke_evidence_sha256"] or verification["browser_evidence_sha256"] != smoke_sha or verification["smoke_observed_at"] != smoke["observed_at"] or verification["smoke_producer"] != "legacy-adoption" or verification["browser_gpt_id"] != builder["gpt_id"]:
            raise DeployError("legacy browser or smoke evidence no longer certifies the adoption")
    else:
        browser, browser_sha, _ = _browser_evidence(browser_path)
        smoke, smoke_sha, _ = _smoke(smoke_path, manifest=manifest, revision=revision, browser=browser, browser_sha=browser_sha)
        if (
            browser["gpt_id"] != builder["gpt_id"] or smoke["gpt_id"] != builder["gpt_id"]
            or browser_sha != verification["browser_evidence_sha256"] or smoke_sha != verification["smoke_evidence_sha256"]
            or verification["smoke_observed_at"] != smoke["observed_at"] or verification["smoke_producer"] != smoke["producer"]
            or verification["browser_evidence_ref"] != browser["conversation_ref"] or verification["browser_gpt_id"] != browser["gpt_id"]
        ):
            raise DeployError("browser or smoke evidence is no longer bound to the current Builder GPT")
    return verification


def _utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DeployError(f"{label} timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DeployError(f"{label} timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise DeployError(f"{label} timestamp is malformed")
    return parsed.astimezone(timezone.utc)


def _activation_gate_material(
    run_dir: Path, manifest: dict[str, Any], revision: dict[str, Any],
    activation: dict[str, Any],
) -> dict[str, Any]:
    def validate_gate(gate: Any, label: str) -> dict[str, Any]:
        required = {
            "checked_at", "provider_receipt", "provider_receipt_sha256",
            "route_receipt", "route_receipt_sha256",
        }
        if not isinstance(gate, dict) or set(gate) != required:
            raise DeployError(f"activation {label} gate is malformed")
        provider_path, provider_body = _bound_artifact(
            run_dir, gate["provider_receipt"],
            label=f"activation {label} provider read-back",
        )
        route_path, route_body = _bound_artifact(
            run_dir, gate["route_receipt"],
            label=f"activation {label} public route",
        )
        if (
            _sha(provider_body) != gate["provider_receipt_sha256"]
            or _sha(route_body) != gate["route_receipt_sha256"]
        ):
            raise DeployError(f"activation {label} gate artifacts changed")
        deployment = revision.get("deployment") or {}
        target = _target_for(manifest, revision)
        try:
            provider = validate_vercel_provider_receipt(
                _read_object(provider_path), target=target,
                create_attestation=deployment.get("create_attestation"),
            )
        except ProviderReadbackError as exc:
            raise DeployError(f"activation {label} provider read-back is invalid") from exc
        route = _read_object(route_path)
        expected_route = {
            "url": manifest["release_identity"]["gateway_domain"] + "/healthz",
            "marker": revision["proxy_revision_id"],
            "release_identity": manifest["release_identity"],
            "deployment_identity": manifest["expected_deployment_identity"],
        }
        checked = _utc_timestamp(gate.get("checked_at"), f"activation {label}")
        provider_checked = _utc_timestamp(
            provider.get("checked_at"), f"{label} provider read-back",
        )
        if (
            provider["deployment_id"] != deployment.get("deployment_id")
            or route != expected_route
            or abs((checked - provider_checked).total_seconds()) > 120
        ):
            raise DeployError(
                f"activation {label} gate is stale or not bound to current production"
            )
        return gate

    gate = validate_gate(activation.get("final_gate"), "final")
    if "pointer_recovery" in activation:
        validate_gate(activation.get("pointer_recovery"), "pointer recovery")
    return gate


def _unlink_durable(path: Path) -> None:
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _fresh_activation_observation(
    *, manifest: dict[str, Any], revision: dict[str, Any],
    provider_reader: Callable[[dict[str, Any]], dict[str, Any]],
    route_checker: Callable[[str], dict[str, Any]], checked_at: str,
) -> tuple[bytes, bytes]:
    deployment = revision.get("deployment") or {}
    target = _target_for(manifest, revision)
    try:
        provider = validate_vercel_provider_receipt(
            provider_reader(deployment.get("create_attestation")),
            target=target,
            create_attestation=deployment.get("create_attestation"),
        )
    except ProviderReadbackError as exc:
        raise DeployError("activation Vercel provider read-back failed") from exc
    route = _route(route_checker, manifest=manifest, revision=revision)
    if abs(
        (
            _utc_timestamp(checked_at, "activation")
            - _utc_timestamp(provider.get("checked_at"), "provider read-back")
        ).total_seconds()
    ) > 120:
        raise DeployError("activation provider read-back is stale")
    return _json_bytes(provider), _json_bytes(route)


def _persist_activation_gate(
    *, run_dir: Path, root: str, checked_at: str,
    provider_body: bytes, route_body: bytes,
) -> dict[str, Any]:
    provider_path = f"{root}/vercel-provider.json"
    route_path = f"{root}/public-route.json"
    _atomic_bytes(run_dir / provider_path, provider_body)
    _atomic_bytes(run_dir / route_path, route_body)
    return {
        "checked_at": checked_at,
        "provider_receipt": provider_path,
        "provider_receipt_sha256": _sha(provider_body),
        "route_receipt": route_path,
        "route_receipt_sha256": _sha(route_body),
    }


def _recover_pending_activation(
    *, home: Path,
    provider_reader: Callable[[dict[str, Any]], dict[str, Any]],
    route_checker: Callable[[str], dict[str, Any]],
    pointer_writer: Callable[[Path, dict[str, Any]], None],
    clock: Callable[[], str],
) -> dict[str, Any] | None:
    pending_path = home / "pending-activation.json"
    if not pending_path.exists():
        return None
    pending = _read_object(pending_path)
    required = {
        "schema_version", "run_id", "proxy_revision_id",
        "activation_id", "pointer",
    }
    pointer = pending.get("pointer")
    pointer_required = {
        "schema_version", "run_id", "release_id", "proxy_revision_id",
        "activation_id", "verification_id", "activated_at", "previous",
    }
    if (
        set(pending) != required
        or pending.get("schema_version") != "1"
        or not RUN_RE.fullmatch(str(pending.get("run_id", "")))
        or not REVISION_RE.fullmatch(str(pending.get("proxy_revision_id", "")))
        or not ACTIVATION_RE.fullmatch(str(pending.get("activation_id", "")))
        or not isinstance(pointer, dict) or set(pointer) != pointer_required
        or pointer.get("schema_version") != "2"
        or any(
            pointer.get(key) != pending.get(key)
            for key in ("run_id", "proxy_revision_id", "activation_id")
        )
    ):
        raise DeployError("pending activation transaction is malformed")
    manifest_path, manifest = _manifest(home, pending["run_id"])
    revision, _, _ = _current_material(manifest_path, manifest)
    if revision["proxy_revision_id"] != pending["proxy_revision_id"]:
        raise DeployError("pending activation is not for the current proxy revision")
    matches = [
        item for item in manifest.get("activations", [])
        if isinstance(item, dict)
        and item.get("activation_id") == pending["activation_id"]
    ]
    if not matches:
        _unlink_durable(pending_path)
        return None
    if len(matches) != 1:
        raise DeployError("pending activation does not identify one activation")
    reference = {
        "run_id": pending["run_id"],
        "proxy_revision_id": pending["proxy_revision_id"],
        "activation_id": pending["activation_id"],
    }
    _validate_activation_reference(home, reference, seen=set())
    if (home / "active.json").exists() and _active(home) == pointer:
        _unlink_durable(pending_path)
        return {"changed": False, "recovered": True, "manifest": manifest, "active": pointer}
    recovered_at = clock()
    provider_body, route_body = _fresh_activation_observation(
        manifest=manifest, revision=revision, provider_reader=provider_reader,
        route_checker=route_checker, checked_at=recovered_at,
    )
    recovery_hash = _sha(provider_body + b"\0" + route_body)
    matches[0]["pointer_recovery"] = _persist_activation_gate(
        run_dir=manifest_path.parent,
        root=f"evidence/activations/{pending['activation_id']}/recoveries/{recovery_hash}",
        checked_at=recovered_at, provider_body=provider_body,
        route_body=route_body,
    )
    _atomic_json(manifest_path, manifest)
    pointer_writer(home / "active.json", pointer)
    _unlink_durable(pending_path)
    return {"changed": True, "recovered": True, "manifest": manifest, "active": pointer}


def activate(
    *, home_path: Path, run_id: str,
    provider_reader: Callable[[dict[str, Any]], dict[str, Any]],
    route_checker: Callable[[str], dict[str, Any]] = _public_route,
    pointer_writer: Callable[[Path, dict[str, Any]], None] = _atomic_json,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    home = _home(home_path)
    with _locked(home):
        recovered = _recover_pending_activation(
            home=home, provider_reader=provider_reader,
            route_checker=route_checker, pointer_writer=pointer_writer,
            clock=clock,
        )
        if recovered is not None:
            return recovered
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
        activated_at = clock()
        provider_body, route_body = _fresh_activation_observation(
            manifest=manifest, revision=revision,
            provider_reader=provider_reader, route_checker=route_checker,
            checked_at=activated_at,
        )
        number = sum(len(_manifest(home, item.parent.name)[1].get("activations", [])) for item in (home / "runs").glob("*/manifest.json")) + 1
        activation_id = _id("gcla-", run_id, revision["proxy_revision_id"], verification["verification_id"], _sha(provider_body), _sha(route_body), number)
        final_gate = _persist_activation_gate(
            run_dir=manifest_path.parent,
            root=f"evidence/activations/{activation_id}",
            checked_at=activated_at, provider_body=provider_body,
            route_body=route_body,
        )
        activation = {"activation_id": activation_id, "proxy_revision_id": revision["proxy_revision_id"], "verification_id": verification["verification_id"], "activated_at": activated_at, "previous": previous, "adopted": False, "final_gate": final_gate}
        verification["consumed_by_activation_id"] = activation_id
        manifest["verification"] = verification
        manifest["activations"].append(activation)
        pointer = {"schema_version": "2", "run_id": run_id, "release_id": manifest["release_identity"]["release_id"], "proxy_revision_id": revision["proxy_revision_id"], "activation_id": activation_id, "verification_id": verification["verification_id"], "activated_at": activated_at, "previous": previous}
        pending_path = home / "pending-activation.json"
        _atomic_json(pending_path, {
            "schema_version": "1", "run_id": run_id,
            "proxy_revision_id": revision["proxy_revision_id"],
            "activation_id": activation_id, "pointer": pointer,
        })
        _atomic_json(manifest_path, manifest)
        pointer_writer(home / "active.json", pointer)
        _unlink_durable(pending_path)
        return {"changed": True, "manifest": manifest, "active": pointer}


def rollback(
    *, home_path: Path, source_run_id: str, record: bool = False,
    production_target: Path | None = None,
    expected_repository: str | None = None,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
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
    restored = repair_proxy(
        home_path=home, run_id=previous["run_id"],
        proxy_upstream=old_revision["upstream"], kind="restore",
        restored_from=previous, production_target=production_target,
        activation_authority=previous, expected_repository=expected_repository,
        clock=clock,
    )
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
                 current_proxy_config: Path, production_gpt_id: str,
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
    if not isinstance(production_gpt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", production_gpt_id):
        raise DeployError("--production-gpt-id is malformed")
    production_gpt_body = _json_bytes({"schema_version": "1", "gpt_id": production_gpt_id})
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
                    "production_gpt_id": production_gpt_id,
                    "production_gpt_artifact_sha256": _sha(production_gpt_body),
                    "expected_deployment_identity_sha256": _sha(_json_bytes(expected_environment)),
                    "legacy_proxy_evidence_sha256": _sha(legacy_proxy_body),
                    "prepared_at": adopted_at, "proxy_upstream": upstream, "current_proxy_revision_id": None, "proxy_revisions": [],
                    "deploy_request_sha256": None, "vercel_config_sha256": None,
                    "changes_from_active": {key: None for key in ("git_commit", "instructions", "openapi", "gateway_artifact", "gateway_domain", "proxy_upstream", "production_target")},
                    "builder": None,
                    "deployment": None, "verification": None, "activations": [], "rollback_attempts": [], "adoption": {"adopted_at": adopted_at, "legacy_layout": True}}
        revision = _write_revision(run_dir, manifest, upstream=upstream, kind="adopt", clock=clock)
        builder_root = f"builder/revisions/{revision['proxy_revision_id']}"
        legacy_builder = {
            "recorded_at": adopted_at, "instructions_sha256": identity["instructions_sha256"], "openapi_sha256": identity["openapi_sha256"],
            "instructions_match": True, "openapi_match": True, "producer": "legacy-bootstrap", "gpt_id": production_gpt_id,
            "exported_at": adopted_at, "attested_proxy_revision_id": revision["proxy_revision_id"],
            "instructions_path": f"{builder_root}/recorded-instructions.md", "openapi_path": f"{builder_root}/recorded-openapi.yaml",
            "attestation": f"{builder_root}/legacy-release-receipt.json", "attestation_sha256": _sha((legacy / "release-receipt.json").read_bytes()),
        }
        revision["legacy_proxy_evidence_sha256"] = _sha(legacy_proxy_body)
        deployment = {"schema_version": "legacy", "provider": "vercel", "target": "production", "run_id": run_id, "release_id": identity["release_id"], "proxy_revision_id": revision["proxy_revision_id"], "request_sha256": revision["request_sha256"], "config_sha256": revision["config_sha256"], "deployment_id": "legacy-adopted-production", "url": identity["gateway_domain"], "status": "legacy-external-verified", "deployed_at": adopted_at, "certifies": "legacy external production observation", "receipt": "release-receipt.json", "secret_file_contents_read_by_orchestrator": False}
        revision["deployment"] = deployment; manifest["deployment"] = deployment
        revision["builder"] = legacy_builder; manifest["builder"] = legacy_builder
        parity_sha = _sha(_json_bytes(parity))
        evidence_set = _sha(_json_bytes({"smoke": smoke_sha, "browser": smoke_sha, "parity": parity_sha}))
        evidence_root = f"evidence/verifications/{revision['proxy_revision_id']}/{evidence_set}"
        verification = {
            "status": "legacy-adopted", "verified_at": adopted_at, "proxy_revision_id": revision["proxy_revision_id"],
            "request_sha256": revision["request_sha256"], "config_sha256": revision["config_sha256"], "deployment_id": deployment["deployment_id"],
            "deployment_receipt": "release-receipt.json", "deployment_receipt_sha256": _sha(_json_bytes(_read_object(legacy / "release-receipt.json"))),
            "route": legacy_route, "builder_hashes": {"instructions_sha256": identity["instructions_sha256"], "openapi_sha256": identity["openapi_sha256"]},
            "builder_attestation": legacy_builder["attestation"], "builder_attestation_sha256": legacy_builder["attestation_sha256"],
            "builder_instructions": legacy_builder["instructions_path"], "builder_openapi": legacy_builder["openapi_path"],
            "parity_receipt": f"{evidence_root}/parity.json", "parity_receipt_sha256": parity_sha,
            "smoke_evidence": f"{evidence_root}/smoke.json", "smoke_evidence_sha256": smoke_sha,
            "smoke_observed_at": smoke["observed_at"], "smoke_producer": "legacy-adoption", "browser_evidence_ref": "legacy/live-smoke.json",
            "browser_evidence": f"{evidence_root}/browser.json", "browser_evidence_sha256": smoke_sha,
            "browser_gpt_id": production_gpt_id, "consumed_by_activation_id": None,
        }
        verification["verification_id"] = _verification_id(verification)
        verification_id = verification["verification_id"]
        activation_id = _id("gcla-", run_id, revision["proxy_revision_id"], verification_id, "adopt")
        verification["consumed_by_activation_id"] = activation_id
        revision["verifications"].append(verification); revision["current_verification_id"] = verification_id; manifest["verification"] = verification
        activation = {"activation_id": activation_id, "proxy_revision_id": revision["proxy_revision_id"], "verification_id": verification_id, "activated_at": adopted_at, "previous": None, "adopted": True}
        manifest["activations"].append(activation)
        pointer = {"schema_version": "2", "run_id": run_id, "release_id": identity["release_id"], "proxy_revision_id": revision["proxy_revision_id"], "activation_id": activation_id, "verification_id": verification_id, "activated_at": adopted_at, "previous": None}
        _atomic_json(run_dir / "bundle.json", bundled); _atomic_json(run_dir / "release-receipt.json", _read_object(legacy / "release-receipt.json")); _atomic_json(run_dir / verification["parity_receipt"], parity)
        _atomic_bytes(run_dir / "evidence" / "legacy-vercel.json", legacy_proxy_body)
        _atomic_bytes(run_dir / "evidence" / "production-gpt.json", production_gpt_body)
        _atomic_bytes(run_dir / "builder" / "expected-instructions.md", bundled["instructions"].encode()); _atomic_bytes(run_dir / "builder" / "expected-openapi.yaml", bundled["openapi"].encode())
        _atomic_bytes(run_dir / legacy_builder["instructions_path"], (legacy / "builder-instructions.md").read_bytes()); _atomic_bytes(run_dir / legacy_builder["openapi_path"], (legacy / "builder-openapi.yaml").read_bytes())
        _atomic_bytes(run_dir / legacy_builder["attestation"], (legacy / "release-receipt.json").read_bytes())
        _atomic_bytes(run_dir / verification["smoke_evidence"], (legacy / "live-smoke.json").read_bytes())
        _atomic_bytes(run_dir / verification["browser_evidence"], (legacy / "live-smoke.json").read_bytes())
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
        if current_verification and current_verification.get("status") in {"passed", "legacy-adopted"}:
            _verification_for_activation(manifest_path.parent, manifest, revision)
        return {"schema_version": "2", "active": active, "run": manifest}
    runs = []
    for path in sorted((home / "runs").glob("*/manifest.json")) if (home / "runs").exists() else []:
        manifest_path, value = _manifest(home, path.parent.name); revision, _, _ = _current_material(manifest_path, value)
        current_verification = next((item for item in revision.get("verifications", []) if item.get("verification_id") == revision.get("current_verification_id")), None)
        if current_verification and current_verification.get("status") in {"passed", "legacy-adopted"}:
            _verification_for_activation(manifest_path.parent, value, revision)
        runs.append({"run_id": value["run_id"], "release_id": value["release_identity"]["release_id"], "prepared_at": value["prepared_at"], "builder_recorded": revision.get("builder") is not None, "deployed": (revision.get("deployment") or {}).get("status") == "provider-verified", "verified": any(item.get("status") == "passed" and item.get("consumed_by_activation_id") is None for item in revision.get("verifications", [])), "active": bool(active and active["run_id"] == value["run_id"] and active["proxy_revision_id"] == revision["proxy_revision_id"])})
    return {"schema_version": "2", "active": active, "runs": runs}


def _load_identity(path: Path) -> dict[str, str]: return _deployment_identity(_read_object(_home(path)))
def _print(value: dict[str, Any]) -> None: print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--home")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status"); show.add_argument("--run-id")
    target = commands.add_parser("init-production-target"); target.add_argument("--output", required=True); target.add_argument("--repository", required=True); target.add_argument("--team-id", required=True); target.add_argument("--project-id", required=True); target.add_argument("--project-name", required=True); target.add_argument("--stable-domain", required=True); target.add_argument("--production-gpt-id", required=True)
    prep = commands.add_parser("prepare"); prep.add_argument("--git-commit", required=True); prep.add_argument("--gateway-domain", required=True); prep.add_argument("--proxy-upstream", required=True); prep.add_argument("--production-target", required=True); prep.add_argument("--expected-deployment-identity", required=True)
    repair = commands.add_parser("repair-proxy"); repair.add_argument("--run-id", required=True); repair.add_argument("--proxy-upstream", required=True); repair.add_argument("--production-target")
    adopt = commands.add_parser("adopt-active"); adopt.add_argument("--legacy-dir", required=True); adopt.add_argument("--current-proxy-upstream", required=True); adopt.add_argument("--current-proxy-config", required=True); adopt.add_argument("--expected-deployment-identity", required=True); adopt.add_argument("--production-gpt-id", required=True); adopt.add_argument("--confirm-live-check", action="store_true")
    builder = commands.add_parser("record-builder"); builder.add_argument("--run-id", required=True); builder.add_argument("--builder-instructions", required=True); builder.add_argument("--builder-openapi", required=True); builder.add_argument("--builder-evidence", required=True)
    deploy = commands.add_parser("run-deployment-adapter"); deploy.add_argument("--run-id", required=True); deploy.add_argument("--secret-env-file", required=True); deploy.add_argument("--runner"); deploy.add_argument("--confirm", action="store_true")
    recorded = commands.add_parser("record-deployment"); recorded.add_argument("--run-id", required=True); recorded.add_argument("--provider-evidence", required=True); recorded.add_argument("--secret-env-file", required=True); recorded.add_argument("--confirm-live-check", action="store_true")
    check = commands.add_parser("verify"); check.add_argument("--run-id", required=True); check.add_argument("--smoke-evidence", required=True); check.add_argument("--browser-evidence", required=True); check.add_argument("--confirm-live-check", action="store_true")
    active = commands.add_parser("activate"); active.add_argument("--run-id", required=True); active.add_argument("--secret-env-file", required=True); active.add_argument("--confirm", action="store_true")
    undo = commands.add_parser("rollback"); undo.add_argument("--run-id", required=True); undo.add_argument("--production-target"); undo.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if args.command != "init-production-target" and not args.home:
        parser.error("--home is required for deploy state commands")
    home = Path(args.home) if args.home else Path(".")
    try:
        if args.command == "status": result = status(home_path=home, run_id=args.run_id)
        elif args.command == "init-production-target": result = init_production_target(output=Path(args.output), repository=args.repository, team_id=args.team_id, project_id=args.project_id, project_name=args.project_name, stable_domain=args.stable_domain, production_gpt_id=args.production_gpt_id)
        elif args.command == "prepare": result = prepare(home_path=home, git_commit=args.git_commit, main_ref="origin/main", gateway_domain=args.gateway_domain, proxy_upstream=args.proxy_upstream, production_target=Path(args.production_target), expected_deployment_identity=_load_identity(Path(args.expected_deployment_identity)))
        elif args.command == "repair-proxy": result = repair_proxy(home_path=home, run_id=args.run_id, proxy_upstream=args.proxy_upstream, production_target=Path(args.production_target) if args.production_target else None)
        elif args.command == "adopt-active" and not args.confirm_live_check: result = {"plan": "no network request or active-pointer write made; re-run with --confirm-live-check"}
        elif args.command == "adopt-active": result = adopt_active(home_path=home, legacy_dir=Path(args.legacy_dir), current_proxy_upstream=args.current_proxy_upstream, current_proxy_config=Path(args.current_proxy_config), expected_deployment_identity=_load_identity(Path(args.expected_deployment_identity)), production_gpt_id=args.production_gpt_id)
        elif args.command == "record-builder": result = record_builder(home_path=home, run_id=args.run_id, instructions_path=Path(args.builder_instructions), openapi_path=Path(args.builder_openapi), builder_evidence=Path(args.builder_evidence))
        elif args.command == "run-deployment-adapter" and not args.confirm:
            manifest_path, manifest = _manifest(_home(home), args.run_id); _, request_path, _ = _current_material(manifest_path, manifest)
            adapter = SubprocessRunner(args.runner)
            result = {"plan": "no command executed; re-run with --confirm", "command": [*adapter.command, "--request", str(request_path), "--secret-env-file", str(Path(args.secret_env_file).expanduser()), "--evidence-output", "<private-temporary-evidence>", "--attempt-state", "<durable-create-attempt>"], "secret_contents_read_by_orchestrator": False}
        elif args.command == "run-deployment-adapter": result = deploy_proxy(home_path=home, run_id=args.run_id, secret_env_file=Path(args.secret_env_file), runner=SubprocessRunner(args.runner))
        elif args.command == "record-deployment" and not args.confirm_live_check:
            result = {"plan": "no Vercel REST request made; re-run with --confirm-live-check", "run_id": args.run_id}
        elif args.command == "record-deployment": result = record_deployment(home_path=home, run_id=args.run_id, evidence_path=Path(args.provider_evidence), secret_env_file=Path(args.secret_env_file))
        elif args.command == "verify" and not args.confirm_live_check:
            manifest = status(home_path=home, run_id=args.run_id)["run"]; result = {"plan": "no network request made; re-run with --confirm-live-check", "health_url": manifest["release_identity"]["gateway_domain"] + "/healthz", "run_id": args.run_id}
        elif args.command == "verify": result = verify(home_path=home, run_id=args.run_id, smoke_evidence=Path(args.smoke_evidence), browser_evidence=Path(args.browser_evidence))
        elif args.command == "activate" and not args.confirm: result = {"plan": "active pointer unchanged; re-run with --confirm", "run_id": args.run_id}
        elif args.command == "activate":
            manifest_path, manifest = _manifest(_home(home), args.run_id)
            revision, _, _ = _current_material(manifest_path, manifest)
            result = activate(home_path=home, run_id=args.run_id, provider_reader=_vercel_rest_reader(Path(args.secret_env_file), _target_for(manifest, revision) or {}))
        elif args.command == "rollback": result = rollback(home_path=home, source_run_id=args.run_id, record=args.confirm, production_target=Path(args.production_target) if args.production_target else None)
        _print(result); return 0
    except (DeployError, ProviderReadbackError, ReleaseIdentityError, subprocess.CalledProcessError, OSError, UnicodeError) as exc:
        print(f"deploy orchestrator blocked: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
