from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.store import apply_decision, history_store, init_store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"


def load(name: str) -> dict:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


class StoreHistoryTests(unittest.TestCase):
    """The chain already holds every version; history has to make it answerable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        init_store(self.state_dir, self.before)
        apply_decision(
            self.state_dir,
            context=load("coach-context-day-4.json"),
            after=self.after,
            event=self.event,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_history_lists_every_revision_with_its_decision(self):
        report = history_store(self.state_dir)
        self.assertEqual("passed", report["status"])
        self.assertEqual(2, report["revision_count"])

        initial, latest = report["revisions"]
        self.assertIsNone(initial["event_id"])
        self.assertEqual(
            sorted(s["session_id"] for s in self.before["week"]["sessions"]),
            initial["sessions_added"],
        )
        self.assertEqual(self.event["event_id"], latest["event_id"])
        self.assertEqual(self.event["mode"], latest["mode"])
        self.assertEqual(self.event["reason_codes"], latest["reason_codes"])
        self.assertEqual(self.event["change"]["summary"], latest["summary"])

    def test_initiative_is_reported_as_null_until_events_carry_it(self):
        # A missing column must read as "never recorded", not vanish from the row.
        report = history_store(self.state_dir)
        for revision in report["revisions"]:
            self.assertIn("initiative", revision)
            self.assertIsNone(revision["initiative"])

    def test_modified_sessions_report_which_fields_changed(self):
        report = history_store(self.state_dir)
        latest = report["revisions"][-1]
        touched = {
            entry["session_id"]: entry["fields"] for entry in latest["sessions_modified"]
        }
        before_by_id = {s["session_id"]: s for s in self.before["week"]["sessions"]}
        after_by_id = {s["session_id"]: s for s in self.after["week"]["sessions"]}
        expected = {
            sid: sorted(
                key for key in set(before_by_id[sid]) | set(after_by_id[sid])
                if before_by_id[sid].get(key) != after_by_id[sid].get(key)
            )
            for sid in before_by_id.keys() & after_by_id.keys()
            if before_by_id[sid] != after_by_id[sid]
        }
        self.assertEqual(expected, touched)

    def test_single_session_timeline_follows_one_session(self):
        session_id = self.before["week"]["sessions"][0]["session_id"]
        report = history_store(self.state_dir, session_id=session_id)
        self.assertEqual(session_id, report["session_id"])
        self.assertEqual("created", report["timeline"][0]["change"])
        self.assertEqual(1, report["timeline"][0]["version"])
        for entry in report["timeline"]:
            self.assertEqual(session_id, entry["snapshot"]["session_id"])

    def test_unknown_session_is_an_explicit_note_not_an_empty_answer(self):
        report = history_store(self.state_dir, session_id="no-such-session")
        self.assertEqual([], report["timeline"])
        self.assertIn("no-such-session", report["note"])


if __name__ == "__main__":
    unittest.main()
