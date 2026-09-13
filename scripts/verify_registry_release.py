#!/usr/bin/env python3
"""Refuse Registry publication until this exact source is serving at production /readyz.

With no arguments the gate answers once. `--wait-minutes N` keeps asking until production
serves this commit or the deadline passes: a push to `production` starts the publish
workflow at the same moment it starts the deployment, and a deployment takes minutes to
come up, so the workflow waits for the new receipt instead of failing against the old one.
A source/version mismatch inside this checkout is not waited on -- no deployment fixes it.
"""
import argparse
import json
import sys
import time
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


def registry_version():
    registry = json.loads((ROOT / "server.json").read_text())
    if registry["version"] != PRODUCT_VERSION:
        raise ReleaseIdentityError("Registry version differs from source version")
    return registry["version"]


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
        return


if __name__ == "__main__":
    try:
        main()
    except (ReleaseIdentityError, OSError, ValueError, KeyError) as exc:
        sys.exit(f"Registry release gate blocked: {exc}")
