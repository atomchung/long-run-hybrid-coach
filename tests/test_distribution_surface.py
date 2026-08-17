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


if __name__ == "__main__":
    unittest.main()
