"""Regression clauses added only after a coaching failure is reproduced.

The training reference is judgment, not a rules engine. These assertions therefore pin
only distinctions the evidence must preserve; they do not pin whether the coach chooses
six repetitions or retries seven in issue #255's scenario.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop.orchestration import training_judgment


class TrainingJudgmentRegressionTests(unittest.TestCase):
    def test_prescribed_and_execution_supported_doses_stay_separate(self):
        text = " ".join(training_judgment().split())

        for phrase in (
            "A completed activity record says an activity occurred",
            "it does not by itself show that every prescribed step was completed",
            "The last dose prescribed and the last dose supported by execution evidence are separate observations",
            "state which one anchors the choice and why",
            "An unconfirmed prescribed dose is not demonstrated capacity",
            "one short activity is not by itself proof that the dose was unsustainable",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
