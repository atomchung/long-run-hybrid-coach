"""One merged list of dated sessions, and the stops derived from it.

Two kinds of evidence describe the same training and neither is complete: the provider
holds what a device recorded over the span it was read on, and the store holds what the
athlete said and uploaded, unwindowed. Read apart, a week the provider's account did not
exist for reads as a week with no running in it -- five weeks of a stop that never
happened, in the same context whose monthly buckets reported those weeks as a 118 km
month (issue #101).

`_activity_observations` is the one list both halves are read into, and
`_build_training_breaks` is the second thing derived from it (issue #222): a blank a
calendar month structurally cannot show, because it begins inside one month and ends
inside another and both simply read as light. These tests hold the merge to counting each
session once, and the breaks to stating dates and observations and nothing else.
"""

from __future__ import annotations

import datetime as dt
import unittest

from garmin_coach_loop.context_core import (
    TRAINING_BREAK_MIN_DAYS,
    TRAINING_BREAKS_MAX_ROWS,
    _activity_observations,
    _build_training_breaks,
)
from garmin_coach_loop.validation import (
    TRAINING_BREAK_MIN_DAYS as VALIDATOR_BREAK_MIN_DAYS,
    _validate_training_breaks,
)


def _actual(date, sport="running", *, km=8.0):
    return {
        "activity_id": f"intervals:{date}",
        "date": date,
        "sport": sport,
        "cost": "easy",
        "distance_km": km,
        "duration_minutes": 40,
    }


def _reported(date, sport="running", *, km=8.0, source="athlete_imported"):
    return {
        "date": date,
        "sport": sport,
        "duration_minutes": 40,
        "distance_km": km,
        "subjective_feel": None,
        "note": None,
        "source": source,
        "imported_from": None,
    }


def _strength_report(date, *, source="athlete_reported"):
    return {
        "date": date,
        "exercise": "bench_press",
        "category": None,
        "sets": [{"set": 1, "weight_kg": 60.0, "assist_kg": None, "reps": 5, "rpe": None}],
        "notes": [],
        "source": source,
    }


def _history(*months):
    return {"months": [
        {"month": month, "sport": sport, "session_count": 1,
         "total_minutes": None, "total_km": km, "provenance_counts": {}}
        for month, sport, km in months
    ]}


class MergedObservationTests(unittest.TestCase):
    def test_both_kinds_of_source_land_in_one_list_with_their_provenance(self):
        rows = _activity_observations(
            [_actual("2026-08-11")], [_reported("2026-07-14")], None
        )
        self.assertEqual(
            [(dt.date(2026, 7, 14), "athlete_imported"),
             (dt.date(2026, 8, 11), "provider_actual")],
            [(row["date"], row["source"]) for row in rows],
        )

    def test_a_stored_row_the_provider_also_holds_that_day_and_sport_is_dropped(self):
        """The predicate `flag_provider_overlap` already writes onto every reported row,
        reused rather than restated. Conservative on purpose: an athlete who genuinely
        ran twice that day has the reported one left out of the count, and the row is
        still in `reported_activities` with its overlap flag set."""
        rows = _activity_observations(
            [_actual("2026-08-11")],
            [_reported("2026-08-11"), _reported("2026-08-13", km=5.0)],
            None,
        )
        self.assertEqual(
            [(dt.date(2026, 8, 11), "provider_actual"),
             (dt.date(2026, 8, 13), "athlete_imported")],
            [(row["date"], row["source"]) for row in rows],
        )

    def test_a_stored_row_on_a_day_the_provider_holds_a_different_sport_still_counts(self):
        rows = _activity_observations(
            [_actual("2026-08-11", "strength", km=None)], [_reported("2026-08-11")], None
        )
        self.assertEqual(2, len(rows))

    def test_two_stored_rows_on_one_day_are_two_sessions(self):
        """An upload is the one case that can leave two real sessions on one day, and
        `training_history` counts them as two for the same reason."""
        rows = _activity_observations(
            [], [_reported("2026-08-11"), _reported("2026-08-11", km=5.0)], None
        )
        self.assertEqual(2, len(rows))

    def test_a_strength_report_covers_a_day_no_activity_row_already_does(self):
        """Without it, a month of lifting recorded only per exercise reads as a stop."""
        rows = _activity_observations([], [], [_strength_report("2026-08-11")])
        self.assertEqual([(dt.date(2026, 8, 11), "strength")],
                         [(row["date"], row["sport"]) for row in rows])

    def test_a_gym_visit_described_twice_over_is_one_observation(self):
        """The same union across two containers `training_history`'s own session count
        uses: one visit summarised coarsely and reported per exercise is one session."""
        rows = _activity_observations(
            [], [_reported("2026-08-11", "strength", km=None)],
            [_strength_report("2026-08-11")],
        )
        self.assertEqual(1, len(rows))

    def test_rows_too_damaged_to_place_are_dropped(self):
        rows = _activity_observations(
            [{"date": None, "sport": "running"}],
            [{"date": "2026-08-11", "sport": None, "source": "athlete_reported"},
             {"date": "nonsense", "sport": "running", "source": "athlete_reported"},
             {"date": "2026-08-12", "sport": "running", "source": "invented_provenance"},
             # `provider_actual` labels a reading of the device's own feed. A stored row
             # claiming it would put the athlete's word behind the device's name.
             {"date": "2026-08-13", "sport": "running", "source": "provider_actual"}],
            None,
        )
        self.assertEqual([], rows)

    def test_a_distance_is_carried_only_when_the_row_stated_a_measured_one(self):
        rows = _activity_observations(
            [], [_reported("2026-08-11", km=None), _reported("2026-08-12", km=6.0)], None
        )
        self.assertEqual([None, 6.0], [row["distance_km"] for row in rows])


class TrainingBreakTests(unittest.TestCase):
    def _rows(self, *dates, sport="running", source="athlete_imported"):
        return _activity_observations(
            [], [_reported(date, sport, source=source) for date in dates], None
        )

    def test_the_stop_no_calendar_month_can_show(self):
        """Issue #222's own case. March holds eight days of training and April four, so
        the monthly buckets read as two light months; the blank between them is seven
        weeks and lives in neither bucket."""
        breaks, truncated = _build_training_breaks(
            self._rows("2026-03-08", "2026-04-27"),
            _history(("2026-02", "running", 157.0), ("2026-03", "running", 41.0),
                     ("2026-04", "running", 21.9), ("2026-05", "running", 63.9)),
        )
        self.assertFalse(truncated)
        self.assertEqual(1, len(breaks))
        row = breaks[0]
        self.assertEqual("running", row["sport"])
        self.assertEqual(("2026-03-09", "2026-04-26", 49), (row["start"], row["end"], row["days"]))
        self.assertEqual("2026-03-08", row["last_before"]["date"])
        self.assertEqual("2026-04-27", row["first_after"]["date"])

    def test_the_months_either_side_are_the_ones_the_break_did_not_cut_through(self):
        """A stop beginning on the 9th leaves its own month holding eight days of
        training, and reporting that as "what they were doing before" understates it by
        two thirds. February is the last month the athlete trained through."""
        breaks, _ = _build_training_breaks(
            self._rows("2026-03-08", "2026-04-27"),
            _history(("2026-02", "running", 157.0), ("2026-03", "running", 41.0),
                     ("2026-04", "running", 21.9), ("2026-05", "running", 63.9)),
        )
        self.assertEqual({"month": "2026-02", "total_km": 157.0},
                         breaks[0]["last_month_before"])
        self.assertEqual({"month": "2026-05", "total_km": 63.9},
                         breaks[0]["first_month_after"])

    def test_a_month_of_another_sport_is_never_read_as_this_ones_volume(self):
        breaks, _ = _build_training_breaks(
            self._rows("2026-03-08", "2026-04-27"),
            _history(("2026-02", "strength", None), ("2026-05", "running", 63.9)),
        )
        self.assertIsNone(breaks[0]["last_month_before"])
        self.assertEqual("2026-05", breaks[0]["first_month_after"]["month"])

    def test_no_monthly_buckets_at_all_leaves_both_sides_null(self):
        breaks, _ = _build_training_breaks(self._rows("2026-03-08", "2026-04-27"), None)
        self.assertIsNone(breaks[0]["last_month_before"])
        self.assertIsNone(breaks[0]["first_month_after"])

    def test_a_blank_one_day_short_of_the_length_is_not_a_break(self):
        """The false-positive control. A down stretch inside a normal block is not a
        stop, and reporting one as a stop is what the length exists to prevent."""
        short = dt.date(2026, 3, 8) + dt.timedelta(days=TRAINING_BREAK_MIN_DAYS)
        exact = dt.date(2026, 3, 8) + dt.timedelta(days=TRAINING_BREAK_MIN_DAYS + 1)
        self.assertIsNone(
            _build_training_breaks(self._rows("2026-03-08", short.isoformat()), None)[0]
        )
        breaks, _ = _build_training_breaks(
            self._rows("2026-03-08", exact.isoformat()), None
        )
        self.assertEqual(TRAINING_BREAK_MIN_DAYS, breaks[0]["days"])

    def test_each_sport_stops_and_restarts_on_its_own(self):
        """A runner who keeps lifting through an injury has a running break and no
        strength break, and one number across both sports would report neither."""
        rows = [
            *self._rows("2026-03-08", "2026-05-20"),
            *self._rows("2026-03-10", "2026-03-24", "2026-04-07", "2026-04-21",
                        "2026-05-05", sport="strength"),
        ]
        breaks, _ = _build_training_breaks(rows, None)
        self.assertEqual(["running"], [row["sport"] for row in breaks])

    def test_a_provider_actual_closes_a_blank_the_store_alone_would_leave_open(self):
        """The merge's own payoff here: the athlete's upload stops in July and the
        provider's account starts in August, and neither half alone can tell whether the
        weeks between were trained."""
        rows = _activity_observations(
            [_actual("2026-08-01")], [_reported("2026-07-20")], None
        )
        self.assertIsNone(_build_training_breaks(rows, None)[0])

    def test_nothing_long_enough_produces_no_group_at_all(self):
        self.assertEqual(
            (None, False), _build_training_breaks(self._rows("2026-03-08"), None)
        )
        self.assertEqual((None, False), _build_training_breaks([], None))

    def test_only_the_most_recent_breaks_survive_and_the_cut_is_stated(self):
        dates = [
            (dt.date(2025, 1, 1) + dt.timedelta(days=60 * index)).isoformat()
            for index in range(TRAINING_BREAKS_MAX_ROWS + 3)
        ]
        breaks, truncated = _build_training_breaks(self._rows(*dates), None)
        self.assertTrue(truncated)
        self.assertEqual(TRAINING_BREAKS_MAX_ROWS, len(breaks))
        self.assertEqual(sorted(row["start"] for row in breaks),
                         [row["start"] for row in breaks])
        # The ones kept are the recent ones -- what a return-to-training read needs.
        # Every consecutive pair of these dates is one break, so the kept starts are the
        # last TRAINING_BREAKS_MAX_ROWS days-after-an-observation in the list.
        expected = [
            (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
            for day in dates[:-1][-TRAINING_BREAKS_MAX_ROWS:]
        ]
        self.assertEqual(expected, [row["start"] for row in breaks])

    def test_nothing_in_a_break_row_is_a_verdict(self):
        """No cause, no recovery conclusion, no readiness figure, no score. Injury,
        travel, illness and a change of mind all leave exactly these rows."""
        breaks, _ = _build_training_breaks(
            self._rows("2026-03-08", "2026-04-27"),
            _history(("2026-02", "running", 157.0), ("2026-05", "running", 63.9)),
        )
        banned = {
            "cause", "reason", "detraining", "fitness_lost", "recovery", "readiness",
            "severity", "score", "status", "verdict", "confidence", "flag", "risk",
        }
        for row in breaks:
            self.assertEqual(set(), banned & set(row))
            for side in ("last_before", "first_after"):
                self.assertEqual(set(), banned & set(row[side]))


class TrainingBreakValidatorTests(unittest.TestCase):
    """The key list is the no-verdict guarantee, so it gets its own arm."""

    def test_the_length_the_validator_refuses_below_is_the_one_the_builder_uses(self):
        """`validation` cannot import `context_core` -- that module imports it, and the
        other direction is a cycle -- so the length is written in both places. This is
        what stops the two copies drifting into a builder that writes rows its own
        validator refuses."""
        self.assertEqual(TRAINING_BREAK_MIN_DAYS, VALIDATOR_BREAK_MIN_DAYS)

    def _rows(self):
        breaks, _ = _build_training_breaks(
            _activity_observations(
                [], [_reported("2026-03-08"), _reported("2026-04-27")], None
            ),
            _history(("2026-02", "running", 157.0), ("2026-05", "running", 63.9)),
        )
        return breaks

    def _errors(self, value):
        errors: list[str] = []
        _validate_training_breaks(value, "context.training_breaks", errors)
        return errors

    def test_the_built_group_passes_its_own_validator(self):
        self.assertEqual([], self._errors(self._rows()))

    def test_a_null_group_is_accepted_and_an_empty_list_is_not(self):
        """Null already says no break was observed. An empty list is a second spelling
        of one fact, and two spellings drift."""
        self.assertEqual([], self._errors(None))
        self.assertTrue(any("must be null" in error for error in self._errors([])))

    def test_a_cause_has_no_legal_place_to_sit(self):
        """The most likely way this group turns into a diagnosis is one innocuous-looking
        string. There is no key it can occupy."""
        rows = self._rows()
        rows[0]["cause"] = "injury"
        self.assertTrue(any("cause is not allowed" in error for error in self._errors(rows)))

    def test_a_span_shorter_than_the_stated_length_is_refused(self):
        rows = self._rows()
        rows[0]["days"] = TRAINING_BREAK_MIN_DAYS - 1
        self.assertTrue(any(".days" in error for error in self._errors(rows)))

    def test_a_length_that_disagrees_with_its_own_dates_is_refused(self):
        rows = self._rows()
        rows[0]["days"] = 60
        self.assertTrue(
            any("length of its own start-to-end span" in e for e in self._errors(rows))
        )

    def test_an_observation_that_does_not_bracket_the_blank_is_refused(self):
        rows = self._rows()
        rows[0]["last_before"]["date"] = "2026-03-01"
        self.assertTrue(
            any("must be the day before start" in e for e in self._errors(rows))
        )


if __name__ == "__main__":
    unittest.main()
