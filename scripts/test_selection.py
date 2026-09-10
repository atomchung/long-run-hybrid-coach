#!/usr/bin/env python3
"""Select the smallest safe local test set for a change.

This is a developer-feedback tool, not a release gate. Pull requests and ``main``
still run the complete suite. Unknown executable changes deliberately fall back to
the full suite instead of silently claiming that a partial selection is enough.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "tests"


def _test_files() -> tuple[str, ...]:
    return tuple(
        path.relative_to(ROOT).as_posix()
        for path in sorted(TEST_ROOT.glob("test_*.py"))
    )


def _normalise_path(raw_path: str) -> str:
    path = raw_path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _add(selected: set[str], reasons: dict[str, set[str]], path: str, reason: str) -> None:
    if (ROOT / path).is_file():
        selected.add(path)
        reasons.setdefault(path, set()).add(reason)


def select_test_paths(changed_paths: list[str] | tuple[str, ...]) -> dict[str, object]:
    """Return selected tests, reasons, and whether an unknown change needs all tests."""

    selected: set[str] = set()
    reasons: dict[str, set[str]] = {}
    full_suite_required = False
    full_suite_reasons: list[str] = []
    test_files = _test_files()

    for raw_path in changed_paths:
        path = _normalise_path(raw_path)
        if not path:
            continue

        if path.startswith("tests/"):
            if path in test_files:
                _add(selected, reasons, path, "the test file changed")
            else:
                full_suite_required = True
                full_suite_reasons.append(f"unrecognised test change: {path}")
            continue

        if path.startswith("garmin_coach_loop/") and path.endswith(".py"):
            module = Path(path).stem
            direct = f"tests/test_{module}.py"
            mapped = False
            if (ROOT / direct).is_file():
                mapped = True
            _add(selected, reasons, direct, f"direct test for {path}")

            needle = f"garmin_coach_loop.{module}"
            import_form = f"from garmin_coach_loop import {module}"
            for test_path in test_files:
                text = (ROOT / test_path).read_text(encoding="utf-8")
                if needle in text or import_form in text:
                    mapped = True
                    _add(selected, reasons, test_path, f"imports {path}")

            # These two modules are the shared protocol/runtime boundary. A test can
            # exercise them through a sibling import, so keep the explicit controls
            # even when a future test stops naming the module directly.
            if module == "gateway":
                mapped = True
                _add(selected, reasons, "tests/test_gateway.py", "gateway boundary control")
                _add(selected, reasons, "tests/test_mcp_gateway.py", "MCP gateway boundary control")
            elif module == "mcp_transport":
                mapped = True
                for test_path in (
                    "tests/test_mcp_gateway.py",
                    "tests/test_mcp_output_contract.py",
                    "tests/test_distribution_surface.py",
                ):
                    _add(selected, reasons, test_path, "MCP transport boundary control")

            if not mapped:
                full_suite_required = True
                full_suite_reasons.append(f"no test mapping for executable change: {path}")
            continue

        if path.startswith("contracts/") and path.endswith(".json"):
            for test_path in (
                "tests/test_contract_parity.py",
                "tests/test_mcp_output_contract.py",
                "tests/test_render_preview.py",
            ):
                _add(selected, reasons, test_path, f"contract changed: {path}")
            continue

        script_tests = {
            "scripts/check_repo_safety.py": ("tests/test_repo_safety.py",),
            "scripts/release_bundle.py": ("tests/test_release_bundle.py",),
            "scripts/verify_registry_release.py": ("tests/test_registry_release.py",),
            "scripts/render_plan_preview.py": ("tests/test_render_preview.py",),
            "scripts/test_selection.py": ("tests/test_process_gates.py",),
            "scripts/change_gates.py": ("tests/test_process_gates.py",),
            "scripts/verify_production_promotion.py": ("tests/test_process_gates.py",),
        }
        if path in script_tests:
            for test_path in script_tests[path]:
                _add(selected, reasons, test_path, f"direct test for {path}")
            continue

        if path.startswith("evals/"):
            _add(selected, reasons, "tests/test_evals.py", f"eval input changed: {path}")
            if path.startswith("evals/ab/"):
                _add(selected, reasons, "tests/test_ab_eval.py", f"A/B eval input changed: {path}")
            if "scenario" in path:
                _add(
                    selected,
                    reasons,
                    "tests/test_coach_session_scenarios.py",
                    f"scenario input changed: {path}",
                )
            continue

        if path.startswith(".agents/skills/"):
            _add(selected, reasons, "tests/test_distribution_surface.py", f"Skill changed: {path}")
            continue

        if path.startswith(".github/"):
            _add(selected, reasons, "tests/test_process_gates.py", f"workflow changed: {path}")
            continue

        # Documentation and release notes do not exercise product code. Any other
        # source/configuration file is unknown and therefore keeps the conservative
        # full-suite fallback.
        if not (
            path.startswith("docs/")
            or path.startswith("README")
            or path in {"AGENTS.md", "CLAUDE.md", "ROADMAP.md"}
            or path.startswith(".github/")
        ):
            full_suite_required = True
            full_suite_reasons.append(f"unclassified change: {path}")

    return {
        "changed_paths": sorted(set(_normalise_path(p) for p in changed_paths if p.strip())),
        "test_paths": sorted(selected),
        "reasons": {path: sorted(values) for path, values in sorted(reasons.items())},
        "full_suite_required": full_suite_required,
        "full_suite_reasons": sorted(set(full_suite_reasons)),
    }


def _git_names(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def changed_paths(base: str | None) -> list[str]:
    paths: set[str] = set()
    if base:
        paths.update(_git_names("diff", "--name-only", f"{base}...HEAD"))
    paths.update(_git_names("diff", "--name-only"))
    paths.update(_git_names("diff", "--cached", "--name-only"))
    paths.update(_git_names("ls-files", "--others", "--exclude-standard"))
    return sorted(paths)


def _default_base() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return "origin/main" if result.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare committed changes with this ref")
    parser.add_argument("--plan-only", action="store_true", help="print the plan without running tests")
    args = parser.parse_args()
    plan = select_test_paths(changed_paths(args.base or _default_base()))
    print(f"changed paths: {len(plan['changed_paths'])}")
    for path in plan["changed_paths"]:
        print(f"  {path}")

    if plan["full_suite_required"]:
        print("selection: full suite (conservative fallback)")
        for reason in plan["full_suite_reasons"]:
            print(f"  reason: {reason}")
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    elif plan["test_paths"]:
        print(f"selection: {len(plan['test_paths'])} affected test files")
        for path in plan["test_paths"]:
            print(f"  {path}")
        command = [sys.executable, "-m", "unittest", *(path[:-3].replace("/", ".") for path in plan["test_paths"])]
    else:
        print("selection: no product tests (docs/configuration-only change)")
        command = []

    if args.plan_only:
        if command:
            print("command:", " ".join(command))
        else:
            print("command: (no product tests)")
        print("then:", sys.executable, "scripts/check_repo_safety.py")
        return 0
    if command:
        test_result = subprocess.run(command, cwd=ROOT)
        if test_result.returncode:
            return test_result.returncode
    return subprocess.run(
        [sys.executable, "scripts/check_repo_safety.py"],
        cwd=ROOT,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
