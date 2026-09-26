#!/usr/bin/env python3
"""Refuse Registry publication until this exact source is serving at production /readyz.

With no arguments the gate answers once, which is how the publish workflow calls it. An
operator checking a roll from a terminal can pass `--wait-minutes N` to keep asking until
production serves this commit or the deadline passes, instead of re-running by hand while a
deployment comes up. A source/version mismatch inside this checkout is not waited on -- no
deployment fixes it.

With `--registry-output PATH`, also check the exact published version after readiness and
write whether publication is needed to a GitHub Actions output file. An already-current
entry succeeds; a conflicting entry or failed lookup does not.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_bundle import bundle, commit_at_head, _open_without_redirects
from garmin_coach_loop.gateway import PRODUCT_VERSION
from garmin_coach_loop.release_identity import ReleaseIdentityError, release_identity

DOMAIN = "https://mcp.paceandstaystrong.com"
REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"


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


def registry_version():
    registry = json.loads((ROOT / "server.json").read_text())
    if registry["version"] != PRODUCT_VERSION:
        raise ReleaseIdentityError("Registry version differs from source version")
    return registry["version"]


def registry_publication_required():
    """Only a missing version needs publication; a conflicting entry is an error."""
    manifest = json.loads((ROOT / "server.json").read_text())
    url = (f"{REGISTRY}/{quote(manifest['name'], safe='')}/versions/"
           f"{quote(manifest['version'], safe='')}")
    try:
        with _open_without_redirects(url, timeout=15) as response:
            if response.geturl() != url:
                raise ReleaseIdentityError("Registry version lookup redirected")
            published = json.loads(response.read())
    except HTTPError as exc:
        if exc.code == 404:
            print(f"Registry does not carry version {manifest['version']}; publication required")
            return True
        raise
    server = published.get("server", {})
    if any(server.get(field) != manifest[field] for field in ("name", "version", "remotes")):
        raise ReleaseIdentityError("published Registry version or remote differs from server.json")
    if published.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {}).get("status") != "active":
        raise ReleaseIdentityError("published Registry version is not active")
    print(f"Registry already carries version {manifest['version']} with the same remote; "
          "entry is current, skipping publication")
    return False


def production_receipt(expected, version):
    url = DOMAIN + "/readyz"
    with _open_without_redirects(url, timeout=15) as response:
        if response.geturl() != url:
            raise ReleaseIdentityError("production readiness redirected")
        health = json.loads(response.read())
    verify_readyz(health, expected, version)
    return health


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wait-minutes", type=float, default=0.0,
                        help="keep polling production until it serves this source, or give up after this long")
    parser.add_argument("--poll-seconds", type=float, default=30.0,
                        help="seconds between polls while waiting")
    parser.add_argument("--registry-output", type=Path,
                        help="after readiness succeeds, check the Registry and append publish_required to this GitHub output file")
    args = parser.parse_args(argv)
    version = registry_version()
    expected = bundle(commit_at_head(), DOMAIN)
    deadline = time.monotonic() + args.wait_minutes * 60
    while True:
        try:
            health = production_receipt(expected, version)
        except (ReleaseIdentityError, OSError, ValueError, KeyError) as exc:
            if time.monotonic() >= deadline:
                raise
            print(f"production is not serving this source yet ({exc}); "
                  f"polling again in {args.poll_seconds:g}s", file=sys.stderr, flush=True)
            time.sleep(args.poll_seconds)
            continue
        print(json.dumps(health, sort_keys=True))
        if args.registry_output is not None:
            required = registry_publication_required()
            with args.registry_output.open("a") as output:
                output.write(f"publish_required={str(required).lower()}\n")
        return


if __name__ == "__main__":
    try:
        main()
    except (ReleaseIdentityError, OSError, ValueError, KeyError) as exc:
        sys.exit(f"Registry release gate blocked: {exc}")
