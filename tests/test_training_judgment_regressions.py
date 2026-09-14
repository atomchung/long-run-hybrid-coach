"""Regression clauses added only after a coaching failure is reproduced.

The training reference is judgment, not a rules engine. These assertions therefore pin
only distinctions the evidence must preserve; they do not pin whether the coach chooses
six repetitions or retries seven in issue #255's scenario.

Each phrase below is the shortest clause that carries one distinction, not the sentence
it currently sits in. Wording around them is free to move -- a rewrite that keeps the
distinctions passes, and only dropping one fails -- because the first review of this file
found the honest fix was itself a rewording, and a test that pins whole sentences bills
every such fix a fight with CI.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop.orchestration import training_judgment


class TrainingJudgmentRegressionTests(unittest.TestCase):
    def test_prescribed_and_execution_supported_doses_stay_separate(self):
        text = " ".join(training_judgment().split())

        for phrase in (
            # an activity having occurred is not completion of the prescribed steps
            "does not by itself show that every prescribed step was completed",
            # whole-activity figures and step-associated evidence are different
            # granularities; an average describes the activity, not one repetition
            "total duration or distance covered the session's overall length",
            "any evidence can be associated with the individual prescribed work steps",
            "A whole-activity average describes the activity as a whole",
            # a provider segment is not a prescribed step until something associates it
            "provider segments are not automatically aligned to the prescription",
            # the anchor is named, and so is the granularity its support actually has
            "what the execution evidence establishes at the granularity it actually has",
            "which of the two doses anchors the next choice and why",
            # neither direction of the asymmetry may be read as settled
            "An unconfirmed prescribed dose is not demonstrated capacity",
            "not by itself proof that the dose was unsustainable",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_execution_trend_and_the_declared_outcome_stay_two_claims(self):
        """Issue #467: one sentence was read as governing both, and it governs one.

        The reproduced failure: three same-goal interval sessions with an unambiguous
        direction -- repetitions up, fastest repetition faster, heart-rate ceiling higher
        -- and the coach answered that progress was simply unanswerable, because the
        cycle's measurement protocol had never been run. Both halves of the repair are
        pinned here: the outcome boundary the answer was right to keep, and the trend
        reading it wrongly suppressed along with it.
        """
        text = " ".join(training_judgment().split())

        for phrase in (
            # the boundary that must survive: completion is not adaptation
            "not proof that the intended adaptation improved",
            # the outcome claim is read from the protocol and from nothing else
            "read from the cycle's own measurement protocol and nothing else",
            "until that has been run the outcome is unproven",
            "no wearable estimate stands in for it",
            # the reading the collapse suppressed, and the thing that makes it a reading
            "several comparable sessions moving one way is a direction",
            # neither claim may answer for the other, in either direction
            "Neither silences the other",
            "leaves the execution reading exactly as readable as it was",
            "does not promote itself into a demonstrated outcome",
            # a disagreement is reported as one rather than resolved by loudness
            "Where a measurement and the trend disagree, give both",
            # the guards against the opposite failure, over-reading
            "Sessions that asked for different things are not a comparison",
            "A single session rarely establishes a direction",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
