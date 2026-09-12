"""Hold the log archive to the one property a log archive has to have: it keeps repeats.

This file exists because the first shape of `archive_access_log.py` did not. It appended
any fetched line that was not already *somewhere* in the stored tail, which reads as
harmless until you notice what these lines are: one athlete refused six times writes six
lines that differ only by a millisecond timestamp, and two of them can share even that. A
single 1,921-line production window pulled on 2026-09-12 already contained one exact
duplicate pair. Set-membership dedup would have stored one of them and silently dropped
the other -- destroying the exact distinction the `owner=` field was added to draw.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.archive_access_log import (
    ANCHOR_LINES,
    append_atomically,
    drop_secrets,
    new_lines,
    stored_tail,
)


def window(*lines: str) -> list[str]:
    return list(lines)


def filler(count: int, tag: str = "f") -> list[str]:
    return [f"2026-09-12 0{index // 60}:{index % 60:02d} {tag}{index}" for index in range(count)]


class OverlapTests(unittest.TestCase):
    def test_an_empty_archive_takes_the_whole_window(self):
        fetched = filler(5)
        fresh, gap = new_lines(fetched, [])
        self.assertEqual(fetched, fresh)
        self.assertFalse(gap)

    def test_a_line_that_repeats_is_stored_every_time_it_happens(self):
        """The regression this module was written for."""
        repeated = "POST /mcp -> 200 access=authenticated owner=31657b3e62cdad2b tool=prepareCoachDecision outcome=blocked:invalid_request"
        stored = filler(ANCHOR_LINES) + [repeated]
        fetched = filler(ANCHOR_LINES) + [repeated, repeated, repeated]

        fresh, gap = new_lines(fetched, stored)

        self.assertFalse(gap)
        self.assertEqual([repeated, repeated], fresh)

    def test_two_identical_lines_inside_one_window_both_survive(self):
        same = "POST /mcp -> 200 access=authenticated owner=aaaaaaaaaaaaaaaa tool=getCoachState outcome=passed"
        fetched = filler(ANCHOR_LINES) + [same, same]
        fresh, _ = new_lines(fetched, filler(ANCHOR_LINES))
        self.assertEqual([same, same], fresh)

    def test_rerunning_with_no_new_lines_adds_nothing(self):
        stored = filler(ANCHOR_LINES + 3)
        fresh, gap = new_lines(list(stored), stored)
        self.assertEqual([], fresh)
        self.assertFalse(gap)

    def test_a_window_that_does_not_reach_back_far_enough_is_reported_as_a_hole(self):
        stored = filler(ANCHOR_LINES + 5, tag="old")
        fetched = filler(4, tag="new")

        fresh, gap = new_lines(fetched, stored)

        self.assertTrue(gap)
        self.assertEqual(fetched, fresh)

    def test_the_join_uses_a_run_of_lines_rather_than_one(self):
        """One shared line is a coincidence these logs produce constantly."""
        coincidence = "POST /mcp -> 401 access=anonymous error=unauthorized"
        stored = filler(ANCHOR_LINES, tag="old") + [coincidence]
        fetched = [coincidence] + filler(3, tag="new")

        fresh, gap = new_lines(fetched, stored)

        # The lone match is not treated as the join, so nothing is silently skipped.
        self.assertTrue(gap)
        self.assertEqual(fetched, fresh)


class SecretTests(unittest.TestCase):
    def test_one_bad_line_costs_that_line_and_not_the_window(self):
        good = filler(3)
        # Deliberately not a token-shaped literal: the repository safety check reads
        # this file too, and a fixture that trips it would fail the build for looking
        # like the thing it is testing the handling of.
        bad = "GET /x -> 200 authorization: redacted-header-value"
        kept, dropped = drop_secrets(good[:2] + [bad] + good[2:])
        self.assertEqual(good, kept)
        self.assertEqual([bad], dropped)

    def test_an_ordinary_window_loses_nothing(self):
        lines = window(
            "POST /mcp -> 200 access=authenticated owner=31657b3e62cdad2b tool=getCoachState outcome=passed",
            "POST /mcp -> 401 access=anonymous error=unauthorized",
        )
        kept, dropped = drop_secrets(lines)
        self.assertEqual(lines, kept)
        self.assertEqual([], dropped)


class AppendTests(unittest.TestCase):
    def test_an_append_round_trips_through_stored_tail(self):
        with TemporaryDirectory() as root:
            out = Path(root) / "access.log"
            first = filler(ANCHOR_LINES + 2)
            append_atomically(out, first)
            self.assertEqual(first, stored_tail(out))

            second = ["POST /mcp -> 200 access=authenticated owner=deadbeefdeadbeef tool=getCoachState outcome=passed"]
            fresh, gap = new_lines(first + second, stored_tail(out))
            self.assertFalse(gap)
            self.assertEqual(second, fresh)
            append_atomically(out, fresh)
            self.assertEqual(first + second, stored_tail(out))

    def test_every_stored_line_ends_in_a_newline(self):
        with TemporaryDirectory() as root:
            out = Path(root) / "access.log"
            append_atomically(out, ["a"])
            append_atomically(out, ["b"])
            self.assertEqual("a\nb\n", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
