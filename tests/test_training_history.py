"""The athlete's complete evidence history, rolled up past the 42-day cycle window.

`recent_actuals`, `reported_activities` and `strength_execution` all read a
six-week-or-shorter span. Nothing in this product could answer "how has my running
volume changed this year" until this group -- and the failure mode that made it
necessary is issue #101's central finding: a six-week evidence gap read as a training
restart, because the account's oldest reachable evidence was mistaken for the athlete's
oldest training. These tests hold the rollup to being accurate, honestly bounded, and
still no more a verdict than `movement_history` -- or any other evidence group in this
module -- is allowed to be.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop.context_core import (
    TRAINING_HISTORY_MAX_MONTHS,
    TRAINING_HISTORY_MAX_MOVEMENTS,
    _build_training_history,
)


BASELINE = {
    "strength_loads": [
        {
            "exercise": "bench_press",
            "display_name": "臥推",
            "load_kg": 60.0,
            "assist_kg": None,
            "scheme": "5x5",
        },
    ]
}

REPORTED = "athlete_reported"
IMPORTED = "athlete_imported"
CONFIRMED = "prescribed_confirmed"


def _activity(date, sport="running", *, minutes=40, km=8.0, source=REPORTED, imported_from=None):
    return {
        "date": date,
        "sport": sport,
        "duration_minutes": minutes,
        "distance_km": km,
        "subjective_feel": None,
        "note": None,
        "source": source,
        "imported_from": imported_from,
    }


def _strength(date, exercise="bench_press", *, sets=None, source=REPORTED):
    return {
        "date": date,
        "exercise": exercise,
        "category": None,
        "sets": sets if sets is not None else [
            {"set": 1, "weight_kg": 65.0, "assist_kg": None, "reps": 5, "rpe": None}
        ],
        "notes": [],
        "source": source,
    }


class MonthBucketMathTests(unittest.TestCase):
    def test_two_sessions_in_one_month_sum_correctly(self):
        history = _build_training_history(
            [
                _activity("2026-06-03", minutes=30, km=5.0),
                _activity("2026-06-20", minutes=40, km=8.0),
            ],
            [],
            BASELINE,
        )
        self.assertEqual(1, len(history["months"]))
        bucket = history["months"][0]
        self.assertEqual("2026-06", bucket["month"])
        self.assertEqual("running", bucket["sport"])
        self.assertEqual(2, bucket["session_count"])
        self.assertEqual(70, bucket["total_minutes"])
        self.assertEqual(13.0, bucket["total_km"])

    def test_different_months_produce_different_buckets_in_chronological_order(self):
        history = _build_training_history(
            [_activity("2026-07-01"), _activity("2026-05-15"), _activity("2026-06-10")],
            [],
            BASELINE,
        )
        self.assertEqual(
            ["2026-05", "2026-06", "2026-07"],
            [bucket["month"] for bucket in history["months"]],
        )

    def test_different_sports_in_one_month_are_different_buckets(self):
        history = _build_training_history(
            [_activity("2026-06-03", sport="running"), _activity("2026-06-05", sport="cycling")],
            [],
            BASELINE,
        )
        self.assertEqual(
            {("2026-06", "running"), ("2026-06", "cycling")},
            {(bucket["month"], bucket["sport"]) for bucket in history["months"]},
        )
        for bucket in history["months"]:
            self.assertEqual(1, bucket["session_count"])

    def test_a_row_with_no_stated_distance_leaves_the_others_km_intact(self):
        """"total_km among those with a stated distance" -- a missing distance is
        dropped from the sum, never read as zero km (AGENTS.md 3)."""
        history = _build_training_history(
            [_activity("2026-06-03", km=5.0), _activity("2026-06-05", km=None)],
            [],
            BASELINE,
        )
        bucket = history["months"][0]
        self.assertEqual(2, bucket["session_count"])
        self.assertEqual(5.0, bucket["total_km"])

    def test_no_row_stating_a_distance_leaves_total_km_null_not_zero(self):
        history = _build_training_history([_activity("2026-06-03", km=None)], [], BASELINE)
        self.assertIsNone(history["months"][0]["total_km"])

    def test_provenance_counts_are_tallied_and_zero_padded(self):
        history = _build_training_history(
            [
                _activity("2026-06-03", source=REPORTED),
                _activity("2026-06-05", source=IMPORTED, imported_from="Garmin Connect"),
                _activity("2026-06-07", source=REPORTED),
            ],
            [],
            BASELINE,
        )
        self.assertEqual(
            {"athlete_reported": 2, "athlete_imported": 1, "prescribed_confirmed": 0},
            history["months"][0]["provenance_counts"],
        )

    def test_source_names_only_the_provenances_actually_kept(self):
        history = _build_training_history(
            [_activity("2026-06-03", source=REPORTED)], [], BASELINE
        )
        self.assertEqual("athlete_reported", history["source"])

        history = _build_training_history(
            [
                _activity("2026-06-03", source=REPORTED),
                _activity("2026-06-05", source=IMPORTED, imported_from="Strava"),
            ],
            [],
            BASELINE,
        )
        self.assertEqual("athlete_reported+athlete_imported", history["source"])


class StrengthSessionCountTests(unittest.TestCase):
    """The one sport whose evidence can arrive through two containers describing the
    same calendar day: a coarse `reported_activities` summary and one-or-more
    per-exercise `strength_reports` entries."""

    def test_a_coarse_summary_and_a_per_exercise_report_on_one_day_are_one_session(self):
        history = _build_training_history(
            [_activity("2026-06-10", sport="strength", minutes=45, km=None)],
            [_strength("2026-06-10", exercise="bench_press")],
            BASELINE,
        )
        self.assertEqual(1, len(history["months"]))
        bucket = history["months"][0]
        self.assertEqual("strength", bucket["sport"])
        self.assertEqual(1, bucket["session_count"])
        # Minutes come only from the coarse summary -- the per-exercise report carries
        # no duration field at all.
        self.assertEqual(45, bucket["total_minutes"])

    def test_two_exercises_the_same_day_are_still_one_session(self):
        """Bench and squat logged separately on one day is one gym visit, not two."""
        history = _build_training_history(
            [],
            [
                _strength("2026-06-10", exercise="bench_press"),
                _strength("2026-06-10", exercise="squat"),
            ],
            BASELINE,
        )
        bucket = history["months"][0]
        self.assertEqual(1, bucket["session_count"])
        # No reported_activities row landed in this bucket, so there is nothing to sum
        # minutes from -- unknown, never zero.
        self.assertIsNone(bucket["total_minutes"])

    def test_a_second_day_with_only_a_per_exercise_report_is_a_second_session(self):
        history = _build_training_history(
            [_activity("2026-06-03", sport="strength", minutes=40, km=None)],
            [_strength("2026-06-10", exercise="squat")],
            BASELINE,
        )
        bucket = history["months"][0]
        self.assertEqual(2, bucket["session_count"])

    def test_strength_provenance_counts_include_both_containers(self):
        history = _build_training_history(
            [_activity("2026-06-10", sport="strength", minutes=45, km=None, source=REPORTED)],
            [_strength("2026-06-10", exercise="bench_press", source=CONFIRMED)],
            BASELINE,
        )
        self.assertEqual(
            {"athlete_reported": 1, "athlete_imported": 0, "prescribed_confirmed": 1},
            history["months"][0]["provenance_counts"],
        )


def _sequential_months(start_year: int, start_month: int, count: int) -> list[str]:
    """``count`` consecutive ``YYYY-MM`` months starting there, oldest first."""
    months = []
    year, month = start_year, start_month
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


class TruncationTests(unittest.TestCase):
    def test_at_most_the_most_recent_24_populated_months_survive(self):
        # One running session per month, January 2024 through August 2026 -- 32 months.
        months = _sequential_months(2024, 1, 32)
        self.assertEqual("2026-08", months[-1])
        rows = [_activity(f"{month}-10") for month in months]

        history = _build_training_history(rows, [], BASELINE)

        self.assertEqual(TRAINING_HISTORY_MAX_MONTHS, len(history["months"]))
        self.assertTrue(history["truncated"])
        self.assertEqual("2024-01", history["earliest_observed_month"])
        # The kept window is the most recent 24 -- 2024-09 through 2026-08.
        self.assertEqual("2024-09", history["months"][0]["month"])
        self.assertEqual("2026-08", history["months"][-1]["month"])

    def test_24_or_fewer_populated_months_are_not_truncated(self):
        rows = [_activity(f"{month}-10") for month in _sequential_months(2025, 1, 12)]
        history = _build_training_history(rows, [], BASELINE)
        self.assertEqual(12, len(history["months"]))
        self.assertFalse(history["truncated"])
        self.assertEqual("2025-01", history["earliest_observed_month"])

    def test_earliest_observed_month_reads_true_even_when_it_was_dropped(self):
        """The fact field names the true earliest month, not the earliest one shown --
        a dropped month must not read as a month that never happened."""
        # 25 consecutive months: one more than the cap, so exactly the oldest is dropped.
        rows = [_activity(f"{month}-10") for month in _sequential_months(2024, 1, 25)]
        history = _build_training_history(rows, [], BASELINE)
        self.assertEqual(TRAINING_HISTORY_MAX_MONTHS, len(history["months"]))
        self.assertTrue(history["truncated"])
        self.assertEqual("2024-01", history["earliest_observed_month"])
        self.assertNotIn(
            "2024-01", [bucket["month"] for bucket in history["months"]]
        )
        self.assertEqual("2024-02", history["months"][0]["month"])


class EmptyStateTests(unittest.TestCase):
    def test_nothing_reported_produces_no_group_at_all(self):
        self.assertIsNone(_build_training_history([], [], BASELINE))
        self.assertIsNone(_build_training_history(None, None, BASELINE))

    def test_malformed_rows_alone_produce_no_group(self):
        self.assertIsNone(_build_training_history(["not a dict"], [None], BASELINE))


class MovementLongevityTests(unittest.TestCase):
    def test_a_single_report_is_both_its_own_earliest_and_heaviest(self):
        history = _build_training_history(
            [], [_strength("2026-06-10", sets=[
                {"set": 1, "weight_kg": 65.0, "assist_kg": None, "reps": 5, "rpe": None}
            ])],
            BASELINE,
        )
        movement = history["movement_longevity"][0]
        self.assertEqual(movement["earliest"], movement["heaviest"])
        self.assertEqual("2026-06-10", movement["earliest"]["date"])
        self.assertEqual(65.0, movement["earliest"]["weight_kg"])

    def test_the_heaviest_ever_set_wins_regardless_of_date_order(self):
        history = _build_training_history(
            [],
            [
                _strength("2026-01-05", sets=[
                    {"set": 1, "weight_kg": 60.0, "assist_kg": None, "reps": 5, "rpe": None}
                ]),
                _strength("2026-06-10", sets=[
                    {"set": 1, "weight_kg": 80.0, "assist_kg": None, "reps": 3, "rpe": None}
                ]),
                _strength("2026-03-01", sets=[
                    {"set": 1, "weight_kg": 70.0, "assist_kg": None, "reps": 4, "rpe": None}
                ]),
            ],
            BASELINE,
        )
        movement = history["movement_longevity"][0]
        self.assertEqual("2026-01-05", movement["earliest"]["date"])
        self.assertEqual(60.0, movement["earliest"]["weight_kg"])
        self.assertEqual("2026-06-10", movement["heaviest"]["date"])
        self.assertEqual(80.0, movement["heaviest"]["weight_kg"])

    def test_a_tie_on_the_heaviest_weight_is_broken_by_the_newest_date(self):
        history = _build_training_history(
            [],
            [
                _strength("2026-01-05", sets=[
                    {"set": 1, "weight_kg": 80.0, "assist_kg": None, "reps": 5, "rpe": None}
                ]),
                _strength("2026-06-10", sets=[
                    {"set": 1, "weight_kg": 80.0, "assist_kg": None, "reps": 3, "rpe": None}
                ]),
            ],
            BASELINE,
        )
        self.assertEqual("2026-06-10", history["movement_longevity"][0]["heaviest"]["date"])

    def test_less_assistance_is_the_heavier_direction(self):
        """Same rule `_load_rollup` uses within one session, reused across the whole
        history: 20 kg of assistance is less help than 24 kg, so it is the heavier
        occurrence even though it is numerically the smaller figure."""
        history = _build_training_history(
            [],
            [
                _strength(
                    "2026-01-05", exercise="pull_up_assisted",
                    sets=[{"set": 1, "weight_kg": None, "assist_kg": 24.0, "reps": 5, "rpe": None}],
                ),
                _strength(
                    "2026-06-10", exercise="pull_up_assisted",
                    sets=[{"set": 1, "weight_kg": None, "assist_kg": 20.0, "reps": 4, "rpe": None}],
                ),
            ],
            BASELINE,
        )
        movement = history["movement_longevity"][0]
        self.assertEqual(20.0, movement["heaviest"]["assist_kg"])

    def test_weighted_beats_assisted_outright(self):
        history = _build_training_history(
            [],
            [
                _strength(
                    "2026-01-05", exercise="pull_up_assisted",
                    sets=[{"set": 1, "weight_kg": None, "assist_kg": 5.0, "reps": 5, "rpe": None}],
                ),
                _strength(
                    "2026-06-10", exercise="pull_up_assisted",
                    sets=[{"set": 1, "weight_kg": 10.0, "assist_kg": None, "reps": 4, "rpe": None}],
                ),
            ],
            BASELINE,
        )
        movement = history["movement_longevity"][0]
        # A 10 kg weighted set beats any assisted one, however little help it used.
        self.assertEqual(10.0, movement["heaviest"]["weight_kg"])

    def test_a_bodyweight_only_movement_has_no_heaviest_but_still_has_an_earliest(self):
        history = _build_training_history(
            [],
            [
                _strength(
                    "2026-06-10", exercise="push_up",
                    sets=[{"set": 1, "weight_kg": None, "assist_kg": None, "reps": 20, "rpe": None}],
                ),
            ],
            BASELINE,
        )
        movement = history["movement_longevity"][0]
        self.assertIsNone(movement["heaviest"])
        self.assertIsNotNone(movement["earliest"])
        self.assertEqual("2026-06-10", movement["earliest"]["date"])

    def test_two_spellings_of_one_movement_are_grouped_together(self):
        history = _build_training_history(
            [],
            [
                _strength("2026-01-05", exercise="bench_press"),
                _strength("2026-06-10", exercise="bench press"),
            ],
            BASELINE,
        )
        self.assertEqual(1, len(history["movement_longevity"]))

    def test_display_name_is_read_from_the_baseline_anchor(self):
        history = _build_training_history([], [_strength("2026-06-10")], BASELINE)
        self.assertEqual("臥推", history["movement_longevity"][0]["display_name"])

    def test_an_unanchored_movement_still_reports_its_history(self):
        history = _build_training_history(
            [], [_strength("2026-06-10", exercise="romanian_deadlift")], BASELINE
        )
        movement = history["movement_longevity"][0]
        self.assertIsNone(movement["display_name"])
        self.assertIsNotNone(movement["earliest"])

    def test_strength_only_evidence_still_produces_an_empty_month_list_placeholder_is_never_used(
        self,
    ):
        """A group present for movement_longevity alone still carries real month buckets
        -- strength_reports rows always land in the strength bucket too."""
        history = _build_training_history([], [_strength("2026-06-10")], BASELINE)
        self.assertEqual(1, len(history["months"]))
        self.assertEqual("strength", history["months"][0]["sport"])

    def test_no_strength_evidence_leaves_movement_longevity_an_empty_list(self):
        history = _build_training_history([_activity("2026-06-10")], [], BASELINE)
        self.assertEqual([], history["movement_longevity"])
        self.assertFalse(history["movement_longevity_truncated"])


def _movement_observed_on(date, exercise):
    """One movement, one observation, at a distinct weight so a heaviness comparison
    between two different movements is never an accidental tie."""
    return _strength(
        date, exercise=exercise,
        sets=[{"set": 1, "weight_kg": 40.0, "assist_kg": None, "reps": 5, "rpe": None}],
    )


class MovementLongevityCapTests(unittest.TestCase):
    """issue #101 follow-up: an exercise vocabulary has no calendar to bound it, so it
    needs its own cap, its own priority rule, and its own honest truncation flag --
    same semantics as the month cap, scoped to movements."""

    def test_more_than_15_movements_keeps_the_15_most_recently_observed(self):
        # 20 movements, one observation each, on 20 distinct and increasing dates --
        # movement_00 is the oldest observation, movement_19 the newest.
        reports = [
            _movement_observed_on(f"2026-01-{index + 1:02d}", f"movement_{index:02d}")
            for index in range(20)
        ]
        history = _build_training_history([], reports, BASELINE)

        self.assertTrue(history["movement_longevity_truncated"])
        self.assertEqual(
            TRAINING_HISTORY_MAX_MOVEMENTS, len(history["movement_longevity"])
        )
        kept = {movement["exercise"] for movement in history["movement_longevity"]}
        self.assertEqual(
            {f"movement_{index:02d}" for index in range(5, 20)}, kept
        )
        self.assertNotIn("movement_00", kept)
        self.assertNotIn("movement_04", kept)

    def test_exactly_15_movements_is_not_truncated(self):
        reports = [
            _movement_observed_on(f"2026-01-{index + 1:02d}", f"movement_{index:02d}")
            for index in range(TRAINING_HISTORY_MAX_MOVEMENTS)
        ]
        history = _build_training_history([], reports, BASELINE)

        self.assertFalse(history["movement_longevity_truncated"])
        self.assertEqual(
            TRAINING_HISTORY_MAX_MOVEMENTS, len(history["movement_longevity"])
        )
        kept = {movement["exercise"] for movement in history["movement_longevity"]}
        self.assertEqual(
            {f"movement_{index:02d}" for index in range(TRAINING_HISTORY_MAX_MOVEMENTS)},
            kept,
        )

    def test_movements_are_ordered_by_most_recent_observation_first(self):
        history = _build_training_history(
            [],
            [
                _movement_observed_on("2026-01-05", "squat"),
                _movement_observed_on("2026-06-10", "bench_press"),
                _movement_observed_on("2026-03-01", "deadlift"),
            ],
            BASELINE,
        )
        self.assertEqual(
            ["bench_press", "deadlift", "squat"],
            [movement["exercise"] for movement in history["movement_longevity"]],
        )

    def test_a_movement_observed_again_later_moves_up_by_its_newest_occurrence(self):
        """The priority is the movement's own latest occurrence, not its first one --
        an old movement lifted again yesterday outranks one only ever seen last month."""
        history = _build_training_history(
            [],
            [
                _strength("2026-01-01", exercise="bench_press"),
                _strength("2026-08-01", exercise="bench_press"),
                _movement_observed_on("2026-06-01", "squat"),
            ],
            BASELINE,
        )
        self.assertEqual(
            ["bench_press", "squat"],
            [movement["exercise"] for movement in history["movement_longevity"]],
        )

    def test_a_tie_on_latest_observation_date_is_broken_by_the_heavier_historical_best(self):
        history = _build_training_history(
            [],
            [
                _strength(
                    "2026-06-10", exercise="bench_press",
                    sets=[{"set": 1, "weight_kg": 60.0, "assist_kg": None, "reps": 5, "rpe": None}],
                ),
                _strength(
                    "2026-06-10", exercise="squat",
                    sets=[{"set": 1, "weight_kg": 100.0, "assist_kg": None, "reps": 5, "rpe": None}],
                ),
            ],
            BASELINE,
        )
        self.assertEqual(
            ["squat", "bench_press"],
            [movement["exercise"] for movement in history["movement_longevity"]],
        )

    def test_weighted_beats_assisted_in_the_tiebreak_too(self):
        """The cross-movement tiebreak reuses the same weighted-beats-assisted rule
        the within-movement heaviest reading already uses -- one comparator, not two."""
        history = _build_training_history(
            [],
            [
                _strength(
                    "2026-06-10", exercise="pull_up_assisted",
                    sets=[{"set": 1, "weight_kg": None, "assist_kg": 5.0, "reps": 5, "rpe": None}],
                ),
                _strength(
                    "2026-06-10", exercise="curl",
                    sets=[{"set": 1, "weight_kg": 10.0, "assist_kg": None, "reps": 5, "rpe": None}],
                ),
            ],
            BASELINE,
        )
        self.assertEqual(
            ["curl", "pull_up_assisted"],
            [movement["exercise"] for movement in history["movement_longevity"]],
        )


class NoVerdictTests(unittest.TestCase):
    def test_nothing_in_the_group_is_a_verdict(self):
        history = _build_training_history(
            [_activity("2026-06-03"), _activity("2026-07-05")],
            [_strength("2026-06-10"), _strength("2026-07-12")],
            BASELINE,
        )
        banned = {
            "trend", "direction", "progressing", "completion_rate", "adherence",
            "score", "percent_complete", "plateau", "regression", "change_pct",
        }
        self.assertEqual(set(), banned & set(history))
        for bucket in history["months"]:
            self.assertEqual(set(), banned & set(bucket))
        for movement in history["movement_longevity"]:
            self.assertEqual(set(), banned & set(movement))


if __name__ == "__main__":
    unittest.main()
