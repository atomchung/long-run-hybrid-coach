#!/usr/bin/env python3
"""Deterministic, external-state orchestrator for manual Custom GPT production runs.

The Builder has no deployment API.  This tool binds the exact main commit, Gateway
runtime identity, external Builder exports, proxy request, public health check, and a
minimal operator smoke receipt without putting any operational state in Git.
"""
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
SMOKE_CERTIFIES = "user-visible Custom GPT smoke only"
DEPLOY_CERTIFIES = "proxy runner completed exact request"


class DeployError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
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
    lock_path = home / ".deploy.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _git_commit(ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not COMMIT_RE.fullmatch(result):
        raise DeployError(f"ref did not resolve to a full commit: {ref}")
    return result


def _run_id(release_id: str) -> str:
    """A release run is stable across repairs of its ephemeral proxy route."""
    return "gcld-" + hashlib.sha256(release_id.encode("utf-8")).hexdigest()


def _proxy_revision_id(proxy_upstream: str) -> str:
    return "gclp-" + hashlib.sha256(proxy_upstream.encode("utf-8")).hexdigest()


def _run_dir(home: Path, run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        raise DeployError("run id is malformed")
    return home / "runs" / run_id


def _manifest(home: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    path = _run_dir(home, run_id) / "manifest.json"
    if not path.is_file():
        raise DeployError(f"unknown deploy run: {run_id}")
    value = _read_object(path)
    if value.get("run_id") != run_id:
        raise DeployError("deploy manifest run id does not match its path")
    identity = release_identity(value.get("release_identity", {}))
    if identity != value["release_identity"]:
        raise DeployError("deploy manifest release identity is not canonical")
    return path, value


def _active(home: Path) -> dict[str, Any] | None:
    path = home / "active.json"
    if not path.exists():
        return None
    value = _read_object(path)
    required = {"schema_version", "run_id", "release_id", "proxy_revision_id", "activated_at", "previous_run_id"}
    if set(value) != required or value.get("schema_version") != "1" or not RUN_RE.fullmatch(str(value.get("run_id", ""))):
        raise DeployError("active pointer is malformed")
    if value["previous_run_id"] is not None and not RUN_RE.fullmatch(str(value["previous_run_id"])):
        raise DeployError("active pointer previous run id is malformed")
    return value


def _revision(manifest: dict[str, Any], revision_id: str | None = None) -> dict[str, Any] | None:
    wanted = revision_id if revision_id is not None else manifest.get("current_proxy_revision_id")
    return next((item for item in manifest.get("proxy_revisions", []) if item.get("proxy_revision_id") == wanted), None)


def _changes(active_manifest: dict[str, Any] | None, identity: dict[str, str], proxy_upstream: str) -> dict[str, bool | None]:
    if active_manifest is None:
        return {key: None for key in ("git_commit", "instructions", "openapi", "gateway_artifact", "gateway_domain", "proxy_upstream")}
    current = active_manifest["release_identity"]
    return {
        "git_commit": current["git_commit"] != identity["git_commit"],
        "instructions": current["instructions_sha256"] != identity["instructions_sha256"],
        "openapi": current["openapi_sha256"] != identity["openapi_sha256"],
        "gateway_artifact": current["gateway_artifact_sha256"] != identity["gateway_artifact_sha256"],
        "gateway_domain": current["gateway_domain"] != identity["gateway_domain"],
        "proxy_upstream": active_manifest["proxy_upstream"] != proxy_upstream,
    }


def _gateway_environment(identity: dict[str, str]) -> dict[str, str]:
    return {
        RELEASE_ID_ENV_VAR: identity["release_id"],
        RELEASE_COMMIT_ENV_VAR: identity["git_commit"],
        RELEASE_INSTRUCTIONS_SHA_ENV_VAR: identity["instructions_sha256"],
        RELEASE_OPENAPI_SHA_ENV_VAR: identity["openapi_sha256"],
        RELEASE_DOMAIN_ENV_VAR: identity["gateway_domain"],
        RELEASE_ARTIFACT_SHA_ENV_VAR: identity["gateway_artifact_sha256"],
    }


def prepare(
    *,
    home_path: Path,
    git_commit: str,
    main_ref: str,
    gateway_domain: str,
    proxy_upstream: str,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    home = _home(home_path)
    if not COMMIT_RE.fullmatch(git_commit):
        raise DeployError("--git-commit must be one exact 40-character SHA")
    main_commit = _git_commit(main_ref)
    candidate = _git_commit(git_commit)
    if candidate != git_commit or candidate != main_commit:
        raise DeployError("candidate commit must exactly equal the resolved main ref")
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
        active_manifest = _manifest(home, active["run_id"])[1] if active else None
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            existing = _manifest(home, run_id)[1]
            expected = {
                "release_identity": identity,
                "git_candidate": {"commit": candidate, "main_ref": main_ref, "resolved_main_commit": main_commit},
            }
            if any(existing.get(key) != value for key, value in expected.items()):
                raise DeployError("existing run id is bound to different deployment inputs")
            if existing.get("proxy_upstream") == upstream:
                return {"changed": False, "manifest": existing}

        proxy_config = {
            "$schema": "https://openapi.vercel.sh/vercel.json",
            "redirects": [{"source": "/oauth/intervals/authorize", "destination": "https://intervals.icu/oauth/authorize", "permanent": False}],
            "rewrites": [{"source": "/:path*", "destination": upstream + "/:path*"}],
        }
        proxy_revision_id = _proxy_revision_id(upstream)
        request = {
            "schema_version": "1",
            "run_id": run_id,
            "proxy_revision_id": proxy_revision_id,
            "release_identity": identity,
            "git_candidate": {"commit": candidate, "main_ref": main_ref, "resolved_main_commit": main_commit},
            "gateway_release_environment": _gateway_environment(identity),
            "proxy": {"provider": "vercel", "config": "proxy/vercel.json", "upstream": upstream},
        }
        created_at = clock()
        if manifest_path.exists():
            manifest = existing
            prior_revision = _revision(manifest)
            if prior_revision is not None:
                prior_revision["deployment"] = manifest.get("deployment")
                prior_revision["verification"] = manifest.get("verification")
            manifest["proxy_upstream"] = upstream
            manifest.setdefault("proxy_revisions", []).append(
                {"proxy_revision_id": proxy_revision_id, "upstream": upstream, "prepared_at": created_at, "deployment": None, "verification": None}
            )
            manifest["current_proxy_revision_id"] = proxy_revision_id
            manifest["deployment"] = None
            manifest["verification"] = None
            manifest["changes_from_active"] = _changes(active_manifest, identity, upstream)
        else:
            manifest = {
            "schema_version": "1",
            "run_id": run_id,
            "release_identity": identity,
            "git_candidate": request["git_candidate"],
            "proxy_upstream": upstream,
            "current_proxy_revision_id": proxy_revision_id,
            "proxy_revisions": [{"proxy_revision_id": proxy_revision_id, "upstream": upstream, "prepared_at": created_at, "deployment": None, "verification": None}],
            "prepared_at": created_at,
            "changes_from_active": _changes(active_manifest, identity, upstream),
            "artifacts": {
                "bundle": "bundle.json",
                "expected_builder_instructions": "builder/expected-instructions.md",
                "expected_builder_openapi": "builder/expected-openapi.yaml",
                "proxy_config": "proxy/vercel.json",
                "deploy_request": "deploy-request.json",
            },
            "builder": None,
            "deployment": None,
            "verification": None,
            "activations": [],
            "rollbacks": [],
            "rollback_plans": [],
            "adoption": None,
            }
        _atomic_json(run_dir / "bundle.json", bundled)
        _atomic_bytes(run_dir / "builder" / "expected-instructions.md", bundled["instructions"].encode("utf-8"))
        _atomic_bytes(run_dir / "builder" / "expected-openapi.yaml", bundled["openapi"].encode("utf-8"))
        _atomic_json(run_dir / "proxy" / "vercel.json", proxy_config)
        _atomic_json(run_dir / "deploy-request.json", request)
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def record_builder(*, home_path: Path, run_id: str, instructions_path: Path, openapi_path: Path, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    instructions_source = _home(instructions_path)
    openapi_source = _home(openapi_path)
    instructions = instructions_source.read_text(encoding="utf-8")
    openapi = openapi_source.read_text(encoding="utf-8")
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        identity = manifest["release_identity"]
        record = {
            "recorded_at": clock(),
            "instructions_sha256": sha256_text(instructions),
            "openapi_sha256": sha256_text(openapi),
            "instructions_match": sha256_text(instructions) == identity["instructions_sha256"],
            "openapi_match": sha256_text(openapi) == identity["openapi_sha256"],
        }
        old = manifest.get("builder")
        comparable = {key: value for key, value in record.items() if key != "recorded_at"}
        if old and {key: old.get(key) for key in comparable} == comparable:
            return {"changed": False, "manifest": manifest}
        run_dir = manifest_path.parent
        _atomic_bytes(run_dir / "builder" / "recorded-instructions.md", instructions.encode("utf-8"))
        _atomic_bytes(run_dir / "builder" / "recorded-openapi.yaml", openapi.encode("utf-8"))
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
    def __init__(self, executable: str):
        self.executable = executable

    def __call__(self, request: Path, secret_file: Path, receipt: Path) -> None:
        subprocess.run(
            [self.executable, "--request", str(request), "--secret-env-file", str(secret_file), "--receipt", str(receipt)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _validate_deploy_receipt(value: dict[str, Any], *, run_id: str, release_id: str, proxy_revision_id: str) -> dict[str, Any]:
    required = {"schema_version", "run_id", "release_id", "proxy_revision_id", "status", "deployed_at", "certifies"}
    if set(value) != required or value.get("schema_version") != "1" or value.get("run_id") != run_id or value.get("release_id") != release_id or value.get("proxy_revision_id") != proxy_revision_id or value.get("status") != "succeeded" or value.get("certifies") != DEPLOY_CERTIFIES or not isinstance(value.get("deployed_at"), str) or not value["deployed_at"]:
        raise DeployError("runner receipt does not certify this exact deploy request")
    return value


def deploy_proxy(*, home_path: Path, run_id: str, secret_env_file: Path, runner: Callable[[Path, Path, Path], None]) -> dict[str, Any]:
    home = _home(home_path)
    secret = _secret_file(secret_env_file)  # metadata only; contents are never opened
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        if (manifest.get("deployment") or {}).get("status") == "succeeded":
            return {"changed": False, "manifest": manifest}
        run_dir = manifest_path.parent
        request = _read_object(run_dir / "deploy-request.json")
        proxy_revision_id = request.get("proxy_revision_id")
        temporary = run_dir / ".runner-receipt.json"
        if temporary.exists():
            temporary.unlink()
        try:
            runner(run_dir / "deploy-request.json", secret, temporary)
            receipt = _validate_deploy_receipt(_read_object(temporary), run_id=run_id, release_id=manifest["release_identity"]["release_id"], proxy_revision_id=proxy_revision_id)
            _atomic_json(run_dir / "deployment-receipt.json", receipt)
        finally:
            if temporary.exists():
                temporary.unlink()
        manifest["deployment"] = {**receipt, "receipt": "deployment-receipt.json", "secret_file_contents_read_by_orchestrator": False}
        current_revision = _revision(manifest)
        if current_revision is not None:
            current_revision["deployment"] = manifest["deployment"]
        manifest["verification"] = None
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def record_deployment(*, home_path: Path, run_id: str, receipt_path: Path) -> dict[str, Any]:
    """Record a strict receipt from an external deploy system or connector."""
    home = _home(home_path)
    receipt_source = _home(receipt_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        request = _read_object(manifest_path.parent / "deploy-request.json")
        receipt = _validate_deploy_receipt(
            _read_object(receipt_source),
            run_id=run_id,
            release_id=manifest["release_identity"]["release_id"],
            proxy_revision_id=request["proxy_revision_id"],
        )
        if (manifest.get("deployment") or {}).get("proxy_revision_id") == request["proxy_revision_id"]:
            return {"changed": False, "manifest": manifest}
        _atomic_json(manifest_path.parent / "deployment-receipt.json", receipt)
        manifest["deployment"] = {**receipt, "receipt": "deployment-receipt.json", "secret_file_contents_read_by_orchestrator": False}
        current_revision = _revision(manifest)
        if current_revision is not None:
            current_revision["deployment"] = manifest["deployment"]
        manifest["verification"] = None
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def _smoke(path: Path, *, run_id: str, release_id: str) -> tuple[dict[str, Any], str]:
    source = _home(path)
    body = source.read_bytes()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeployError("smoke evidence must be valid JSON") from exc
    required = {"schema_version", "run_id", "release_id", "status", "observed_at", "certifies"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != "1" or value.get("run_id") != run_id or value.get("release_id") != release_id or value.get("status") != "passed" or value.get("certifies") != SMOKE_CERTIFIES or not isinstance(value.get("observed_at"), str) or not value["observed_at"]:
        raise DeployError("smoke evidence does not certify this exact run")
    return value, hashlib.sha256(body).hexdigest()


def verify(*, home_path: Path, run_id: str, smoke_evidence: Path, verifier: Callable[..., dict[str, Any]] = verify_release, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        if (manifest.get("deployment") or {}).get("status") != "succeeded":
            raise DeployError("proxy deployment receipt is required before verification")
        builder = manifest.get("builder")
        if not builder:
            raise DeployError("recorded Builder exports are required before verification")
        if not builder.get("instructions_match") or not builder.get("openapi_match"):
            raise DeployError("recorded Builder exports do not match the exact release bundle")
        smoke, smoke_sha = _smoke(smoke_evidence, run_id=run_id, release_id=manifest["release_identity"]["release_id"])
        prior = manifest.get("verification")
        if prior and prior.get("status") == "passed" and prior.get("smoke_evidence_sha256") == smoke_sha:
            return {"changed": False, "manifest": manifest}
        run_dir = manifest_path.parent
        temporary = run_dir / ".parity-receipt.json"
        if temporary.exists():
            temporary.unlink()
        try:
            parity = verifier(
                bundle_path=run_dir / "bundle.json",
                builder_instructions_path=run_dir / "builder" / "recorded-instructions.md",
                builder_openapi_path=run_dir / "builder" / "recorded-openapi.yaml",
                receipt_path=temporary,
            )
            _atomic_json(run_dir / "parity-receipt.json", parity)
        finally:
            if temporary.exists():
                temporary.unlink()
        manifest["verification"] = {
            "status": "passed",
            "verified_at": clock(),
            "parity_receipt": "parity-receipt.json",
            "smoke_evidence_sha256": smoke_sha,
            "smoke_observed_at": smoke["observed_at"],
            "smoke_certifies": smoke["certifies"],
        }
        current_revision = _revision(manifest)
        if current_revision is not None:
            current_revision["verification"] = manifest["verification"]
        _atomic_json(manifest_path, manifest)
        return {"changed": True, "manifest": manifest}


def activate(*, home_path: Path, run_id: str, clock: Callable[[], str] = _now) -> dict[str, Any]:
    home = _home(home_path)
    with _locked(home):
        manifest_path, manifest = _manifest(home, run_id)
        if (manifest.get("verification") or {}).get("status") != "passed":
            raise DeployError("only a parity-verified run with passing smoke evidence may be activated")
        current = _active(home)
        proxy_revision_id = manifest.get("current_proxy_revision_id")
        if current and current["run_id"] == run_id and current["proxy_revision_id"] == proxy_revision_id:
            return {"changed": False, "manifest": manifest, "active": current}
        if current is None and not manifest.get("adoption"):
            raise DeployError("adopt the currently verified production release before the first activation")
        activated_at = clock()
        previous = (current["previous_run_id"] if current and current["run_id"] == run_id else current["run_id"]) if current else None
        pointer = {"schema_version": "1", "run_id": run_id, "release_id": manifest["release_identity"]["release_id"], "proxy_revision_id": proxy_revision_id, "activated_at": activated_at, "previous_run_id": previous}
        manifest["activations"].append({"activated_at": activated_at, "previous_run_id": previous})
        _atomic_json(manifest_path, manifest)
        _atomic_json(home / "active.json", pointer)
        return {"changed": True, "manifest": manifest, "active": pointer}


def rollback(*, home_path: Path, source_run_id: str, record: bool = False, clock: Callable[[], str] = _now) -> dict[str, Any]:
    """Plan a rollback target; never claim or perform a live deployment rollback."""
    home = _home(home_path)
    with _locked(home):
        source_path, source = _manifest(home, source_run_id)
        target_run_id = source.get("activations", [{}])[-1].get("previous_run_id") if source.get("activations") else None
        if target_run_id is None:
            raise DeployError("source run has no previous active run")
        current = _active(home)
        if not current or current["run_id"] != source_run_id:
            raise DeployError("rollback source is not the active run")
        _, target = _manifest(home, target_run_id)
        if (target.get("verification") or {}).get("status") != "passed":
            raise DeployError("previous run is not verified")
        plan = {
            "schema_version": "1",
            "source_run_id": source_run_id,
            "target_run_id": target_run_id,
            "target_release_identity": target["release_identity"],
            "planned_at": clock(),
            "live_state_changed": False,
            "next": "redeploy the target through an explicit runner, re-verify public parity and smoke, then activate it",
        }
        comparable = {key: value for key, value in plan.items() if key != "planned_at"}
        prior = next((item for item in source.get("rollback_plans", []) if all(item.get(key) == value for key, value in comparable.items())), None)
        if not record or prior:
            return {"changed": False, "recorded": bool(prior), "plan": prior or plan, "active": current}
        source.setdefault("rollback_plans", []).append(plan)
        _atomic_json(source_path, source)
        _atomic_json(source_path.parent / "rollback-plan.json", plan)
        return {"changed": True, "recorded": True, "plan": plan, "active": current}


def _legacy_smoke(path: Path, identity: dict[str, str]) -> tuple[dict[str, str], str]:
    body = _home(path).read_bytes()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeployError("legacy live smoke must be valid JSON") from exc
    checks = value.get("checks") if isinstance(value, dict) else None
    writes = value.get("writes_during_smoke") if isinstance(value, dict) else None
    required_writes = {"plan_modified", "provider_publish_requested", "provider_withdraw_requested"}
    if (
        not isinstance(checks, dict)
        or not isinstance(writes, dict)
        or value.get("schema_version") != "1"
        or value.get("release_id") != identity["release_id"]
        or value.get("git_commit") != identity["git_commit"]
        or not isinstance(value.get("observed_at"), str)
        or not value["observed_at"]
        or checks.get("release_gate") != "passed"
        or checks.get("start_coach_session") != "passed"
        or checks.get("fresh_conversation_today_coaching") != "passed"
        or not required_writes.issubset(writes)
        or any(writes[key] is not False for key in required_writes)
    ):
        raise DeployError("legacy live smoke does not prove a safe passing user-visible check")
    return {"observed_at": value["observed_at"], "certifies": "legacy user-visible Custom GPT smoke only"}, hashlib.sha256(body).hexdigest()


def adopt_active(*, home_path: Path, legacy_dir: Path, verifier: Callable[..., dict[str, Any]] = verify_release, clock: Callable[[], str] = _now) -> dict[str, Any]:
    """Bootstrap the canonical pointer from one already-verified legacy production release."""
    home = _home(home_path)
    legacy = _home(legacy_dir)
    bundled = _read_object(legacy / "builder-bundle.json")
    identity = release_identity(bundled)
    if sha256_text(str(bundled.get("instructions", ""))) != identity["instructions_sha256"] or sha256_text(str(bundled.get("openapi", ""))) != identity["openapi_sha256"]:
        raise DeployError("legacy bundle content does not match its release identity")
    legacy_receipt = _read_object(legacy / "release-receipt.json")
    if legacy_receipt != {"schema_version": "1", "release_identity": identity, "certifies": "gateway artifact and Builder content parity only"}:
        raise DeployError("legacy release receipt does not certify the exact bundle")
    smoke, smoke_sha = _legacy_smoke(legacy / "live-smoke.json", identity)
    run_id = _run_id(identity["release_id"])
    with _locked(home):
        current = _active(home)
        if current:
            if current["run_id"] == run_id:
                return {"changed": False, "active": current, "manifest": _manifest(home, run_id)[1]}
            raise DeployError("an active release is already adopted")
        run_dir = _run_dir(home, run_id)
        temporary = run_dir / ".adopt-parity-receipt.json"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            parity = verifier(
                bundle_path=legacy / "builder-bundle.json",
                builder_instructions_path=legacy / "builder-instructions.md",
                builder_openapi_path=legacy / "builder-openapi.yaml",
                receipt_path=temporary,
            )
            _atomic_json(run_dir / "parity-receipt.json", parity)
        finally:
            if temporary.exists():
                temporary.unlink()
        adopted_at = clock()
        manifest = {
            "schema_version": "1",
            "run_id": run_id,
            "release_identity": identity,
            "git_candidate": {"commit": identity["git_commit"], "main_ref": "legacy-adopt", "resolved_main_commit": identity["git_commit"]},
            "proxy_upstream": None,
            "current_proxy_revision_id": None,
            "proxy_revisions": [],
            "prepared_at": adopted_at,
            "changes_from_active": {key: None for key in ("git_commit", "instructions", "openapi", "gateway_artifact", "gateway_domain", "proxy_upstream")},
            "artifacts": {"bundle": "bundle.json", "expected_builder_instructions": "builder/expected-instructions.md", "expected_builder_openapi": "builder/expected-openapi.yaml", "proxy_config": None, "deploy_request": None},
            "builder": {"recorded_at": adopted_at, "instructions_sha256": identity["instructions_sha256"], "openapi_sha256": identity["openapi_sha256"], "instructions_match": True, "openapi_match": True},
            "deployment": {"status": "legacy-external-verified", "secret_file_contents_read_by_orchestrator": False},
            "verification": {"status": "passed", "verified_at": adopted_at, "parity_receipt": "parity-receipt.json", "smoke_evidence_sha256": smoke_sha, "smoke_observed_at": smoke["observed_at"], "smoke_certifies": smoke["certifies"]},
            "activations": [{"activated_at": adopted_at, "previous_run_id": None, "adopted": True}],
            "rollbacks": [],
            "rollback_plans": [],
            "adoption": {"adopted_at": adopted_at, "legacy_layout": True},
        }
        _atomic_json(run_dir / "bundle.json", bundled)
        _atomic_bytes(run_dir / "builder" / "expected-instructions.md", bundled["instructions"].encode("utf-8"))
        _atomic_bytes(run_dir / "builder" / "expected-openapi.yaml", bundled["openapi"].encode("utf-8"))
        _atomic_bytes(run_dir / "builder" / "recorded-instructions.md", (legacy / "builder-instructions.md").read_bytes())
        _atomic_bytes(run_dir / "builder" / "recorded-openapi.yaml", (legacy / "builder-openapi.yaml").read_bytes())
        _atomic_json(run_dir / "manifest.json", manifest)
        pointer = {"schema_version": "1", "run_id": run_id, "release_id": identity["release_id"], "proxy_revision_id": None, "activated_at": adopted_at, "previous_run_id": None}
        _atomic_json(home / "active.json", pointer)
        return {"changed": True, "active": pointer, "manifest": manifest}


def status(*, home_path: Path, run_id: str | None = None) -> dict[str, Any]:
    home = _home(home_path)
    if not home.exists():
        return {"schema_version": "1", "active": None, "runs": []}
    active = _active(home)
    if run_id:
        _, manifest = _manifest(home, run_id)
        return {"schema_version": "1", "active": active, "run": manifest}
    runs = []
    for path in sorted((home / "runs").glob("*/manifest.json")) if (home / "runs").exists() else []:
        value = _read_object(path)
        runs.append({"run_id": value.get("run_id"), "release_id": value.get("release_identity", {}).get("release_id"), "prepared_at": value.get("prepared_at"), "builder_recorded": value.get("builder") is not None, "deployed": (value.get("deployment") or {}).get("status") == "succeeded", "verified": (value.get("verification") or {}).get("status") == "passed", "active": bool(active and active["run_id"] == value.get("run_id"))})
    return {"schema_version": "1", "active": active, "runs": runs}


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, help="external release home; must be outside this repository")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status"); show.add_argument("--run-id")
    prep = commands.add_parser("prepare")
    prep.add_argument("--git-commit", required=True); prep.add_argument("--main-ref", default="origin/main"); prep.add_argument("--gateway-domain", required=True); prep.add_argument("--proxy-upstream", required=True)
    adopt = commands.add_parser("adopt-active")
    adopt.add_argument("--legacy-dir", required=True); adopt.add_argument("--confirm-live-check", action="store_true")
    builder = commands.add_parser("record-builder")
    builder.add_argument("--run-id", required=True); builder.add_argument("--builder-instructions", required=True); builder.add_argument("--builder-openapi", required=True)
    deploy = commands.add_parser("run-deployment-adapter", help="invoke an external adapter; this repository does not provide one")
    deploy.add_argument("--run-id", required=True); deploy.add_argument("--secret-env-file", required=True); deploy.add_argument("--runner", required=True); deploy.add_argument("--confirm", action="store_true")
    recorded = commands.add_parser("record-deployment", help="record a strict receipt from an external deploy system")
    recorded.add_argument("--run-id", required=True); recorded.add_argument("--receipt", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--run-id", required=True); check.add_argument("--smoke-evidence", required=True); check.add_argument("--confirm-live-check", action="store_true")
    active = commands.add_parser("activate")
    active.add_argument("--run-id", required=True); active.add_argument("--confirm", action="store_true")
    undo = commands.add_parser("rollback")
    undo.add_argument("--run-id", required=True, help="the active source run to roll back"); undo.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    home = Path(args.home)
    try:
        if args.command == "status":
            result = status(home_path=home, run_id=args.run_id)
        elif args.command == "prepare":
            result = prepare(home_path=home, git_commit=args.git_commit, main_ref=args.main_ref, gateway_domain=args.gateway_domain, proxy_upstream=args.proxy_upstream)
        elif args.command == "adopt-active" and not args.confirm_live_check:
            result = {"plan": "no network request or active-pointer write made; re-run with --confirm-live-check", "legacy_dir": str(Path(args.legacy_dir).expanduser())}
        elif args.command == "adopt-active":
            result = adopt_active(home_path=home, legacy_dir=Path(args.legacy_dir))
        elif args.command == "record-builder":
            result = record_builder(home_path=home, run_id=args.run_id, instructions_path=Path(args.builder_instructions), openapi_path=Path(args.builder_openapi))
        elif args.command == "run-deployment-adapter" and not args.confirm:
            run_dir = _run_dir(_home(home), args.run_id)
            result = {"plan": "no command executed; re-run with --confirm", "command": [args.runner, "--request", str(run_dir / "deploy-request.json"), "--secret-env-file", str(Path(args.secret_env_file).expanduser()), "--receipt", str(run_dir / ".runner-receipt.json")], "secret_contents_read_by_orchestrator": False}
        elif args.command == "run-deployment-adapter":
            result = deploy_proxy(home_path=home, run_id=args.run_id, secret_env_file=Path(args.secret_env_file), runner=SubprocessRunner(args.runner))
        elif args.command == "record-deployment":
            result = record_deployment(home_path=home, run_id=args.run_id, receipt_path=Path(args.receipt))
        elif args.command == "verify" and not args.confirm_live_check:
            manifest = status(home_path=home, run_id=args.run_id)["run"]
            result = {"plan": "no network request made; re-run with --confirm-live-check", "health_url": manifest["release_identity"]["gateway_domain"] + "/healthz", "run_id": args.run_id}
        elif args.command == "verify":
            result = verify(home_path=home, run_id=args.run_id, smoke_evidence=Path(args.smoke_evidence))
        elif args.command == "activate" and not args.confirm:
            result = {"plan": "active pointer unchanged; re-run with --confirm", "run_id": args.run_id}
        elif args.command == "activate":
            result = activate(home_path=home, run_id=args.run_id)
        elif args.command == "rollback":
            result = rollback(home_path=home, source_run_id=args.run_id, record=args.confirm)
        _print(result)
        return 0
    except (DeployError, ReleaseIdentityError, subprocess.CalledProcessError, OSError, UnicodeError) as exc:
        print(f"deploy orchestrator blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
