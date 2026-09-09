"""The journey ceilings, so a regression fails here rather than in a release note.

`journey_cost.py` says what this measures and why one call's ceiling is not enough. This
file is the gate: four common journeys, two arms each, with a ceiling on the candidate
arm and one property that matters more than any number -- the candidate arm must not
cost *more* than reading everything, on the journeys it was built for.
"""

from __future__ import annotations

import datetime as dt
import shutil
import unittest

from tests.journey_cost import JOURNEY_NAMES, JourneyDriver, report, run_all
from tests.test_gateway import (
    GatewayTestCase,
    TOKEN_A,
    WEEKLY_CHANGE,
    load,
)


# Characters of compact JSON for the whole journey, sent plus received, on the fixture
# account below. Ceilings rather than measurements, set above what this checkout
# produces, for the reason every other budget in this repository is: a journey that grows
# has to say what it bought.
CANDIDATE_CEILINGS: dict[str, int] = {
    "what_should_i_do_today": 26_500,
    "correct_a_goal_then_move_thursday": 31_000,
    "how_did_thursdays_session_go": 30_500,
    "review_the_week_and_roll_it": 52_000,
}

# The one journey the candidate arm does not win, and by how much. A day question
# answered narrowly and then expanded to one session makes two calls where reading
# everything makes one, and this fixture's whole account is small enough that the second
# call's fixed cost -- envelope, index, group map -- is not repaid. Measured at +1,640
# characters, +6%.
#
# It is bounded rather than hidden. The mechanism's claim is a ceiling on any single
# result and a cheap narrow turn on a real account, not that every journey is smaller;
# a heavy account reads 59,744 characters for every group and 4,399-9,748 for one
# session, median 5,765 (`test_context_view.py`). What this pin catches is that ratio getting worse.
KNOWN_LOSS = {"how_did_thursdays_session_go": 1.10}

class JourneyCostTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.before = load("plan-state-v1.json")
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.before)
        self.driver = JourneyDriver(
            self._call,
            plan_id=self.before["plan_id"],
            plan_version=self.before["version"],
            weekly_change=WEEKLY_CHANGE,
            reset=self._reset,
        )

    def _reset(self):
        """The same account, from the same commit, for every arm of every journey.

        The clock moves too. A `context_id` is derived from the instant it was built at,
        so two arms measured at one frozen instant would produce the same id -- and the
        second arm would resolve the first arm's held context, which is a comparison of
        nothing.
        """
        shutil.rmtree(self.owner_dir(self.owner_id), ignore_errors=True)
        self.gateway._forget_retained_contexts(self.owner_id)
        self.now = self.now + dt.timedelta(minutes=1)
        self.seed_owner(TOKEN_A, plan=load("plan-state-v1.json"))

    def _call(self, kind, body):
        status, payload = self.route(kind, body=body, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        return payload

    def _journeys(self):
        return {
            (name, arm): self.driver.run(name, arm)
            for name in JOURNEY_NAMES
            for arm in ("reference", "candidate")
        }

    def test_every_journey_fits_its_ceiling(self):
        for name in JOURNEY_NAMES:
            with self.subTest(journey=name):
                journey = self.driver.run(name, "candidate")
                self.assertLessEqual(
                    journey.total,
                    CANDIDATE_CEILINGS[name],
                    f"{name} is over budget:\n{report([journey])}",
                )

    def test_no_journey_costs_materially_more_than_reading_everything(self):
        """The failure this whole lane could most easily become.

        A mechanism that halves one call and adds two is not a saving, and the way that
        happens is a compact read followed by expanding everything -- which pays the
        compact read *plus* the whole payload. Every journey here is one a coach really
        has, and the only one allowed to come out worse is the one `KNOWN_LOSS` names,
        by the margin it names.
        """
        for name in JOURNEY_NAMES:
            with self.subTest(journey=name):
                reference = self.driver.run(name, "reference")
                candidate = self.driver.run(name, "candidate")
                self.assertLessEqual(
                    candidate.total,
                    reference.total * KNOWN_LOSS.get(name, 1.0),
                    f"{name} costs more than reading everything:\n"
                    f"{report([reference, candidate])}",
                )

    def test_the_whole_set_of_journeys_is_materially_cheaper(self):
        """Four journeys, both arms, one number.

        Per-journey results can be traded against each other -- that is what an average
        hides. This is the total, and it is the claim the release makes: measured at
        184,381 characters against 133,437, a 28% saving, with one journey losing.
        """
        journeys = run_all(self.driver)
        reference = sum(item.total for item in journeys if item.arm == "reference")
        candidate = sum(item.total for item in journeys if item.arm == "candidate")

        self.assertLess(candidate * 5, reference * 4, report(journeys))

    def test_the_journeys_the_mechanism_was_built_for_are_materially_cheaper(self):
        """A stored-record turn and a weekly change are the shapes it was built for.

        The first stops carrying six weeks of provider actuals to correct a goal; the
        second stops authoring the context and the change request a second time to
        confirm the preview it was just shown.
        """
        for name in ("correct_a_goal_then_move_thursday", "review_the_week_and_roll_it"):
            with self.subTest(journey=name):
                reference = self.driver.run(name, "reference")
                candidate = self.driver.run(name, "candidate")
                self.assertLess(candidate.total * 3, reference.total * 2)


if __name__ == "__main__":  # pragma: no cover - a report, not a test run
    unittest.main()
