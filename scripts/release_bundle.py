#!/usr/bin/env python3
"""What this release *is*, and whether the gateway serving traffic is actually it.

Three subcommands, none of them about any one client:

- ``build`` hashes the commit's own orchestration prompt, MCP tool catalogue, canonical
  Agent Skill and package into one ``release_id`` bound to the domain it will be served
  from.
- ``deployment-identity`` computes the environment, instance and configuration binding
  the same deployment must report.
- ``verify`` reads ``/healthz`` on the live domain and refuses unless the release and
  deployment identity it reports are the ones built here.

It once carried a fourth step -- comparing the bundle against text a human had pasted
into a client's console -- and the release identity once bound a rendered API document
rather than the artifacts. Both are gone (issue #117). What remains is what ``/readyz``
is polled for on every deploy, whichever entry the athlete reaches the gateway through.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach_loop.release_identity import (  # noqa: E402
    DEPLOYMENT_ENVIRONMENT_ENV_VAR,
    DEPLOYMENT_INSTANCE_ID_ENV_VAR,
    EXPECTED_DEPLOYMENT_IDENTITY_FILE_ENV_VAR,
    PREDATES_RELEASE_IDENTITY_CHANGE,
    SHA256_RE,
    ReleaseIdentityError,
    deployment_identity,
    make_deployment_identity,
    make_release_id,
    normalise_gateway_domain,
    package_artifact_sha256,
    predates_release_identity_change,
    release_identity,
    sha256_text,
)
from garmin_coach_loop.gateway import (  # noqa: E402
    CLIENT_ID_ENV_VAR,
    MIN_HMAC_KEY_CHARACTERS,
    STATE_ROOT_ENV_VAR,
    TOKEN_HMAC_KEY_ENV_VAR,
)

# What a release is beyond its code, and what each one is doing here:
#
# - the orchestration prompt the gateway serves to any MCP client that asks for it;
# - the tool catalogue `/mcp` answers `tools/list` with -- not a file, so it is hashed by
#   running the commit's own code (see `tool_catalogue_digest`);
# - the canonical Agent Skill, which is what OpenAI and Claude packaging both install.
#
# All three are hashed into the release id, so a deploy that shipped one without the
# others is visible at `/readyz` instead of being discovered in a conversation.
INSTRUCTIONS = "garmin_coach_loop/orchestration.md"
PACKAGE = "garmin_coach_loop"
SKILL = ".agents/skills/garmin-coach-loop"
_CATALOGUE_PROGRAM = (
    "from garmin_coach_loop.mcp_transport import tool_catalogue_sha256;"
    "print(tool_catalogue_sha256())"
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _open_without_redirects(url: str, *, timeout: int):
    return urllib.request.build_opener(_NoRedirect()).open(url, timeout=timeout)


def outside_repo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ReleaseIdentityError("release evidence must be written outside the repository")
    return resolved


def git_text(commit: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{commit}:{path}"], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def commit_at_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def git_tree(commit: str, prefix: str, suffixes: tuple[str, ...] | None = None) -> list[tuple[str, bytes]]:
    """Every file the commit holds under one directory, as ``(relative path, bytes)``."""
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit, prefix], cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
    return [
        (
            name.removeprefix(prefix + "/"),
            subprocess.run(["git", "show", f"{commit}:{name}"], cwd=ROOT, check=True, capture_output=True).stdout,
        )
        for name in names
        if suffixes is None or name.endswith(suffixes)
    ]


def tool_catalogue_digest(package: list[tuple[str, bytes]]) -> str:
    """Hash the catalogue by running the code that builds it, at the released commit.

    There is no file to ``git show`` here: the catalogue exists only once
    ``mcp_transport`` has been imported. So the commit's own package is written to a
    scratch directory and imported by a child interpreter, and a bundle built for a
    commit other than the checkout still describes *that* commit rather than whatever
    the working tree currently holds.
    """
    with tempfile.TemporaryDirectory() as directory:
        for name, body in package:
            target = Path(directory) / PACKAGE / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        result = subprocess.run(
            [sys.executable, "-c", _CATALOGUE_PROGRAM],
            cwd=directory,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": directory, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    digest = result.stdout.strip()
    if result.returncode != 0 or not SHA256_RE.fullmatch(digest):
        if "tool_catalogue_sha256" in result.stderr:
            raise ReleaseIdentityError(
                "this commit predates the release-identity change: its package has no "
                "tool catalogue digest to bind"
            )
        raise ReleaseIdentityError(
            "could not hash the tool catalogue at this commit: "
            + (result.stderr.strip().splitlines() or ["no output"])[-1]
        )
    return digest


def bundle(commit: str, domain: str) -> dict:
    # Terminal newlines are stripped to match `orchestration.instructions()`, which is the
    # value `prompts/get` actually serves; hashing Git's conventional final newline would
    # bind a string no client ever receives.
    instructions = git_text(commit, INSTRUCTIONS).rstrip("\r\n")
    package = git_tree(commit, PACKAGE, (".py", ".md"))
    skill = git_tree(commit, SKILL)
    if not skill:
        raise ReleaseIdentityError("this commit carries no canonical Agent Skill to bind")
    identity = {
        "git_commit": commit,
        "instructions_sha256": sha256_text(instructions),
        "tool_catalogue_sha256": tool_catalogue_digest(package),
        "skill_sha256": package_artifact_sha256(skill),
        "gateway_artifact_sha256": package_artifact_sha256(package),
        "gateway_domain": domain,
    }
    return {
        "schema_version": "2",
        "release_id": make_release_id(**identity),
        **identity,
        # The prompt itself, not only its digest: it is the one bound artifact a person
        # may need to read back without a checkout of the released commit.
        "instructions": instructions,
    }


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseIdentityError("JSON artifact must be an object")
    return value


def read_private_env(path: Path) -> dict[str, str]:
    """Read one external 0600 env file without shell evaluation or value logging."""
    source = path.expanduser()
    if source.is_symlink():
        raise ReleaseIdentityError("deployment env file must not be a symlink")
    source = outside_repo(source)
    metadata = source.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseIdentityError("deployment env file must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ReleaseIdentityError("deployment env file must have mode 0600")
    values: dict[str, str] = {}
    for number, raw_line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ReleaseIdentityError(f"deployment env file line {number} is invalid")
        name, value = line.split("=", 1)
        if not _ENV_NAME.fullmatch(name) or name in values:
            raise ReleaseIdentityError(f"deployment env file line {number} is invalid")
        values[name] = value
    return values


def expected_deployment_identity_from_env(env: dict[str, str]) -> dict[str, str]:
    """Compute expected public identity while keeping private values inside the runner."""
    required = (
        STATE_ROOT_ENV_VAR,
        TOKEN_HMAC_KEY_ENV_VAR,
        CLIENT_ID_ENV_VAR,
        DEPLOYMENT_ENVIRONMENT_ENV_VAR,
        DEPLOYMENT_INSTANCE_ID_ENV_VAR,
    )
    missing = [name for name in required if not str(env.get(name) or "").strip()]
    if missing:
        raise ReleaseIdentityError(
            "deployment env file is incomplete; set " + ", ".join(missing)
        )
    key = str(env[TOKEN_HMAC_KEY_ENV_VAR]).strip()
    if len(key) < MIN_HMAC_KEY_CHARACTERS:
        raise ReleaseIdentityError(
            f"{TOKEN_HMAC_KEY_ENV_VAR} must be at least "
            f"{MIN_HMAC_KEY_CHARACTERS} characters"
        )
    return make_deployment_identity(
        resolved_state_root=Path(str(env[STATE_ROOT_ENV_VAR]).strip())
        .expanduser()
        .resolve(),
        intervals_client_id=str(env[CLIENT_ID_ENV_VAR]).strip(),
        environment=str(env[DEPLOYMENT_ENVIRONMENT_ENV_VAR]),
        instance_id=str(env[DEPLOYMENT_INSTANCE_ID_ENV_VAR]),
        token_hmac_key=key.encode("utf-8"),
    )


def write_private_json(path: Path, value: dict) -> None:
    """Write trusted-runner evidence outside the repository with mode 0600."""
    output = outside_repo(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        output.chmod(0o600)


def fetch_runtime_health(
    gateway_domain: str,
    *,
    opener=_open_without_redirects,
) -> dict:
    health_url = gateway_domain + "/healthz"
    with opener(health_url, timeout=15) as response:
        if response.geturl() != health_url:
            raise ReleaseIdentityError("gateway health redirected away from the release origin")
        health = json.loads(response.read().decode("utf-8"))
    if not isinstance(health, dict):
        raise ReleaseIdentityError("gateway health must be a JSON object")
    return health


def verify_release(
    *,
    bundle_path: Path,
    receipt_path: Path,
    expected_deployment_identity_path: Path | None = None,
    opener=_open_without_redirects,
) -> dict:
    """Hold the live gateway to the release built here, and write down what that proves.

    It used to also compare the bundle against text exported by hand from one client's
    console. That comparison certified that a person had pasted matching text into that
    console, which is not a fact about this deployment, and it went with the release
    ritual it belonged to. Everything left reads the account rather
    than the plan: the gateway's own ``/healthz``, its release identity, its deployment
    identity.

    A deployment older than the release-identity change is answered in words before any
    hash is compared. It reports a shape this checkout no longer builds, so every field
    would "mismatch" and none of that would say the actual thing, which is that the code
    is deployed in the wrong order.
    """
    bundled = read_json(outside_repo(bundle_path))
    identity = release_identity(bundled)
    health = fetch_runtime_health(identity["gateway_domain"], opener=opener)
    if predates_release_identity_change(health.get("release_identity")):
        raise ReleaseIdentityError(PREDATES_RELEASE_IDENTITY_CHANGE)
    if health.get("status") != "ok":
        raise ReleaseIdentityError("gateway health is not ready")
    runtime = release_identity(health.get("release_identity", {}))
    if runtime != identity:
        raise ReleaseIdentityError("gateway runtime identity does not match the release built here")
    expected_path = expected_deployment_identity_path
    if expected_path is None:
        configured_path = os.environ.get(EXPECTED_DEPLOYMENT_IDENTITY_FILE_ENV_VAR)
        if configured_path:
            expected_path = Path(configured_path)
    if expected_path is None:
        raise ReleaseIdentityError("expected deployment identity is required")
    expected_deployment = deployment_identity(read_json(outside_repo(expected_path)))
    runtime_deployment = deployment_identity(health.get("deployment_identity", {}))
    if runtime_deployment != expected_deployment:
        raise ReleaseIdentityError(
            "gateway runtime deployment identity does not match expected configuration"
        )
    receipt = {
        "schema_version": "3",
        "release_identity": identity,
        "deployment_identity": runtime_deployment,
        "certifies": (
            "gateway artifact and deployment configuration parity only"
        ),
    }
    output = outside_repo(receipt_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build"); build.add_argument("--gateway-domain", required=True); build.add_argument("--output", required=True); build.add_argument("--git-commit")
    verify = sub.add_parser("verify"); verify.add_argument("--bundle", required=True); verify.add_argument("--receipt", required=True); verify.add_argument("--expected-deployment-identity", required=True)
    expected = sub.add_parser("deployment-identity"); expected.add_argument("--env-file", required=True); expected.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            domain = normalise_gateway_domain(args.gateway_domain); commit = args.git_commit or commit_at_head()
            result = bundle(commit, domain); output = outside_repo(Path(args.output)); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(result["release_id"]); return 0
        if args.command == "deployment-identity":
            identity = expected_deployment_identity_from_env(
                read_private_env(Path(args.env_file))
            )
            write_private_json(Path(args.output), identity)
            print(identity["configuration_binding"])
            return 0
        receipt = verify_release(
            bundle_path=Path(args.bundle),
            receipt_path=Path(args.receipt),
            expected_deployment_identity_path=Path(
                args.expected_deployment_identity
            ),
        )
        print(receipt["release_identity"]["release_id"])
        return 0
    except (ReleaseIdentityError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(f"release gate blocked: {exc}", file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
