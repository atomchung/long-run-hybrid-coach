#!/usr/bin/env python3
"""Create and verify the external-only manual Custom GPT Builder release evidence."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach_loop.release_identity import (ReleaseIdentityError, make_release_id, normalise_gateway_domain, package_artifact_sha256, release_identity, sha256_text)  # noqa: E402

INSTRUCTIONS = "entrypoints/custom-gpt/instructions.md"
OPENAPI = "entrypoints/custom-gpt/openapi.yaml"


def outside_repo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ReleaseIdentityError("release evidence must be written outside the repository")
    return resolved


def git_text(commit: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{commit}:{path}"], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def commit_at_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def bundle(commit: str, domain: str) -> dict:
    instructions = git_text(commit, INSTRUCTIONS)
    openapi = git_text(commit, OPENAPI).replace("YOUR-GATEWAY-DOMAIN", domain.removeprefix("https://"))
    if "YOUR-GATEWAY-DOMAIN" in openapi:
        raise ReleaseIdentityError("rendered OpenAPI retains a placeholder domain")
    ih, oh = sha256_text(instructions), sha256_text(openapi)
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit, "garmin_coach_loop"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
    artifact = package_artifact_sha256([(name.removeprefix("garmin_coach_loop/"), subprocess.run(["git", "show", f"{commit}:{name}"], cwd=ROOT, check=True, capture_output=True).stdout) for name in names if name.endswith(".py")])
    return {"schema_version": "1", "release_id": make_release_id(git_commit=commit, instructions_sha256=ih, openapi_sha256=oh, gateway_artifact_sha256=artifact, gateway_domain=domain), "git_commit": commit, "gateway_domain": domain, "instructions": instructions, "instructions_sha256": ih, "openapi": openapi, "openapi_sha256": oh, "gateway_artifact_sha256": artifact}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise ReleaseIdentityError("JSON artifact must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build"); build.add_argument("--gateway-domain", required=True); build.add_argument("--output", required=True); build.add_argument("--git-commit")
    verify = sub.add_parser("verify"); verify.add_argument("--bundle", required=True); verify.add_argument("--builder-instructions", required=True); verify.add_argument("--builder-openapi", required=True); verify.add_argument("--receipt", required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            domain = normalise_gateway_domain(args.gateway_domain); commit = args.git_commit or commit_at_head()
            result = bundle(commit, domain); output = outside_repo(Path(args.output)); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(result["release_id"]); return 0
        b = read_json(outside_repo(Path(args.bundle))); identity = release_identity(b)
        if sha256_text(outside_repo(Path(args.builder_instructions)).read_text(encoding="utf-8")) != identity["instructions_sha256"]: raise ReleaseIdentityError("Builder instructions hash does not match bundle")
        if sha256_text(outside_repo(Path(args.builder_openapi)).read_text(encoding="utf-8")) != identity["openapi_sha256"]: raise ReleaseIdentityError("Builder OpenAPI hash does not match bundle")
        with urllib.request.urlopen(identity["gateway_domain"] + "/healthz", timeout=15) as response:
            health = json.loads(response.read().decode("utf-8"))
        if health.get("status") != "ok": raise ReleaseIdentityError("gateway health is not ready")
        runtime = release_identity(health.get("release_identity", {}))
        if runtime != identity: raise ReleaseIdentityError("gateway runtime identity does not match Builder bundle")
        receipt = {"schema_version": "1", "release_identity": identity, "certifies": "gateway artifact and Builder content parity only"}
        output = outside_repo(Path(args.receipt)); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8"); print(identity["release_id"]); return 0
    except (ReleaseIdentityError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(f"release gate blocked: {exc}", file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
