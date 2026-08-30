"""The `archived issue #NN` spelling is what marks a citation as history (AGENTS.md 16).

Most of this code was written in a private repository that is now archived, and its
comments cite that repository's issue numbers. The counter here has since passed the
archived repository's own range, so a bare number can silently resolve to a real, unrelated
issue in this repository. The one thing that stops a reader from following it is the literal
prefix -- `archived issue #NN` -- and a prefix only protects a reader if every occurrence
spells it exactly the same way. Nothing else catches a hand-edit that drifts to `Archived
issue`, `archived Issue`, `archived-issue`, or a missing space; blame cannot be run inside a
test, so this checks the one thing that can be: spelling, not provenance.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Matches "archived" immediately followed by an issue-number citation, however that join
# is spelled -- case, separator, and spacing all free -- so every real occurrence is
# caught regardless of how it drifted. `CANONICAL` is the one spelling AGENTS.md 16
# names; anything the loose pattern finds that is not an exact match of it is a drift
# with nothing else to catch it.
NEAR_MISS = re.compile(r"\barchived[\s_-]*issue\s*#\s*\d+", re.IGNORECASE)
CANONICAL = re.compile(r"^archived issue #\d+$")


class ArchivedIssueCitationSpellingTest(unittest.TestCase):
    def test_every_archived_issue_citation_uses_the_canonical_spelling(self):
        drifted: list[str] = []
        for path in sorted(ROOT.rglob("*.py")):
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in NEAR_MISS.finditer(text):
                found = match.group(0)
                if not CANONICAL.match(found):
                    line_no = text.count("\n", 0, match.start()) + 1
                    drifted.append(f"{path.relative_to(ROOT)}:{line_no}: {found!r}")
        self.assertEqual(
            drifted,
            [],
            "found a non-canonical spelling of 'archived issue #NN' -- "
            "this exact wording is what keeps it from reading as a live link "
            "(AGENTS.md 16):\n" + "\n".join(drifted),
        )

    def test_the_canonical_pattern_itself_still_matches_a_real_example(self):
        """A pattern that stopped matching anything would pass vacuously.

        Built from fragments rather than written out plainly, same as
        tests/test_repo_safety.py's samples: this file is itself a candidate the
        scan above reads, and a correctly-spelled example is fine to contain, but
        the drifted ones must not accidentally satisfy the loose pattern in a way
        that then reports itself as a finding.
        """
        prefix, number = "archived", "#75"
        self.assertTrue(CANONICAL.match(f"{prefix} issue {number}"))
        self.assertIsNone(CANONICAL.match(f"{prefix.capitalize()} issue {number}"))
        self.assertIsNone(CANONICAL.match(f"{prefix} Issue {number}"))
        self.assertIsNone(CANONICAL.match(f"{prefix} issue{number}"))
        self.assertIsNone(CANONICAL.match(f"{prefix}-issue {number}"))


if __name__ == "__main__":
    unittest.main()
