"""The two served prompts: one file each, a hard size budget, and the clauses they keep.

Both are whole-conversation text wherever a host puts them in front of a model, so a
paragraph in either is a paragraph of every one of that conversation's turns.
That is why the budgets below are tests rather than notes, and why raising one is a
decision instead of a way to fit a new paragraph.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from garmin_coach_loop import orchestration
from garmin_coach_loop.context_core import TRAINING_BREAK_MIN_DAYS
from garmin_coach_loop.gateway import gateway_artifact_sha256
from garmin_coach_loop.release_identity import package_artifact_sha256
from scripts import release_bundle


ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS_PATH = ROOT / "garmin_coach_loop" / "orchestration.md"
# The orchestration prompt's ceiling. It arrived as one client's paste limit and stays
# for a reason that outlived it: a host that puts this in front of a model carries it
# for the whole conversation, so a paragraph here is a paragraph of every one of that
# conversation's turns. Unbounded growth is also how an orchestration layer becomes a
# shadow coach (AGENTS.md 11) -- one reasonable-sounding sentence at a time. Raising
# it is a decision, not a way to fit a new paragraph; a new one costs an old one.
MAX_ORCHESTRATION_CHARACTERS = 7600

TRAINING_PATH = ROOT / "garmin_coach_loop" / "hybrid_training.md"
# The training layer's own ceiling, and now the same number as the orchestration one
# above. It was set at what the file already was when it started being served -- a size
# it happened to have because it was written to a Skill's budget -- which fixed the
# price of coaching judgment at an accident rather than at a decision. #221 moved the
# product's scope past what that accident left room for: sport this plan never
# prescribed is still load the week has to be arranged around, and the judgment for
# reading it does not fit in the two hundred characters that were spare. So the two
# served prompts share one ceiling, on this file's own principle that a coaching
# paragraph is not cheaper than an orchestration one -- and, read the other way round,
# not dearer either. Inside the ceiling nothing changes: a new paragraph still costs an
# old one, and the week-shape passage below it paid part of this one.
#
# #230 moved the scope again: an athlete with a weight goal has body-composition
# evidence the coach must know how to read, and the judgment for reading it -- the
# lens over existing metrics, the eating-disorder boundaries -- did not fit in the
# hundred-odd characters that were spare. The chapter arrived distilled (a three-way
# review cut it from 2.9k to 1.2k by the test "would a strong model, given the
# evidence and no sentence, get this wrong?") and the two bare-link reference lists
# retired to pay most of the price, so the file grew by ~700 characters net. The
# training ceiling moves to 8200 to admit that; orchestration stays at 7600. The
# shared number above was a statement of equal price per paragraph, not a coupling,
# and that principle is untouched: a new paragraph still costs an old one.
#
# 2026-08-23, same day: the owner relaxed this ceiling to 12000. Holding the file
# to the character made every edit end in wordsmithing rounds that traded clarity
# for characters, and the growth discipline it enforced lives better in review --
# every change to this file arrives by PR. What remains here is a runaway
# backstop, not a budget.
MAX_TRAINING_CHARACTERS = 12000


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
            "ONE confirmation",
            "`prepareCoachDecision`",
            "`applyCoachDecision`",
            # A confirmation carries the proposal and the athlete's answer. The phrase
            # this replaced told the model to send the whole CoachContext and change
            # request a second time, which is the 33 KB issue #239 measured -- and the
            # gateway has held both under the proposal since #355, so the instruction was
            # buying nothing but the resend.
            "nothing prepare already holds",
            "`goal_context.measurement_protocol`",
            # A null measurement is declared, not merely marked: `measures` counts only
            # inside the week `goal.measurement` names (`marks_the_comparison`), so the
            # sentence this replaced -- schedule it "with `measures` set" -- sent the
            # coach to a marker the product ignores until the goal is rewritten under
            # cycle scope (issue #372).
            "`goal.measurement`: cycle scope, `measures` on the repeat",
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

    def test_the_first_512_characters_carry_the_write_boundary(self):
        """What survives a host that keeps only the opening.

        OpenAI's own guidance is to "keep the most important details in the first 512
        characters" of `instructions`, because a host may truncate or summarize the rest
        (docs/distribution/openai-review-conformance.md recorded this as held nowhere).
        The most important detail is not which call to make first -- a model that skips
        `startCoachSession` gives a weak answer. It is that a write to the plan or the
        calendar takes a preview and one explicit confirmation, because a model that
        skips *that* writes to the athlete's calendar without asking.
        """
        opening = " ".join(INSTRUCTIONS_PATH.read_text(encoding="utf-8")[:512].split())
        for phrase in (
            "not chat memory",
            "`startCoachSession`",
            "only source of truth",
            "ONE confirmation",
            "nothing is saved or delivered until the apply",
        ):
            self.assertIn(phrase, opening, phrase)

    def test_the_training_prompt_is_the_training_file_and_fits_its_budget(self):
        training = TRAINING_PATH.read_text(encoding="utf-8")
        self.assertEqual(training.rstrip("\r\n"), orchestration.training_judgment())
        self.assertLessEqual(
            len(training),
            MAX_TRAINING_CHARACTERS,
            "the training prompt is over budget; a new paragraph costs an old one",
        )
        # Flattened for the same reason the orchestration list is: where a hard wrap
        # falls is not a fact about the contract.
        flattened = " ".join(training.split())
        required = (
            # An absence is not a zero, and a null in either long-range field is not
            # coverage confirmed. All four answers of the 2026-09-09 run's before arm --
            # both questions, two samples each -- turned two unmatched weeks into 0 km
            # without this; none of the four did with it (issue #402).
            "not zero kilometres, not a confirmed rest",
            "neither a sync gap nor the athlete's own account is ruled out",
            # Pinned against the constant, not as a literal: a served sentence stating a
            # threshold has to move when the threshold does, and a string assertion would
            # stay green while the coach was told the wrong number.
            f"no blank of {TRAINING_BREAK_MIN_DAYS} days or more was observed",
            # A pair of fields the cycle's own author fills that no served text named
            # before (issue #333). This is the exact line measured on the GPT family
            # there; rewording it makes that measurement about something else. The
            # matching sentence for a session's own `fallback` (#312) was measured as a
            # no-op on this family and is deliberately absent.
            "read `plan.cycle.adjust_conditions` and `plan.cycle.stop_conditions`",
            "Say which one you acted on, or that none of them describes what happened.",
        )
        for phrase in required:
            self.assertIn(phrase, flattened, phrase)

    def test_the_two_prompts_are_not_each_other(self):
        """The boundary, checked where both texts are in hand.

        Sequencing that drifts into the training layer stops being reviewable by the
        coaching evals; coaching that drifts into the orchestration layer is a rule with
        no reviewer (AGENTS.md 11). Neither is caught by either file's own budget.
        """
        self.assertNotEqual(orchestration.instructions(), orchestration.training_judgment())
        self.assertEqual(
            {orchestration.PROMPT_NAME, orchestration.TRAINING_PROMPT_NAME},
            set(orchestration.PROMPTS),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
