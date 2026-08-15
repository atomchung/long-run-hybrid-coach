"""baseline_evidence: each athlete_baseline field's claim beside what was observed.

The baseline is written by hand and nothing notices when it stops describing the
athlete (issue #32). These tests hold the comparison to being complete (every field,
running and strength through one row shape), honest about what was never observed,
and empty of verdicts -- no stale flag, no confidence, no suggested value, and no
once-vs-established boundary, because which side is right is the coaching judgment
the group exists to inform.
"""

from __future__ import annotations

import datetime as dt
import unittest

from garmin_coach_loop.context_core import (
    BuildWindow,
    _build_baseline_evidence,
    _build_movement_history,
)
from garmin_coach_loop.validation import _validate_baseline_evidence


BASELINE = {
    "threshold_pace_sec_per_km": 370,
    "max_hr": 188,
    "easy_hr_ceiling": 150,
    "longest_recent_run_km": 7.11,
    "weekly_volume_km_4wk_avg": 15.9,
    "max_session_minutes": 75,
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
    ],
}

SCALAR_FIELDS = (
    "threshold_pace_sec_per_km",
    "max_hr",
    "easy_hr_ceiling",
    "longest_recent_run_km",
    "weekly_volume_km_4wk_avg",
    "max_session_minutes",
)


def _window(as_of: str = "2026-08-15") -> BuildWindow:
    day = dt.date.fromisoformat(as_of)
    moment = dt.datetime.combine(day, dt.time(9, 0), tzinfo=dt.timezone.utc)
    return BuildWindow(
        as_of=moment,
        resolved_now=moment,
        now_iso=moment.isoformat(),
        window_start=day - dt.timedelta(days=6),
        window_end=day,
        window14_start=day - dt.timedelta(days=13),
        window14_end=day,
        window42_start=day - dt.timedelta(days=41),
        window42_end=day,
    )


def _run(activity_id, date, *, distance_km=None, pace=None, hr=None, minutes=None, cost="easy"):
    return {
        "activity_id": activity_id,
        "date": date,
        "sport": "running",
        "cost": cost,
        "distance_km": distance_km,
        "average_pace_sec_per_km": pace,
        "average_hr": hr,
        "duration_minutes": minutes,
    }


def _strength_actual(activity_id, date, *, hr=None, minutes=None):
    return {
        "activity_id": activity_id,
        "date": date,
        "sport": "strength",
        "cost": "moderate",
        "average_hr": hr,
        "duration_minutes": minutes,
    }


def _sets(*pairs):
    return [
        {"set": index, "weight_kg": weight, "assist_kg": None, "reps": reps, "rpe": None}
        for index, (weight, reps) in enumerate(pairs, start=1)
    ]


def _assist_sets(*pairs):
    return [
        {"set": index, "weight_kg": None, "assist_kg": assist, "reps": reps, "rpe": None}
        for index, (assist, reps) in enumerate(pairs, start=1)
    ]


def _history(*sessions, baseline=BASELINE):
    execution = {
        "source": "personal-os:strength_log",
        "window_start": "2026-07-05",
        "window_end": "2026-08-15",
        "sessions": list(sessions),
    }
    plan = {"week": {"start": "2026-08-10", "sessions": []}, "athlete_baseline": baseline}
    return _build_movement_history([], plan, execution, baseline), execution


def _build(recent_actuals, movement_history=None, strength_execution=None, *,
           baseline=BASELINE, actuals_start="2026-07-05", as_of="2026-08-15"):
    return _build_baseline_evidence(
        baseline,
        recent_actuals,
        movement_history,
        strength_execution,
        actuals_window_start=dt.date.fromisoformat(actuals_start),
        window=_window(as_of),
    )


def _row(rows, field, exercise=None):
    matches = [
        row for row in rows
        if row["field"] == field and (exercise is None or row.get("exercise") == exercise)
    ]
    assert len(matches) == 1, f"{field}/{exercise}: {len(matches)} rows"
    return matches[0]


class BaselineEvidenceTests(unittest.TestCase):
    def test_the_live_drift_scene_surfaces_claim_beside_evidence_with_counts(self):
        """The scene this issue was filed on: longest run written 7.11 while 8.17 was
        run on 8/14, bench written 60 while the athlete has been working at 65. Both
        must be readable from the rows alone, with how many observations back them."""
        history, execution = _history(
            {"date": "2026-07-25", "exercise": "bench_press", "category": "chest",
             "sets": _sets((60.0, 5), (60.0, 5), (60.0, 5), (60.0, 5), (60.0, 5)), "notes": []},
            {"date": "2026-08-01", "exercise": "bench_press", "category": "chest",
             "sets": _sets((60.0, 5), (60.0, 5), (60.0, 5), (60.0, 5), (60.0, 5)), "notes": []},
            {"date": "2026-08-08", "exercise": "bench_press", "category": "chest",
             "sets": _sets((62.5, 5), (62.5, 5), (62.5, 4)), "notes": []},
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5), (65.0, 5), (65.0, 5), (65.0, 5), (60.0, 5)), "notes": []},
            {"date": "2026-08-15", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4), (65.0, 4)), "notes": []},
        )
        rows = _build(
            [
                _run("a1", "2026-08-02", distance_km=6.5),
                _run("a2", "2026-08-09", distance_km=7.11),
                _run("a3", "2026-08-14", distance_km=8.17),
            ],
            history,
            execution,
        )

        longest = _row(rows, "longest_recent_run_km")
        self.assertEqual(7.11, longest["baseline"])
        self.assertEqual({"longest_run_km": 8.17, "date": "2026-08-14"}, longest["observed"])
        self.assertEqual(3, longest["observations"])

        bench = _row(rows, "strength_loads", "bench_press")
        self.assertEqual(60.0, bench["baseline"]["load_kg"])
        self.assertEqual("臥推", bench["display_name"])
        self.assertEqual(5, bench["observations"])
        loads = bench["observed"]["loads"]
        self.assertEqual(
            {"load_kg": 65.0, "assist_kg": None, "sessions": 2,
             "first": "2026-08-11", "last": "2026-08-15"},
            loads[0],
        )
        self.assertEqual(
            {"load_kg": 60.0, "assist_kg": None, "sessions": 2,
             "first": "2026-07-25", "last": "2026-08-01"},
            loads[-1],
        )

    def test_every_scalar_field_reports_even_with_nothing_observed(self):
        """A field with no recent evidence is reported as having none -- observed null,
        zero observations -- never dropped and never altered."""
        rows = _build([])
        for field in SCALAR_FIELDS:
            row = _row(rows, field)
            if field == "weekly_volume_km_4wk_avg":
                continue  # the window's weeks are always stated; asserted separately
            self.assertIsNone(row["observed"], field)
            self.assertEqual(0, row["observations"], field)
            self.assertEqual(BASELINE[field], row["baseline"], field)

    def test_running_and_strength_rows_share_one_shape(self):
        history, execution = _history(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5)), "notes": []},
        )
        rows = _build([_run("a1", "2026-08-14", distance_km=8.0)], history, execution)
        scalar_keys = {"field", "baseline", "observed", "observations", "window_start", "window_end"}
        strength_keys = scalar_keys | {"exercise", "display_name"}
        for row in rows:
            expected = strength_keys if row["field"] == "strength_loads" else scalar_keys
            self.assertEqual(expected, set(row), row["field"])

    def test_weekly_totals_use_natural_weeks_and_keep_unknown_distance_unknown(self):
        """A run with no recorded distance makes its week's total unknown, never zero;
        a week the window only partly holds is excluded rather than undercounted; the
        running week says how far it has run via `through`."""
        rows = _build(
            [
                _run("a1", "2026-08-14", distance_km=8.17),
                _run("a2", "2026-08-11"),  # no distance recorded
                _run("a3", "2026-08-09", distance_km=7.11),
                _run("a4", "2026-08-05", distance_km=5.0),
            ],
            actuals_start="2026-07-05",
            as_of="2026-08-15",
        )
        weeks = _row(rows, "weekly_volume_km_4wk_avg")["observed"]["weeks"]
        self.assertEqual(
            ["2026-08-10", "2026-08-03", "2026-07-27", "2026-07-20", "2026-07-13", "2026-07-06"],
            [week["week_start"] for week in weeks],
        )  # 2026-06-29 began before the window and is excluded, not undercounted
        current = weeks[0]
        self.assertEqual("2026-08-15", current["through"])
        self.assertEqual(2, current["runs"])
        self.assertIsNone(current["km"])
        self.assertEqual({"week_start": "2026-08-03", "through": "2026-08-09",
                          "km": 12.11, "runs": 2}, weeks[1])
        self.assertEqual(0, weeks[2]["runs"])
        self.assertEqual(0, weeks[2]["km"])

    def test_extremes_read_only_measured_values_and_name_their_sport(self):
        """max_hr and max_session_minutes read any sport, and say which one carried the
        observation -- an average on a strength session is not read like a run's."""
        rows = _build(
            [
                _run("a1", "2026-08-09", hr=152.0, minutes=40, pace=380, cost="easy"),
                _run("a2", "2026-08-12", hr=171.0, minutes=48, pace=352, cost="hard"),
                _strength_actual("a3", "2026-08-13", hr=132.0, minutes=62),
            ]
        )
        pace = _row(rows, "threshold_pace_sec_per_km")
        self.assertEqual(352, pace["observed"]["fastest_average_pace_sec_per_km"])
        self.assertEqual(2, pace["observations"])

        max_hr = _row(rows, "max_hr")
        self.assertEqual(
            {"highest_average_hr": 171.0, "date": "2026-08-12", "sport": "running"},
            max_hr["observed"],
        )
        self.assertEqual(3, max_hr["observations"])

        minutes = _row(rows, "max_session_minutes")
        self.assertEqual(
            {"longest_session_minutes": 62, "date": "2026-08-13", "sport": "strength"},
            minutes["observed"],
        )

        easy = _row(rows, "easy_hr_ceiling")
        self.assertEqual({"average_hr_low": 152.0, "average_hr_high": 152.0}, easy["observed"])
        self.assertEqual(1, easy["observations"])

    def test_an_assisted_movement_groups_by_least_assistance(self):
        """Less help is the heavier direction: the working level of an assisted lift is
        the day's least assistance, and 24 kg -> 21 kg must read as two levels, not one."""
        history, execution = _history(
            {"date": "2026-07-16", "exercise": "pull_up_assisted", "category": "back",
             "sets": _assist_sets((24.0, 5), (24.0, 5)), "notes": []},
            {"date": "2026-07-23", "exercise": "pull_up_assisted", "category": "back",
             "sets": _assist_sets((21.0, 5), (24.0, 5)), "notes": []},
            {"date": "2026-08-06", "exercise": "pull_up_assisted", "category": "back",
             "sets": _assist_sets((21.0, 5), (21.0, 5)), "notes": []},
        )
        rows = _build([], history, execution)
        row = _row(rows, "strength_loads", "pull_up_assisted")
        self.assertEqual(24.0, row["baseline"]["assist_kg"])
        self.assertEqual(
            [
                {"load_kg": None, "assist_kg": 21.0, "sessions": 2,
                 "first": "2026-07-23", "last": "2026-08-06"},
                {"load_kg": None, "assist_kg": 24.0, "sessions": 1,
                 "first": "2026-07-16", "last": "2026-07-16"},
            ],
            row["observed"]["loads"],
        )

    def test_unanchored_evidence_and_unobserved_claims_both_still_report(self):
        """A movement trained with no baseline entry reports with a null claim; a
        baseline entry the window holds nothing for reports with null observed. Both
        halves of the comparison surface, whichever one is missing."""
        history, execution = _history(
            {"date": "2026-08-12", "exercise": "romanian_deadlift", "category": "legs",
             "sets": _sets((40.0, 8)), "notes": []},
        )
        rows = _build([], history, execution)
        unanchored = _row(rows, "strength_loads", "romanian_deadlift")
        self.assertIsNone(unanchored["baseline"])
        self.assertEqual(1, unanchored["observations"])

        unobserved = _row(rows, "strength_loads", "bench_press")
        self.assertIsNone(unobserved["observed"])
        self.assertEqual(0, unobserved["observations"])
        self.assertEqual("2026-07-05", unobserved["window_start"])

    def test_a_strength_row_with_no_source_read_has_a_null_window(self):
        """No strength source read at all is a different fact from reading one and
        finding nothing, and the row's window says which happened."""
        rows = _build([], None, None)
        row = _row(rows, "strength_loads", "bench_press")
        self.assertIsNone(row["window_start"])
        self.assertIsNone(row["window_end"])
        self.assertEqual(0, row["observations"])

    def test_nothing_in_the_group_is_a_verdict(self):
        """No stale flag, no confidence, no suggested value, no established boolean --
        the comparison is reported and the judgment is left where it belongs."""
        history, execution = _history(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5)), "notes": []},
        )
        rows = _build([_run("a1", "2026-08-14", distance_km=8.17, hr=150.0)], history, execution)
        banned = {
            "stale", "established", "confidence", "suggested", "suggested_value",
            "trend", "direction", "verdict", "status", "score", "current",
        }
        for row in rows:
            self.assertEqual(set(), banned & set(row))
            observed = row.get("observed")
            if isinstance(observed, dict):
                self.assertEqual(set(), banned & set(observed))
                for item in observed.get("loads") or []:
                    self.assertEqual(set(), banned & set(item))
                for item in observed.get("weeks") or []:
                    self.assertEqual(set(), banned & set(item))

    def test_the_built_group_passes_its_own_validator(self):
        history, execution = _history(
            {"date": "2026-08-11", "exercise": "bench_press", "category": "chest",
             "sets": _sets((65.0, 5)), "notes": []},
        )
        rows = _build(
            [_run("a1", "2026-08-14", distance_km=8.17, hr=150.0, pace=390, minutes=55)],
            history,
            execution,
        )
        errors: list[str] = []
        _validate_baseline_evidence(rows, "context.baseline_evidence", errors)
        self.assertEqual([], errors)


class BaselineEvidenceValidatorTests(unittest.TestCase):
    """The exact-keys contract is the no-verdict guarantee, so it gets its own arm."""

    def _rows(self):
        return _build([_run("a1", "2026-08-14", distance_km=8.17)])

    def test_an_established_flag_has_no_legal_place_to_sit(self):
        """The most likely way the no-verdict boundary erodes is one innocuous-looking
        boolean. There is no key it can legally occupy, on any row."""
        rows = self._rows()
        rows[0]["established"] = True
        errors: list[str] = []
        _validate_baseline_evidence(rows, "context.baseline_evidence", errors)
        self.assertTrue(any("established is not allowed" in error for error in errors))

    def test_every_scalar_field_must_appear_exactly_once(self):
        rows = [row for row in self._rows() if row["field"] != "max_hr"]
        errors: list[str] = []
        _validate_baseline_evidence(rows, "context.baseline_evidence", errors)
        self.assertTrue(any("exactly one row for max_hr" in error for error in errors))

    def test_nothing_observed_and_a_positive_count_cannot_both_be_true(self):
        rows = self._rows()
        row = next(item for item in rows if item["field"] == "max_hr")
        row["observed"] = None
        row["observations"] = 3
        errors: list[str] = []
        _validate_baseline_evidence(rows, "context.baseline_evidence", errors)
        self.assertTrue(any("observed null requires observations 0" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
