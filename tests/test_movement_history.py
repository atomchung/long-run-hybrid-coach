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


MEASURED = "personal-os:strength_log"
REPORTED = "athlete_reported"


def _execution(*sessions, source=MEASURED):
    # Each session carries its own source, because a real group can hold both: the local
    # strength log writes what was measured, and the athlete reports movements it never
    # saw. A session that names none inherits the group's, which is the ordinary case.
    return {
        "source": source,
        "window_start": "2026-07-05",
        "window_end": "2026-08-15",
        "sessions": [{"source": source, **session} for session in sessions],
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

    def test_a_recalled_occurrence_is_not_read_as_a_measured_one(self):
        """The series can mix measured rows with the athlete's own account of a session.

        A local strength log holds what was measured; a movement it never saw arrives
        only because the athlete said so. 65 kg measured followed by 70 kg recalled is
        not the same evidence as two measured figures, and this group exists to be read
        row against row -- so without provenance on the row itself, a change of source
        would read as a change of load.
        """
        execution = _execution(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5)), "notes": []},
            {"date": "2026-08-15", "exercise": "bench press", "category": None,
             "sets": _sets((70.0, 4)), "notes": [], "source": REPORTED},
        )

        movements = _build_movement_history([], _plan([]), execution, BASELINE)["movements"]

        # One movement, however the two sides spelled it -- the log writes bench_press,
        # the athlete says bench press, and both resolve through the same normalizer.
        self.assertEqual(1, len(movements))
        movement = movements[0]
        self.assertEqual(
            [("2026-08-11", MEASURED), ("2026-08-15", REPORTED)],
            [(item["date"], item["source"]) for item in movement["occurrences"]],
        )

    def test_the_athletes_word_and_the_plans_key_are_one_movement(self):
        """Issue #238's own case: one lift confirmed under the plan's key and reported
        in the athlete's word must read back as one series, not two strangers.

        The baseline carries both names -- ``bench_press`` as the key, 臥推 as
        ``display_name`` -- and ``anchoring_baseline`` already resolves either to it.
        Grouping is what failed to use that answer.
        """
        execution = _execution(
            {"date": "2026-08-18", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5)), "notes": []},
            {"date": "2026-08-22", "exercise": "臥推", "category": None,
             "sets": _sets((65.0, 5)), "notes": [], "source": REPORTED},
        )

        movements = _build_movement_history([], _plan([]), execution, BASELINE)["movements"]

        self.assertEqual(1, len(movements))
        movement = movements[0]
        # The merged group is named by the baseline's own key -- the one stable name
        # both spellings resolve to -- with the athlete's word beside it.
        self.assertEqual("bench_press", movement["exercise"])
        self.assertEqual("臥推", movement["display_name"])
        self.assertEqual(60.0, movement["baseline"]["load_kg"])
        self.assertEqual(
            ["2026-08-18", "2026-08-22"],
            [occurrence["date"] for occurrence in movement["occurrences"]],
        )
        # A merged row still says which name it was stored under, because a correction
        # or retraction keyed on the group's name would miss it. The row whose stored
        # spelling IS the group's name carries nothing extra.
        self.assertNotIn("reported_as", movement["occurrences"][0])
        self.assertEqual("臥推", movement["occurrences"][1]["reported_as"])

    def test_a_prescription_under_the_canonical_key_matches_a_report_in_the_athletes_word(self):
        """The plan prescribes under its key; the athlete answers in their own word.
        Both resolve through the same baseline, so the occurrence must carry the
        prescription instead of reading as trained off-plan."""
        plan = _plan([
            _strength_session("strength-sat-01", "2026-08-15", [
                _movement("bench_press", 5, 5, load_kg=65.0)
            ])
        ])
        execution = _execution(
            {"date": "2026-08-15", "exercise": "臥推", "category": None,
             "sets": _sets((65.0, 4)), "notes": [], "source": REPORTED}
        )

        occurrence = _build_movement_history([], plan, execution, BASELINE)["movements"][0][
            "occurrences"
        ][0]

        self.assertIsNotNone(occurrence["prescribed"])
        self.assertEqual(65.0, occurrence["prescribed"][0]["load_kg"])

    def test_a_name_that_is_an_entrys_own_key_beats_another_entrys_display_name(self):
        """Nothing forbids one baseline entry's display_name equalling another entry's
        key. A report that names an entry verbatim must anchor there -- list order
        deciding instead would credit the athlete's loads against the wrong baseline."""
        baseline = {
            "strength_loads": [
                {"exercise": "pull_up_assisted", "display_name": "輔助引體",
                 "load_kg": None, "assist_kg": 24.0, "scheme": "5x5"},
                {"exercise": "輔助引體", "display_name": None,
                 "load_kg": None, "assist_kg": 30.0, "scheme": "5x5"},
            ]
        }
        execution = _execution(
            {"date": "2026-08-16", "exercise": "輔助引體", "category": None,
             "sets": [{"set": 1, "weight_kg": None, "assist_kg": 30.0, "reps": 8,
                       "rpe": None}],
             "notes": [], "source": REPORTED},
        )

        movements = _build_movement_history([], _plan([]), execution, baseline)["movements"]

        self.assertEqual(1, len(movements))
        movement = movements[0]
        self.assertEqual("輔助引體", movement["exercise"])
        self.assertEqual(30.0, movement["baseline"]["assist_kg"])

    def test_a_prescription_in_the_athletes_word_matches_a_report_under_the_canonical_key(self):
        """The reverse direction of the alias join: the plan written in the athlete's
        word, the report arriving under the canonical key. Both resolve through the
        same baseline, so neither direction may read as trained off-plan."""
        plan = _plan([
            _strength_session("strength-sat-01", "2026-08-15", [
                _movement("臥推", 5, 5, load_kg=65.0)
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

    def test_a_word_no_baseline_carries_stays_its_own_movement(self):
        """輔助引體 names the assisted pull-up's baseline in a word neither its key nor
        its display_name carries. Merging them would be guessing the athlete's meaning
        (AGENTS.md 5); the row stands alone under the reported spelling."""
        execution = _execution(
            {"date": "2026-08-16", "exercise": "輔助引體", "category": None,
             "sets": [{"set": 1, "weight_kg": None, "assist_kg": 20.0, "reps": 8,
                       "rpe": None}],
             "notes": [], "source": REPORTED},
        )

        movements = _build_movement_history([], _plan([]), execution, BASELINE)["movements"]

        self.assertEqual(1, len(movements))
        self.assertEqual("輔助引體", movements[0]["exercise"])
        self.assertIsNone(movements[0]["baseline"])
        self.assertIsNone(movements[0]["display_name"])

    def test_the_two_ways_a_load_concedes_stay_distinguishable(self):
        """8/11 dropped the weight on the last set; 8/15 held it and dropped a rep.

        Same load conceding in opposite directions. Either row alone reads like a plain
        pass or fail, which is the whole reason they have to sit together. The raw sets
        live in strength_execution now, so what has to keep the two apart here is the
        rollup: a second by_load row and a top load that was not held every set on one
        side, one row held every set on the other.
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
        self.assertNotIn("performed_sets", occurrences[0])
        first, second = occurrences[0]["load_rollup"], occurrences[1]["load_rollup"]
        self.assertEqual(
            [{"weight_kg": 65.0, "assist_kg": None, "reps": 20},
             {"weight_kg": 60.0, "assist_kg": None, "reps": 5}],
            first["by_load"],
        )
        self.assertFalse(first["top_load"]["held_every_set"])
        self.assertEqual(
            [{"weight_kg": 65.0, "assist_kg": None, "reps": 20}], second["by_load"]
        )
        self.assertTrue(second["top_load"]["held_every_set"])

    def test_the_65kg_pair_from_the_bug_report_rolls_up_to_equal_reps(self):
        """The exact case a coach got wrong by hand: reps at 65 kg had not moved.

        8/11 -> [(65,5),(65,5),(65,5),(65,5),(60,5)]: 20 reps at 65 kg, 25 total.
        8/15 -> [(65,4),(65,4),(65,4),(65,4),(65,4)]: 20 reps at 65 kg, 20 total.
        The 25 belongs to 8/11's total; reading it as 8/15's load at 65 kg is the
        addition error this rollup exists so nobody has to do by hand again.
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
        first, second = occurrences[0]["load_rollup"], occurrences[1]["load_rollup"]

        def reps_at(rollup, weight):
            return next(row["reps"] for row in rollup["by_load"] if row["weight_kg"] == weight)

        self.assertEqual(20, reps_at(first, 65.0))
        self.assertEqual(20, reps_at(second, 65.0))
        self.assertEqual(25, first["total_reps"])
        self.assertEqual(20, second["total_reps"])

    def test_top_load_and_whether_every_set_held_it(self):
        """8/11 dropped the weight on the last set; 8/15 held the weight and dropped a
        rep instead. Both are "the load conceded", but only one held the top load."""
        execution = _execution(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5), (65.0, 5), (65.0, 5), (65.0, 5), (60.0, 5)), "notes": []},
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4)), "notes": []},
        )
        occurrences = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0][
            "occurrences"
        ]
        first, second = occurrences[0]["load_rollup"], occurrences[1]["load_rollup"]

        self.assertEqual(
            {"weight_kg": 65.0, "assist_kg": None, "held_every_set": False}, first["top_load"]
        )
        self.assertEqual(
            {"weight_kg": 65.0, "assist_kg": None, "held_every_set": True}, second["top_load"]
        )

    def test_assist_kg_joins_weight_kg_in_the_grouping_key(self):
        """An assisted movement's load lives in assist_kg; less assistance is the
        heavier direction, so the top load is the set with the least help."""
        execution = _execution(
            {"date": "2026-08-11", "exercise": "pull_up_assisted", "category": "back",
             "sets": [
                 {"set": 1, "weight_kg": None, "assist_kg": 24.0, "reps": 5, "rpe": None},
                 {"set": 2, "weight_kg": None, "assist_kg": 24.0, "reps": 5, "rpe": None},
                 {"set": 3, "weight_kg": None, "assist_kg": 20.0, "reps": 4, "rpe": None},
             ], "notes": []},
        )
        rollup = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0][
            "occurrences"
        ][0]["load_rollup"]

        self.assertEqual(
            [
                {"weight_kg": None, "assist_kg": 24.0, "reps": 10},
                {"weight_kg": None, "assist_kg": 20.0, "reps": 4},
            ],
            rollup["by_load"],
        )
        self.assertEqual(14, rollup["total_reps"])
        # 20 kg of assistance is less help than 24 kg, so it is the heavier set --
        # the top load -- even though it is numerically the smaller figure.
        self.assertEqual(
            {"weight_kg": None, "assist_kg": 20.0, "held_every_set": False}, rollup["top_load"]
        )

    def test_a_missing_rep_count_makes_the_total_unknown_not_zero(self):
        """A set with no recorded reps must not silently count as zero reps (AGENTS.md
        3) -- the honest total is that it is not fully known."""
        execution = _execution(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": [
                 {"set": 1, "weight_kg": 65.0, "assist_kg": None, "reps": 5, "rpe": None},
                 {"set": 2, "weight_kg": 65.0, "assist_kg": None, "reps": None, "rpe": None},
             ], "notes": []},
        )
        rollup = _build_movement_history([], _plan([]), execution, BASELINE)["movements"][0][
            "occurrences"
        ][0]["load_rollup"]

        self.assertIsNone(rollup["total_reps"])
        self.assertEqual(1, len(rollup["by_load"]))
        self.assertIsNone(rollup["by_load"][0]["reps"])
        # The load itself is still known even though the rep count is not -- only the
        # count degrades, not the whole row.
        self.assertEqual(65.0, rollup["by_load"][0]["weight_kg"])

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
            # The rollup is arithmetic, not a reading of it -- only the counting moves,
            # and the judgment stays with the coach.
            self.assertEqual(set(), banned & set(occurrence["load_rollup"]))
            for row in occurrence["load_rollup"]["by_load"]:
                self.assertEqual(set(), banned & set(row))


if __name__ == "__main__":
    unittest.main()
