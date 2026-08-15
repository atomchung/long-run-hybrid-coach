"""Data-free identity binding for a Custom GPT gateway release."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseIdentityError(ValueError):
    pass


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
