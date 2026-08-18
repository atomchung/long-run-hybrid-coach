"""Data-free identity bindings for one gateway deployment, whatever reaches it."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ENVIRONMENT_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
INSTANCE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

DEPLOYMENT_ENVIRONMENT_ENV_VAR = "GARMIN_COACH_LOOP_DEPLOYMENT_ENVIRONMENT"
DEPLOYMENT_INSTANCE_ID_ENV_VAR = "GARMIN_COACH_LOOP_DEPLOYMENT_INSTANCE_ID"
EXPECTED_DEPLOYMENT_IDENTITY_FILE_ENV_VAR = (
    "GARMIN_COACH_LOOP_EXPECTED_DEPLOYMENT_IDENTITY_FILE"
)
MIN_CONFIGURATION_HMAC_KEY_BYTES = 32


class ReleaseIdentityError(ValueError):
    pass


def normalise_deployment_environment(value: str) -> str:
    if not isinstance(value, str):
        raise ReleaseIdentityError("deployment environment must be a string")
    environment = value.strip()
    if not ENVIRONMENT_RE.fullmatch(environment):
        raise ReleaseIdentityError(
            "deployment environment must be a lowercase slug of at most 32 characters"
        )
    return environment


def normalise_deployment_instance_id(value: str) -> str:
    if not isinstance(value, str):
        raise ReleaseIdentityError("deployment instance_id must be a string")
    instance_id = value.strip()
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ReleaseIdentityError(
            "deployment instance_id must be a lowercase nonsecret identifier "
            "of at most 64 characters"
        )
    return instance_id


def make_configuration_binding(
    *,
    resolved_state_root: Path | str,
    intervals_client_id: str,
    environment: str,
    instance_id: str,
    token_hmac_key: bytes,
) -> str:
    """Bind the effective private configuration without disclosing its values.

    The token-fingerprint key first derives a purpose-specific key, so it influences the
    result without reusing the token-fingerprint HMAC message domain.  The returned digest
    is safe to compare and expose; none of the bound inputs can be recovered from it.
    ``resolved_state_root`` must already be the path the gateway will actually use.
    """
    root = Path(resolved_state_root)
    if not root.is_absolute() or root != root.resolve():
        raise ReleaseIdentityError("deployment state root must be resolved and absolute")
    if not isinstance(intervals_client_id, str):
        raise ReleaseIdentityError("Intervals client_id must be a string")
    client_id = intervals_client_id.strip()
    if not client_id:
        raise ReleaseIdentityError("Intervals client_id must be a non-empty string")
    if (
        not isinstance(token_hmac_key, (bytes, bytearray))
        or len(bytes(token_hmac_key)) < MIN_CONFIGURATION_HMAC_KEY_BYTES
    ):
        raise ReleaseIdentityError("token HMAC key must be at least 32 bytes")
    environment = normalise_deployment_environment(environment)
    instance_id = normalise_deployment_instance_id(instance_id)
    message = json.dumps(
        {
            "environment": environment,
            "instance_id": instance_id,
            "intervals_client_id": client_id,
            "resolved_state_root": str(root),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    binding_key = hmac.new(
        bytes(token_hmac_key),
        b"garmin-coach-loop/configuration-binding/key/v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(
        binding_key,
        b"garmin-coach-loop/configuration-binding/message/v1\0" + message,
        hashlib.sha256,
    ).hexdigest()


def make_deployment_identity(
    *,
    resolved_state_root: Path | str,
    intervals_client_id: str,
    environment: str,
    instance_id: str,
    token_hmac_key: bytes,
) -> dict[str, str]:
    """Pure, exportable helper used by both the gateway and a trusted runner."""
    environment = normalise_deployment_environment(environment)
    instance_id = normalise_deployment_instance_id(instance_id)
    return {
        "environment": environment,
        "instance_id": instance_id,
        "configuration_binding": make_configuration_binding(
            resolved_state_root=resolved_state_root,
            intervals_client_id=intervals_client_id,
            environment=environment,
            instance_id=instance_id,
            token_hmac_key=token_hmac_key,
        ),
    }


def deployment_identity(payload: dict[str, Any]) -> dict[str, str]:
    """Validate the strict, privacy-safe identity returned by ``/healthz``."""
    required = ("environment", "instance_id", "configuration_binding")
    if not isinstance(payload, dict) or set(payload) != set(required):
        raise ReleaseIdentityError("runtime deployment identity is incomplete or unexpected")
    if any(not isinstance(payload[key], str) for key in required):
        raise ReleaseIdentityError("runtime deployment identity fields must be strings")
    identity = {key: payload[key] for key in required}
    environment = normalise_deployment_environment(identity["environment"])
    instance_id = normalise_deployment_instance_id(identity["instance_id"])
    if environment != identity["environment"] or instance_id != identity["instance_id"]:
        raise ReleaseIdentityError("runtime deployment identity is not canonical")
    identity["environment"] = environment
    identity["instance_id"] = instance_id
    if not SHA256_RE.fullmatch(identity["configuration_binding"]):
        raise ReleaseIdentityError("runtime configuration binding must be SHA-256 hex")
    return identity


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise_gateway_domain(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment or "YOUR-GATEWAY-DOMAIN" in value:
        raise ReleaseIdentityError("gateway domain must be one concrete HTTPS origin")
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReleaseIdentityError("gateway domain must be one concrete HTTPS origin") from exc
    return "https://" + host + (f":{port}" if port and port != 443 else "")


def package_artifact_sha256(files: list[tuple[str, bytes]]) -> str:
    """One deterministic digest for every executed package source file."""
    digest = hashlib.sha256()
    for path, body in sorted(files):
        digest.update(path.encode("utf-8") + b"\0" + body + b"\0")
    return digest.hexdigest()


def make_release_id(*, git_commit: str, instructions_sha256: str, openapi_sha256: str, gateway_artifact_sha256: str, gateway_domain: str) -> str:
    if not COMMIT_RE.fullmatch(git_commit):
        raise ReleaseIdentityError("git commit must be a full 40-character SHA")
    for value in (instructions_sha256, openapi_sha256, gateway_artifact_sha256):
        if not SHA256_RE.fullmatch(value):
            raise ReleaseIdentityError("content hashes must be SHA-256 hex")
    domain = normalise_gateway_domain(gateway_domain)
    return "gclr-" + sha256_text("\n".join((git_commit, instructions_sha256, openapi_sha256, gateway_artifact_sha256, domain)))


def release_identity(payload: dict[str, Any]) -> dict[str, str]:
    required = ("release_id", "git_commit", "instructions_sha256", "openapi_sha256", "gateway_artifact_sha256", "gateway_domain")
    if set(required) - set(payload):
        raise ReleaseIdentityError("runtime release identity is incomplete")
    identity = {key: str(payload[key]) for key in required}
    domain = normalise_gateway_domain(identity["gateway_domain"])
    expected = make_release_id(
        git_commit=identity["git_commit"],
        instructions_sha256=identity["instructions_sha256"],
        openapi_sha256=identity["openapi_sha256"],
        gateway_artifact_sha256=identity["gateway_artifact_sha256"],
        gateway_domain=domain,
    )
    if identity["release_id"] != expected:
        raise ReleaseIdentityError("runtime release_id does not bind its content")
    identity["gateway_domain"] = domain
    return identity
