"""The five layers of the distributed product, and the lines between them (#117).

What ships is one Agent Skill and one MCP tool surface, packaged thinly per platform.
That only stays true if each layer keeps to what it owns, and every one of these
boundaries has an obvious way to erode:

- the **Skill** absorbs the tool surface, and now says something about `applyWorkoutDelivery`
  that the tool descriptions no longer do;
- the **orchestration layer** grows a training rule, and becomes a second coach whose
  advice nobody evaluates (AGENTS.md 11);
- the **packaging** file grows coaching prose, and one platform quietly forks;
- the **Skill** links a repository path, which reads fine here and is a dead link for
  everyone who installed it;
- the **submission dossier** keeps describing a tool that was renamed, a hint that was
  corrected, or a product name that was changed -- and a directory reviewer reads the
  stale version. This layer is the one where drift is read by somebody outside the
  project, which is why the tool table there is asserted row by row against the running
  catalogue rather than trusted.

None of these breaks a test that only exercises the running product, which is why they
are checked directly against the files.
"""

from __future__ import annotations

import inspect
import json
import re
import unittest
from pathlib import Path

from garmin_coach_loop import orchestration
from garmin_coach_loop import release_identity
from garmin_coach_loop.gateway import (
    INTERVALS_OAUTH_SCOPES,
    MCP_PATH,
    PRODUCT_VERSION,
    PROTECTED_RESOURCE_METADATA_PATH,
    ROUTES,
    _METADATA_PATH_SUFFIX,
)
from garmin_coach_loop.mcp_transport import TOOLS


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".agents" / "skills" / "garmin-coach-loop"
SKILL = SKILL_ROOT / "SKILL.md"
REGISTRY_ENTRY = ROOT / "server.json"
TRAINING_REFERENCE = ROOT / "garmin_coach_loop" / "hybrid_training.md"
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
# README's translations publish the same counts to a reader who never opens the Chinese
# original, and nothing was checking them: when this check was added they stated three
# contracts and, in the simplified file, thirty CLI commands and five identity tables.
README_EN = ROOT / "README.en.md"
README_ZH_HANS = ROOT / "README.zh-Hans.md"

# The user-flows page publishes how many eval cases exist, the same way README
# publishes the tool count -- and for the same reason it can go stale (#346 was the
# repair after #341 added cases and the sentence was not revisited).
USER_FLOWS = ROOT / "docs" / "user-flows.md"
EVAL_CASES = ROOT / "evals" / "cases"

# The submission dossier: what a directory asks for, written down once and mapped per
# platform. Its shared file carries the tool table a reviewer is shown.
DISTRIBUTION = ROOT / "docs" / "distribution"
DOSSIER = DISTRIBUTION / "README.md"
DOSSIER_CONFORMANCE = DISTRIBUTION / "openai-review-conformance.md"

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
        """An installed copy is the whole of what it needs, or it is broken on arrival.

        There are no links left to check: the layers this file used to point at are
        served by the product now, so a client that has the Skill and no connection has
        a trigger and a loop rather than a dangling path. The loop still runs, because
        anything it can dereference has to be inside the bundle.
        """
        text = SKILL.read_text(encoding="utf-8")
        # Not vacuous on an empty file, which is the way this check could go quiet.
        self.assertIn("## The loop", text)

        for target in _MARKDOWN_LINK.findall(text):
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

        This is the prompt a host puts in front of its model unevaluated by
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


# The runbook gives the operator one copyable statement of the authorize set, and a
# scope list is the one setting that cannot be corrected quietly: asking for one scope
# too many costs the whole authorization (issue #97), and changing the set afterwards
# re-authorizes every connected client by hand (docs/ops/scope-change-costs.md).
_SCOPE_LIST_IN_PROSE = re.compile(r"`([A-Z]+:[A-Z]+(?:,[A-Z]+:[A-Z]+)+)`")
SETUP_RUNBOOK = ROOT / "docs" / "deploy-gateway.md"


class AuthorizeScopeTests(unittest.TestCase):
    def test_the_runbook_offers_exactly_the_scopes_the_gateway_requests(self):
        # Only copyable scope lists are checked; prose is free to name a scope to explain
        # it. The gateway's own tuple is the single source -- this asserts the operator
        # cannot be told to paste a different one.
        offered = _SCOPE_LIST_IN_PROSE.findall(SETUP_RUNBOOK.read_text(encoding="utf-8"))
        self.assertEqual([",".join(INTERVALS_OAUTH_SCOPES)], offered)


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

_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _english_count(token: str) -> int | None:
    """0-99 written out, hyphenated or not: "forty-one", "thirty nine", "seven".

    The fixed `_NUMBER_WORDS` tuple above stops at twenty-five because a tool count has
    no reason to pass it. A case count does -- it grows every time a coaching line gets
    pinned -- so this composes the tens rather than listing them.
    """
    token = token.strip().lower().replace("\u2011", "-")
    if token.isdigit():
        return int(token)
    if token in _NUMBER_WORDS:
        return _NUMBER_WORDS.index(token)
    parts = re.split(r"[-\s]+", token)
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _NUMBER_WORDS[1:10]:
        return (_TENS.index(parts[0]) + 2) * 10 + _NUMBER_WORDS.index(parts[1])
    if len(parts) == 1 and parts[0] in _TENS:
        return (_TENS.index(parts[0]) + 2) * 10
    return None


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
            # The count is `prompts/list`'s own arity, not a number chosen here.
            "prompts": len(orchestration.PROMPTS),
            "cli_commands": len(_cli_command_names()),
            "contracts": len(sorted((ROOT / "contracts").glob("*.schema.json"))),
            "identity_tables": len(_identity_table_names()),
        }

    # file -> {key: pattern}. Each pattern captures one integer.
    PATTERNS = {
        ROOT_README: {
            "mcp_tools": re.compile(r"(\d+) 個 MCP tool"),
            "prompts": re.compile(r"(\d+) 個 prompt"),
            "cli_commands": re.compile(r"(\d+) 個 CLI 指令"),
            "contracts": re.compile(r"(\d+) 份 JSON Schema contract"),
            "identity_tables": re.compile(r"(\d+) 張 identity 表"),
        },
        RELEASE_INVENTORY: {
            "mcp_tools": re.compile(r"(\d+) MCP tools"),
            "prompts": re.compile(r"\*\*(\d+) prompts\*\*"),
            "cli_commands": re.compile(r"(\d+) CLI commands"),
            "contracts": re.compile(r"(\d+) JSON Schema contracts"),
            "identity_tables": re.compile(r"(\d+) identity tables"),
        },
        # Anchored on the bold markers the inventory sentence uses, so a bare number
        # elsewhere in the file can neither satisfy the check nor trip it.
        README_EN: {
            "mcp_tools": re.compile(r"\*\*(\d+) MCP tools\*\*"),
            "prompts": re.compile(r"\*\*(\d+) prompts\*\*"),
            "cli_commands": re.compile(r"\*\*(\d+) CLI commands\*\*"),
            "contracts": re.compile(r"\*\*(\d+) JSON Schema contracts\*\*"),
            "identity_tables": re.compile(r"\*\*(\d+) identity tables\*\*"),
        },
        README_ZH_HANS: {
            "mcp_tools": re.compile(r"\*\*(\d+) 个 MCP tool\*\*"),
            "prompts": re.compile(r"\*\*(\d+) 个 prompt\*\*"),
            "cli_commands": re.compile(r"\*\*(\d+) 个 CLI 指令\*\*"),
            "contracts": re.compile(r"\*\*(\d+) 份 JSON Schema contract\*\*"),
            "identity_tables": re.compile(r"\*\*(\d+) 张 identity 表\*\*"),
        },
    }

    def test_the_user_flows_page_states_the_real_eval_case_count(self):
        """Issue #346: the page publishes how many cases exist, and cases keep landing.

        The sentence was written at thirty-six, went stale when #341 added three, and was
        repaired by hand afterwards. A count maintained by whoever remembers is a count
        that is wrong between the two commits, so this reads both numbers out of that one
        sentence and checks them against the directory.

        Both numbers, because the sentence makes two claims: how many cases there are and
        how many coaching modes they carry. A case landing under a sixth mode is exactly
        as much a change to that sentence as a thirty-seventh case.
        """
        text = re.sub(r"\s+", " ", USER_FLOWS.read_text(encoding="utf-8"))
        sentence = re.search(
            r"([A-Za-z-]+|\d+) cases carrying all ([A-Za-z-]+|\d+) coaching modes", text
        )
        self.assertIsNotNone(
            sentence,
            "docs/user-flows.md no longer states its eval-case count in the form this "
            "test reads. Restate it, or move this check to the sentence that replaced it.",
        )

        cases = sorted(EVAL_CASES.glob("*.json"))
        modes = {json.loads(path.read_text(encoding="utf-8"))["mode"] for path in cases}

        stated_cases = _english_count(sentence.group(1))
        stated_modes = _english_count(sentence.group(2))
        self.assertIsNotNone(
            stated_cases, f"cannot read {sentence.group(1)!r} as a number"
        )
        self.assertIsNotNone(
            stated_modes, f"cannot read {sentence.group(2)!r} as a number"
        )
        self.assertEqual(
            len(cases),
            stated_cases,
            f"docs/user-flows.md says {stated_cases} eval cases; evals/cases holds "
            f"{len(cases)}. Update the sentence in the same change that adds the case.",
        )
        self.assertEqual(
            len(modes),
            stated_modes,
            f"docs/user-flows.md says {stated_modes} coaching modes; evals/cases carries "
            f"{len(modes)}: {sorted(modes)}.",
        )

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


# Every markdown file that describes this product to somebody outside it: the per-entry
# packaging, and the submission material a directory reviewer is handed. They are checked
# together because the two ways they go wrong are the same two -- naming an operation that
# no longer exists, and pointing at a file that no longer does.
def _public_documents() -> list[Path]:
    return (
        sorted(ENTRYPOINTS.glob("*/README.md"))
        + [ENTRYPOINTS / "README.md"]
        + sorted(DISTRIBUTION.glob("*.md"))
    )


# A backticked identifier of two or more camelCase words -- the shape every tool name has.
# Single lowercase words (`main`, `title`, `resource`) are excluded by requiring the
# interior capital, which is what keeps this from matching ordinary prose in backticks.
_CAMEL_CASE_TOKEN = re.compile(r"`([a-z]+(?:[A-Z][a-z0-9]+)+)`")

# Identifiers of that shape which are not tools and never will be: protocol fields, config
# keys, and the annotation names themselves. Anything else matching the pattern has to be a
# real tool, so a renamed operation left behind in prose fails rather than misleading a
# reviewer. A genuine new term lands here deliberately, which is the point -- the default is
# "this is a tool name", not "this is probably fine".
_NOT_TOOL_NAMES = frozenset(
    {
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
        "outputSchema",
        "structuredContent",
        "isError",
        "protocolVersion",
        "mcpServers",
        "nextCursor",
        "operationId",
        "operationIds",
        "displayName",
        "shortDescription",
        "composerIcon",
        "securitySchemes",
    }
)


class SubmissionDossierTests(unittest.TestCase):
    """Issue #117 acceptance, at the layer a directory reviewer actually reads.

    The dossier under `docs/distribution/` restates part of the tool catalogue on purpose:
    one directory requires a title, three hints and a justification for every tool, and an
    operator filling that form cannot paste Python. Restating it is safe only while it
    cannot drift, so every fact in that table is asserted against `TOOLS` here, and the
    prose columns are the only thing a human owns.
    """

    @classmethod
    def setUpClass(cls):
        assert DOSSIER.exists(), f"the submission dossier is missing: {DOSSIER}"
        cls.dossier = DOSSIER.read_text(encoding="utf-8")
        cls.documents = _public_documents()

    def test_the_tool_table_is_the_running_catalogue_row_for_row(self):
        rows = []
        for line in self.dossier.splitlines():
            match = re.match(
                r"\|\s*`(\w+)`\s*\|([^|]+)\|\s*(yes|no)\s*\|\s*(yes|no)\s*\|\s*(yes|no)\s*\|",
                line.strip(),
            )
            if match:
                name, title, read_only, destructive, open_world = match.groups()
                rows.append(
                    (
                        name,
                        title.strip(),
                        read_only == "yes",
                        destructive == "yes",
                        open_world == "yes",
                    )
                )

        expected = [
            (
                tool.name,
                tool.annotations["title"],
                tool.annotations["readOnlyHint"],
                tool.annotations["destructiveHint"],
                tool.annotations["openWorldHint"],
            )
            for tool in TOOLS
        ]
        self.assertEqual(expected, rows)

    def test_the_stated_read_only_split_is_the_real_one(self):
        read_only = sum(1 for tool in TOOLS if tool.annotations["readOnlyHint"])
        stated = [
            (int(a), int(b))
            for a, b in re.findall(r"(\d+) read-only and (\d+) write", self._all_text())
        ]

        self.assertTrue(stated, "no document states the read-only/write split any more")
        for pair in stated:
            self.assertEqual((read_only, len(TOOLS) - read_only), pair)

    def test_the_stated_longest_tool_name_is_the_real_one(self):
        stated = [
            int(value)
            for value in re.findall(r"longest name is (\d+) characters", self._all_text())
        ]

        self.assertTrue(stated, "no document states the longest tool name any more")
        for length in stated:
            self.assertEqual(max(len(tool.name) for tool in TOOLS), length)

    def test_the_listing_identity_is_the_packaging_files_own_strings(self):
        """One name and one one-liner, not one per platform.

        The character counts are checked too, because they are what a platform's field
        limit is judged against -- and a shortened string with a stale count beside it is
        how a submission gets built on a number nobody re-measured.
        """
        packaging = PACKAGING.read_text(encoding="utf-8")
        canonical = {
            key: re.search(rf'{key}:\s*"([^"]+)"', packaging).group(1)
            for key in ("display_name", "short_description")
        }

        stated = {
            key: (value, int(count))
            for key, value, count in re.findall(
                r"`(display_name|short_description)`[^`]*`([^`]+)`,\s*(\d+) characters",
                self.dossier,
            )
        }
        self.assertEqual({"display_name", "short_description"}, set(stated))
        for key, (value, count) in stated.items():
            with self.subTest(field=key):
                self.assertEqual(canonical[key], value)
                self.assertEqual(len(value), count)

        # And the same strings wherever a platform file pastes them into a form. Every
        # settled string is removed before the scan, not just the one being checked: the
        # public one-liner and the subtitle deliberately open the same way, and a sibling
        # string the shared facts already name is not the fork this is looking for -- an
        # unlisted variant of one is.
        settled = set(canonical.values()) | {
            match.group(1)
            for match in re.finditer(r"^\| `([^`]+)` \| \d+ \| ", self.dossier, re.M)
        }
        for path in DISTRIBUTION.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            residue = text
            for string in settled:
                residue = residue.replace(f"`{string}`", "")
            for key, value in canonical.items():
                with self.subTest(file=path.relative_to(ROOT), field=key):
                    self.assertNotIn(
                        f"`{value[:12]}",
                        residue,
                        "a near-copy of the canonical string, which is how a fork starts",
                    )

    def test_no_public_document_names_a_tool_the_catalogue_does_not_have(self):
        names = {tool.name for tool in TOOLS}
        for path in self.documents:
            text = path.read_text(encoding="utf-8")
            for token in sorted(set(_CAMEL_CASE_TOKEN.findall(text))):
                if token in _NOT_TOOL_NAMES:
                    continue
                with self.subTest(file=path.relative_to(ROOT), token=token):
                    self.assertIn(
                        token,
                        names,
                        "names an operation this product does not serve",
                    )

    def test_every_relative_link_in_a_public_document_resolves(self):
        for path in self.documents:
            for target in _MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (path.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(file=path.relative_to(ROOT), link=target):
                    self.assertTrue(resolved.exists(), f"dead link: {target}")

    def test_the_dossier_quotes_the_challenge_this_gateway_actually_sends(self):
        """The one line in the dossier a reviewer is most likely to paste into a client.

        It went stale the moment the challenge changed, silently, because nothing held it
        to the code -- the same failure the tool table above exists to prevent, one
        section further down the same file. A reviewer reading a `resource_metadata` URL
        this server no longer names would be sent to a document describing a different
        resource, and would have no reason to doubt it.
        """
        quoted = re.search(
            r'resource_metadata="([^"]+)"', self.dossier
        )
        self.assertIsNotNone(quoted, "the dossier no longer quotes a challenge at all")
        self.assertTrue(
            quoted.group(1).endswith(
                PROTECTED_RESOURCE_METADATA_PATH + _METADATA_PATH_SUFFIX
            ),
            "the dossier quotes a resource_metadata path the gateway does not name",
        )

    def test_every_discovery_path_the_dossier_promises_is_actually_routed(self):
        """`ROUTES` is the only authority on what answers; the dossier restates it.

        The restating is deliberate -- a directory reviewer cannot read Python -- so it
        is held to the table rather than trusted, exactly like the tool rows. A promised
        path that 404s is worse than an unmentioned one: it is the reviewer concluding
        this server has no discovery to find.
        """
        promised = {
            path
            for path in re.findall(r"`(/[\w./-]*\.well-known/[\w./-]+)`", self.dossier)
            if "openid-configuration" not in path  # documented as a deliberate 404
        }
        self.assertIn(
            MCP_PATH + PROTECTED_RESOURCE_METADATA_PATH,
            promised,
            "the joined spelling is served but no longer written down",
        )
        for path in sorted(promised):
            with self.subTest(path=path):
                self.assertIn(path, ROUTES, "promised in the dossier, absent from ROUTES")

    def test_the_dossier_states_the_real_tool_count(self):
        for path in DISTRIBUTION.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            for stated in _stated_tool_counts(text):
                with self.subTest(file=path.relative_to(ROOT), stated=stated):
                    self.assertEqual(len(TOOLS), stated)

    def test_the_dossier_never_calls_the_session_route_read_only(self):
        """The same claim `PublishedCountTests` guards in README, one audience further out.

        `startCoachSession` applies reconciliation, and reconciliation commits. A
        submission form that called it read-only would be telling a directory the tool can
        run without asking. The stronger half of this guard is the table above, whose
        `startCoachSession` row is asserted equal to the annotation itself; these are the
        prose spellings that would contradict it.
        """
        for path in DISTRIBUTION.glob("*.md"):
            text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).lower()
            for claim in (
                "startcoachsession is read-only",
                "`startcoachsession` is read-only",
                "read-only tools: `startcoachsession`",
            ):
                with self.subTest(file=path.relative_to(ROOT), claim=claim):
                    self.assertNotIn(claim, text)

    def test_the_dossier_does_not_grow_coaching_vocabulary(self):
        """Submission material is connection, metadata and review information.

        A directory listing that started explaining how to arrange a training week would be
        the fourth copy of a judgment the Skill owns and the evals cover.
        """
        for path in DISTRIBUTION.glob("*.md"):
            text = path.read_text(encoding="utf-8").lower()
            for term in TRAINING_VOCABULARY:
                with self.subTest(file=path.relative_to(ROOT), term=term):
                    self.assertNotIn(term, text)

    def _all_text(self) -> str:
        return "\n".join(path.read_text(encoding="utf-8") for path in self.documents)


if __name__ == "__main__":
    unittest.main()


class RegistryEntryTests(unittest.TestCase):
    """`server.json` is published to a registry that keeps whatever it was last given.

    A stale version or a moved endpoint there is not a failing build anywhere -- it is a
    listing that quietly describes a release nobody is running. So the two fields that can
    drift are read from the product rather than typed here, the same way every other
    published fact in this file is.
    """

    @classmethod
    def setUpClass(cls):
        cls.entry = json.loads(REGISTRY_ENTRY.read_text(encoding="utf-8"))

    def test_the_published_version_is_the_running_one(self):
        self.assertEqual(PRODUCT_VERSION, self.entry["version"])

    def test_the_published_endpoint_is_the_gateway_path(self):
        remotes = self.entry["remotes"]
        self.assertEqual(1, len(remotes), "one endpoint, or a client has to choose")
        self.assertEqual("streamable-http", remotes[0]["type"])
        self.assertTrue(
            remotes[0]["url"].endswith(MCP_PATH),
            f"the listed URL does not end at the gateway's own {MCP_PATH}",
        )

    def test_it_fits_the_fields_the_registry_caps(self):
        # Rejected-on-submit lengths, not style preferences: the schema caps description
        # and title at 100 characters each.
        for field in ("description", "title"):
            with self.subTest(field=field):
                self.assertLessEqual(len(self.entry[field]), 100)


class SnapshotClassificationTests(unittest.TestCase):
    """The re-scan table answers "does this change cost a resubmission" from the two hashes.

    It answers it by naming what they bind, in prose. A field added to either one without
    that prose moving turns the table into a confident wrong answer -- which is worse than
    no table, because the table exists to be trusted without re-deriving it.
    """

    @staticmethod
    def _bound(function) -> set[str]:
        return set(inspect.signature(function).parameters)

    def test_the_dossier_names_every_field_release_id_binds(self):
        stated = {
            "git_commit",
            "instructions_sha256",
            "tool_catalogue_sha256",
            "skill_sha256",
            "gateway_artifact_sha256",
            "gateway_domain",
        }
        self.assertEqual(self._bound(release_identity.make_release_id), stated)
        text = DOSSIER_CONFORMANCE.read_text(encoding="utf-8")
        for field in stated - {"gateway_domain"}:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", text)

    def test_the_dossier_names_every_field_the_configuration_binding_binds(self):
        self.assertEqual(
            {
                "resolved_state_root",
                "intervals_client_id",
                "environment",
                "instance_id",
                "token_hmac_key",
            },
            self._bound(release_identity.make_configuration_binding),
        )
