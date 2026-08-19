"""The orchestration prompt: one file, a hard size budget, and the clauses it must keep.

Every MCP client is handed this text at connect time and carries it for the whole
conversation, so a paragraph here is a paragraph of every future turn's context. That is
why the budget below is a test rather than a note, and why raising it is a decision
instead of a way to fit a new paragraph.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from garmin_coach_loop import orchestration
from garmin_coach_loop.gateway import gateway_artifact_sha256
from garmin_coach_loop.release_identity import package_artifact_sha256
from scripts import release_bundle


ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS_PATH = ROOT / "garmin_coach_loop" / "orchestration.md"
# The orchestration prompt's ceiling. It arrived as one client's paste limit and stays
# for a reason that outlived it: every MCP client is handed this file at connect time
# and carries it for the whole conversation, so a paragraph here is a paragraph of
# every future turn. Unbounded growth is also how an orchestration layer becomes a
# shadow coach (AGENTS.md 11) -- one reasonable-sounding sentence at a time. Raising
# it is a decision, not a way to fit a new paragraph; a new one costs an old one.
MAX_ORCHESTRATION_CHARACTERS = 7600


class OrchestrationPromptTests(unittest.TestCase):
    def test_the_release_path_and_the_runtime_path_name_one_file(self):
        """Issue #125: the anti-drift property, held by there being nothing to sync.

        The gateway serves this text as an MCP prompt and the release bundle binds its
        digest. Two hand-maintained copies would drift the way field descriptions drift,
        so there is one file -- which is only true while both name the same one.
        """
        self.assertEqual(INSTRUCTIONS_PATH, ROOT / release_bundle.INSTRUCTIONS)
        self.assertEqual(
            INSTRUCTIONS_PATH.read_text(encoding="utf-8").rstrip("\r\n"),
            orchestration.instructions(),
        )
    def test_the_gateway_artifact_digest_covers_the_text_the_gateway_serves(self):
        """A prose-only change has to move the deployed artifact identity.

        The gateway serves this file verbatim to every MCP client that fetches the
        prompt, so a digest that skipped it would call two deployments identical while
        they told two different stories about when a confirmation is required.
        """
        package = INSTRUCTIONS_PATH.parent
        without_the_prompt = package_artifact_sha256(
            [
                (path.name, path.read_bytes())
                for path in package.iterdir()
                if path.is_file()
                and path.suffix in {".py", ".md"}
                and path != INSTRUCTIONS_PATH
            ]
        )
        self.assertNotEqual(without_the_prompt, gateway_artifact_sha256())
    def test_the_orchestration_prompt_fits_its_budget_and_keeps_the_contract(self):
        instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(
            len(instructions),
            MAX_ORCHESTRATION_CHARACTERS,
            "the orchestration prompt is over budget; a new paragraph costs an old one",
        )
        # Flattened, because the file is hard-wrapped and where a line happens to break is
        # not a fact about the contract. Rewrapping a paragraph is the most ordinary edit
        # there is, and it should not be able to fail this test or, worse, pass it by
        # moving a phrase back onto one line.
        instructions = " ".join(instructions.split())
        required = (
            "`startCoachSession`",
            "only source of truth",
            "not chat memory",
            "`inspectIntervalsPermissions`",
            "no PlanState or coaching-session",
            # The two live classifications, not the recorded scope list: a diagnostic the
            # model explains from a stored value is what issue #162 cost a day to.
            "`settings_read` and `calendar_read`",
            "`readable` = 200",
            "`denied` = 403",
            "`invalid_or_expired` = 401",
            "Settings values, tokens, fingerprints",
            "athlete ids, or owner",
            "`prepareCoachInitialization`",
            "`initializeCoachPlan`",
            "identical `initialization_request`",
            "ONE confirmation",
            "`prepareCoachDecision`",
            "`applyCoachDecision`",
            "identical `context`, `change_request`",
            "`goal_context.measurement_protocol`",
            "Monday-Sunday",
            "`prepareWorkoutDelivery`",
            "`applyWorkoutDelivery`",
            "`withdraw: true`",
            "`intervals_accepted`",
            "Garmin Connect or the watch",
            "`status: \"partial\"`",
            "`attempt_open: true`",
            "never withdraw a past workout",
            # The recovery path only works if the model asks first and clears second.
            "`clearDeliveryAttempt`",
            "Never clear on your own initiative",
            "`stale_plan_version`",
            "reconnect Intervals",
        )
        for phrase in required:
            self.assertIn(phrase, instructions, phrase)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
