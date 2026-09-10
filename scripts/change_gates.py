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

PACKAGE = "garmin_coach_loop/"
PACKAGE_SUFFIXES = (".py", ".md")

LIVE_SMOKE_PATHS = frozenset(
    {
        "garmin_coach_loop/gateway.py",
        "garmin_coach_loop/identity.py",
        "garmin_coach_loop/token_envelope.py",
        "garmin_coach_loop/source_intervals.py",
        "garmin_coach_loop/delivery.py",
        "garmin_coach_loop/delivery_content.py",
        "garmin_coach_loop/decision_delivery.py",
        # The local MCP client performs the same register/authorize/PKCE/redeem hop a
        # connector does. Unit tests drive it against a fake provider; whether the hop
        # still works is a question only a real run answers.
        "garmin_coach_loop/hosted.py",
    }
)

MODEL_FACING_PATHS = frozenset(
    {
        "garmin_coach_loop/orchestration.md",
        "garmin_coach_loop/orchestration.py",
        "garmin_coach_loop/hybrid_training.md",
    }
)

# Diff-gated: the file carries both the served MCP surface and ordinary transport code.
DIFF_GATED_SURFACE_PATH = "garmin_coach_loop/mcp_transport.py"

# Everything else in the package, named rather than assumed. A file that is in none of
# these four lists is reported as unclassified and conservatively asks for both live
# smoke and client acceptance, and `tests/test_process_gates.py` refuses to let a new
# module stay that way -- the list is the decision, made once, at the pull request that
# adds the file.
INTERNAL_PACKAGE_PATHS = frozenset(
    {
        "garmin_coach_loop/__init__.py",
        "garmin_coach_loop/athlete_evidence.py",
        "garmin_coach_loop/cli.py",
        "garmin_coach_loop/connected_account.py",
        "garmin_coach_loop/context_builder.py",
        "garmin_coach_loop/context_core.py",
        "garmin_coach_loop/context_view.py",
        "garmin_coach_loop/decision_scope.py",
        "garmin_coach_loop/evidence_import.py",
        "garmin_coach_loop/fit_sets.py",
        "garmin_coach_loop/intent_text.py",
        "garmin_coach_loop/owner_data.py",
        "garmin_coach_loop/plan_change.py",
        "garmin_coach_loop/plan_init.py",
        "garmin_coach_loop/prescription.py",
        "garmin_coach_loop/proposals.py",
        "garmin_coach_loop/reconcile.py",
        "garmin_coach_loop/release_identity.py",
        "garmin_coach_loop/security_log.py",
        "garmin_coach_loop/source_personal_os.py",
        "garmin_coach_loop/store.py",
        "garmin_coach_loop/validation.py",
    }
)

CLASSIFIED_PACKAGE_PATHS = (
    LIVE_SMOKE_PATHS | MODEL_FACING_PATHS | INTERNAL_PACKAGE_PATHS | {DIFF_GATED_SURFACE_PATH}
)

# The files a reviewer or a registry actually receives. Editing one changes submitted
# bytes even when the served tool catalogue is untouched, so the next submission is a
# different submission. They do not move the reviewed MCP surface, so they do not ask
# for Scan Tools on their own.
SUBMISSION_ARTIFACT_PATHS = frozenset(
    {
        "chatgpt-app-submission.json",
        "server.json",
        "plugins/long-run-hybrid-coach/.codex-plugin/plugin.json",
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


def package_file(path: str) -> bool:
    return path.startswith(PACKAGE) and path.endswith(PACKAGE_SUFFIXES)


def unclassified_package_file(path: str) -> bool:
    """A file in the package that no list places. The tool refuses to call it internal."""

    return package_file(path) and path not in CLASSIFIED_PACKAGE_PATHS


def mcp_surface_changed(path: str, diff: str | None = None) -> bool:
    if path != DIFF_GATED_SURFACE_PATH:
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

        if path in MODEL_FACING_PATHS:
            surface_reasons.append(path)
        elif path.startswith(".agents/skills/garmin-coach-loop/"):
            # The Skill is a client-facing surface, but the current OpenAI submission
            # is MCP-only and does not snapshot it. Client acceptance still applies.
            surface_reasons.append(path)
        elif path.startswith("contracts/") and path.endswith(".json"):
            surface_reasons.append(path)
        elif mcp_surface_changed(path, diffs_by_path.get(path)):
            surface_reasons.append(path)

    submission_reasons = [path for path in paths if path in SUBMISSION_ARTIFACT_PATHS]

    # Scan Tools stays out of this: it is about the reviewed tool catalogue, and a new
    # module cannot move that without `mcp_transport.py` changing too, which is caught
    # above on its own.
    unclassified = [path for path in paths if unclassified_package_file(path)]
    smoke_reasons.extend(unclassified)
    surface_reasons.extend(unclassified)

    scan_tools = any(
        not path.startswith(".agents/skills/garmin-coach-loop/")
        and path not in unclassified
        for path in surface_reasons
    )
    live_smoke = bool(smoke_reasons)
    client_acceptance = bool(surface_reasons)
    resubmission_reasons = sorted(
        set(submission_reasons) | (set(surface_reasons) if scan_tools else set())
    )
    return {
        "changed_paths": paths,
        "unclassified_paths": unclassified,
        "live_smoke": live_smoke,
        "live_smoke_reasons": sorted(set(smoke_reasons)),
        "client_acceptance": client_acceptance,
        "client_acceptance_reasons": sorted(set(surface_reasons)),
        "scan_tools": scan_tools,
        "plugin_resubmission": bool(resubmission_reasons),
        "plugin_resubmission_reasons": resubmission_reasons,
        "notes": [
            "production /readyz verification remains required after every deployment",
            "Skill-only changes need client acceptance but not Scan Tools for the current MCP-only OpenAI submission",
            "a changed tool catalogue, schema, annotation, or served prompt needs Scan Tools and a new plugin version",
            "an edited submission packet, registry entry or plugin manifest is resubmitted content on its own, without a Scan Tools run",
            "an unclassified package file is treated as both a live and a model-facing surface until change_gates.py names it",
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
    diff_paths = LIVE_SMOKE_PATHS | {DIFF_GATED_SURFACE_PATH}
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
