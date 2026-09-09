#!/usr/bin/env python3
"""Refuse Registry publication until this exact source is serving at production /readyz."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_bundle import bundle, commit_at_head, _open_without_redirects
from garmin_coach_loop.gateway import PRODUCT_VERSION
from garmin_coach_loop.release_identity import ReleaseIdentityError, release_identity

DOMAIN = "https://mcp.paceandstaystrong.com"


def verify_readyz(health, expected, version):
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise ReleaseIdentityError("production /readyz is not ready")
    if health.get("source_git_commit") != expected["git_commit"]:
        raise ReleaseIdentityError("production source commit differs from publication source")
    if health.get("product_version") != version:
        raise ReleaseIdentityError("production version differs from Registry version")
    if release_identity(health.get("release_identity", {})) != release_identity(expected):
        raise ReleaseIdentityError("production release identity/digests differ from publication source")
    if health.get("deployment_identity", {}).get("environment") != "production":
        raise ReleaseIdentityError("receipt is not from production")


def main():
    expected = bundle(commit_at_head(), DOMAIN)
    registry = json.loads((ROOT / "server.json").read_text())
    if registry["version"] != PRODUCT_VERSION:
        raise ReleaseIdentityError("Registry version differs from source version")
    url = DOMAIN + "/readyz"
    with _open_without_redirects(url, timeout=15) as response:
        if response.geturl() != url:
            raise ReleaseIdentityError("production readiness redirected")
        health = json.loads(response.read())
    verify_readyz(health, expected, registry["version"])
    print(json.dumps(health, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ReleaseIdentityError, OSError, ValueError, KeyError) as exc:
        sys.exit(f"Registry release gate blocked: {exc}")
