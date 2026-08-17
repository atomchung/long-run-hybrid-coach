"""The four layers of the distributed product, and the lines between them (#117).

What ships is one Agent Skill and one MCP tool surface, packaged thinly per platform.
That only stays true if each layer keeps to what it owns, and every one of these
boundaries has an obvious way to erode:

- the **Skill** absorbs the tool surface, and now says something about `publishWorkoutDelivery`
  that the tool descriptions no longer do;
- the **orchestration layer** grows a training rule, and becomes a second coach whose
  advice nobody evaluates (AGENTS.md 11);
- the **packaging** file grows coaching prose, and one platform quietly forks;
- the **Skill** links a repository path, which reads fine here and is a dead link for
  everyone who installed it.

None of these breaks a test that only exercises the running product, which is why they
are checked directly against the files.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from garmin_coach_loop import orchestration
from garmin_coach_loop.mcp_transport import TOOLS


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".agents" / "skills" / "garmin-coach-loop"
SKILL = SKILL_ROOT / "SKILL.md"
TRAINING_REFERENCE = SKILL_ROOT / "references" / "hybrid-training.md"
PACKAGING = SKILL_ROOT / "agents" / "openai.yaml"

ENTRYPOINTS = ROOT / "entrypoints"
# The one entrypoints file whose job is naming individual tools -- the protocol-level
# reference every other entry links to instead of repeating itself. Every other README
# under entrypoints/ is thin platform packaging (#117 item 4), and that is the property
# the rest of this module checks.
PROTOCOL_REFERENCE = ENTRYPOINTS / "mcp" / "README.md"

_MARKDOWN_LINK = re.compile(r"\]\(([^)]+)\)")

# Terms that belong to the training reference and nowhere else. Each names a coaching
# choice rather than a product mechanic, so finding one in the orchestration layer or in
# a packaging file means judgment has leaked into a layer that is not evaluated as
# coaching.
TRAINING_VOCABULARY = (
    "primary adaptation",
    "maintenance direction",
    "progression percentage",
    "easy run",
    "conversational",
    "heart-rate ceiling",
)


class CanonicalSkillTests(unittest.TestCase):
    def test_it_reaches_nothing_an_installed_user_cannot_see(self):
        links = _MARKDOWN_LINK.findall(SKILL.read_text(encoding="utf-8"))

        self.assertTrue(links)
        for target in links:
            with self.subTest(link=target):
                self.assertFalse(target.startswith(".."), "escapes the installed bundle")
                if "://" in target:
                    continue
                self.assertTrue(
                    (SKILL_ROOT / target).exists(), "bundled file does not exist"
                )

    def test_it_does_not_restate_the_tool_surface(self):
        """The command surface is delivered with the product; a copy here goes stale.

        An entry's own operations are named where they are served -- in the tool
        descriptions, and in the orchestration layer above them. The Skill saying the
        same thing is how the two start disagreeing, and the Skill is the copy the
        product cannot correct.
        """
        text = SKILL.read_text(encoding="utf-8")
        for tool in TOOLS:
            with self.subTest(tool=tool.name):
                self.assertNotIn(tool.name, text)


class OrchestrationBoundaryTests(unittest.TestCase):
    @staticmethod
    def _flat(text: str) -> str:
        """One line, lowercased -- these files are hard-wrapped, and a term that happens
        to straddle a line break is still the term."""
        return " ".join(text.split()).lower()

    def test_it_stays_out_of_the_training_reference(self):
        """AGENTS.md 11: a hosted instruction must not become a shadow coach either.

        This is the prompt every MCP client is handed at connect time, unevaluated by
        the coaching evals and unread by anyone reviewing training judgment. A training
        rule that lands here is a rule with no reviewer.
        """
        text = self._flat(orchestration.instructions())
        reference = self._flat(TRAINING_REFERENCE.read_text(encoding="utf-8"))

        for term in TRAINING_VOCABULARY:
            with self.subTest(term=term):
                self.assertIn(term, reference, "the term no longer names this layer")
                self.assertNotIn(term, text)

    def test_every_tool_is_sequenced_somewhere_in_it(self):
        """The other direction: an operation the orchestration layer forgot is an
        operation whose sequencing the model has to guess at."""
        text = orchestration.instructions()
        for tool in TOOLS:
            with self.subTest(tool=tool.name):
                self.assertIn(f"`{tool.name}`", text)


class PlatformPackagingTests(unittest.TestCase):
    def test_the_packaging_file_carries_connection_metadata_and_nothing_else(self):
        text = PACKAGING.read_text(encoding="utf-8")

        # A shallow scan, not a YAML parse: this package stays stdlib-only, and the
        # question here is which keys exist, which the indentation already answers.
        keys = {
            line.split(":", 1)[0].strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and ":" in line
        }
        self.assertEqual(
            {"interface", "display_name", "short_description", "default_prompt"}, keys
        )
        for term in TRAINING_VOCABULARY:
            with self.subTest(term=term):
                self.assertNotIn(term, text.lower())


# Number words a packaging README might spell a tool count out with (`entrypoints/mcp`'s
# own "the nineteen coach operations" is exactly this). Only as wide as any plausible tool
# count needs to be -- this is a generic word-to-number table, not a fact about the product.
_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "twenty-one", "twenty-two", "twenty-three",
    "twenty-four", "twenty-five",
)

_TOOL_COUNT_MENTION = re.compile(
    r"\b(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\s+(?:mcp\s+|coach\s+)*(?:tools|operations)\b"
)


def _stated_tool_counts(text: str) -> list[int]:
    """Every number this text puts directly in front of "tools" or "operations"."""
    counts = []
    for match in _TOOL_COUNT_MENTION.finditer(text.lower()):
        token = match.group(1)
        counts.append(int(token) if token.isdigit() else _NUMBER_WORDS.index(token))
    return counts


class EntrypointsPackagingTests(unittest.TestCase):
    """Issue #117 item 4: thin platform packaging around the one canonical surface.

    A packaging file that grows coaching prose, restates the tool catalogue, or drifts
    from the real tool count is the same failure the Skill and orchestration tests above
    guard against, one layer further out -- checked directly against the README files
    rather than against the running product, for the same reason the module docstring
    gives for the rest of this file.
    """

    @classmethod
    def setUpClass(cls):
        cls.readmes = sorted(ENTRYPOINTS.glob("*/README.md")) + [ENTRYPOINTS / "README.md"]
        for readme in cls.readmes:
            assert readme.exists(), f"expected entrypoints README missing: {readme}"

    def test_no_entrypoints_readme_grows_coaching_vocabulary(self):
        for readme in self.readmes:
            text = readme.read_text(encoding="utf-8").lower()
            for term in TRAINING_VOCABULARY:
                with self.subTest(readme=readme.relative_to(ROOT), term=term):
                    self.assertNotIn(term, text)

    def test_packaging_files_do_not_restate_the_tool_catalogue(self):
        """`entrypoints/mcp/README.md` is the one file whose job is naming tools; every
        other entrypoints README is packaging around it and should link there instead of
        repeating operationIds -- a couple of illustrative names is not a catalogue,
        naming most of the surface is.
        """
        for readme in self.readmes:
            if readme == PROTOCOL_REFERENCE:
                continue
            text = readme.read_text(encoding="utf-8")
            named = [tool.name for tool in TOOLS if tool.name in text]
            with self.subTest(readme=readme.relative_to(ROOT)):
                self.assertLessEqual(len(named), 2, f"restates the tool catalogue: {named}")

    def test_every_stated_tool_count_matches_the_real_catalogue(self):
        """A count is derived from ``TOOLS`` here, never hardcoded -- so a 20th tool
        landing without every prose mention of the count being updated fails this test
        instead of quietly going stale in front of an athlete choosing a client.
        """
        expected = len(TOOLS)
        for readme in self.readmes:
            text = readme.read_text(encoding="utf-8")
            for stated in _stated_tool_counts(text):
                with self.subTest(readme=readme.relative_to(ROOT), stated=stated):
                    self.assertEqual(stated, expected)


if __name__ == "__main__":
    unittest.main()
