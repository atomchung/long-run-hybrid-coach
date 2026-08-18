"""One sport vocabulary, checked across every hand-maintained copy of it.

The vocabulary lives in ``validation.SPORTS`` and is copied by hand into the two
contract schemas, the MCP tool schemas, and the OpenAPI document -- five-plus literal
copies with no generator between them. Each copy exists for a real reason (a schema a
client downloads cannot import a Python set), but nothing before this test failed when
one copy moved and another did not: a sport the validator accepted would simply be
refused at whichever surface was forgotten, and the athlete would read it as the
product's answer rather than as drift.

Same stdlib-only, line-level reading of openapi.yaml the other contract tests use --
no YAML parser enters the repository for this.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from garmin_coach_loop import mcp_transport
from garmin_coach_loop.athlete_evidence import REPORTABLE_SPORTS
from garmin_coach_loop.validation import SPORTS

ROOT = Path(__file__).resolve().parents[1]
PLAN_STATE_SCHEMA = ROOT / "contracts" / "plan-state.schema.json"
COACH_CONTEXT_SCHEMA = ROOT / "contracts" / "coach-context.schema.json"
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"


class SportVocabularyParityTests(unittest.TestCase):
    def test_the_plan_state_schema_agrees_with_the_validator(self):
        schema = json.loads(PLAN_STATE_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(SPORTS, set(schema["$defs"]["sport"]["enum"]))

    def test_the_coach_context_schema_agrees_with_the_validator(self):
        schema = json.loads(COACH_CONTEXT_SCHEMA.read_text(encoding="utf-8"))
        defs = schema["$defs"]
        # Rows describing training that happened, or sessions to train: rest is not a
        # session, so the row vocabularies drop it -- the same subtraction the
        # validator makes for each of these groups.
        for name in ("actual", "cycle_session", "reported_activity"):
            with self.subTest(name=name):
                self.assertEqual(
                    SPORTS - {"rest"}, set(defs[name]["properties"]["sport"]["enum"])
                )
        self.assertEqual(
            SPORTS, set(defs["calendar_item"]["properties"]["sport"]["enum"])
        )

    def test_the_mcp_tool_schemas_agree_with_the_validator(self):
        self.assertEqual(SPORTS, set(mcp_transport._SPORTS))

    def test_the_reportable_vocabulary_is_derived_not_copied(self):
        self.assertEqual(SPORTS - {"rest"}, set(REPORTABLE_SPORTS))

    def test_every_openapi_sport_enum_agrees_with_the_validator(self):
        # "running" appears in no other vocabulary this document declares, so every
        # flow-list enum naming it is a sport enum: the full set on session writes, and
        # the set minus rest where the field describes a session that was trained.
        lines = OPENAPI_PATH.read_text(encoding="utf-8").splitlines()
        found = []
        for line in lines:
            match = re.search(r"enum: \[([^\]]+)\]", line)
            if not match:
                continue
            members = {item.strip() for item in match.group(1).split(",")}
            if "running" not in members:
                continue
            found.append(members)
            with self.subTest(line=line.strip()):
                self.assertIn(members, (SPORTS, SPORTS - {"rest"}))
        # The document currently carries three: initializeCoachPlan's sessions, the
        # decision surface's session operations, and recordActivitySummary. Fewer means
        # a copy stopped being findable by this scan and the guard silently narrowed.
        self.assertGreaterEqual(len(found), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
