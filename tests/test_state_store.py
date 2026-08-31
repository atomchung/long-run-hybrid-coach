from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.plan_change import project_change_request
from garmin_coach_loop.prescription import render_prescription
from garmin_coach_loop.store import (
    StateStoreError,
    adopt_store,
    apply_confirmed_decision,
    apply_decision,
    apply_delivery_observations,
    canonical_hash,
    close_delivery_attempt,
    delete_owner_store,
    delivery_session_content_hash,
    doctor_store,
    init_store,
    open_delivery_attempt,
    pending_delivery_attempt,
    cycle_sessions,
    read_current_plan,
    set_baseline,
    snapshot_store,
    status_store,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"


def load(name: str) -> dict:
    value = json.loads((EXAMPLE / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{name} is not a JSON object")
    return value


class CoachLoopStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.context = load("coach-context-day-4.json")
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")

    def test_init_doctor_and_status_persist_current_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            initialized = init_store(state_dir, self.before)
            self.assertEqual("initialized", initialized["status"])
            self.assertEqual(0, initialized["event_count"])
            self.assertEqual(0o700, os.stat(state_dir).st_mode & 0o777)

            doctor = doctor_store(state_dir)
            self.assertEqual([], doctor["errors"])
            self.assertEqual("passed", doctor["status"])

            # Pinned to a date inside the fixture's week: "next" is answered relative to
            # a real day, so an unpinned assertion would stop meaning anything once the
            # fixture's week is in the past.
            status = status_store(state_dir, today="2026-08-13")
            self.assertEqual(1, status["current_version"])
            self.assertEqual("run-quality-01", status["next_session"]["session_id"])
            self.assertEqual(self.before, status["current_plan"])

    def test_next_never_answers_with_a_day_that_has_passed(self):
        # Read from the live store on 2026-08-13: "next" was 8/12's strength session,
        # because the earliest unresolved session was being read as the coming one. A
        # session whose day has passed cannot be next -- whether the athlete trained it,
        # skipped it, or simply has not said, all of which are ordinary states.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)

            status = status_store(state_dir, today="2026-08-14")

            self.assertEqual("2026-08-14", status["as_of_date"])
            self.assertEqual("strength-upper-01", status["next_session"]["session_id"])
            self.assertEqual(
                ["run-quality-01"],
                [s["session_id"] for s in status["elapsed_without_outcome"]],
            )

    def test_a_week_entirely_in_the_past_has_no_next_session(self):
        # None is the honest answer, and the sessions are still reported rather than
        # dropped: the coach decides whether any of them reshapes the next week.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)

            status = status_store(state_dir, today="2026-09-01")

            self.assertIsNone(status["next_session"])
            self.assertEqual(4, len(status["elapsed_without_outcome"]))

    def test_a_moved_or_replaced_session_can_still_be_next(self):
        # Both are actionable work the athlete is meant to do. Reading only "planned"
        # dropped exactly the session a plan change had just touched.
        for status_value in ("moved", "replaced"):
            with self.subTest(match_status=status_value):
                plan = copy.deepcopy(self.before)
                session = next(
                    s for s in plan["week"]["sessions"] if s["session_id"] == "run-quality-01"
                )
                session["match_status"] = status_value
                with tempfile.TemporaryDirectory() as temporary:
                    state_dir = Path(temporary) / "coach-state"
                    init_store(state_dir, plan)

                    status = status_store(state_dir, today="2026-08-13")

                    self.assertEqual("run-quality-01", status["next_session"]["session_id"])

    def _rolled_over_store(self, state_dir: Path) -> None:
        """Init the example week, then roll the week forward so last week's sessions
        exist only in the commit chain -- the shape that makes the record necessary."""
        init_store(state_dir, self.before)
        next_week = copy.deepcopy(
            next(s for s in self.before["week"]["sessions"] if s["session_id"] == "run-long-01")
        )
        next_week.update({"session_id": "run-quality-week-2", "scheduled_date": "2026-08-20"})
        after = copy.deepcopy(self.before)
        after["version"] += 1
        after["week"].update(
            {
                "start": "2026-08-17",
                "intent": "Continue the cycle with the next threshold anchor",
                "sessions": [next_week],
            }
        )
        # The week that was outlined is now the precise one, so it leaves the outlook
        # -- which is what a review does when it rolls the week forward (issue #61).
        after["cycle"]["outlook"] = after["cycle"]["outlook"][1:]
        event = copy.deepcopy(self.event)
        event.update({"mode": "plan_week", "action": "adjust", "session_id": None})
        apply_decision(state_dir, context=self.context, after=after, event=event)

    def test_last_weeks_sessions_survive_the_week_rolling_forward(self):
        # The plan holds one week. Without reading the chain the record resets every
        # Monday: neither "too many missed" nor "this prescription keeps not finishing"
        # can be asked, because the prescription itself is gone.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            self._rolled_over_store(state_dir)

            record = cycle_sessions(state_dir, since="2026-08-10", before="2026-08-17")

            self.assertEqual(
                [
                    "strength-full-01",
                    "run-easy-01",
                    "mobility-01",
                    "run-quality-01",
                    "strength-upper-01",
                    "run-long-01",
                ],
                [s["session_id"] for s in record],
            )
            # Completed work stays in: this is what the cycle prescribed, not a list of
            # failures, and "did it get finished" is read from what came back for it.
            self.assertEqual(
                "completed",
                next(s for s in record if s["session_id"] == "strength-full-01")["match_status"],
            )
            # The prescription is the reason to read the chain at all.
            self.assertEqual(
                "Easy run 8公里 配速 6:30-7:00/km",
                next(s for s in record if s["session_id"] == "run-easy-01")["prescription"],
            )
            # The rest day and a session outside the window never appear.
            self.assertNotIn("rest-01", [s["session_id"] for s in record])
            self.assertNotIn("run-quality-week-2", [s["session_id"] for s in record])

    def test_a_session_rewritten_out_of_its_own_week_is_not_a_miss(self):
        # Read from the live store: a session sat in the early versions, was rewritten out
        # of the week while its day was still ahead, and then surfaced as missed. The plan
        # changed its mind and the athlete was never asked -- reporting that as their miss
        # reports the coach's decisions as the athlete's.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            rewritten = copy.deepcopy(self.before)
            rewritten["version"] += 1
            rewritten["week"]["sessions"] = [
                session for session in rewritten["week"]["sessions"]
                if session["session_id"] != "strength-upper-01"
            ]
            event = copy.deepcopy(self.event)
            event.update({"mode": "plan_week", "action": "adjust", "session_id": None})
            apply_decision(state_dir, context=self.context, after=rewritten, event=event)

            reported = [
                s["session_id"]
                for s in cycle_sessions(state_dir, since="2026-08-10", before="2026-08-20")
            ]

            self.assertNotIn("strength-upper-01", reported)
            # The week it was dropped from still covered its day; the ones that survived
            # into the record are untouched by that.
            self.assertEqual(
                [
                    "strength-full-01",
                    "run-easy-01",
                    "mobility-01",
                    "run-quality-01",
                    "run-long-01",
                ],
                reported,
            )

    def test_a_session_carried_into_the_new_week_appears_once_at_its_latest_date(self):
        # Same session_id, later date: one piece of work moved, not two entries. Reading
        # the last state each session was written with is what makes that true.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            carried = copy.deepcopy(
                next(
                    s for s in self.before["week"]["sessions"]
                    if s["session_id"] == "run-quality-01"
                )
            )
            carried.update({"scheduled_date": "2026-08-20", "match_status": "moved"})
            moved = copy.deepcopy(self.before)
            moved["version"] += 1
            # The week that was outlined becomes the precise one and leaves the outlook.
            moved["cycle"]["outlook"] = moved["cycle"]["outlook"][1:]
            moved["week"].update(
                {
                    "start": "2026-08-17",
                    "intent": "Carry the missed threshold anchor into the new week",
                    "sessions": [carried],
                }
            )
            event = copy.deepcopy(self.event)
            event.update({"mode": "plan_week", "action": "adjust", "session_id": None})
            apply_decision(state_dir, context=self.context, after=moved, event=event)

            record = cycle_sessions(state_dir, since="2026-08-10", before="2026-08-17")

            self.assertNotIn("run-quality-01", [s["session_id"] for s in record])

    def test_a_week_rolled_forward_by_the_projection_stays_in_the_cycle_record(self):
        """The roll, end to end: project it, commit it, then read the cycle back.

        ``plan_change`` decides what leaves the week and ``cycle_sessions`` decides what
        the cycle remembers, and the two agree only because the version that drops a
        session has already moved past its date. Projecting and validating proves the
        first half; nothing but a real second commit proves they meet.
        """
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)

            projection = project_change_request(
                self.before,
                {
                    "summary": "上一週練完了，換下一週",
                    "reason_codes": ["schedule_or_equipment_changed"],
                    "evidence": [{"field": "constraints", "observation": "一週結束"}],
                    "goal_effect": {"week": "新的一週", "cycle": "方向不變"},
                    "next_review_condition": "下週結束時再看",
                    "week": {"start": "2026-08-17", "intent": "下一週"},
                    "cycle": {"outlook": self.before["cycle"]["outlook"][1:]},
                    "sessions": [{
                        "operation": "add",
                        "scheduled_date": "2026-08-18",
                        "sport": "running",
                        "purpose": "下一週的輕鬆跑",
                        "adaptation": "aerobic_base",
                        "cost": "easy",
                        "priority": "flexible",
                        "body_stress": "lower",
                        "planned_minutes": 45,
                        "plan": {
                            "kind": "time_axis",
                            "name": "45 分鐘輕鬆跑",
                            "steps": [{
                                "kind": "work",
                                "name": "輕鬆跑",
                                "duration": {"kind": "time", "seconds": 2700},
                                "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 150},
                            }],
                        },
                        "fallback": {"action": "reduce", "description": "縮短，心率上限不變"},
                    }],
                },
                context=self.context,
                issued_at=dt.datetime(2026, 8, 17, 0, 0, tzinfo=dt.timezone.utc),
            )
            event = copy.deepcopy(self.event)
            event.update({"mode": "plan_week", "action": "adjust", "session_id": None})
            apply_decision(
                state_dir,
                context=self.context,
                after=projection["after_plan"],
                event=event,
            )

            reported = [
                session["session_id"]
                for session in cycle_sessions(state_dir, since="2026-08-10", before="2026-08-24")
            ]

            # Every session of the week that rolled past is still what the cycle planned,
            # the three completed ones and the three that were never done alike -- the
            # roll handed them to history, it did not erase them. Rest is not work, so
            # `rest-01` is the one that does not appear.
            self.assertEqual(
                [
                    "strength-full-01",
                    "run-easy-01",
                    "mobility-01",
                    "run-quality-01",
                    "strength-upper-01",
                    "run-long-01",
                    "running-2026-08-18",
                ],
                reported,
            )

    def test_apply_decision_records_plan_and_event_but_not_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            result = apply_decision(
                state_dir,
                context=self.context,
                after=self.after,
                event=self.event,
            )
            self.assertEqual("passed", result["status"])
            self.assertFalse(result["idempotent_replay"])
            self.assertEqual(2, result["current_version"])
            self.assertEqual(1, result["event_count"])

            status = status_store(state_dir)
            self.assertEqual(self.after, status["current_plan"])
            names = {path.name for path in state_dir.rglob("*")}
            self.assertNotIn("context.json", names)
            self.assertEqual(1, sum(name == "event.json" for name in names))

    def test_verified_delivery_is_the_only_execution_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            plan = copy.deepcopy(self.before)
            session = next(
                item for item in plan["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )
            session["execution"]["publish_supported"] = True
            init_store(state_dir, plan)
            observation = {
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "session_id": "run-quality-01",
                "session_content_hash": delivery_session_content_hash(session),
                "external_id": "128500001",
                "proposal_hash": "a" * 64,
                "readback_hash": "b" * 64,
                "verified_at": "2026-08-12T10:00:00+00:00",
            }

            result = apply_delivery_observations(state_dir, observations=[observation])

            self.assertFalse(result["idempotent_replay"])
            self.assertEqual("intervals_accepted", result["delivery_state"])
            current = status_store(state_dir)["current_plan"]
            delivered = next(
                item for item in current["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )
            self.assertEqual("128500001", delivered["execution"]["external_id"])
            self.assertEqual("intervals_accepted", delivered["execution"]["delivery_state"])
            self.assertEqual("passed", doctor_store(state_dir)["status"])

            replay = apply_delivery_observations(state_dir, observations=[observation])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(2, len(list((state_dir / "commits").iterdir())))

    def test_delivery_observation_cannot_target_an_unpublishable_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            observation = {
                "plan_id": self.before["plan_id"],
                "plan_version": self.before["version"],
                "session_id": "run-quality-01",
                "session_content_hash": delivery_session_content_hash(
                    next(
                        item for item in self.before["week"]["sessions"]
                        if item["session_id"] == "run-quality-01"
                    )
                ),
                "external_id": "128500001",
                "proposal_hash": "a" * 64,
                "readback_hash": "b" * 64,
                "verified_at": "2026-08-12T10:00:00+00:00",
            }
            with self.assertRaisesRegex(StateStoreError, "failed validation"):
                apply_delivery_observations(state_dir, observations=[observation])
            self.assertEqual(1, len(list((state_dir / "commits").iterdir())))

    def test_coaching_commit_after_readback_refuses_stale_delivery_state_advancement(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            plan = copy.deepcopy(self.before)
            session = next(
                item for item in plan["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )
            session["execution"]["publish_supported"] = True
            init_store(state_dir, plan)
            observation = {
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "session_id": session["session_id"],
                "session_content_hash": delivery_session_content_hash(session),
                "external_id": "128500001",
                "proposal_hash": "a" * 64,
                "readback_hash": "b" * 64,
                "verified_at": "2026-08-12T10:00:00+00:00",
            }

            after = copy.deepcopy(plan)
            after["version"] = 2
            changed = next(
                item for item in after["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )
            changed["planned_minutes"] = 52
            changed["plan"]["name"] = "4x800m threshold"
            changed["plan"]["steps"][1]["repetitions"] = 4
            changed["prescription"] = render_prescription(changed["plan"])
            event = copy.deepcopy(self.event)
            event.update(
                {
                    "event_id": "coaching-won-the-delivery-race",
                    "action": "reduce",
                    "plan_version_before": 1,
                    "plan_version_after": 2,
                }
            )
            apply_decision(state_dir, context=self.context, after=after, event=event)

            with self.assertRaisesRegex(StateStoreError, "version is stale"):
                apply_delivery_observations(state_dir, observations=[observation])
            current = status_store(state_dir)["current_plan"]
            self.assertEqual(2, current["version"])
            self.assertEqual("not_published", changed["execution"]["delivery_state"])
            self.assertEqual("not_published", next(
                item for item in current["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )["execution"]["delivery_state"])
            self.assertEqual(2, len(list((state_dir / "commits").iterdir())))

    def test_exact_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            apply_decision(state_dir, context=self.context, after=self.after, event=self.event)
            replay = apply_decision(
                state_dir,
                context=self.context,
                after=self.after,
                event=self.event,
            )
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(1, replay["event_count"])
            self.assertEqual(2, len(list((state_dir / "commits").iterdir())))

    def test_conflicting_event_replay_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            apply_decision(state_dir, context=self.context, after=self.after, event=self.event)
            changed_context = copy.deepcopy(self.context)
            changed_context["context_id"] = "different-context"
            with self.assertRaisesRegex(StateStoreError, "different content"):
                apply_decision(
                    state_dir,
                    context=changed_context,
                    after=self.after,
                    event=self.event,
                )

    def test_stale_before_version_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            apply_decision(state_dir, context=self.context, after=self.after, event=self.event)
            stale_event = copy.deepcopy(self.event)
            stale_event["event_id"] = "fixture-stale-event"
            with self.assertRaisesRegex(StateStoreError, "not the current PlanState"):
                apply_decision(
                    state_dir,
                    context=self.context,
                    after=self.after,
                    event=stale_event,
                )

    def test_invalid_daily_change_is_not_persisted(self):
        # The harmful case that must never commit: an explicit positive red flag
        # with a normal training action. Stale evidence stopped being the trigger
        # in #43 -- see the false-positive control below.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            flagged_context = copy.deepcopy(self.context)
            flagged_context["constraints"]["red_flags"]["chest_pain"] = True
            with self.assertRaisesRegex(StateStoreError, "failed validation"):
                apply_decision(
                    state_dir,
                    context=flagged_context,
                    after=self.after,
                    event=copy.deepcopy(self.event),
                )
            self.assertEqual(1, len(list((state_dir / "commits").iterdir())))

    def test_stale_evidence_daily_change_is_persisted_with_unknowns(self):
        # #43 false-positive control at the store boundary: non-fresh optional
        # evidence no longer keeps a legitimate daily decision from committing;
        # the gap travels with the event as a preserved unknown instead.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            stale_context = copy.deepcopy(self.context)
            stale_context["freshness"]["activities"] = "stale"
            stale_context["unknowns"] = ["activities_after_last_observation"]
            stale_event = copy.deepcopy(self.event)
            stale_event["unknowns"] = ["activities_after_last_observation"]
            result = apply_decision(
                state_dir,
                context=stale_context,
                after=self.after,
                event=stale_event,
            )
            self.assertEqual(2, result["current_version"])
            self.assertEqual(1, result["event_count"])

    def test_keep_event_is_recorded_without_incrementing_plan_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            keep = copy.deepcopy(self.event)
            keep.update(
                {
                    "event_id": "fixture-keep-event",
                    "action": "keep",
                    "plan_version_after": 1,
                    "reason_codes": ["plan_kept_no_material_change"],
                    "change": {
                        "before": "Current plan selected",
                        "after": "Current plan selected",
                        "summary": "Keep the selected session and weekly plan",
                    },
                }
            )
            result = apply_decision(
                state_dir,
                context=self.context,
                after=self.before,
                event=keep,
            )
            self.assertEqual(1, result["current_version"])
            self.assertEqual(1, result["event_count"])

    def test_doctor_detects_tampered_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            plan_path = next((state_dir / "commits").glob("*/plan.json"))
            tampered = json.loads(plan_path.read_text(encoding="utf-8"))
            tampered["week"]["intent"] = "tampered"
            plan_path.write_text(json.dumps(tampered), encoding="utf-8")
            report = doctor_store(state_dir)
            self.assertEqual("blocked", report["status"])
            self.assertTrue(any("integrity hash mismatch" in error for error in report["errors"]))

    def test_doctor_detects_tampered_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            receipt_path = next((state_dir / "commits").glob("*/receipt.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["context_hash"] = "tampered"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            report = doctor_store(state_dir)
            self.assertEqual("blocked", report["status"])
            self.assertTrue(any("receipt integrity hash mismatch" in error for error in report["errors"]))

    def test_doctor_detects_incomplete_pending_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            (state_dir / "commits" / ".pending-interrupted").mkdir()
            report = doctor_store(state_dir)
            self.assertEqual("blocked", report["status"])
            self.assertIn("store contains an incomplete pending commit", report["errors"])

    def test_repository_local_state_is_refused_before_write(self):
        with self.assertRaisesRegex(StateStoreError, "outside the repository"):
            init_store(ROOT / "private-test-state", self.before)
        self.assertFalse((ROOT / "private-test-state").exists())

    def test_cli_state_survives_separate_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            initialize = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "garmin_coach_loop.cli",
                    "init-store",
                    "--state-dir",
                    str(state_dir),
                    "--plan",
                    str(EXAMPLE / "plan-state-v1.json"),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, initialize.returncode, initialize.stderr)
            status = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "garmin_coach_loop.cli",
                    "status",
                    "--state-dir",
                    str(state_dir),
                    "--today",
                    "2026-08-13",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, status.returncode, status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(1, payload["current_version"])
            self.assertEqual("run-quality-01", payload["next_session"]["session_id"])


class ConfirmedDecisionTests(unittest.TestCase):
    """What a write gains when an athlete confirmed it somewhere else, some time ago.

    ``apply_decision`` is called by whoever built its arguments, in the same process, from
    a plan it read moments earlier. ``apply_confirmed_decision`` is called on behalf of a
    preview that may be a quarter of an hour old, by a caller whose every read of the
    store happened outside its lock. These tests are about the difference.
    """

    def setUp(self):
        self.context = load("coach-context-day-4.json")
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name).resolve() / "coach-state"
        init_store(self.state_dir, self.before)
        self.addCleanup(self._tmp.cleanup)

    def claims(self, **overrides) -> dict:
        """The claims a verified proposal for this decision carries."""
        claims = {
            "kind": "decision",
            "plan_id": self.before["plan_id"],
            "base_version": self.before["version"],
            "before_hash": canonical_hash(self.before),
            "after_hash": canonical_hash(self.after),
            "event_hash": canonical_hash(self.event),
            "context_hash": canonical_hash(self.context),
        }
        claims.update(overrides)
        return claims

    def commits(self) -> int:
        return len(list((self.state_dir / "commits").iterdir()))

    def test_a_confirmed_decision_commits_exactly_as_an_unconfirmed_one_does(self):
        result = apply_confirmed_decision(
            self.state_dir,
            proposal_claims=self.claims(),
            context=self.context,
            after=self.after,
            event=self.event,
        )

        self.assertEqual("passed", result["status"])
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(2, result["current_version"])
        self.assertEqual(self.after, status_store(self.state_dir)["current_plan"])

    def test_a_different_plan_at_the_same_version_refuses_the_confirmed_write(self):
        """The fork ``base_version`` cannot see, and the projections do not cover.

        A restore or an adopt can put a plan at version 1 that is not the plan that was
        at version 1 when the preview was computed. Everything else about the
        confirmation still matches -- the plan id, the version, the candidate artifacts
        it projects to -- so the only claim that can refuse it is the one covering the
        whole stored plan.
        """
        forked = copy.deepcopy(self.before)
        for session in forked["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session["purpose"] = "A purpose this athlete was never shown"

        with self.assertRaisesRegex(StateStoreError, "stored plan was replaced"):
            apply_confirmed_decision(
                self.state_dir,
                proposal_claims=self.claims(before_hash=canonical_hash(forked)),
                context=self.context,
                after=self.after,
                event=self.event,
            )

        self.assertEqual(1, self.commits())
        self.assertEqual(1, status_store(self.state_dir)["current_plan"]["version"])

    def test_a_plan_that_moved_on_says_so_rather_than_naming_a_replacement(self):
        """The other branch of the same refusal, and the reason it is a branch.

        A plan that advanced normally and a plan that was swapped underneath one send
        whoever reads the message to different places, so the two cases say different
        things.
        """
        apply_decision(
            self.state_dir, context=self.context, after=self.after, event=self.event
        )
        second = copy.deepcopy(self.event)
        second.update({"event_id": "a-later-decision", "plan_version_before": 2})

        with self.assertRaisesRegex(StateStoreError, "stored plan is now version 2"):
            apply_confirmed_decision(
                self.state_dir,
                proposal_claims=self.claims(),
                context=self.context,
                after=self.after,
                event=second,
            )

    def test_the_stored_plan_is_compared_only_after_the_lock_is_held(self):
        """Ordering, demonstrated rather than described.

        A store already locked answers with the lock, even for a confirmation whose plan
        claim could not possibly match -- which it could only do if the comparison sits
        behind the lock acquisition. Unlocking the same store and repeating the same call
        then produces the plan refusal, so the first answer was the order and not the
        check being absent.
        """
        forked_claims = self.claims(before_hash=canonical_hash(self.after))
        lock_path = self.state_dir / ".lock"
        lock_path.write_text("pid=someone-else\n", encoding="utf-8")

        def confirm():
            return apply_confirmed_decision(
                self.state_dir,
                proposal_claims=forked_claims,
                context=self.context,
                after=self.after,
                event=self.event,
            )

        with self.assertRaisesRegex(StateStoreError, "locked by another operation"):
            confirm()

        lock_path.unlink()
        with self.assertRaisesRegex(StateStoreError, "stored plan was replaced"):
            confirm()

        self.assertEqual(1, self.commits())

    def test_the_receipt_records_the_context_hash_the_confirmation_named(self):
        """Item the replay path depends on: one value, stated once, compared twice.

        The confirmation says which evidence it was issued for, and a retried apply is
        recognised by comparing that same claim against this receipt. Recomputing the
        hash here from the context object would put a second derivation between them, so
        this passes a claim the context object could never produce and requires the
        receipt to carry it.
        """
        named = "0" * 64

        apply_confirmed_decision(
            self.state_dir,
            proposal_claims=self.claims(context_hash=named),
            context=self.context,
            after=self.after,
            event=self.event,
        )

        receipt = read_current_plan(self.state_dir)["receipt"]
        self.assertEqual(named, receipt["context_hash"])
        self.assertNotEqual(canonical_hash(self.context), receipt["context_hash"])

    def test_a_replay_is_recognised_by_the_hash_the_confirmation_carried(self):
        """And the consequence of the test above: the two comparisons meet."""
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = apply_confirmed_decision(
                    self.state_dir,
                    proposal_claims=self.claims(),
                    context=self.context,
                    after=self.after,
                    event=self.event,
                )
                self.assertEqual(bool(attempt), result["idempotent_replay"])
        self.assertEqual(2, self.commits())

    def test_a_confirmation_missing_either_hash_writes_nothing(self):
        for claim in ("before_hash", "context_hash"):
            with self.subTest(claim=claim):
                incomplete = self.claims()
                incomplete.pop(claim)

                with self.assertRaisesRegex(StateStoreError, f"must carry its {claim}"):
                    apply_confirmed_decision(
                        self.state_dir,
                        proposal_claims=incomplete,
                        context=self.context,
                        after=self.after,
                        event=self.event,
                    )

                self.assertEqual(1, self.commits())


class SetBaselineModeTests(unittest.TestCase):
    """Judging the baseline travels with prescribing (issue #32), so `set-baseline`
    takes the mode the conversation is already in -- planning a week, reviewing one --
    instead of demanding a `plan_cycle` event to move one number. The shared
    mode/action rules still apply unchanged; the wrapper just stopped adding its own.
    """

    def setUp(self):
        self.context = load("coach-context-day-4.json")
        self.before = load("plan-state-v1.json")
        self.event = load("decision-event-day-4.json")

    def _baseline_event(self, *, mode: str, action: str, event_id: str) -> dict:
        event = copy.deepcopy(self.event)
        event.update(
            {
                "event_id": event_id,
                "mode": mode,
                "action": action,
                "session_id": None,
                "reason_codes": ["actual_load_above_plan"],
                "change": {
                    "before": "longest_recent_run_km 12.0",
                    "after": "longest_recent_run_km 13.2",
                    "summary": "The 13.2 km long run moved the longest-run baseline",
                },
            }
        )
        return event

    def _moved_baseline(self) -> dict:
        baseline = copy.deepcopy(self.before["athlete_baseline"])
        baseline["longest_recent_run_km"] = 13.2
        return baseline

    def test_a_week_plan_that_found_its_anchor_stale_updates_it_in_its_own_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            report = set_baseline(
                state_dir,
                context=self.context,
                baseline=self._moved_baseline(),
                event=self._baseline_event(
                    mode="plan_week", action="adjust", event_id="baseline-week-001"
                ),
            )
            self.assertEqual("passed", report["status"])
            self.assertEqual(2, report["current_version"])
            self.assertEqual(
                13.2,
                status_store(state_dir)["current_plan"]["athlete_baseline"][
                    "longest_recent_run_km"
                ],
            )

    def test_a_review_reaches_the_baseline_through_the_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            report = set_baseline(
                state_dir,
                context=self.context,
                baseline=self._moved_baseline(),
                event=self._baseline_event(
                    mode="review_week", action="adjust", event_id="baseline-review-001"
                ),
            )
            self.assertEqual("passed", report["status"])
            self.assertEqual(2, report["current_version"])

    def test_the_shared_mode_action_rules_still_hold(self):
        """The gate moved, it did not vanish: an action its mode does not allow is
        refused by the same validation every other decision passes through."""
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "coach-state"
            init_store(state_dir, self.before)
            with self.assertRaises(StateStoreError):
                set_baseline(
                    state_dir,
                    context=self.context,
                    baseline=self._moved_baseline(),
                    event=self._baseline_event(
                        mode="revisit_today", action="adjust", event_id="baseline-today-001"
                    ),
                )


class StatusStoreTimezoneTests(unittest.TestCase):
    """`status`'s athlete-local date boundary (archived issue #112).

    `--today` still wins outright when given, but when it is omitted the date must come
    from an explicit IANA timezone -- the same resolution every context-building command
    already applies to `as_of` -- never from the server's own clock or a single
    hard-coded zone.
    """

    def setUp(self):
        self.plan = load("plan-state-v1.json")

    def _store(self, temporary: str) -> Path:
        state_dir = Path(temporary) / "coach-state"
        init_store(state_dir, self.plan)
        return state_dir

    def test_default_timezone_matches_explicit_asia_taipei(self):
        # DEFAULT_TIMEZONE is a documented backward-compatible default, not a separate
        # code path: omitting `timezone` must answer exactly as `Asia/Taipei` does.
        now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._store(temporary)
            default = status_store(state_dir, now=now)
            explicit = status_store(state_dir, timezone="Asia/Taipei", now=now)
            self.assertEqual("2026-08-14", default["as_of_date"])
            self.assertEqual(default["as_of_date"], explicit["as_of_date"])
            self.assertEqual(default["next_session"], explicit["next_session"])

    def test_taipei_and_utc_disagree_on_next_session_at_the_same_instant(self):
        # 2026-08-13T18:00:00Z is already 2026-08-14 02:00 in Taipei (UTC+8) but still
        # 2026-08-13 in UTC -- the exact shape of archived issue #112: one instant, two different
        # "today"s, and (before this fix) only one of them reachable from `status`.
        now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._store(temporary)

            taipei = status_store(state_dir, timezone="Asia/Taipei", now=now)
            utc = status_store(state_dir, timezone="UTC", now=now)

            self.assertEqual("2026-08-14", taipei["as_of_date"])
            self.assertEqual("strength-upper-01", taipei["next_session"]["session_id"])
            self.assertEqual(
                ["run-quality-01"],
                [s["session_id"] for s in taipei["elapsed_without_outcome"]],
            )

            self.assertEqual("2026-08-13", utc["as_of_date"])
            self.assertEqual("run-quality-01", utc["next_session"]["session_id"])
            self.assertEqual([], utc["elapsed_without_outcome"])

    def test_utc_and_new_york_disagree_across_the_utc_midnight_boundary(self):
        # 2026-08-14T00:30:00Z has just crossed UTC midnight, but America/New_York
        # (UTC-4 in August) is still 2026-08-13 20:30 -- the other side of the same
        # boundary: here UTC has rolled over and a zone behind it has not.
        now = dt.datetime(2026, 8, 14, 0, 30, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._store(temporary)

            utc = status_store(state_dir, timezone="UTC", now=now)
            new_york = status_store(state_dir, timezone="America/New_York", now=now)

            self.assertEqual("2026-08-14", utc["as_of_date"])
            self.assertEqual("strength-upper-01", utc["next_session"]["session_id"])

            self.assertEqual("2026-08-13", new_york["as_of_date"])
            self.assertEqual("run-quality-01", new_york["next_session"]["session_id"])

    def test_explicit_today_overrides_timezone_and_needs_no_valid_zone(self):
        # An already-resolved date is authoritative; a bogus timezone alongside it is
        # simply never consulted -- the same override contract context-building commands
        # already give an explicit `--as-of`.
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._store(temporary)
            status = status_store(state_dir, today="2026-08-14", timezone="Not/AZone")
            self.assertEqual("2026-08-14", status["as_of_date"])
            self.assertEqual("strength-upper-01", status["next_session"]["session_id"])

    def test_unknown_timezone_fails_with_one_actionable_error_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._store(temporary)
            with self.assertRaisesRegex(StateStoreError, "unknown timezone: 'Nowhere/Nothing'"):
                status_store(state_dir, timezone="Nowhere/Nothing")


class DeleteOwnerStoreTests(unittest.TestCase):
    """Issue #6's operator deletion, store half."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.plan = load("plan-state-v1.json")

    def _snapshot_tree(self, directory: Path) -> dict[str, str]:
        return {
            str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    def test_an_owner_with_no_directory_and_no_snapshot_is_reported_absent(self):
        never_initialized = self.root / "owners" / "11111111-2222-3333-4444-555555555555"

        preview = delete_owner_store(never_initialized, confirm=False)
        confirmed = delete_owner_store(never_initialized, confirm=True)

        for report in (preview, confirmed):
            self.assertEqual("absent", report["status"])
            self.assertFalse(report["state_dir_existed"])
            self.assertFalse(report["snapshots_dir_existed"])

    def test_dry_run_reports_what_exists_and_removes_nothing(self):
        state_dir = self.root / "owners" / "owner-a"
        init_store(state_dir, self.plan)
        before = self._snapshot_tree(state_dir)

        preview = delete_owner_store(state_dir, confirm=False)

        self.assertEqual("preview", preview["status"])
        self.assertTrue(preview["state_dir_existed"])
        self.assertFalse(preview["state_dir_removed"])
        self.assertFalse(preview["state_dir_is_link"])
        self.assertTrue(state_dir.is_dir())
        self.assertEqual(before, self._snapshot_tree(state_dir))

    def test_confirm_removes_the_store_directory(self):
        state_dir = self.root / "owners" / "owner-a"
        init_store(state_dir, self.plan)

        result = delete_owner_store(state_dir, confirm=True)

        self.assertEqual("deleted", result["status"])
        self.assertTrue(result["state_dir_removed"])
        self.assertFalse(state_dir.exists())

    def test_confirm_also_removes_the_automatic_pre_migration_snapshot(self):
        # apply_decision/apply_delivery_observations take exactly this snapshot,
        # sibling to the store, before a writer-contract upgrade -- leaving it behind
        # would keep a full verified copy of the same owner's history right beside a
        # directory this command just claimed to have deleted.
        state_dir = self.root / "owners" / "owner-a"
        init_store(state_dir, self.plan)
        snapshot = snapshot_store(state_dir, reason="pre-migration")
        snapshots_dir = Path(snapshot["snapshot_dir"]).parent
        self.assertTrue(snapshots_dir.exists())

        result = delete_owner_store(state_dir, confirm=True)

        self.assertTrue(result["snapshots_dir_removed"])
        self.assertFalse(state_dir.exists())
        self.assertFalse(snapshots_dir.exists())

    def test_deleting_one_owner_leaves_a_sibling_owner_byte_for_byte_intact(self):
        first = self.root / "owners" / "owner-a"
        second = self.root / "owners" / "owner-b"
        init_store(first, self.plan)
        init_store(second, self.plan)
        before = self._snapshot_tree(second)

        result = delete_owner_store(first, confirm=True)

        self.assertTrue(result["state_dir_removed"])
        self.assertFalse(first.exists())
        self.assertTrue(second.is_dir())
        self.assertEqual(before, self._snapshot_tree(second))

    def test_a_delivery_in_flight_blocks_preview_and_confirm_alike(self):
        state_dir = self.root / "owners" / "owner-a"
        init_store(state_dir, self.plan)
        session_id = self.plan["week"]["sessions"][0]["session_id"]
        attempt = open_delivery_attempt(
            state_dir,
            kind="delivery",
            plan_id=self.plan["plan_id"],
            plan_version=self.plan["version"],
            proposal_hash="deadbeef",
            operations=[
                {
                    "session_id": session_id,
                    "operation": "upsert",
                    "owned_external_id": "gcl:test:owned",
                    "scheduled_date": "2026-08-20",
                }
            ],
        )

        with self.assertRaises(StateStoreError) as preview_blocked:
            delete_owner_store(state_dir, confirm=False)
        self.assertIn(attempt["attempt_id"], str(preview_blocked.exception))

        with self.assertRaises(StateStoreError) as confirm_blocked:
            delete_owner_store(state_dir, confirm=True)
        self.assertIn(attempt["attempt_id"], str(confirm_blocked.exception))

        self.assertTrue(state_dir.is_dir())
        self.assertTrue((state_dir / "store.json").exists())

    def test_deletion_resumes_once_the_delivery_reservation_is_settled(self):
        state_dir = self.root / "owners" / "owner-a"
        init_store(state_dir, self.plan)
        session_id = self.plan["week"]["sessions"][0]["session_id"]
        open_delivery_attempt(
            state_dir,
            kind="delivery",
            plan_id=self.plan["plan_id"],
            plan_version=self.plan["version"],
            proposal_hash="deadbeef",
            operations=[
                {
                    "session_id": session_id,
                    "operation": "upsert",
                    "owned_external_id": "gcl:test:owned",
                    "scheduled_date": "2026-08-20",
                }
            ],
        )
        close_delivery_attempt(state_dir)

        result = delete_owner_store(state_dir, confirm=True)

        self.assertEqual("deleted", result["status"])
        self.assertFalse(state_dir.exists())

    def test_a_linked_owner_directory_is_previewed_without_touching_the_source(self):
        source = self.root / "source-store"
        init_store(source, self.plan)
        linked = self.root / "owners" / "owner-a"
        adopt_store(source, linked, mode="link", confirm=True)
        before = self._snapshot_tree(source)

        preview = delete_owner_store(linked, confirm=False)

        self.assertEqual("preview", preview["status"])
        self.assertTrue(preview["state_dir_is_link"])
        self.assertTrue(linked.is_symlink())
        self.assertEqual(before, self._snapshot_tree(source))

    def test_confirmed_deletion_of_a_linked_owner_removes_only_the_link(self):
        source = self.root / "source-store"
        init_store(source, self.plan)
        linked = self.root / "owners" / "owner-a"
        adopt_store(source, linked, mode="link", confirm=True)
        before = self._snapshot_tree(source)

        result = delete_owner_store(linked, confirm=True)

        self.assertEqual("deleted", result["status"])
        self.assertTrue(result["state_dir_is_link"])
        self.assertFalse(linked.exists())
        self.assertFalse(linked.is_symlink())
        # The whole point: the shared store this link pointed at is untouched, byte for
        # byte, including for whichever other path (a CLI operator's own --state-dir,
        # most plausibly) still reaches it directly.
        self.assertTrue(source.is_dir())
        self.assertEqual(before, self._snapshot_tree(source))

    def test_a_delivery_in_flight_on_the_linked_source_does_not_block_removing_the_link(self):
        # Removing a link changes nothing Intervals can observe -- the reservation, and
        # the store it protects, live entirely on the source side and are untouched.
        source = self.root / "source-store"
        init_store(source, self.plan)
        linked = self.root / "owners" / "owner-a"
        adopt_store(source, linked, mode="link", confirm=True)
        session_id = self.plan["week"]["sessions"][0]["session_id"]
        attempt = open_delivery_attempt(
            source,
            kind="delivery",
            plan_id=self.plan["plan_id"],
            plan_version=self.plan["version"],
            proposal_hash="deadbeef",
            operations=[
                {
                    "session_id": session_id,
                    "operation": "upsert",
                    "owned_external_id": "gcl:test:owned",
                    "scheduled_date": "2026-08-20",
                }
            ],
        )

        result = delete_owner_store(linked, confirm=True)

        self.assertEqual("deleted", result["status"])
        self.assertFalse(linked.exists())
        self.assertTrue(source.is_dir())
        # The reservation itself is untouched -- reachable from the source path exactly
        # as it was before the link was removed.
        still_open = pending_delivery_attempt(source)
        self.assertIsNotNone(still_open)
        self.assertEqual(attempt["attempt_id"], still_open["attempt_id"])


if __name__ == "__main__":
    unittest.main()
