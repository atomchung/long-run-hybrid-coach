"""What a session's start against its end reports, and what it refuses to report.

Both fields exist for one reason: a session's averages cannot separate two sessions of
identical duration that ran opposite ways. These tests hold the separation, and hold the
three places the readers stay silent rather than guess -- a run too short to have thirds,
a series the device never recorded, and a file that carried no sets.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest

from garmin_coach_loop.source_intervals import ProviderResponse
import urllib.request

from garmin_coach_loop import source_intervals as si
from garmin_coach_loop.context_core import ContextBuildError
from garmin_coach_loop.fit_sets import FitParseError, summarise_sets
from tests import fit_fixtures

END = dt.date(2026, 8, 28)
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.timezone.utc)
CREDENTIALS = si.IntervalsCredentials(api_key="k", athlete_id="i0", auth_scheme="basic")


def _window() -> si.BuildWindow:
    return si.BuildWindow(
        as_of=NOW, resolved_now=NOW, now_iso=NOW.isoformat(),
        window_start=END - dt.timedelta(days=6), window_end=END,
        window14_start=END - dt.timedelta(days=13), window14_end=END,
        window28_start=END - dt.timedelta(days=27), window28_end=END,
        window42_start=END - dt.timedelta(days=41), window42_end=END,
    )


def _activity(activity_id: str, day: dt.date, sport: str) -> dict:
    return {
        "id": activity_id,
        "type": "Run" if sport == "running" else "WeightTraining",
        "start_date_local": f"{day.isoformat()}T18:00:00",
    }


def _fetcher(responses: dict[str, bytes], *, failures: frozenset[str] = frozenset()):
    """A fetcher keyed by the tail of the URL, so a test names only what it serves."""

    def fetch(request: urllib.request.Request) -> ProviderResponse:
        url = request.full_url
        for marker in failures:
            if marker in url:
                raise ContextBuildError(f"simulated read failure for {marker}")
        for marker, body in responses.items():
            if marker in url:
                return ProviderResponse(body)
        raise AssertionError(f"unexpected URL: {url}")

    return fetch


def _long_run_streams(**series: list[float]) -> bytes:
    return json.dumps(fit_fixtures.drift_streams(**series)).encode("utf-8")


def _steady(value: float, count: int = 1200) -> list[float]:
    return [value] * count


def _rising(start: float, end: float, count: int = 1200) -> list[float]:
    step = (end - start) / (count - 1)
    return [start + step * index for index in range(count)]


class RunDriftTests(unittest.TestCase):
    def _build(self, streams: bytes, *, day: dt.date = END, failures=frozenset()):
        notes: list[str] = []
        group = si._build_run_drift(
            [_activity("i1", day, "running")], _window(), CREDENTIALS, notes,
            fetch=_fetcher({"/streams": streams}, failures=failures),
        )
        return group, notes

    def test_a_run_that_cost_more_while_producing_less_shows_both_halves(self):
        """The case the session average cannot state at all.

        Heart rate up, pace down, cadence giving way, ground contact unchanged. Whether
        that is heat, fatigue or terrain is not decided here and must not be: the field
        reports the measurements at each end and stops (AGENTS.md 1).
        """
        group, _ = self._build(_long_run_streams(
            heartrate=_rising(130, 140),
            velocity_smooth=[2.0] * 600 + [1.9] * 600,
            cadence=[73.0] * 600 + [71.5] * 600,
            stance_time=_steady(333),
        ))
        entry = group["activities"][0]
        self.assertLess(entry["first_third"]["average_hr"], entry["last_third"]["average_hr"])
        self.assertLess(
            entry["first_third"]["average_pace_sec_per_km"],
            entry["last_third"]["average_pace_sec_per_km"],
            "a slower last third is a larger seconds-per-km, not a smaller one",
        )
        self.assertEqual(entry["first_third"]["stance_time_ms"], entry["last_third"]["stance_time_ms"])
        self.assertGreater(
            entry["first_third"]["average_cadence_spm"],
            entry["last_third"]["average_cadence_spm"],
            "cadence is the half of speed the athlete can act on, so it has to be readable",
        )

    def test_a_run_that_finished_faster_reads_the_opposite_way(self):
        """The control the test above needs to mean anything.

        A negative split and a heat-loaded fade both raise heart rate. Only the pace
        separates them, so a field that could not tell them apart would be worse than
        no field: it would make every hard finish look like a fade.
        """
        group, _ = self._build(_long_run_streams(
            heartrate=_rising(130, 158),
            velocity_smooth=[2.0] * 600 + [2.4] * 600,
            cadence=[72.9] * 600 + [74.6] * 600,
            stance_time=[333.0] * 600 + [297.0] * 600,
        ))
        entry = group["activities"][0]
        self.assertGreater(
            entry["first_third"]["average_pace_sec_per_km"],
            entry["last_third"]["average_pace_sec_per_km"],
        )
        self.assertGreater(
            entry["first_third"]["stance_time_ms"], entry["last_third"]["stance_time_ms"]
        )

    def test_a_series_the_device_never_recorded_is_absent_from_both_ends(self):
        """Absent, never zeroed, and never costing the series that were recorded."""
        group, _ = self._build(_long_run_streams(
            heartrate=_rising(130, 140), velocity_smooth=_steady(2.0),
        ))
        entry = group["activities"][0]
        for end in ("first_third", "last_third"):
            self.assertNotIn("average_cadence_spm", entry[end])
            self.assertNotIn("stance_time_ms", entry[end])
            self.assertIn("average_hr", entry[end])

    def test_a_run_too_short_for_thirds_reports_nothing_rather_than_a_shared_reading(self):
        """Under the fifteen-minute floor the two ends would differ by warm-up."""
        group, _ = self._build(_long_run_streams(heartrate=[130] * 300))
        self.assertIsNone(group)

    def test_a_failed_stream_read_is_named_and_does_not_fail_the_build(self):
        group, notes = self._build(b"", failures=frozenset({"/streams"}))
        self.assertIsNone(group)
        self.assertEqual(["run_drift: 1 activity stream read(s) failed"], notes)

    def test_runs_past_the_cap_are_named_rather_than_silently_dropped(self):
        """A capped read that reports nothing reads exactly like a quiet fortnight."""
        notes: list[str] = []
        activities = [
            _activity(f"i{index}", END - dt.timedelta(days=index), "running")
            for index in range(si._MAX_DRIFT_ACTIVITIES + 2)
        ]
        si._build_run_drift(
            activities, _window(), CREDENTIALS, notes,
            fetch=_fetcher({"/streams": _long_run_streams(heartrate=_rising(130, 140))}),
        )
        self.assertIn("run_drift: 2 older run(s) in the window were not read", notes)

    def test_a_strength_entry_is_never_read_for_drift(self):
        notes: list[str] = []
        group = si._build_run_drift(
            [_activity("i1", END, "strength")], _window(), CREDENTIALS, notes,
            fetch=_fetcher({}),  # any request at all would raise
        )
        self.assertIsNone(group)


class SetStructureTests(unittest.TestCase):
    def _build(self, payload: bytes, *, failures=frozenset()):
        notes: list[str] = []
        group = si._build_set_structure(
            [_activity("i1", END, "strength")], _window(), CREDENTIALS, notes,
            fetch=_fetcher({"/file": payload}, failures=failures),
        )
        return group, notes

    def test_two_sessions_of_equal_length_can_read_opposite_ways(self):
        """The whole reason this field exists, as one assertion.

        Both sessions below run 810 seconds with six working sets, which is everything
        the provider's own summary would report about either of them. One shortens its
        rest while its sets get longer; the other does exactly the reverse.
        """
        toward_metabolic = [(30_000, 1), (120_000, 0)] * 3 + [(60_000, 1), (60_000, 0)] * 3
        toward_strength = [(60_000, 1), (60_000, 0)] * 3 + [(30_000, 1), (120_000, 0)] * 3
        first = summarise_sets(fit_fixtures.fit_file_with_sets(toward_metabolic))
        second = summarise_sets(fit_fixtures.fit_file_with_sets(toward_strength))

        self.assertEqual(first["recorded_sec"], second["recorded_sec"])
        self.assertEqual(first["work_sets"], second["work_sets"])
        self.assertGreater(first["rest_first_third_sec"], first["rest_last_third_sec"])
        self.assertLess(first["set_first_third_sec"], first["set_last_third_sec"])
        self.assertLess(second["rest_first_third_sec"], second["rest_last_third_sec"])
        self.assertGreater(second["set_first_third_sec"], second["set_last_third_sec"])

    def test_no_exercise_reps_or_load_ever_leaves_the_reader(self):
        """Named explicitly, because all three are present in the file and readable.

        The device records reps and an exercise guess, and would record weight if it
        were typed in. None of them are carried: the guess is a list of candidates, the
        counts disagree with what the athlete logged, and the athlete's own statement
        through strength_execution is the record for all three (see fit_sets).
        """
        summary = summarise_sets(
            fit_fixtures.fit_file_with_sets([(30_000, 1), (120_000, 0)] * 3)
        )
        for banned in ("reps", "repetitions", "weight", "weight_kg", "exercise", "category"):
            self.assertNotIn(banned, summary)

    def test_a_session_with_no_sets_reports_nothing_and_is_not_a_failure(self):
        """A strength activity started but never stepped through."""
        group, notes = self._build(fit_fixtures.fit_file_without_sets())
        self.assertIsNone(group)
        self.assertEqual([], notes)

    def test_a_file_that_cannot_be_parsed_is_a_failure_and_says_so(self):
        """Distinct from the case above: this one the coach should not read as empty."""
        group, notes = self._build(b"this is not a FIT file")
        self.assertIsNone(group)
        self.assertEqual(["set_structure: 1 strength file read(s) failed"], notes)

    def test_a_session_too_short_for_thirds_still_reports_its_count_and_load(self):
        """The drift half goes absent; the half that is always knowable stays."""
        summary = summarise_sets(fit_fixtures.fit_file_with_sets([(30_000, 1), (60_000, 0)]))
        self.assertEqual(1, summary["work_sets"])
        self.assertEqual(30, summary["under_load_sec"])
        self.assertIsNone(summary["rest_first_third_sec"])
        self.assertIsNone(summary["set_first_third_sec"])

    def test_a_running_entry_is_never_read_for_sets(self):
        notes: list[str] = []
        group = si._build_set_structure(
            [_activity("i1", END, "running")], _window(), CREDENTIALS, notes,
            fetch=_fetcher({}),
        )
        self.assertIsNone(group)

    def test_garbage_is_refused_rather_than_read_as_an_empty_session(self):
        with self.assertRaises(FitParseError):
            summarise_sets(b"not a FIT file at all")


if __name__ == "__main__":
    unittest.main()
