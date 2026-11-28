"""The full product's 90-day tool-call record (issue #556), as a store on its own."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop import request_events
from garmin_coach_loop.request_events import RequestEvents, read_rows, refused_field

NOW = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.timezone.utc)


class RefusedFieldTests(unittest.TestCase):
    def test_a_request_refusal_yields_its_field_path_and_not_the_sentence(self):
        self.assertEqual(
            "initialization_request.availability",
            refused_field("initialization_request.availability does not accept note"),
        )

    def test_a_validation_report_yields_its_first_field_path(self):
        self.assertEqual(
            "week.sessions[2].plan.steps[0].duration_minutes",
            refused_field(
                "plan validation failed",
                ["week.sessions[2].plan.steps[0].duration_minutes must be >= 1", "goal.outcome is required"],
            ),
        )

    def test_free_text_around_a_path_never_comes_through(self):
        # The harmful case: an athlete's words inside a refusal sentence. Only the path,
        # which this product's own validators spell, survives.
        field = refused_field("week.sessions[0].label 週二晚上 跟同事跑 is too long")
        self.assertEqual("week.sessions[0].label", field)
        self.assertIsNone(refused_field("週二晚上 跟同事跑"))
        self.assertIsNone(refused_field("A plan needs a goal"))
        self.assertIsNone(refused_field(None))

    def test_a_long_path_is_cut_rather_than_kept_whole(self):
        path = "a." + ".".join(["segment"] * 40)
        self.assertLessEqual(len(refused_field(path)), 160)


class RequestEventsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / request_events.FILE_NAME

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_row_holds_only_the_closed_fields(self):
        events = RequestEvents(self.path, retention_days=90)

        self.assertTrue(
            events.record(
                at=NOW,
                account="0123456789abcdef",
                tool="prepareCoachDecision",
                outcome="blocked:invalid_request",
                field="change_request.week.intent",
                client="https://claude.ai",
                protocol="2026-07-28",
                intervals_calls=None,
                duration_ms=42,
            )
        )

        (row,) = read_rows(self.path)
        self.assertEqual(
            {
                "at": "2026-11-28T12:00:00+00:00",
                "account": "0123456789abcdef",
                "tool": "prepareCoachDecision",
                "outcome": "blocked:invalid_request",
                "field": "change_request.week.intent",
                "client": "https://claude.ai",
                "protocol": "2026-07-28",
                "intervals_calls": None,
                "duration_ms": 42,
            },
            row,
        )

    def test_rows_older_than_the_retention_are_pruned(self):
        events = RequestEvents(self.path, retention_days=90)
        events.record(at=NOW - dt.timedelta(days=91), account="a" * 16, tool="getCoachState", outcome="passed")
        events.record(at=NOW - dt.timedelta(days=89), account="b" * 16, tool="getCoachState", outcome="passed")
        # A later instance, as after a restart, prunes on its first write.
        RequestEvents(self.path, retention_days=90).record(
            at=NOW, account="c" * 16, tool="startCoachSession", outcome="no_plan_state"
        )

        self.assertEqual(["b" * 16, "c" * 16], [row["account"] for row in read_rows(self.path)])

    def test_a_failed_write_is_reported_and_never_raised(self):
        # The file's directory does not exist, so sqlite cannot open it.
        events = RequestEvents(Path(self._tmp.name) / "missing" / request_events.FILE_NAME, retention_days=90)
        with self.assertLogs("garmin_coach_loop.request_events", level="WARNING"):
            self.assertFalse(
                events.record(at=NOW, account="a" * 16, tool="getCoachState", outcome="passed")
            )

    def test_a_record_kept_for_no_days_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            RequestEvents(self.path, retention_days=0)

    def test_reading_never_creates_the_file(self):
        self.assertEqual([], read_rows(self.path))
        self.assertFalse(self.path.exists())


class RequestEventScopeTests(unittest.TestCase):
    def test_only_the_full_product_keeps_a_record(self):
        self.assertEqual(90, request_events.retention_days("hybrid"))
        # The Plugin entry: held to a reviewer's read-only reading of its tools (#408).
        self.assertEqual(0, request_events.retention_days("training"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
