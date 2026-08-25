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
            # an activity record is not step completion
            "does not by itself show that every prescribed step was completed",
            # execution support is a level, and the levels reach different things
            "segment records can speak to individual repetitions",
            "the level of support it rests on",
            "say why that anchor is preferred here",
            # neither direction of the asymmetry may be read as settled
            "An unconfirmed prescribed dose is not demonstrated capacity",
            "not by itself proof that the dose was unsustainable",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
