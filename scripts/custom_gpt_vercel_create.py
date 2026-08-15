#!/usr/bin/env python3
"""Create one exact Vercel production proxy deployment and emit a bounded attestation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class CreateError(RuntimeError):
    pass


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^gclp-[0-9a-f]{64}$")
CREATE_ATTEMPT_RE = re.compile(r"^gclc-[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts).encode("utf-8")
    return prefix + hashlib.sha256(material).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, request, file_pointer, code, message, headers, new_url,
    ):
        return None


def _private_regular(path: Path, label: str) -> Path:
    supplied = path.expanduser()
    try:
        metadata = supplied.lstat()
    except OSError as exc:
        raise CreateError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise CreateError(f"{label} must be one 0600 regular non-symlink file")
    return supplied.resolve()


def _token(path: Path) -> str:
    source = _private_regular(path, "Vercel secret env file")
    values = []
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != "VERCEL_TOKEN":
            raise CreateError("Vercel secret env file may contain only VERCEL_TOKEN")
        values.append(value.strip())
    if len(values) != 1 or not 16 <= len(values[0]) <= 512 or any(
        character.isspace() for character in values[0]
    ):
        raise CreateError("Vercel secret env file has a malformed token")
    return values[0]


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CreateError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CreateError(f"{label} must be a JSON object")
    return value


def _request_context(path: Path) -> tuple[dict[str, Any], bytes, Path, bytes]:
    supplied = path.expanduser()
    try:
        metadata = supplied.lstat()
    except OSError as exc:
        raise CreateError("deploy request is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CreateError("deploy request must be one regular non-symlink file")
    source = supplied.resolve()
    if source.parent.name != "deploy-requests":
        raise CreateError("deploy request is outside the orchestrator run layout")
    run_root = source.parent.parent
    try:
        run_metadata = run_root.lstat()
    except OSError as exc:
        raise CreateError("deploy request run is unavailable") from exc
    if stat.S_ISLNK(run_metadata.st_mode) or not stat.S_ISDIR(run_metadata.st_mode):
        raise CreateError("deploy request run must be one non-symlink directory")
    request_body = source.read_bytes()
    try:
        value = json.loads(request_body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CreateError("deploy request is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CreateError("deploy request must be a JSON object")
    target = value.get("vercel_target")
    proxy = value.get("proxy")
    if (
        value.get("schema_version") != "2"
        or value.get("provider") != "vercel"
        or value.get("target") != "production"
        or not isinstance(value.get("production_target_binding_sha256"), str)
        or not isinstance(target, dict)
        or set(target) != {
            "team_id", "project_id", "project_name", "stable_domain",
        }
        or not isinstance(proxy, dict)
        or set(proxy) != {"config", "config_sha256", "upstream"}
    ):
        raise CreateError("deploy request is not a fixed Vercel production request")
    relative = Path(str(proxy.get("config", "")))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise CreateError("deploy request config path escapes its run")
    config_path = run_root
    try:
        for index, part in enumerate(relative.parts):
            config_path = config_path / part
            config_metadata = config_path.lstat()
            if stat.S_ISLNK(config_metadata.st_mode):
                raise CreateError("Vercel config path must not contain symlinks")
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(
                config_metadata.st_mode
            ):
                raise CreateError("Vercel config path is malformed")
        config_body = config_path.read_bytes()
    except CreateError:
        raise
    except OSError as exc:
        raise CreateError("Vercel config is unavailable") from exc
    if stat.S_ISLNK(config_metadata.st_mode) or not stat.S_ISREG(config_metadata.st_mode):
        raise CreateError("Vercel config must be one regular non-symlink file")
    if hashlib.sha256(config_body).hexdigest() != proxy.get("config_sha256"):
        raise CreateError("Vercel config does not match the deploy request")
    return value, config_body, run_root, request_body


def _request_material(path: Path) -> tuple[dict[str, Any], bytes]:
    """Return the public request/config pair used by existing adapter callers."""
    value, config_body, _, _ = _request_context(path)
    return value, config_body


def _metadata(request_value: dict[str, Any], request_body: bytes) -> dict[str, str]:
    proxy = request_value.get("proxy")
    revision = request_value.get("proxy_revision_id")
    if (
        not REVISION_RE.fullmatch(str(revision or ""))
        or not isinstance(proxy, dict)
        or not SHA256_RE.fullmatch(str(proxy.get("config_sha256", "")))
    ):
        raise CreateError("deploy request has no canonical revision metadata")
    return {
        "gclProxyRevision": revision,
        "gclRequestSha256": hashlib.sha256(request_body).hexdigest(),
        "gclConfigSha256": proxy["config_sha256"],
    }


def _validate_attempt(
    value: Any, *, request_value: dict[str, Any], request_body: bytes,
) -> dict[str, Any]:
    required = {
        "schema_version", "state", "attempt_id", "run_id", "release_id",
        "proxy_revision_id", "request_sha256", "config_sha256",
        "target_binding_sha256", "team_id", "project_id", "project_name",
        "metadata", "prepared_at", "submission_started_at",
        "attestation_path", "attestation_sha256",
    }
    release = request_value.get("release_identity")
    target = request_value.get("vercel_target")
    metadata = _metadata(request_value, request_body)
    expected_attempt_id = _id(
        "gclc-", request_value.get("run_id"),
        release.get("release_id") if isinstance(release, dict) else None,
        request_value.get("proxy_revision_id"), metadata["gclRequestSha256"],
        metadata["gclConfigSha256"],
        request_value.get("production_target_binding_sha256"),
    )
    valid = (
        isinstance(value, dict)
        and set(value) == required
        and value.get("schema_version") == "1"
        and value.get("state") in {
            "prepared", "submission_started", "attested", "provider_verified",
        }
        and CREATE_ATTEMPT_RE.fullmatch(str(value.get("attempt_id", "")))
        and value.get("attempt_id") == expected_attempt_id
        and value.get("run_id") == request_value.get("run_id")
        and isinstance(release, dict)
        and value.get("release_id") == release.get("release_id")
        and value.get("proxy_revision_id") == request_value.get("proxy_revision_id")
        and value.get("request_sha256") == metadata["gclRequestSha256"]
        and value.get("config_sha256") == metadata["gclConfigSha256"]
        and value.get("target_binding_sha256")
        == request_value.get("production_target_binding_sha256")
        and isinstance(target, dict)
        and value.get("team_id") == target.get("team_id")
        and value.get("project_id") == target.get("project_id")
        and value.get("project_name") == target.get("project_name")
        and value.get("metadata") == metadata
        and isinstance(value.get("prepared_at"), str)
        and bool(value["prepared_at"])
    )
    state = value.get("state") if isinstance(value, dict) else None
    if state == "prepared":
        valid = valid and all(value.get(key) is None for key in (
            "submission_started_at", "attestation_path", "attestation_sha256",
        ))
    elif state == "submission_started":
        valid = (
            valid
            and isinstance(value.get("submission_started_at"), str)
            and bool(value["submission_started_at"])
            and value.get("attestation_path") is None
            and value.get("attestation_sha256") is None
        )
    elif state in {"attested", "provider_verified"}:
        valid = (
            valid
            and isinstance(value.get("submission_started_at"), str)
            and bool(value["submission_started_at"])
            and isinstance(value.get("attestation_path"), str)
            and bool(value["attestation_path"])
            and SHA256_RE.fullmatch(str(value.get("attestation_sha256", "")))
        )
    if not valid:
        raise CreateError("durable create attempt is malformed or changed")
    return json.loads(_canonical_bytes(value))


def _atomic_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CreateError("create attempt directory must be one non-symlink directory")
    if path.exists():
        existing = path.lstat()
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise CreateError("create attempt must be one regular non-symlink file")
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=".create-attempt-", dir=parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_attempt(
    path: Path, *, run_root: Path, request_value: dict[str, Any],
    request_body: bytes,
) -> tuple[Path, dict[str, Any]]:
    revision = str(request_value.get("proxy_revision_id", ""))
    if not REVISION_RE.fullmatch(revision):
        raise CreateError("proxy revision id is malformed")
    supplied = path.expanduser()
    try:
        metadata = supplied.lstat()
    except OSError as exc:
        raise CreateError("durable create attempt is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CreateError("durable create attempt must be one regular non-symlink file")
    source = supplied.resolve()
    expected_parent = run_root / "create-attempts"
    try:
        parent_metadata = expected_parent.lstat()
    except OSError as exc:
        raise CreateError("durable create attempt directory is unavailable") from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise CreateError("durable create attempt directory must be one non-symlink directory")
    expected = expected_parent / f"{revision}.json"
    if source != expected.resolve():
        raise CreateError("durable create attempt is outside the orchestrator run layout")
    source = _private_regular(source, "durable create attempt")
    return source, _validate_attempt(
        _read_object(source, "durable create attempt"),
        request_value=request_value, request_body=request_body,
    )


def _transition_attempt(
    path: Path, attempt: dict[str, Any], *, state: str,
    clock: Callable[[], str], attestation_path: str | None = None,
    attestation_sha256: str | None = None,
) -> dict[str, Any]:
    if state not in {"submission_started", "attested"}:
        raise CreateError("create attempt state transition is invalid")
    if state == "submission_started" and attempt["state"] != "prepared":
        raise CreateError("create attempt state transition is invalid")
    if state == "attested" and attempt["state"] != "submission_started":
        raise CreateError("create attempt state transition is invalid")
    source = _private_regular(path, "durable create attempt")
    if _read_object(source, "durable create attempt") != attempt:
        raise CreateError("durable create attempt changed before transition")
    if state == "attested" and (
        not isinstance(attestation_path, str) or not attestation_path
        or not SHA256_RE.fullmatch(str(attestation_sha256 or ""))
    ):
        raise CreateError("create attempt attestation transition is malformed")
    updated = {**attempt, "state": state}
    if state == "submission_started":
        started_at = clock()
        if not isinstance(started_at, str) or not started_at:
            raise CreateError("create attempt transition time is malformed")
        updated["submission_started_at"] = started_at
    if state == "attested":
        updated["attestation_path"] = attestation_path
        updated["attestation_sha256"] = attestation_sha256
    _atomic_private(path, updated)
    return updated


class VercelCreateClient:
    def __init__(self, token: str, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def _request(
        self, url: str, *, method: str, body: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "User-Agent": "garmin-coach-loop-release/1",
            **(extra_headers or {}),
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            url, data=body, method=method, headers=headers,
        )
        try:
            with self.opener(request, timeout=30) as response:
                if response.geturl() != url or response.status not in {200, 201, 202}:
                    raise CreateError("Vercel provider request failed")
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            raise CreateError("Vercel provider request failed") from exc
        except (OSError, TimeoutError) as exc:
            raise CreateError("Vercel provider request failed") from exc
        if not response_body:
            return {}
        try:
            value = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CreateError("Vercel provider response is malformed") from exc
        if not isinstance(value, dict):
            raise CreateError("Vercel provider response is malformed")
        return value

    def _send(
        self, url: str, *, body: bytes, content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            url, method="POST", body=body, content_type=content_type,
            extra_headers=extra_headers,
        )

    def upload_config(
        self, request_value: dict[str, Any], config_body: bytes,
    ) -> str:
        target = request_value["vercel_target"]
        team_query = urllib.parse.urlencode({"teamId": target["team_id"]})
        digest = hashlib.sha1(config_body).hexdigest()  # noqa: S324 - Vercel file-address contract
        self._send(
            "https://api.vercel.com/v2/files?" + team_query,
            body=config_body, content_type="application/octet-stream",
            extra_headers={"x-vercel-digest": digest},
        )
        return digest

    @staticmethod
    def _evidence(
        deployment: dict[str, Any], *, target: dict[str, Any],
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        deployment_id = deployment.get("id", deployment.get("uid"))
        deployment_url = deployment.get("url")
        if (
            not isinstance(deployment_id, str) or not deployment_id
            or not isinstance(deployment_url, str) or not deployment_url
        ):
            raise CreateError("Vercel create response has no deployment identity")
        if "://" in deployment_url and not deployment_url.startswith("https://"):
            raise CreateError("Vercel create response has an invalid deployment URL")
        if not deployment_url.startswith("https://"):
            deployment_url = "https://" + deployment_url
        return {
            "schema_version": "1",
            "producer": "vercel-create-attestation-v1",
            "create_response": {
                "provider": "vercel", "target": "production",
                "teamId": target["team_id"],
                "projectId": target["project_id"],
                "projectName": target["project_name"],
                "deploymentId": deployment_id,
                "url": deployment_url,
                "metadata": metadata,
            },
        }

    def create_deployment(
        self, request_value: dict[str, Any], *, digest: str, config_size: int,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        target = request_value["vercel_target"]
        team_query = urllib.parse.urlencode({"teamId": target["team_id"]})
        payload = {
            "name": target["project_name"],
            "project": target["project_id"],
            "target": "production",
            "meta": metadata,
            "files": [{"file": "vercel.json", "sha": digest, "size": config_size}],
            "projectSettings": {"framework": None},
        }
        created = self._send(
            "https://api.vercel.com/v13/deployments?" + team_query + "&forceNew=1",
            body=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )
        return self._evidence(created, target=target, metadata=metadata)

    def create(
        self, request_value: dict[str, Any], config_body: bytes,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        digest = self.upload_config(request_value, config_body)
        return self.create_deployment(
            request_value, digest=digest, config_size=len(config_body),
            metadata=metadata,
        )

    def reconcile(
        self, request_value: dict[str, Any], metadata: dict[str, str],
    ) -> dict[str, Any]:
        target = request_value["vercel_target"]
        query = urllib.parse.urlencode({
            "teamId": target["team_id"],
            "projectId": target["project_id"],
            "target": "production",
            "limit": "100",
        })
        value = self._request(
            "https://api.vercel.com/v6/deployments?" + query, method="GET",
        )
        deployments = value.get("deployments")
        if not isinstance(deployments, list):
            raise CreateError("Vercel deployment reconciliation response is malformed")
        matches: list[dict[str, Any]] = []
        for item in deployments:
            if not isinstance(item, dict):
                raise CreateError("Vercel deployment reconciliation response is malformed")
            observed_metadata = item.get("meta")
            if not isinstance(observed_metadata, dict):
                continue
            if (
                observed_metadata.get("gclProxyRevision")
                == metadata["gclProxyRevision"]
                and observed_metadata != metadata
            ):
                raise CreateError("Vercel deployment metadata collision")
            if (
                observed_metadata == metadata
                and item.get("projectId") == target["project_id"]
                and item.get("name") == target["project_name"]
                and item.get("target") == "production"
            ):
                matches.append(item)
        if len(matches) != 1:
            if not matches:
                raise CreateError("Vercel ambiguous create has no exact deployment match")
            raise CreateError("Vercel ambiguous create has multiple exact deployment matches")
        return self._evidence(
            matches[0], target=target, metadata=matches[0]["meta"],
        )


def _validate_evidence(
    value: Any, *, request_value: dict[str, Any],
    metadata: dict[str, str],
) -> dict[str, Any]:
    target = request_value["vercel_target"]
    create = value.get("create_response") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "producer", "create_response"}
        or value.get("schema_version") != "1"
        or value.get("producer") != "vercel-create-attestation-v1"
        or not isinstance(create, dict)
        or set(create) != {
            "provider", "target", "teamId", "projectId", "projectName",
            "deploymentId", "url", "metadata",
        }
        or create.get("provider") != "vercel"
        or create.get("target") != "production"
        or create.get("teamId") != target["team_id"]
        or create.get("projectId") != target["project_id"]
        or create.get("projectName") != target["project_name"]
        or create.get("metadata") != metadata
        or not isinstance(create.get("deploymentId"), str)
        or not create["deploymentId"]
        or not isinstance(create.get("url"), str)
        or not create["url"].startswith("https://")
    ):
        raise CreateError("durable Vercel create attestation is malformed")
    return json.loads(_canonical_bytes(value))


def _attestation_location(
    run_root: Path, request_value: dict[str, Any],
) -> tuple[str, Path]:
    revision = str(request_value.get("proxy_revision_id", ""))
    if not REVISION_RE.fullmatch(revision):
        raise CreateError("proxy revision id is malformed")
    relative = f"create-attestations/{revision}.json"
    return relative, run_root / relative


def _persist_attestation(
    run_root: Path, request_value: dict[str, Any], evidence: dict[str, Any],
    metadata: dict[str, str],
) -> tuple[str, str]:
    evidence = _validate_evidence(
        evidence, request_value=request_value, metadata=metadata,
    )
    relative, path = _attestation_location(run_root, request_value)
    expected_body = (
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.exists():
        source = _private_regular(path, "durable Vercel create attestation")
        try:
            observed_body = source.read_bytes()
        except OSError as exc:
            raise CreateError("durable Vercel create attestation is unavailable") from exc
        if observed_body != expected_body:
            raise CreateError("durable Vercel create attestation changed")
    else:
        _atomic_private(path, evidence)
        observed_body = path.read_bytes()
    return relative, hashlib.sha256(observed_body).hexdigest()


def _load_attestation(
    run_root: Path, request_value: dict[str, Any], attempt: dict[str, Any],
) -> dict[str, Any]:
    relative, expected_path = _attestation_location(run_root, request_value)
    if attempt.get("attestation_path") != relative:
        raise CreateError("durable Vercel create attestation path changed")
    source = _private_regular(expected_path, "durable Vercel create attestation")
    try:
        body = source.read_bytes()
    except OSError as exc:
        raise CreateError("durable Vercel create attestation is unavailable") from exc
    if hashlib.sha256(body).hexdigest() != attempt.get("attestation_sha256"):
        raise CreateError("durable Vercel create attestation changed")
    value = _read_object(source, "durable Vercel create attestation")
    canonical = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if body != canonical:
        raise CreateError("durable Vercel create attestation is not canonical JSON")
    return _validate_evidence(
        value, request_value=request_value, metadata=attempt["metadata"],
    )


def run_create(
    *, request_path: Path, secret_env_file: Path, evidence_output: Path,
    attempt_state: Path, opener=None, clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    request_value, config_body, run_root, request_body = _request_context(
        request_path,
    )
    attempt_path, attempt = _load_attempt(
        attempt_state, run_root=run_root, request_value=request_value,
        request_body=request_body,
    )
    if attempt["state"] in {"attested", "provider_verified"}:
        evidence = _load_attestation(run_root, request_value, attempt)
        _write_private(evidence_output, evidence)
        return evidence

    client = VercelCreateClient(_token(secret_env_file), opener=opener)
    if attempt["state"] == "prepared":
        digest = client.upload_config(request_value, config_body)
        attempt = _transition_attempt(
            attempt_path, attempt, state="submission_started", clock=clock,
        )
        attempt = _validate_attempt(
            attempt, request_value=request_value, request_body=request_body,
        )
        evidence = client.create_deployment(
            request_value, digest=digest, config_size=len(config_body),
            metadata=attempt["metadata"],
        )
    else:
        evidence = client.reconcile(request_value, attempt["metadata"])
    relative, digest = _persist_attestation(
        run_root, request_value, evidence, attempt["metadata"],
    )
    attempt = _transition_attempt(
        attempt_path, attempt, state="attested", clock=clock,
        attestation_path=relative, attestation_sha256=digest,
    )
    _validate_attempt(
        attempt, request_value=request_value, request_body=request_body,
    )
    _write_private(evidence_output, evidence)
    return evidence


def _write_private(path: Path, value: dict[str, Any]) -> None:
    destination = _private_regular(path, "create evidence output")
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=".vercel-create-", dir=destination.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--secret-env-file", required=True)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--attempt-state", required=True)
    args = parser.parse_args()
    try:
        run_create(
            request_path=Path(args.request),
            secret_env_file=Path(args.secret_env_file),
            evidence_output=Path(args.evidence_output),
            attempt_state=Path(args.attempt_state),
        )
        return 0
    except (CreateError, OSError, UnicodeError) as exc:
        print(f"Vercel create adapter blocked: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
