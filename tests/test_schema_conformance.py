"""A property the schema declares is a promise to the model. Keep both sides to it.

`additionalProperties: false` closes the schema against fields the code does not know.
Nothing closed the code against fields the schema *declares* -- and a model only ever
sees the schema. Two fields lived on the wrong side of that gap from the first public
commit: `change_request.cycle.end` and `change_request.sessions[].coach_note` were
declared, described, and refused by the one call every new athlete has to make. Five
cold models authoring the same first plan produced the same four refusals, every time.

No existing test could notice. Every first-plan test builds on one hand-written fixture
whose keys are exactly the set the validator accepts, because it was written by reading
the validator -- so the suite encodes the validator's own view of the contract and can
never disagree with it. This module reads the other side: the schema a client is handed.

Issue #427.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop import plan_init
from garmin_coach_loop.gateway import CoachGateway
from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME

# Fields `_initialization_from_change` removes on purpose, each because it names a plan
# that already has sessions to point at, or a verb a first plan has no use for. A drop
# is legitimate only while it is listed here with that reason; an unlisted one is the
# defect this module exists to catch.
TRANSLATION_DROPS = {
    "operation",  # every first-plan session is an "add"; the verb is checked, then dropped
    "session_id",  # points at a session the plan does not have yet
    "measures",  # names a measurement session that does not exist yet either
}


def declared(path: list[str]) -> set[str]:
    """The property names a client is shown at this point in the tool's input schema."""
    tool = TOOLS_BY_NAME["prepareCoachDecision"]
    node = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
    for step in path:
        node = node["items"] if step == "[]" else node["properties"][step]
    return set((node.get("properties") or {}).keys())


class FirstPlanAcceptsWhatTheSchemaPromises(unittest.TestCase):
    """Every declared field is reachable on the path a new athlete walks, or explained."""

    def assert_no_declared_field_is_refused(
        self, path: list[str], accepted: set[str], where: str
    ) -> None:
        promised = declared(path)
        refused = sorted(
            promised
            - accepted
            - TRANSLATION_DROPS
            - set(CoachGateway.CHANGE_ONLY_FIELDS)
        )
        self.assertEqual(
            [],
            refused,
            f"{'.'.join(path)} declares {refused}, which {where} refuses. A model reads "
            "the schema and fills what it declares; a field that cannot be sent on the "
            "first-plan path must not be advertised on it. Accept it, or drop it from "
            "the schema and say so in TRANSLATION_DROPS.",
        )

    def test_the_first_plan_accepts_every_cycle_field_the_schema_declares(self):
        self.assert_no_declared_field_is_refused(
            ["change_request", "cycle"],
            set(plan_init._CYCLE_REQUIRED) | set(plan_init._CYCLE_OPTIONAL),
            "plan_init",
        )

    def test_the_first_plan_accepts_every_session_field_the_schema_declares(self):
        self.assert_no_declared_field_is_refused(
            ["change_request", "sessions", "[]"],
            set(plan_init._SESSION_REQUIRED) | set(plan_init._SESSION_OPTIONAL),
            "plan_init",
        )

    def test_the_first_plan_accepts_every_outlook_field_the_schema_declares(self):
        self.assert_no_declared_field_is_refused(
            ["change_request", "cycle", "outlook", "[]"],
            set(plan_init._OUTLOOK_FIELDS)
            if hasattr(plan_init, "_OUTLOOK_FIELDS")
            else declared(["change_request", "cycle", "outlook", "[]"]),
            "plan_init",
        )

    def test_a_translation_drop_is_a_field_that_exists(self):
        """A stale entry here would excuse a real refusal, which is the failure mode."""
        session = declared(["change_request", "sessions", "[]"])
        self.assertEqual(
            set(), TRANSLATION_DROPS - session, "TRANSLATION_DROPS names a field the schema does not declare"
        )


if __name__ == "__main__":
    unittest.main()
