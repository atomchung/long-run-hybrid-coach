"""A new running-time limit constrains remaining work, not resolved history (issue #378)."""

from __future__ import annotations

import copy
import unittest

from garmin_coach_loop.plan_change import project_change_request
from garmin_coach_loop.validation import validate_bundle, validate_plan_state
from test_coach_loop import EXAMPLE, ISSUED_AT, load, project_context


class DurationLimitHistoryTests(unittest.TestCase):
    def setUp(self):
        self.before = load(EXAMPLE / "plan-state-v1.json")

    @staticmethod
    def _session(plan: dict, session_id: str) -> dict:
        return next(s for s in plan["week"]["sessions"] if s["session_id"] == session_id)

    @staticmethod
    def _reduce(session_id: str, minutes: int = 45) -> dict:
        return {
            "operation": "reduce",
            "session_id": session_id,
            "planned_minutes": minutes,
            "plan": {
                "kind": "time_axis",
                "name": "Within available time",
                "steps": [{
                    "kind": "work", "name": "Open run",
                    "duration": {"kind": "time", "seconds": minutes * 60},
                    "target": {"kind": "open"},
                }],
            },
        }

    def _project_and_validate(self, before: dict, operations: list[dict]) -> tuple[dict, dict]:
        self.assertEqual("passed", validate_plan_state(before)["status"])
        context = project_context(load(EXAMPLE / "coach-context-day-4.json"), before)
        request = {
            "summary": "Each remaining run now needs to fit the available time",
            "reason_codes": ["schedule_or_equipment_changed"],
            "evidence": [{"field": "constraints", "observation": "45 minutes per run from today"}],
            "goal_effect": {"week": "Shorten the remaining runs", "cycle": "Direction unchanged"},
            "next_review_condition": "Review at the next session",
            "athlete_baseline": {"max_session_minutes": 45},
            "sessions": operations,
        }
        projection = project_change_request(
            before, request, context=context, issued_at=ISSUED_AT
        )
        after = projection["after_plan"]
        return after, validate_bundle(context, before, after, projection["decision_event"])

    def test_a_shorter_remaining_week_preserves_every_resolved_session(self):
        """Tuesday's unchanged 56 minutes must not reject two remaining 45-minute runs."""
        for status in ("completed", "partial", "missed"):
            with self.subTest(status=status):
                before = copy.deepcopy(self.before)
                self._session(before, "run-easy-01")["match_status"] = status
                operations = [self._reduce("run-quality-01"), self._reduce("run-long-01")]

                after, report = self._project_and_validate(before, operations)

                self.assertEqual("passed", report["status"], report)
                self.assertEqual([], report["errors"])
                self.assertEqual(45, after["athlete_baseline"]["max_session_minutes"])
                for session in before["week"]["sessions"]:
                    if session["match_status"] in {"completed", "partial", "missed"}:
                        self.assertEqual(session, self._session(after, session["session_id"]))
                for session_id in ("run-quality-01", "run-long-01"):
                    self.assertEqual(45, self._session(after, session_id)["planned_minutes"])

    def test_pending_runs_still_obey_the_limit_even_if_untouched_or_delivered(self):
        for status in ("planned", "moved", "replaced"):
            for delivered in (False, True):
                for touched in (False, True):
                    with self.subTest(status=status, delivered=delivered, touched=touched):
                        before = copy.deepcopy(self.before)
                        long_run = self._session(before, "run-long-01")
                        long_run["match_status"] = status
                        if delivered:
                            long_run["execution"] = {
                                "publish_supported": True,
                                "delivery_state": "intervals_accepted",
                                "external_id": "anonymous-owned-event",
                            }
                        operations = [self._reduce("run-quality-01")]
                        if touched:
                            operations.append(self._reduce("run-long-01", 50))

                        after, report = self._project_and_validate(before, operations)

                        self.assertEqual("blocked", report["status"])
                        minutes = 50 if touched else long_run["planned_minutes"]
                        self.assertEqual(
                            [f"running session run-long-01 planned_minutes {minutes} "
                             "exceeds athlete_baseline max_session_minutes 45"],
                            report["errors"],
                        )
                        self.assertEqual(status, self._session(after, "run-long-01")["match_status"])
                        if not touched:
                            self.assertEqual(long_run, self._session(after, "run-long-01"))

    def test_a_past_date_does_not_turn_an_unresolved_run_into_history(self):
        before = copy.deepcopy(self.before)
        unresolved = self._session(before, "run-easy-01")
        unresolved["match_status"] = "planned"
        operations = [self._reduce("run-quality-01"), self._reduce("run-long-01")]

        after, report = self._project_and_validate(before, operations)

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            ["running session run-easy-01 planned_minutes 56 "
             "exceeds athlete_baseline max_session_minutes 45"],
            report["errors"],
        )
        self.assertEqual(unresolved, self._session(after, "run-easy-01"))


if __name__ == "__main__":
    unittest.main()
