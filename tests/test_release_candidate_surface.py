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
from garmin_coach_loop.orchestration import instructions
from garmin_coach_loop.release_identity import sha256_text, skill_tree_sha256

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "garmin-coach-loop"

# The release candidate this repository currently carries.
RELEASE_CANDIDATE_VERSION = "1.4.10"

# What the catalogue is, at that version.
RELEASE_CANDIDATE_TOOL_COUNT = 23
RELEASE_CANDIDATE_TOOL_CATALOGUE_SHA256 = (
    "90d57ad5c13c7dd4ae821a0183d7d156c45d98a046c777c292f8782e26f700a2"
)

# The other two reviewed surfaces. AGENTS.md "Version numbers": a patch that moves
# `tool_catalogue_sha256`, `instructions_sha256` **or** `skill_sha256` still creates a new
# reviewed surface. Only the catalogue was written down here, so until now the served
# prompt and the packaged Skill could move with nothing but a path rule in
# `change_gates.py` to notice -- and a path rule says a file changed, never that the bytes
# a reviewer approved are no longer the bytes being served.
RELEASE_CANDIDATE_INSTRUCTIONS_SHA256 = (
    "897b240fc1aeca9d979fda2ed088cfc4ef59a6c36f43cc48757d796ad9ea0dc4"
)
RELEASE_CANDIDATE_SKILL_SHA256 = (
    "8932154ff3321e62be79cc7468389059139c5328281b0807daa6b6dc18d406fb"
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

    def test_the_served_instructions_digest_is_the_one_this_release_states(self):
        """The orchestration prompt every MCP client is handed before its first turn.

        Hashed the way `release_bundle.py` hashes it -- over `instructions()`, which is
        the string `prompts/get` actually serves, not the file with Git's trailing
        newline -- so this constant and the one in the release receipt are the same fact.
        """
        self.assertEqual(
            RELEASE_CANDIDATE_INSTRUCTIONS_SHA256,
            sha256_text(instructions()),
            "the served orchestration prompt moved. If that is intended, this release is "
            "a new reviewed surface: update RELEASE_CANDIDATE_INSTRUCTIONS_SHA256 in the "
            "same commit, bump the version, and re-run client acceptance before "
            "resubmitting.",
        )

    def test_the_canonical_skill_digest_is_the_one_this_release_states(self):
        """Everything under the Skill directory, because that is what a user installs.

        A reference file or a packaging manifest moving is as much a surface change as
        `SKILL.md` moving, and neither is visible in the tool catalogue.
        """
        self.assertEqual(
            RELEASE_CANDIDATE_SKILL_SHA256,
            skill_tree_sha256(SKILL),
            "the canonical Agent Skill moved. If that is intended, update "
            "RELEASE_CANDIDATE_SKILL_SHA256 in the same commit and re-run client "
            "acceptance for the Skill-consuming entries.",
        )

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
