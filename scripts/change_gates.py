#!/usr/bin/env python3
"""Classify the expensive gates a change actually needs.

The result is deliberately about evidence surfaces, not release version numbers. It
is safe to use for local planning because a model-facing or executable change is
classified conservatively; documentation-only changes do not acquire a live ceremony.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]

LIVE_SMOKE_PATHS = frozenset(
    {
        "garmin_coach_loop/gateway.py",
        "garmin_coach_loop/identity.py",
        "garmin_coach_loop/token_envelope.py",
        "garmin_coach_loop/source_intervals.py",
        "garmin_coach_loop/delivery.py",
        "garmin_coach_loop/delivery_content.py",
        "garmin_coach_loop/decision_delivery.py",
    }
)

MCP_SURFACE_MARKERS = (
    "Tool(",
    "name=",
    "title=",
    "description=",
    "input_schema=",
    "output_schema=",
    "annotations=",
    "_hints(",
    "inputSchema",
    "outputSchema",
    "serverInfo",
    "prompts",
)

LIVE_BOUNDARY_MARKERS = (
    "oauth",
    "authorize",
    "callback",
    "pkce",
    "token",
    "client_origin",
    "prepare_delivery",
    "apply_delivery",
    "provider_payload",
    "verify_readback",
    "delivery_state",
    "intervals_accepted",
    "calendar",
)


def _normalise_path(raw_path: str) -> str:
    path = raw_path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _changed_lines(diff: str) -> str:
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def mcp_surface_changed(path: str, diff: str | None = None) -> bool:
    if path != "garmin_coach_loop/mcp_transport.py":
        return False
    if diff is None:
        return True
    changed = _changed_lines(diff)
    return any(marker in changed for marker in MCP_SURFACE_MARKERS)


def live_boundary_changed(path: str, diff: str | None = None) -> bool:
    if path not in LIVE_SMOKE_PATHS:
        return False
    if diff is None:
        return True
    changed = _changed_lines(diff).lower()
    return any(marker in changed for marker in LIVE_BOUNDARY_MARKERS)


def classify_changed_paths(
    changed_paths: Iterable[str],
    *,
    diffs_by_path: dict[str, str] | None = None,
) -> dict[str, object]:
    paths = sorted(set(_normalise_path(path) for path in changed_paths if path.strip()))
    diffs_by_path = diffs_by_path or {}
    smoke_reasons: list[str] = []
    surface_reasons: list[str] = []

    for path in paths:
        if live_boundary_changed(path, diffs_by_path.get(path)):
            smoke_reasons.append(path)

        if path in {
            "garmin_coach_loop/orchestration.md",
            "garmin_coach_loop/orchestration.py",
            "garmin_coach_loop/hybrid_training.md",
        }:
            surface_reasons.append(path)
        elif path.startswith(".agents/skills/garmin-coach-loop/"):
            # The Skill is a client-facing surface, but the current OpenAI submission
            # is MCP-only and does not snapshot it. Client acceptance still applies.
            surface_reasons.append(path)
        elif path.startswith("contracts/") and path.endswith(".json"):
            surface_reasons.append(path)
        elif mcp_surface_changed(path, diffs_by_path.get(path)):
            surface_reasons.append(path)

    live_smoke = bool(smoke_reasons)
    client_acceptance = bool(surface_reasons)
    scan_tools = any(
        not path.startswith(".agents/skills/garmin-coach-loop/")
        for path in surface_reasons
    )
    return {
        "changed_paths": paths,
        "live_smoke": live_smoke,
        "live_smoke_reasons": smoke_reasons,
        "client_acceptance": client_acceptance,
        "client_acceptance_reasons": surface_reasons,
        "scan_tools": scan_tools,
        "plugin_resubmission": scan_tools,
        "notes": [
            "production /readyz verification remains required after every deployment",
            "Skill-only changes need client acceptance but not Scan Tools for the current MCP-only OpenAI submission",
            "a changed tool catalogue, schema, annotation, or served prompt needs Scan Tools and a new plugin version",
        ],
    }


def _git_names(base: str | None) -> list[str]:
    commands = []
    if base:
        commands.append(["git", "diff", "--name-only", f"{base}...HEAD"])
    commands.extend(
        [
            ["git", "diff", "--name-only"],
            ["git", "diff", "--cached", "--name-only"],
            ["git", "ls-files", "--others", "--exclude-standard"],
        ]
    )
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
        paths.update(line for line in result.stdout.splitlines() if line)
    return sorted(paths)


def _git_diff_for_path(path: str, base: str | None) -> str | None:
    commands = []
    if base:
        commands.append(["git", "diff", "--unified=0", f"{base}...HEAD", "--", path])
    commands.extend(
        [
            ["git", "diff", "--unified=0", "--", path],
            ["git", "diff", "--cached", "--unified=0", "--", path],
        ]
    )
    diffs = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
        if result.stdout:
            diffs.append(result.stdout)
    return "\n".join(diffs) or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare committed changes with this ref")
    args = parser.parse_args()
    paths = _git_names(args.base)
    # Only the two file families whose hunk contents distinguish an internal edit
    # from a live/model-facing surface need diff inspection. The common docs/config
    # path stays a few git calls, even in a large documentation change.
    diff_paths = LIVE_SMOKE_PATHS | {"garmin_coach_loop/mcp_transport.py"}
    diffs = {
        path: _git_diff_for_path(path, args.base)
        for path in paths
        if path in diff_paths
    }
    print(
        json.dumps(
            classify_changed_paths(paths, diffs_by_path=diffs),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
