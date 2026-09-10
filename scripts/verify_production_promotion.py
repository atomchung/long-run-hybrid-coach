#!/usr/bin/env python3
"""Verify a production promotion without repeating the main full test suite.

The production branch is only a release pointer. This gate proves that its exact SHA
is the current ``main`` head, that the successful full CI run belongs to that SHA, and
that the release bundle/identity can be built for it. Railway's post-deploy ``/readyz``
still proves what actually started with the staged private configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# This runs as `python3 scripts/verify_production_promotion.py` in CI, so the repository
# root is not on the path yet. Same bootstrap as the other release scripts.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_bundle import bundle  # noqa: E402
from garmin_coach_loop.release_identity import (  # noqa: E402
    ReleaseIdentityError,
    release_identity,
)


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_DOMAIN = "https://mcp.paceandstaystrong.com"
API_ROOT = "https://api.github.com"


class PromotionGateError(ValueError):
    pass


def github_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise PromotionGateError("GitHub API response was not an object")
    return payload


def verify_main_green(
    *,
    repository: str,
    sha: str,
    token: str,
    api: Callable[[str, str], dict[str, Any]] = github_json,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(sha):
        raise PromotionGateError("promotion SHA must be a full 40-character lowercase SHA")
    if not repository or "/" not in repository:
        raise PromotionGateError("GitHub repository must be owner/name")

    main_ref = api(f"{API_ROOT}/repos/{repository}/git/ref/heads/main", token)
    main_object = main_ref.get("object")
    main_sha = main_object.get("sha") if isinstance(main_object, dict) else None
    if main_sha != sha:
        raise PromotionGateError(
            f"production SHA is not current main: main is {main_sha or 'unknown'}"
        )

    query = urllib.parse.urlencode(
        {
            "branch": "main",
            "event": "push",
            "head_sha": sha,
            "status": "completed",
            "per_page": "20",
        }
    )
    runs = api(
        f"{API_ROOT}/repos/{repository}/actions/workflows/ci.yml/runs?{query}",
        token,
    ).get("workflow_runs")
    if not isinstance(runs, list):
        raise PromotionGateError("GitHub did not return main CI runs")
    successful = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not successful:
        raise PromotionGateError("the exact SHA has no successful main push CI run")
    return {"main_sha": main_sha, "successful_main_runs": [run.get("id") for run in successful]}


def verify_release_bundle(*, sha: str, gateway_domain: str) -> dict[str, str]:
    try:
        release = bundle(sha, gateway_domain)
        identity = release_identity(release)
    except (ReleaseIdentityError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise PromotionGateError(f"release bundle/identity is invalid: {exc}") from exc
    if identity["git_commit"] != sha:
        raise PromotionGateError("release bundle commit does not equal promotion SHA")
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--gateway-domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    args = parser.parse_args()
    try:
        if not args.sha:
            raise PromotionGateError("GITHUB_SHA is required")
        if not args.repository:
            raise PromotionGateError("GITHUB_REPOSITORY is required")
        if not args.token:
            raise PromotionGateError("GITHUB_TOKEN is required")
        main_result = verify_main_green(
            repository=args.repository,
            sha=args.sha,
            token=args.token,
        )
        identity = verify_release_bundle(sha=args.sha, gateway_domain=args.gateway_domain)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "promotion_sha": args.sha,
                    "main": main_result,
                    "release_id": identity["release_id"],
                    "gateway_artifact_sha256": identity["gateway_artifact_sha256"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (PromotionGateError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"production promotion gate blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
