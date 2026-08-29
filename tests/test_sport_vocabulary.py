"""One sport vocabulary, checked across every hand-maintained copy of it.

The vocabulary lives in ``validation.SPORTS`` and is copied by hand into the two
contract schemas and the MCP tool schemas, with no generator between them. Each copy
exists for a real reason (a schema a client downloads cannot import a Python set), but
nothing before this test failed when one copy moved and another did not: a sport the
validator accepted would simply be refused at whichever surface was forgotten, and the
athlete would read it as the product's answer rather than as drift.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from garmin_coach_loop import mcp_transport
from garmin_coach_loop.athlete_evidence import REPORTABLE_SPORTS
from garmin_coach_loop.validation import SPORTS

ROOT = Path(__file__).resolve().parents[1]
PLAN_STATE_SCHEMA = ROOT / "contracts" / "plan-state.schema.json"
COACH_CONTEXT_SCHEMA = ROOT / "contracts" / "coach-context.schema.json"
PRE_PLAN_SCHEMA = ROOT / "contracts" / "pre-plan-observations.schema.json"


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

    def test_the_pre_plan_observations_schema_agrees_with_the_validator(self):
        """The one vocabulary that contract copies rather than referencing.

        Its row shapes point at the CoachContext's, across files, so a rename there fails
        here on its own. An enum has no shape of its own to point at, which puts this copy
        in the same position as the two above and under the same check.
        """
        schema = json.loads(PRE_PLAN_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(
            SPORTS - {"rest"}, set(schema["$defs"]["reported_sport"]["enum"])
        )

    def test_the_mcp_tool_schemas_agree_with_the_validator(self):
        self.assertEqual(SPORTS, set(mcp_transport._SPORTS))

    def test_the_reportable_vocabulary_is_derived_not_copied(self):
        self.assertEqual(SPORTS - {"rest"}, set(REPORTABLE_SPORTS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
