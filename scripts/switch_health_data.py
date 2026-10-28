"""Turn one deployment's health data switch on or off, end to end, in one command.

The switch is a Railway variable, but the variable alone does nothing: the catalogue a
deployment serves moves with it, readiness compares that catalogue against the staged
release identity, and Railway's health check keeps the previous deployment serving when
the two disagree. So a flip is four steps, run here in order:

1. read which commit the deployment runs from its own ``/readyz``;
2. build that commit's release identity for the new setting (``release_bundle.bundle``);
3. stage the switch and the identity together, then redeploy the same source;
4. read ``/readyz`` back until it reports the new setting, or say that it did not.

    python3 scripts/switch_health_data.py off
    python3 scripts/switch_health_data.py on --service coach-plugin

The Railway CLI must be logged in and run from a directory linked to the project, or be
given ``--project``/``--environment``. Nothing here changes code, an athlete's Intervals
authorization or their records; see docs/ops/independent-coach-products.md.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach_loop.gateway import (  # noqa: E402
    RELEASE_ARTIFACT_SHA_ENV_VAR,
    RELEASE_COMMIT_ENV_VAR,
    RELEASE_DOMAIN_ENV_VAR,
    RELEASE_ID_ENV_VAR,
    RELEASE_INSTRUCTIONS_SHA_ENV_VAR,
    RELEASE_SKILL_SHA_ENV_VAR,
    RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR,
)
from garmin_coach_loop.product_scope import HEALTH_DATA_ENV_VAR  # noqa: E402
from garmin_coach_loop.release_identity import (  # noqa: E402
    ReleaseIdentityError,
    normalise_gateway_domain,
)
from scripts import release_bundle  # noqa: E402

DEFAULT_SERVICE = "coach-plugin"
DEFAULT_GATEWAY_DOMAIN = "https://coach-plugin-production.up.railway.app"
READ_TIMEOUT_SECONDS = 10
READ_ATTEMPTS = 3
WAIT_SECONDS = 600
POLL_SECONDS = 15

# Bundle key -> the variable the gateway reads it from.
_RELEASE_VARIABLES = {
    "release_id": RELEASE_ID_ENV_VAR,
    "git_commit": RELEASE_COMMIT_ENV_VAR,
    "instructions_sha256": RELEASE_INSTRUCTIONS_SHA_ENV_VAR,
    "tool_catalogue_sha256": RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR,
    "skill_sha256": RELEASE_SKILL_SHA_ENV_VAR,
    "gateway_domain": RELEASE_DOMAIN_ENV_VAR,
    "gateway_artifact_sha256": RELEASE_ARTIFACT_SHA_ENV_VAR,
}


class SwitchError(RuntimeError):
    """The switch could not be read, staged or confirmed."""


def read_readiness(domain: str) -> dict[str, Any]:
    """The deployment's own ``/readyz`` body; a 503 still carries one."""
    last: Exception | None = None
    for attempt in range(READ_ATTEMPTS):
        try:
            with urllib.request.urlopen(f"{domain}/readyz", timeout=READ_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return json.load(exc)
            except ValueError as body_error:
                last = body_error
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
        if attempt + 1 < READ_ATTEMPTS:
            time.sleep(2)
    raise SwitchError(f"could not read {domain}/readyz: {last}")


def has_commit(commit: str) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=ROOT, capture_output=True
    ).returncode == 0


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def staged_variables(bundle: dict[str, Any], health_data: bool) -> dict[str, str]:
    """The switch and the release identity built for it, staged together."""
    staged = {HEALTH_DATA_ENV_VAR: "on" if health_data else "off"}
    staged.update({name: str(bundle[key]) for key, name in _RELEASE_VARIABLES.items()})
    return staged


def railway_commands(service: str, staged: dict[str, str], scope: list[str]) -> list[list[str]]:
    """Stage every variable without a deploy, then redeploy the source already running."""
    sets = [arg for name, value in staged.items() for arg in ("--set", f"{name}={value}")]
    return [
        ["railway", "variables", "--service", service, *scope, "--skip-deploys", *sets],
        ["railway", "redeploy", "--service", service, *scope, "--yes"],
    ]


def switch(
    health_data: bool,
    *,
    service: str,
    domain: str,
    scope: list[str],
    read: Callable[[str], dict[str, Any]] = read_readiness,
    build: Callable[[str, str, bool], dict[str, Any]] = release_bundle.bundle,
    execute: Callable[[list[str]], None] = run,
    commit_present: Callable[[str], bool] = has_commit,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Flip ``service`` to ``health_data`` and return the readiness that confirms it."""
    wanted = "on" if health_data else "off"
    live = read(domain)
    if live.get("status") == "ok" and live.get("health_data") == wanted:
        return live
    identity = live.get("release_identity") or {}
    commit = identity.get("git_commit")
    if not commit or not identity.get("gateway_domain"):
        raise SwitchError(f"{domain} reports no release identity; deploy it once before switching")
    if not commit_present(commit):
        raise SwitchError(f"commit {commit} is not in this checkout; run git fetch and retry")
    # Built for the domain the running release names, not the URL used to reach it: the
    # issuer, audience and OAuth callback come from it, so another value would sign every
    # connection out.
    bundle = build(commit, identity["gateway_domain"], health_data)
    stage, redeploy = railway_commands(service, staged_variables(bundle, health_data), scope)
    try:
        execute(stage)
    except subprocess.CalledProcessError as exc:
        raise SwitchError(f"staging the variables failed; nothing was redeployed: {exc}") from exc
    try:
        execute(redeploy)
    except subprocess.CalledProcessError as exc:
        raise SwitchError(
            f"the variables are staged as health_data={wanted} but the redeploy failed, so "
            "they ship with whatever deploys next. Re-run to retry, or run the other value "
            f"to put them back: {exc}"
        ) from exc
    deadline = clock() + wait_seconds
    while True:
        try:
            now = read(domain)
        except SwitchError:
            now = {}
        if (
            now.get("status") == "ok"
            and now.get("health_data") == wanted
            and (now.get("release_identity") or {}).get("release_id") == bundle["release_id"]
        ):
            return now
        if clock() >= deadline:
            raise SwitchError(
                f"{service} still reports health_data={now.get('health_data')} and "
                f"status={now.get('status')}. Railway keeps the previous deployment serving "
                "when the new one fails readiness, so read its deploy logs. The staged "
                f"variables already say {wanted}: re-run to retry, or run the other value to "
                "put them back."
            )
        sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("setting", choices=("on", "off"))
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--gateway-domain", default=DEFAULT_GATEWAY_DOMAIN)
    parser.add_argument("--project")
    parser.add_argument("--environment")
    args = parser.parse_args(argv)
    scope = [
        *(["--project", args.project] if args.project else []),
        *(["--environment", args.environment] if args.environment else []),
    ]
    try:
        live = switch(
            args.setting == "on",
            service=args.service,
            domain=normalise_gateway_domain(args.gateway_domain),
            scope=scope,
        )
    except (SwitchError, ReleaseIdentityError, subprocess.CalledProcessError) as exc:
        print(f"switch failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"{args.service}: health_data={live['health_data']}, status={live['status']}, "
        f"release {live['release_identity']['release_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
