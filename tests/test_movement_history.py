"""Per-movement strength history: the same evidence, pivoted to the coaching question.

`strength_execution` answers "what was lifted on this date". Deciding the next
prescription needs "how has this movement been going", and the difference is not
cosmetic: two occurrences read together say something neither says alone. These tests
hold the pivot to being complete, honest about what was never prescribed, and empty of
verdicts.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop.context_core import _build_movement_history


BASELINE = {
    "strength_loads": [
        {
            "exercise": "bench_press",
            "display_name": "臥推",
            "load_kg": 60.0,
            "assist_kg": None,
            "scheme": "5x5",
        },
        {
            "exercise": "pull_up_assisted",
            "display_name": "引體向上",
            "load_kg": None,
            "assist_kg": 24.0,
            "scheme": "5x5",
        },
    ]
}


def _sets(*pairs):
    return [
        {"set": index, "weight_kg": weight, "assist_kg": None, "reps": reps, "rpe": None}
        for index, (weight, reps) in enumerate(pairs, start=1)
    ]


def _execution(*sessions):
    return {
        "source": "personal-os:strength_log",
        "window_start": "2026-07-05",
        "window_end": "2026-08-15",
        "sessions": list(sessions),
    }


def _plan(sessions):
    return {"week": {"start": "2026-08-10", "sessions": sessions}, "athlete_baseline": BASELINE}


def _strength_session(session_id, date, movements):
    return {
        "session_id": session_id,
        "sport": "strength",
        "scheduled_date": date,
        "plan": {"kind": "movement_list", "movements": movements},
    }


def _movement(exercise, sets, reps, load_kg=None, assist_kg=None, basis="measured_baseline"):
    return {
        "exercise": exercise,
        "display_name": "x",
        "sets": sets,
        "reps": reps,
        "load_kg": load_kg,
        "assist_kg": assist_kg,
        "load_basis": basis,
    }


class MovementHistoryTests(unittest.TestCase):
    def test_one_movement_gathers_every_occurrence_in_date_order(self):
        """The rows a coach compares have to arrive as one series, not scattered by day."""
        execution = _execution(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5), (65.0, 5), (65.0, 5), (65.0, 5), (60.0, 5)),
             "notes": ["做不完五組65kg"]},
            {"date": "2026-08-01", "exercise": "bench_press", "category": "chest",
             "sets": _sets((60.0, 5), (60.0, 5)), "notes": []},
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4)),
             "notes": []},
        )
        history = _build_movement_history([], _plan([]), execution, BASELINE)

        self.assertEqual(1, len(history["movements"]))
        movement = history["movements"][0]
        self.assertEqual("臥推", movement["display_name"])
        self.assertEqual(60.0, movement["baseline"]["load_kg"])
        self.assertEqual(
            ["2026-08-01", "2026-08-11", "2026-08-15"],
            [occurrence["date"] for occurrence in movement["occurrences"]],
        )

    def test_the_two_ways_a_load_concedes_stay_distinguishable(self):
        """8/11 dropped the weight on the last set; 8/15 held it and dropped a rep.

        Same load conceding in opposite directions. Either row alone reads like a plain
        pass or fail, which is the whole reason they have to sit together.
        """
        execution = _execution(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5), (65.0, 5), (65.0, 5), (65.0, 5), (60.0, 5)), "notes": []},
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4)), "notes": []},
        )
        occurrences = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0][
            "occurrences"
        ]
        first, second = occurrences[0]["performed_sets"], occurrences[1]["performed_sets"]
        self.assertEqual([65.0, 65.0, 65.0, 65.0, 60.0], [s["weight_kg"] for s in first])
        self.assertEqual([5, 5, 5, 5, 5], [s["reps"] for s in first])
        self.assertEqual([65.0] * 5, [s["weight_kg"] for s in second])
        self.assertEqual([4] * 5, [s["reps"] for s in second])

    def test_a_prescription_in_two_parts_is_not_collapsed_into_one(self):
        """Four sets at one load and a fifth at another is one prescription in two
        parts, and the second part is where the load was expected to give way."""
        plan = _plan([
            _strength_session("strength-sat-01", "2026-08-15", [
                _movement("bench_press", 4, 5, load_kg=65.0),
                _movement("bench_press", 1, 5, load_kg=60.0),
            ])
        ])
        execution = _execution(
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4)), "notes": []}
        )
        prescribed = _build_movement_history([], plan, execution, BASELINE)["movements"][0][
            "occurrences"
        ][0]["prescribed"]
        self.assertEqual(2, len(prescribed))
        self.assertEqual((4, 65.0), (prescribed[0]["sets"], prescribed[0]["load_kg"]))
        self.assertEqual((1, 60.0), (prescribed[1]["sets"], prescribed[1]["load_kg"]))

    def test_a_day_that_prescribed_nothing_says_null_not_an_empty_list(self):
        """Trained off-plan, or older than the plan record. Different from prescribed
        and missed, which shows as a prescription with no performed sets."""
        execution = _execution(
            {"date": "2026-07-19", "exercise": "bench_press", "category": "chest",
             "sets": _sets((55.0, 5)), "notes": []}
        )
        occurrence = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0][
            "occurrences"
        ][0]
        self.assertIsNone(occurrence["prescribed"])

    def test_todays_session_is_matched_from_the_plan_the_cycle_record_does_not_hold_yet(self):
        """Today is in the plan and not yet in the elapsed-cycle record, and today is
        the session most likely to be read against."""
        plan = _plan([
            _strength_session("strength-sat-01", "2026-08-15", [
                _movement("bench_press", 5, 5, load_kg=65.0)
            ])
        ])
        execution = _execution(
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4)), "notes": []}
        )
        occurrence = _build_movement_history([], plan, execution, BASELINE)["movements"][0][
            "occurrences"
        ][0]
        self.assertIsNotNone(occurrence["prescribed"])
        self.assertEqual(65.0, occurrence["prescribed"][0]["load_kg"])

    def test_a_movement_with_no_baseline_still_reports_its_history(self):
        """An unanchored movement is exactly the one whose history a coach needs; the
        anchor is what is missing, not the evidence."""
        execution = _execution(
            {"date": "2026-08-12", "exercise": "romanian_deadlift", "category": "legs",
             "sets": _sets((40.0, 8)), "notes": []}
        )
        movement = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0]
        self.assertIsNone(movement["baseline"])
        self.assertIsNone(movement["display_name"])
        self.assertEqual(1, len(movement["occurrences"]))

    def test_no_strength_evidence_produces_no_group_at_all(self):
        self.assertIsNone(_build_movement_history([], _plan([]), None, BASELINE))
        self.assertIsNone(_build_movement_history([], _plan([]), _execution(), BASELINE))

    def test_nothing_in_the_group_is_a_verdict(self):
        """The group exists so the coach can read a direction. Computing one here would
        replace the judgment with a rule."""
        execution = _execution(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5)), "notes": []},
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4)), "notes": []},
        )
        movement = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0]
        banned = {
            "trend", "direction", "progressing", "completion_rate", "adherence",
            "score", "percent_complete", "plateau", "regression",
        }
        self.assertEqual(set(), banned & set(movement))
        for occurrence in movement["occurrences"]:
            self.assertEqual(set(), banned & set(occurrence))


if __name__ == "__main__":
    unittest.main()
