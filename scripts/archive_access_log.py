#!/usr/bin/env python3
"""Keep the operator's own copy of a log window the platform is about to discard.

Railway holds this service's logs for roughly half a day. The questions they answer --
which account was refused, how many times, on which tool -- are asked days later, and
since 1.4.3 removed the registry counters (issue #408) there is no second place holding
them. So this pulls the current window down and appends whatever is not already stored.

It is an operator tool, not a product feature: it runs on a machine the operator owns,
touches no deployment, reads nothing but `railway logs`, and writes one plain file. See
`docs/ops/security-events.md` for the fields it stores and for the log drain that is the
push-based version of the same idea.

    python3 scripts/archive_access_log.py --out ~/.local/share/garmin-coach-loop-ops/access.log

Running it twice adds nothing the second time. Missing a run costs only the lines that
fell out of the window in between, so schedule it well inside the window rather than at
its edge.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# How much of the tail is read back to recognise a line already stored. The window this
# pulls from is far smaller than this, so an overlap is always found; reading the whole
# file would make every run cost the size of the archive instead.
OVERLAP_LINES = 40_000

# A refusal to store a line that should never have been on the wire. The gateway does not
# print these, and this is the check that says so rather than assuming it -- an archive is
# the one copy that outlives the platform's own retention, so it is the wrong place to
# discover later that something leaked into it.
FORBIDDEN = {
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    "private key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "authorization header": re.compile(r"authorization:\s*\S+", re.IGNORECASE),
}


def fetch(lines: int, extra: list[str]) -> list[str]:
    command = ["railway", "logs", "--lines", str(lines), *extra]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise SystemExit("railway: not installed, or not on PATH")
    except subprocess.TimeoutExpired:
        raise SystemExit("railway logs: timed out after 180s")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise SystemExit(f"railway logs failed: {tail[-1] if tail else done.returncode}")
    return [line for line in done.stdout.splitlines() if line.strip()]


def stored_tail(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return set(handle.read().splitlines()[-OVERLAP_LINES:])


def refuse_secrets(lines: list[str]) -> None:
    for line in lines:
        for name, pattern in FORBIDDEN.items():
            if pattern.search(line):
                raise SystemExit(
                    f"refusing to archive: a fetched line matched {name}. "
                    "Nothing was written. Read the window by hand before storing it."
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="file to append to; parent directories are created",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=5000,
        help="how much of the current window to ask for (default 5000)",
    )
    parser.add_argument(
        "--railway-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra argument passed through to `railway logs`, repeatable",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print nothing when there is nothing new"
    )
    args = parser.parse_args()

    fetched = fetch(args.lines, args.railway_arg)
    refuse_secrets(fetched)

    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    known = stored_tail(out)
    fresh = [line for line in fetched if line not in known]

    if fresh:
        with out.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(fresh) + "\n")
    if fresh or not args.quiet:
        print(f"{len(fresh)} new of {len(fetched)} fetched -> {out}")
    # A window that arrived entirely new means the previous run was longer ago than the
    # platform keeps: lines fell out between the two, and this file now has a hole.
    if fresh and len(fresh) == len(fetched) and known:
        print(
            "warning: every fetched line was new, so the gap since the last run was "
            "longer than the retained window. Run this more often.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
