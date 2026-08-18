"""The four layers of the distributed product, and the lines between them (#117).

What ships is one Agent Skill and one MCP tool surface, packaged thinly per platform.
That only stays true if each layer keeps to what it owns, and every one of these
boundaries has an obvious way to erode:

- the **Skill** absorbs the tool surface, and now says something about `applyWorkoutDelivery`
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

# The two files that publish the release's own interface scale to a reader (#132). They are
# outside `entrypoints/`, so the packaging checks below never reached them.
ROOT_README = ROOT / "README.md"
RELEASE_INVENTORY = ROOT / "docs" / "release-inventory.md"

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


def _cli_command_names() -> list[str]:
    """Every subcommand the CLI actually offers, read off the parser rather than a list."""
    from garmin_coach_loop import cli

    parser = cli.build_parser()
    for action in parser._actions:  # noqa: SLF001 -- argparse exposes no public accessor
        if getattr(action, "dest", None) == "command" and action.choices:
            return list(action.choices)
    raise AssertionError("the CLI parser no longer has a `command` subparser")


def _identity_table_names() -> list[str]:
    """The identity registry's tables, read out of the schema it creates."""
    from garmin_coach_loop import identity

    pattern = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")
    names = []
    for statement in identity._SCHEMA_STATEMENTS:  # noqa: SLF001 -- the schema is the fact
        match = pattern.search(statement)
        assert match, f"unrecognised schema statement: {statement[:40]}"
        names.append(match.group(1))
    return names


class PublishedCountTests(unittest.TestCase):
    """Issue #132: README publishes this release's interface scale, so it can go stale.

    `EntrypointsPackagingTests.test_every_stated_tool_count_matches_the_real_catalogue`
    already does this for the packaging READMEs, but only for a count written directly in
    front of the English words "tools"/"operations" -- which the root README, written in
    Chinese, never is. These patterns are the ones those two files actually use, and each
    is required to be found: a rewrite that drops the sentence fails here rather than
    quietly stopping the check.

    Every expected value is derived from the running product. Nothing in this test is a
    hardcoded fact about the product's size.
    """

    @classmethod
    def setUpClass(cls):
        cls.expected = {
            "mcp_tools": len(TOOLS),
            # `prompts/list` serves exactly one descriptor; the count is that surface's
            # own arity, not a number chosen here.
            "prompts": len([orchestration.prompt_descriptor()]),
            "cli_commands": len(_cli_command_names()),
            "contracts": len(sorted((ROOT / "contracts").glob("*.schema.json"))),
            "identity_tables": len(_identity_table_names()),
        }

    # file -> {key: pattern}. Each pattern captures one integer.
    PATTERNS = {
        ROOT_README: {
            "mcp_tools": re.compile(r"(\d+) 個 MCP tool"),
            "prompts": re.compile(r"(\d+) 個 orchestration prompt"),
            "cli_commands": re.compile(r"(\d+) 個 CLI 指令"),
            "contracts": re.compile(r"(\d+) 份 JSON Schema contract"),
            "identity_tables": re.compile(r"(\d+) 張 identity 表"),
        },
        RELEASE_INVENTORY: {
            "mcp_tools": re.compile(r"(\d+) MCP tools"),
            "prompts": re.compile(r"(\d+) orchestration prompt"),
            "cli_commands": re.compile(r"(\d+) CLI commands"),
            "contracts": re.compile(r"(\d+) JSON Schema contracts"),
            "identity_tables": re.compile(r"(\d+) identity tables"),
        },
    }

    def test_every_published_count_is_the_real_one(self):
        for path, patterns in self.PATTERNS.items():
            # Markdown wraps, and a wrapped sentence is still one statement.
            text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
            for key, pattern in patterns.items():
                found = [int(value) for value in pattern.findall(text)]
                with self.subTest(file=path.relative_to(ROOT), count=key):
                    self.assertTrue(
                        found, f"{path.name} no longer states its {key} count"
                    )
                    for stated in found:
                        self.assertEqual(self.expected[key], stated)

    def test_the_readme_never_calls_the_session_route_read_only(self):
        """Issue #129, checked where a reader would be misled rather than only in code.

        `startCoachSession` applies reconciliation, and reconciliation is store commits.
        The two files here describe it in prose to an athlete choosing how to use it, so
        this is the layer where "read-only" would do the damage.
        """
        for path in (ROOT_README, RELEASE_INVENTORY):
            text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).lower()
            for claim in ("startcoachsession is read-only", "startcoachsession 是 read-only"):
                with self.subTest(file=path.relative_to(ROOT), claim=claim):
                    self.assertNotIn(claim, text)
            # The narrower shape that would be wrong in the same way: the two names in
            # one read-only sentence, with only `getCoachState` entitled to be there.
            for sentence in re.split(r"[.。\n]", text):
                if "read-only" not in sentence and "唯讀" not in sentence:
                    continue
                if "startcoachsession" not in sentence:
                    continue
                with self.subTest(file=path.relative_to(ROOT), sentence=sentence.strip()):
                    self.assertIn(
                        "getcoachstate",
                        sentence,
                        "a read-only sentence naming startCoachSession and nothing else",
                    )


class OwnerDataProseTests(unittest.TestCase):
    """Issue #132: what README promises about export and deletion is what the tools say.

    Both lists are read by an athlete deciding whether to trust an erasure, and both exist
    in two places once README describes them. These check the *substance* of each line --
    the noun the athlete would look for -- rather than the sentence, so a rewording that
    keeps the meaning passes and a dropped exclusion does not.
    """

    @classmethod
    def setUpClass(cls):
        cls.readme = re.sub(
            r"\s+", " ", ROOT_README.read_text(encoding="utf-8")
        ).lower()

    def test_every_export_exclusion_is_stated(self):
        from garmin_coach_loop import owner_data

        self.assertEqual(3, len(owner_data.EXCLUDED))
        for marker in ("fingerprint", "gps", "owner id"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_every_deletion_exclusion_is_stated(self):
        from garmin_coach_loop import owner_data

        self.assertEqual(3, len(owner_data.NOT_REMOVED))
        # The calendar, the provider authorization, and the operational logs -- the three
        # things `NOT_REMOVED` names, each in the form README uses for it.
        for marker in ("intervals.icu 日曆", "intervals.icu settings", "營運紀錄"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_no_documented_step_takes_a_caller_supplied_owner_id(self):
        """Issue #132 acceptance: no tutorial step may ask for or accept an owner id.

        Checked against the tools themselves rather than against prose: the property is
        that the parameter does not exist to be asked for.
        """
        for tool in TOOLS:
            properties = tool.input_schema.get("properties") or {}
            for name in properties:
                with self.subTest(tool=tool.name, field=name):
                    self.assertNotIn("owner", name.lower())
                    self.assertNotIn("athlete_id", name.lower())


if __name__ == "__main__":
    unittest.main()
