"""The reviewed surface, pinned to stated values rather than to itself.

Every other check of the tool catalogue in this repository computes
``tool_catalogue_sha256()`` and compares it against ``tool_catalogue_sha256()``.
That proves the catalogue agrees with itself, which it always will. It cannot
notice that a tool description changed -- and a changed description is precisely
what creates a new reviewed surface that cannot roll under a pending review's
snapshot (issue #182, AGENTS.md "Version numbers").

So the numbers below are written out. Changing a tool, its description, its input
schema or an annotation fails this file, and the failure says what to do: decide
whether the move is intended, and if it is, update these constants in the same
commit and treat the release as a new reviewed surface.

This is the assertion that makes "we do not expect to change tools for submission"
checkable instead of hoped.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from garmin_coach_loop.gateway import PRODUCT_VERSION
from garmin_coach_loop.mcp_transport import (
    RETIRED_TOOLS,
    TOOLS,
    TOOLS_BY_NAME,
    tool_catalogue_sha256,
)

ROOT = Path(__file__).resolve().parents[1]

# The release candidate this repository currently carries.
RELEASE_CANDIDATE_VERSION = "1.4.8"

# What the catalogue is, at that version.
RELEASE_CANDIDATE_TOOL_COUNT = 22
RELEASE_CANDIDATE_TOOL_CATALOGUE_SHA256 = (
    "136e8d0cdb16f3f7ab7d92d3794dab6fdc020361779d3239705ec4f02d09ceaf"
)

# Names a client may still hold in a cached catalogue. They are answered as a refusal
# rather than a protocol error, and they must not come back as tools.
RELEASE_CANDIDATE_RETIRED_TOOLS = ("applyOwnerDeletion", "prepareOwnerDeletion")


class ReleaseCandidateSurfaceTests(unittest.TestCase):
    def test_the_tool_catalogue_digest_is_the_one_this_release_states(self):
        """The failure this catches: a tool description moving without anyone noticing.

        Every other digest check in this repository compares the catalogue to itself.
        This one compares it to a number a human wrote down, which is the only way the
        comparison can disagree.
        """
        self.assertEqual(
            RELEASE_CANDIDATE_TOOL_CATALOGUE_SHA256,
            tool_catalogue_sha256(),
            "the served tool catalogue moved. If that is intended, this release is a new "
            "reviewed surface: update RELEASE_CANDIDATE_TOOL_CATALOGUE_SHA256 in the same "
            "commit, bump the version, and run Scan Tools before resubmitting.",
        )

    def test_the_tool_count_is_the_one_this_release_states(self):
        self.assertEqual(RELEASE_CANDIDATE_TOOL_COUNT, len(TOOLS))

    def test_every_retired_tool_is_still_absent_from_the_catalogue(self):
        """A retired name must not reappear as a tool, whatever else changes."""
        for name in RELEASE_CANDIDATE_RETIRED_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, RETIRED_TOOLS)
                self.assertNotIn(name, TOOLS_BY_NAME)

    def test_the_three_version_carriers_agree_with_the_stated_candidate(self):
        """gateway.py, server.json and the Codex plugin manifest carry one number.

        A test already holds the three equal to each other. This holds them to the
        candidate this file names, so a bump that reaches two of the three, or a bump
        nobody meant to make, is a failure rather than a silently shipped version.
        """
        server = json.loads((ROOT / "server.json").read_text())
        plugin = json.loads(
            (ROOT / "plugins/long-run-hybrid-coach/.codex-plugin/plugin.json").read_text()
        )
        self.assertEqual(RELEASE_CANDIDATE_VERSION, PRODUCT_VERSION)
        self.assertEqual(RELEASE_CANDIDATE_VERSION, server["version"])
        self.assertEqual(RELEASE_CANDIDATE_VERSION, plugin["version"])


if __name__ == "__main__":
    unittest.main()
