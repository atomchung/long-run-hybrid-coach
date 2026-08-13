"""Regression tests for the CLI's own surface.

`--help` is the first thing anyone reads, including the two commands every session is
told to run before deciding anything (`doctor-store`, `status`). argparse lists a
subcommand's name among the choices whether or not it was given a help string, and
describes only the ones that were -- so an undescribed command is visible as a word and
explained nowhere.
"""

from __future__ import annotations

import argparse
import unittest

from garmin_coach_loop.cli import build_parser


def _subcommands() -> argparse._SubParsersAction:
    parser = build_parser()
    for action in parser._actions:  # argparse exposes no public accessor for these
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("the CLI declares no subcommands")


class CommandHelpTests(unittest.TestCase):
    def test_every_subcommand_is_described_not_only_listed(self):
        action = _subcommands()
        described = {choice.dest for choice in action._choices_actions}
        self.assertEqual(set(action.choices), described)

    def test_no_subcommand_is_described_with_an_empty_string(self):
        for choice in _subcommands()._choices_actions:
            with self.subTest(command=choice.dest):
                self.assertTrue((choice.help or "").strip())


if __name__ == "__main__":
    unittest.main()
