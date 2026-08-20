#!/usr/bin/env python3
"""Fail when distributable Git candidates contain private operational material."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATHS = {
    "application/garmin-connect-developer-program-application-pack.md",
    "docs/GARMIN_CONNECT_COMPUTER_USE_RUNBOOK.md",
    "docs/GARMIN_INTEGRATION_AUDIT_2026-08-08.md",
    "projects/garmin-coach-loop-design-2026-08-10.md",
    "projects/garmin-voice-workout-mvp.md",
}

SECRET_PATTERNS = {
    "private key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    "assigned secret": re.compile(
        r"(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password)"
        r"\s*[:=]\s*['\"][^$<{\s][^'\"]{7,}['\"]",
        re.IGNORECASE,
    ),
}

PERSONAL_PATTERNS = {
    "local home path": re.compile(r"(?:/Users/|[A-Za-z]:\\Users\\)"),
    "personal GitHub/account handle": re.compile(r"\b(?:atomchung|atomo)\b", re.IGNORECASE),
    "owner personal name": re.compile(r"\bting(?:'s)?\b", re.IGNORECASE),
    "email address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}

# The public repositories and Pages URL necessarily carry the account hosting
# them. Those exact self-references are public by construction, so they are
# removed before the personal scan rather than read as a leak. A handle appearing
# anywhere else still fails the gate.
PUBLIC_SELF_REFERENCES = (
    # The registry namespace is the same self-reference in the shape a registry asks
    # for rather than a URL: proving it *is* signing in as the account that hosts the
    # repository above, so it can carry no more than that URL already does.
    "io.github.atomchung/long-run-hybrid-coach",
    "https://github.com/atomchung/long-run-hybrid-coach",
    "https://github.com/atomchung/paceandstaystrong-site",
    "https://atomchung.github.io/paceandstaystrong-site",
)

LIVE_STATE_MARKERS = {
    "submitted application receipt": "Got it! Your information has been sent successfully",
    "live local audit": "本轮只读现场核对",
    "signed-in personal account": "用户已登录的个人账号",
    "observed private calendar state": "Calendar 的 9 日格显示",
}


def git_candidates() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(path for path in result.stdout.decode().split("\0") if path)


def readable_text(path: Path) -> str | None:
    if path.is_symlink():
        return str(path.readlink())
    # `git ls-files --cached` includes tracked files deleted in the worktree until
    # their removal is staged.  A cleanup must still be able to run the safety gate
    # before staging; deleted paths have no candidate content to inspect.
    if not path.exists():
        return None
    if path.is_dir():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    findings: list[str] = []
    for relative in git_candidates():
        if relative == "scripts/check_repo_safety.py":
            continue
        if relative in FORBIDDEN_PATHS:
            findings.append(f"{relative}: forbidden local operational record")
            continue
        lowered = relative.lower()
        if lowered.endswith(".fit") or Path(relative).name.startswith(".env"):
            findings.append(f"{relative}: forbidden private artifact type")
            continue
        text = readable_text(ROOT / relative)
        if text is None:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{relative}: possible {label}")
        personal_scope = text
        for reference in PUBLIC_SELF_REFERENCES:
            personal_scope = personal_scope.replace(reference, "")
        for label, pattern in PERSONAL_PATTERNS.items():
            if pattern.search(personal_scope):
                findings.append(f"{relative}: possible {label}")
        for label, marker in LIVE_STATE_MARKERS.items():
            if marker in text:
                findings.append(f"{relative}: possible {label}")

    if findings:
        print("Repository safety check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(f"Repository safety check passed for {len(git_candidates())} candidate files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
