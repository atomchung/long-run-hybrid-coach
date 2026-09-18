#!/usr/bin/env python3
"""Classify the expensive gates a change actually needs.

The result is deliberately about evidence surfaces, not release version numbers. It
is safe to use for local planning because a model-facing or executable change is
classified conservatively; documentation-only changes do not acquire a live ceremony.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
        # The wire every connected client reaches `/mcp` on. It serves no catalogue of
        # its own -- the reviewed surface is still `mcp_transport.py`, and the digest row
        # below is what notices a moved one whichever file moved it -- but it decides
        # which protocol era a request is answered on, so a change here is exactly the
        # kind only a real client proves.
        "garmin_coach_loop/mcp_sdk_transport.py",
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

# The wire itself. It serves no catalogue of its own -- so it never asks for Scan Tools or
# a resubmission -- but it decides which protocol era a request is answered on, and a
# 2026-07-28 client that is refused falls back to 2025 silently. Only a real client of
# each era proves that did not happen: docs/ops/accept-both-protocol-eras.md.
PROTOCOL_SURFACE_PATH = "garmin_coach_loop/mcp_sdk_transport.py"

# The dependency surface. `requirements.txt` is the intent, `requirements.lock` is what
# every build installs, and between them they decide which protocol implementation is
# serving `/mcp` -- without a line of this repository's code changing. Naming the files is
# only the question; `dependency_pin_at` below is the answer, so a comment edit in either
# file does not acquire a live ceremony.
DEPENDENCY_PATHS = frozenset({"requirements.txt", "requirements.lock"})

DEPENDENCY_PIN_MOVED_REASON = "requirements.lock (pinned dependency set moved)"
DEPENDENCY_PIN_UNKNOWN_REASON = "requirements.lock (pinned dependency set not comparable)"

# What travels to another ref so `tool_catalogue_sha256()` can be built there. The whole
# package goes, because the catalogue is assembled from constants several modules own.
# Today's import needs nothing else; `contracts/` rides along as the schema surface the
# catalogue mirrors, which is the one directory a tool definition might come to read.
CATALOGUE_EXPORT_PATHS = ("garmin_coach_loop", "contracts")

CATALOGUE_DIGEST_PROGRAM = (
    "from garmin_coach_loop.mcp_transport import tool_catalogue_sha256;"
    "print(tool_catalogue_sha256())"
)

# The reason line a moved digest reports: the surface, and the evidence that fired on it,
# so an operator reading the JSON can tell it apart from a line-marker hit. It stays the
# transport path even when some other module moved the catalogue -- the served surface is
# what the gate is about, not the file that happened to change.
CATALOGUE_MOVED_REASON = f"{DIFF_GATED_SURFACE_PATH} (tool_catalogue_sha256 moved)"

SHA256_RE = re.compile(r"[0-9a-f]{64}")

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
        "garmin_coach_loop/privacy_request.py",
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

# entrypoints/demo/ is a second deployable, not part of the package above -- AGENTS.md says
# so explicitly -- so it gets its own classification rather than being folded into the
# lists above. Named here: everything that decides whether the demo can answer a real
# visitor turn at all -- its model settings, its orchestration prompt, the service/session/
# HTTP wiring around them, the fixture it replays against, and the two deploy artifacts
# that put a build in front of traffic. No unit test judges any of these (they run against
# a fake model), and docs/ops/deploy-demo-service.md ("Proving it") names the acceptance
# run as the only check in this repository that reaches the real Responses API. Issue #478
# is what a change here does without this row: a changed constant, a green suite, and every
# visitor turn answering 504 until somebody ran the command by hand.
DEMO_ENTRYPOINT_PREFIX = "entrypoints/demo/"

DEMO_ACCEPTANCE_PATHS = frozenset(
    {
        "entrypoints/demo/model.py",
        "entrypoints/demo/service.py",
        "entrypoints/demo/boundary.py",
        "entrypoints/demo/orchestration.md",
        "entrypoints/demo/config.py",
        "entrypoints/demo/server.py",
        "entrypoints/demo/sessions.py",
        "entrypoints/demo/fixture.py",
        # Outside entrypoints/demo/ itself, but the same question: a moved model image, a
        # moved provider timeout or a moved health check reaches production before any
        # test does.
        "Dockerfile.demo",
        "railway.demo.toml",
    }
)

# A prefix, not a fixed set, the way LIVE_SMOKE_PATHS and MODEL_FACING_PATHS are fixed sets
# but contracts/*.json below is a prefix: the acceptance run replays committed prompts
# against this committed fixture, so a changed fixture is a changed answer whichever file
# inside the directory moved.
DEMO_ACCEPTANCE_FIXTURE_PREFIX = "entrypoints/demo/fixtures/"

# Named rather than assumed, the same way INTERNAL_PACKAGE_PATHS is: these ask for no live
# run -- the README, the acceptance script itself, and the two files Python requires to
# exist and that carry no behaviour of their own. tests/ also asks for no live run here,
# but it is a prefix outside entrypoints/demo/ entirely (already mapped to the demo's own
# unit tests by scripts/test_selection.py) and never matches DEMO_ENTRYPOINT_PREFIX, so it
# needs no entry in this set.
DEMO_NO_GATE_PATHS = frozenset(
    {
        "entrypoints/demo/README.md",
        "entrypoints/demo/acceptance.py",
        "entrypoints/demo/__init__.py",
        "entrypoints/demo/__main__.py",
    }
)

# The one command that proves any of the paths above: the acceptance run against the
# deployed service (docs/ops/deploy-demo-service.md, "Proving it"). Every path shares the
# same command, so the reason string can name it once and reuse it per path.
DEMO_ACCEPTANCE_COMMAND = (
    "python3 -m entrypoints.demo.acceptance --base-url https://demo-api.paceandstaystrong.com"
)

CLASSIFIED_DEMO_PATHS = DEMO_ACCEPTANCE_PATHS | DEMO_NO_GATE_PATHS

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


def _catalogue_digest_in(directory: str | Path) -> str | None:
    """Run ``tool_catalogue_sha256()`` against the package in ``directory``.

    A child interpreter, never an in-process import: this module is also imported by the
    test suite, where ``garmin_coach_loop`` is already in ``sys.modules`` from the
    checkout, so an in-process import would answer for that copy whichever directory was
    asked. A cleared ``PYTHONPATH`` keeps the caller's environment out for the same reason.
    """
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", CATALOGUE_DIGEST_PROGRAM],
        cwd=directory,
        capture_output=True,
        text=True,
        env={**environment, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    digest = result.stdout.strip()
    if result.returncode != 0 or not SHA256_RE.fullmatch(digest):
        return None
    return digest


def tool_catalogue_sha256_at(ref: str) -> str | None:
    """The catalogue digest ``ref`` serves, or ``None`` when it cannot be built.

    There is no file to read: ``tools/list`` exists only once ``mcp_transport`` has been
    imported, so the ref's own package is exported into a scratch directory and imported
    there, leaving this checkout untouched. ``None`` is an answer rather than an error --
    an unknown ref, or a base that predates the module, simply carries no digest evidence,
    and a planning tool that raised there would report nothing at all.

    ``scripts/release_bundle.py`` hashes the same catalogue for a released commit and
    raises when it cannot. That one is binding a release identity; this one is choosing a
    gate, so the two disagree about failure on purpose.
    """
    with tempfile.TemporaryDirectory() as directory:
        archive = subprocess.run(
            ["git", "archive", ref, *CATALOGUE_EXPORT_PATHS],
            cwd=ROOT,
            capture_output=True,
        )
        if archive.returncode != 0:
            return None
        extracted = subprocess.run(
            ["tar", "-x", "-C", directory],
            input=archive.stdout,
            capture_output=True,
        )
        if extracted.returncode != 0:
            return None
        return _catalogue_digest_in(directory)


def working_tree_tool_catalogue_sha256() -> str | None:
    """The catalogue digest this checkout serves, uncommitted edits included."""

    return _catalogue_digest_in(ROOT)


def _pinned_dependency_set(contents: dict[str, str | None]) -> str:
    """One digest over what a build would actually install.

    Comments and blank lines are dropped before hashing, so the prose in
    `requirements.txt` -- which is where the reason for the dependency lives -- can be
    rewritten without anybody being sent to run a manual acceptance. What survives is the
    requirement lines and their `--hash=` continuations: the distributions, their
    versions, and the artifacts allowed to satisfy them. A missing file hashes as absent
    rather than as empty, so adding or deleting the lock is itself a move.
    """
    digest = hashlib.sha256()
    for name in sorted(contents):
        digest.update(f"\n[{name}]\n".encode("utf-8"))
        text = contents[name]
        if text is None:
            digest.update(b"<absent>")
            continue
        for line in text.splitlines():
            # pip's own comment rule: a `#` starts one only at the beginning of a line or
            # after whitespace. Splitting on every `#` would also eat the fragment of a
            # direct URL requirement -- which is where such a requirement carries its
            # artifact digest, and dropping that would hide the one thing this hashes for.
            stripped = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
            if stripped:
                digest.update(" ".join(stripped.split()).encode("utf-8") + b"\n")
    return digest.hexdigest()


def dependency_pin_at(ref: str) -> str | None:
    """The pinned dependency set ``ref`` builds from, or ``None`` when it cannot be read.

    ``None`` means "the question was never answered" here as it does for the catalogue
    digest, but what happens next is **not** the same and the difference is deliberate.
    An unanswered catalogue question falls back to the line markers in
    ``mcp_transport.py``, which are an estimate of the same thing. There is no estimate of
    a dependency pin -- a version and a set of hashes are either compared or they are not --
    so an unanswered pin asks for the acceptance run instead of assuming it stood still.
    Aligning the two would mean answering "no gate" from having measured nothing.
    """
    contents: dict[str, str | None] = {}
    for name in sorted(DEPENDENCY_PATHS):
        shown = subprocess.run(
            ["git", "show", f"{ref}:{name}"], cwd=ROOT, capture_output=True, text=True
        )
        if shown.returncode != 0:
            # A ref that predates the lock file is a real answer -- the file was not there
            # yet -- and a read that failed for any other reason is not an answer at all.
            # `git show` exits non-zero for both, so the tree is asked directly: a path it
            # does not list is genuinely absent, and anything else (an unresolvable ref, an
            # unreadable object, git missing) is `None`, which the classification reads as
            # "not measured" and answers conservatively. Guessing "absent" there would turn
            # a broken read into the claim that the pin stood still.
            listed = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", f"{ref}^{{tree}}", "--", name],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if listed.returncode != 0:
                return None
            if listed.stdout.strip():
                return None
            contents[name] = None
            continue
        contents[name] = shown.stdout
    return _pinned_dependency_set(contents)


def working_tree_dependency_pin() -> str:
    """The pinned dependency set this checkout would install, uncommitted edits included."""

    return _pinned_dependency_set(
        {
            name: (ROOT / name).read_text(encoding="utf-8")
            if (ROOT / name).is_file()
            else None
            for name in sorted(DEPENDENCY_PATHS)
        }
    )


def package_file(path: str) -> bool:
    return path.startswith(PACKAGE) and path.endswith(PACKAGE_SUFFIXES)


def unclassified_package_file(path: str) -> bool:
    """A file in the package that no list places. The tool refuses to call it internal."""

    return package_file(path) and path not in CLASSIFIED_PACKAGE_PATHS


def unclassified_demo_file(path: str) -> bool:
    """A file under entrypoints/demo/ that no list places -- the demo's analogue of
    ``unclassified_package_file``. A new demo module must not inherit silence by being
    new, so this is reported through the *same* ``unclassified_paths`` key the package
    uses (see the comment beside it in ``classify_changed_paths``) rather than a new key:
    a no-op diff must add only the two ``demo_acceptance*`` keys to the report, and a
    second ``unclassified_*`` key would not be one.
    """

    return (
        path.startswith(DEMO_ENTRYPOINT_PREFIX)
        and path not in CLASSIFIED_DEMO_PATHS
        and not path.startswith(DEMO_ACCEPTANCE_FIXTURE_PREFIX)
    )


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


def demo_acceptance_changed(path: str) -> bool:
    """Whole-path, unlike mcp_surface_changed/live_boundary_changed: no line in a diff
    distinguishes a demo settings change from an internal one the way ``description=`` or
    ``oauth`` does for the product's own surfaces -- the demo has no equivalent internal/
    live split, so any change to a named path is the gate.
    """
    return path in DEMO_ACCEPTANCE_PATHS or path.startswith(DEMO_ACCEPTANCE_FIXTURE_PREFIX)


def demo_acceptance_reason(path: str) -> str:
    """The reason string for one demo-surface path: the path, and the command that clears it.

    Every other reason in this module names only the path and leaves the command to the
    AGENTS.md table. This one inlines ``python3 -m entrypoints.demo.acceptance`` itself,
    because issue #478 was exactly a named gate whose command nobody ran by hand -- the
    string doubles as the reminder.
    """
    return f"{path} ({DEMO_ACCEPTANCE_COMMAND})"


def classify_changed_paths(
    changed_paths: Iterable[str],
    *,
    diffs_by_path: dict[str, str] | None = None,
    catalogue_moved: bool | None = None,
    dependency_pin_moved: bool | None = None,
) -> dict[str, object]:
    """Name the gates these paths need.

    ``catalogue_moved`` is the digest evidence: ``True`` when ``tool_catalogue_sha256()``
    differs between the base and this checkout, ``False`` when it does not, and ``None``
    when no base could be built and the question was never asked.

    It outranks the line markers whenever it is not ``None``. The digest is rebuilt from
    the same descriptors ``tools/list`` serves, so it answers the question the markers
    only estimate, and an estimate does not overturn the measurement it stands in for.

    ``dependency_pin_moved`` is the same three states for the installed dependency set,
    and it is only consulted when one of ``DEPENDENCY_PATHS`` is in the change. A moved
    pin is a transport change with no diff: the protocol implementation behind `/mcp`
    belongs to the SDK, so which revisions are agreed to, what an `initialize` result
    carries and which error a refused batch returns can all move while every line in this
    repository stands still. What that asks for is the dual-era acceptance run, and only
    that -- the reviewed tool catalogue and `instructions` are this repository's own
    bytes, so a pin cannot move them and must not trigger a resubmission.
    """
    paths = sorted(set(_normalise_path(path) for path in changed_paths if path.strip()))
    diffs_by_path = diffs_by_path or {}
    smoke_reasons: list[str] = []
    surface_reasons: list[str] = []
    protocol_reasons: list[str] = []
    demo_reasons: list[str] = []

    for path in paths:
        if live_boundary_changed(path, diffs_by_path.get(path)):
            smoke_reasons.append(path)

        if demo_acceptance_changed(path):
            demo_reasons.append(demo_acceptance_reason(path))

        if path in MODEL_FACING_PATHS:
            surface_reasons.append(path)
        elif path.startswith(".agents/skills/garmin-coach-loop/"):
            # The Skill is a client-facing surface, but the current OpenAI submission
            # is MCP-only and does not snapshot it. Client acceptance still applies.
            surface_reasons.append(path)
        elif path.startswith("contracts/") and path.endswith(".json"):
            surface_reasons.append(path)
        elif catalogue_moved is None and mcp_surface_changed(
            path, diffs_by_path.get(path)
        ):
            # The markers are the fallback, and only that: they read a changed *line*, so
            # rewriting the text inside a description string or the inner lines of an
            # input schema moves the served catalogue without touching one, and deleting
            # code that merely mentions `inputSchema` touches one without moving the
            # catalogue at all. Either way they are a guess about the served bytes.
            #
            # So they run only when the digest could not be built (`None`) and the
            # question therefore went unanswered. A digest that answered outranks them in
            # both directions: `True` names itself below, and `False` is proof -- it is
            # rebuilt from the same `descriptor()` output `tools/list` serves, so two
            # equal digests are two identical catalogues, and a marker cannot overturn
            # that by having matched a line.
            #
            # Issue #352's transport migration is why this is spelled out. It deleted the
            # hand-written protocol layer, whose removed lines contained `inputSchema`,
            # `prompts` and `serverInfo`, and the markers asked for Scan Tools and a
            # resubmission on a catalogue the digest proved byte-identical -- against a
            # release that was in OpenAI review at the time.
            surface_reasons.append(path)

    if PROTOCOL_SURFACE_PATH in paths:
        # Whatever the diff says: this module has no reviewed catalogue to move, so the
        # markers above never speak for it, and the failure it is capable of -- a
        # 2026-07-28 client answered `400` and falling back to 2025 -- is invisible from
        # inside the conversation that fell back.
        protocol_reasons.append(PROTOCOL_SURFACE_PATH)

    if any(path in DEPENDENCY_PATHS for path in paths):
        # The path check is load-bearing rather than belt-and-braces. `--base` compares
        # this checkout against the *base ref's* files, while the change list is what this
        # branch touched, so a lock that moved on `main` after the branch point makes the
        # two digests differ over a change the branch never made. Asking whether one of
        # these files is in the change is what keeps that from billing this branch for
        # somebody else's upgrade.
        if dependency_pin_moved is None:
            protocol_reasons.append(DEPENDENCY_PIN_UNKNOWN_REASON)
        elif dependency_pin_moved:
            protocol_reasons.append(DEPENDENCY_PIN_MOVED_REASON)

    if catalogue_moved:
        # The reviewed bytes are what the digest binds, so a moved digest is a changed
        # surface whichever file moved it -- the catalogue is assembled from constants
        # that live in several modules, not only in the transport one.
        surface_reasons.append(CATALOGUE_MOVED_REASON)

    submission_reasons = [path for path in paths if path in SUBMISSION_ARTIFACT_PATHS]

    # Scan Tools stays out of this: it is about the reviewed tool catalogue, and a new
    # module cannot move that without `mcp_transport.py` changing too, which is caught
    # above on its own.
    unclassified = [path for path in paths if unclassified_package_file(path)]
    smoke_reasons.extend(unclassified)
    surface_reasons.extend(unclassified)

    # The demo's own unnamed file. It is not a live MCP boundary or a reviewed tool
    # catalogue, so it does not join the two lists above -- only demo_acceptance, the one
    # gate an unrecognised file under entrypoints/demo/ could plausibly need. It still
    # joins `unclassified` (not a new list) so the report gains no third key beyond
    # demo_acceptance/demo_acceptance_reasons.
    unclassified_demo = [path for path in paths if unclassified_demo_file(path)]
    demo_reasons.extend(demo_acceptance_reason(path) for path in unclassified_demo)
    unclassified = sorted(unclassified + unclassified_demo)

    # Scan Tools and the resubmission are decided from the reviewed surface alone. A
    # protocol reason never enters either: the SDK owns the wire, this repository owns the
    # bytes on it, and a pin that moves the first cannot move the second.
    scan_tools = any(
        not path.startswith(".agents/skills/garmin-coach-loop/")
        and path not in unclassified
        for path in surface_reasons
    )
    live_smoke = bool(smoke_reasons)
    client_acceptance = bool(surface_reasons or protocol_reasons)
    resubmission_reasons = sorted(
        set(submission_reasons) | (set(surface_reasons) if scan_tools else set())
    )
    return {
        "changed_paths": paths,
        "unclassified_paths": unclassified,
        "live_smoke": live_smoke,
        "live_smoke_reasons": sorted(set(smoke_reasons)),
        "client_acceptance": client_acceptance,
        "client_acceptance_reasons": sorted(set(surface_reasons) | set(protocol_reasons)),
        "protocol_acceptance": bool(protocol_reasons),
        "protocol_acceptance_reasons": sorted(set(protocol_reasons)),
        "scan_tools": scan_tools,
        "plugin_resubmission": bool(resubmission_reasons),
        "plugin_resubmission_reasons": resubmission_reasons,
        "demo_acceptance": bool(demo_reasons),
        "demo_acceptance_reasons": sorted(set(demo_reasons)),
        "notes": [
            "production /readyz verification remains required after every deployment",
            "Skill-only changes need client acceptance but not Scan Tools for the current MCP-only OpenAI submission",
            "a changed tool catalogue, schema, annotation, or served prompt needs Scan Tools and a new plugin version",
            "an edited submission packet, registry entry or plugin manifest is resubmitted content on its own, without a Scan Tools run",
            "an unclassified package file is treated as both a live and a model-facing surface until change_gates.py names it",
            "a moved dependency pin or a changed wire module needs the dual-era acceptance run (docs/ops/accept-both-protocol-eras.md) and never a resubmission",
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
    head_digest = working_tree_tool_catalogue_sha256()
    base_digest = tool_catalogue_sha256_at(args.base) if args.base else None
    # Both digests are printed even when one is unknown, so a release receipt can quote
    # what the decision was made from rather than re-deriving it later.
    catalogue_moved = (
        None if base_digest is None or head_digest is None else base_digest != head_digest
    )
    head_pin = working_tree_dependency_pin()
    base_pin = dependency_pin_at(args.base) if args.base else None
    dependency_pin_moved = None if base_pin is None else base_pin != head_pin
    print(
        json.dumps(
            {
                **classify_changed_paths(
                    paths,
                    diffs_by_path=diffs,
                    catalogue_moved=catalogue_moved,
                    dependency_pin_moved=dependency_pin_moved,
                ),
                "tool_catalogue_sha256_base": base_digest,
                "tool_catalogue_sha256_head": head_digest,
                "dependency_pin_base": base_pin,
                "dependency_pin_head": head_pin,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
