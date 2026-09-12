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
import contextlib
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

# How many stored lines are read back to find where the fetched window overlaps what is
# already here. The window a single run pulls is far smaller than this, so an anchor is
# found whenever the previous run was inside the platform's retention.
OVERLAP_LINES = 40_000

# How many consecutive stored lines have to match for the overlap to be believed. One
# line is not enough: the same line genuinely repeats -- `access=anonymous` challenges
# and accepted authentications are near-identical and two can land in the same
# millisecond, which a single 1,921-line window already contained. A run of this many
# identical consecutive lines in the same order is not a coincidence.
ANCHOR_LINES = 12

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


def stored_tail(path: Path) -> list[str]:
    """The end of the archive, in order.

    Order is the whole point. An earlier version of this held a *set* and appended any
    fetched line not in it, which is wrong for a log: the same line genuinely recurs, and
    dropping the recurrence destroys exactly what this archive exists to count -- whether
    six refusals were one athlete six times or six athletes once. A 1,921-line window
    pulled on 2026-09-12 already held one such pair.
    """
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()[-OVERLAP_LINES:]


def new_lines(fetched: list[str], stored: list[str]) -> tuple[list[str], bool]:
    """What of ``fetched`` is not already in ``stored``, and whether a gap was found.

    Positional, not by content: the tail of the archive is searched for inside the
    fetched window, and everything after the match is what is new. A recurrence of an
    identical line survives because nothing here asks whether a line was seen before,
    only where the two sequences join.

    No match means the previous run was longer ago than the platform retains, so the two
    do not overlap at all. Everything is appended and the caller is told there is a hole.
    """
    if not stored:
        return fetched, False
    anchor = stored[-min(ANCHOR_LINES, len(stored)) :]
    for start in range(len(fetched) - len(anchor), -1, -1):
        if fetched[start : start + len(anchor)] == anchor:
            return fetched[start + len(anchor) :], False
    return fetched, True


def drop_secrets(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split the window into what is safe to keep and what is not.

    The line is dropped, not the window. Refusing the whole run was the first shape of
    this and it fails the wrong way: one `authorization:` inside an unrelated error
    message would stop every subsequent run too, while the platform kept discarding the
    window at its own pace -- a check meant to protect the archive would have emptied it.
    A dropped line is reported loudly enough to go read by hand.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        if any(pattern.search(line) for pattern in FORBIDDEN.values()):
            dropped.append(line)
        else:
            kept.append(line)
    return kept, dropped


@contextlib.contextmanager
def exclusive(out: Path):
    """Hold a lock beside the archive for the length of a run, or yield ``False``.

    `flock` on a sibling file rather than on the archive: the archive is opened for
    append and closed inside the critical section, and a lock held on a handle that is
    about to be closed is not a lock.
    """
    lock = out.with_suffix(out.suffix + ".lock")
    with lock.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_atomically(out: Path, lines: list[str]) -> None:
    """Append whole lines or none of them.

    A write interrupted mid-line leaves a fragment that no later fetch will ever match,
    so the join in `new_lines` would never find its anchor again and every subsequent
    run would report a hole. One `write` of one buffer ending in a newline, flushed and
    fsynced before the handle closes, is as close to atomic as an append gets here.
    """
    payload = "\n".join(lines) + "\n"
    with out.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


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

    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    # One writer at a time. Two runs overlapping -- a schedule firing while someone runs
    # it by hand -- would each read the same tail, each find the same join, and each
    # append the same lines.
    with exclusive(out) as locked:
        if not locked:
            print(f"another run holds {out}; nothing done", file=sys.stderr)
            return 0

        fetched = fetch(args.lines, args.railway_arg)
        fetched, dropped = drop_secrets(fetched)
        stored = stored_tail(out)
        fresh, gap = new_lines(fetched, stored)

        if fresh:
            append_atomically(out, fresh)

    if fresh or not args.quiet:
        print(f"{len(fresh)} new of {len(fetched)} fetched -> {out}")
    if dropped:
        print(
            f"warning: {len(dropped)} line(s) matched a credential pattern and were not "
            "stored. Read the window by hand -- the gateway is not supposed to print "
            "these.",
            file=sys.stderr,
        )
    if gap:
        print(
            "warning: the fetched window does not overlap what is already stored, so "
            "the gap since the last run was longer than the platform retains and this "
            "file now has a hole. Run this more often.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
