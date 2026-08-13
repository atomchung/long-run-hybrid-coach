from __future__ import annotations

import copy
import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop.cli import main as cli_main
from garmin_coach_loop.delivery import (
    DeliveryError,
    IntervalsTransport,
    approve_delivery_proposal,
    prepare_delivery_proposal,
    publish_delivery,
)
from garmin_coach_loop.source_intervals import IntervalsCredentials
from garmin_coach_loop.store import (
    apply_delivery_observations,
    canonical_hash,
    doctor_store,
    init_store,
    status_store,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def plan_fixture() -> dict[str, Any]:
    plan = load("plan-state-v1.json")
    for session in plan["week"]["sessions"]:
        if session["session_id"] in {"run-quality-01", "run-long-01"}:
            session["execution"]["publish_supported"] = True
    return plan


def four_by_800_steps() -> list[dict[str, Any]]:
    return [
        {
            "kind": "work",
            "name": "Warm-up",
            "duration": {"kind": "time", "seconds": 600},
            "target": {"kind": "open"},
        },
        {
            "kind": "repeat",
            "repetitions": 4,
            "steps": [
                {
                    "kind": "work",
                    "name": "800 m interval",
                    "duration": {"kind": "distance", "meters": 800},
                    "target": {
                        "kind": "pace",
                        "unit": "sec_per_km",
                        "low_seconds_per_km": 320,
                        "high_seconds_per_km": 335,
                    },
                },
                {
                    "kind": "work",
                    "name": "400 m recovery",
                    "duration": {"kind": "distance", "meters": 400},
                    "target": {"kind": "open"},
                },
            ],
        },
        {
            "kind": "work",
            "name": "Cool-down",
            "duration": {"kind": "time", "seconds": 600},
            "target": {"kind": "open"},
        },
    ]


def provider_step(step: dict[str, Any]) -> dict[str, Any]:
    if step["kind"] == "repeat":
        return {
            "reps": step["repetitions"],
            "steps": [provider_step(child) for child in step["steps"]],
        }
    result: dict[str, Any] = {"text": step["name"]}
    duration = step["duration"]
    if duration["kind"] == "time":
        result["duration"] = duration["seconds"]
    else:
        result["distance"] = duration["meters"]
    target = step["target"]
    if target["kind"] == "pace":
        result["pace"] = {
            "start": target["low_seconds_per_km"],
            "end": target["high_seconds_per_km"],
            "units": "secs/km",
        }
    elif target["kind"] == "hr_ceiling":
        result["hr"] = {"start": 0, "end": target["ceiling_bpm"], "units": "bpm"}
    return result


def hr_ceiling_steps(ceiling_bpm: int = 140) -> list[dict[str, Any]]:
    return [
        {
            "kind": "work",
            "name": "Easy run",
            "duration": {"kind": "time", "seconds": 1800},
            "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": ceiling_bpm},
        },
    ]


def hr_ceiling_plan_fixture(ceiling_bpm: int = 140) -> dict[str, Any]:
    plan = plan_fixture()
    session = next(
        item for item in plan["week"]["sessions"] if item["session_id"] == "run-quality-01"
    )
    session["structured_workout"] = {
        "name": "Easy recovery run",
        "steps": hr_ceiling_steps(ceiling_bpm),
    }
    return plan


class FakeTransport:
    def __init__(self, *, events: list[dict[str, Any]] | None = None):
        self.events = list(events or [])
        self.bulk_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.readbacks: dict[str, dict[str, Any]] = {}

    def list_events(self, day: str) -> list[dict[str, Any]]:
        return copy.deepcopy([
            event for event in self.events
            if str(event.get("start_date_local", ""))[:10] == day
        ])

    def bulk_upsert(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.bulk_calls.append(copy.deepcopy(event))
        existing = next(
            (item for item in self.events if item.get("external_id") == event["external_id"]),
            None,
        )
        event_id = str(existing["id"]) if existing else str(9000 + len(self.bulk_calls))
        self.events = [item for item in self.events if item.get("external_id") != event["external_id"]]
        self.events.append({"id": event_id, **event})
        self.readbacks[event_id] = self._readback(event_id, event)
        return [{"id": event_id, **event}]

    def get_event(self, event_id: str) -> dict[str, Any]:
        self.get_calls.append(event_id)
        return copy.deepcopy(self.readbacks[event_id])

    def _readback(self, event_id: str, event: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("tests must install a proposal-aware readback")


def install_readback_builder(
    transport: FakeTransport,
    proposal: dict[str, Any] | list[dict[str, Any]],
) -> None:
    proposals = proposal if isinstance(proposal, list) else [proposal]
    by_external_id = {item["owned_external_id"]: item for item in proposals}

    def build(event_id: str, event: dict[str, Any]) -> dict[str, Any]:
        selected = by_external_id[event["external_id"]]
        return {
            "id": int(event_id),
            **event,
            "workout_doc": {
                "steps": [provider_step(step) for step in selected["workout"]["steps"]]
            },
        }

    transport._readback = build  # type: ignore[method-assign]


class DeliveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.now = dt.datetime(2026, 8, 12, 10, 0, tzinfo=dt.timezone.utc)
        self.proposal = prepare_delivery_proposal(
            self.plan, "run-quality-01", now=self.now
        )
        self.approval = approve_delivery_proposal(
            self.proposal,
            approved_by="fixture-athlete",
            approved_at=self.now,
        )

    def test_create_then_readback_returns_observation_with_internal_event_id(self):
        transport = FakeTransport()
        install_readback_builder(transport, self.proposal)

        receipt = publish_delivery(
            self.proposal,
            self.approval,
            load_current_plan=lambda: self.plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("intervals_accepted", receipt["delivery_state"])
        self.assertEqual("upserted", receipt["operation"])
        self.assertEqual("9001", receipt["observation"]["external_id"])
        self.assertEqual(self.plan["version"], receipt["observation"]["plan_version"])
        self.assertEqual(
            self.proposal["session_content_hash"],
            receipt["observation"]["session_content_hash"],
        )
        self.assertEqual(
            self.proposal["proposal_hash"], receipt["observation"]["proposal_hash"]
        )
        self.assertNotEqual(receipt["owned_external_id"], receipt["observation"]["external_id"])
        self.assertEqual(1, len(transport.bulk_calls))
        self.assertNotIn("workout_doc", transport.bulk_calls[0])
        self.assertEqual(["9001"], transport.get_calls)

    def test_existing_exact_owned_event_is_deduplicated_without_a_write(self):
        event = {
            "id": 8123,
            "external_id": self.proposal["owned_external_id"],
            "category": "WORKOUT",
            "type": "Run",
            "name": self.proposal["workout"]["name"],
            "start_date_local": "2026-08-13T00:00:00",
            "description": self.proposal["workout"]["description"],
            "workout_doc": {
                "steps": [provider_step(step) for step in self.proposal["workout"]["steps"]]
            },
        }
        transport = FakeTransport(events=[event])
        transport.readbacks["8123"] = event

        receipt = publish_delivery(
            self.proposal,
            self.approval,
            load_current_plan=lambda: self.plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("deduplicated_existing", receipt["operation"])
        self.assertEqual("8123", receipt["observation"]["external_id"])
        self.assertEqual([], transport.bulk_calls)

    def test_other_workouts_on_the_same_day_are_not_a_universal_collision(self):
        transport = FakeTransport(
            events=[{"id": 10, "external_id": "gcl:another-session", "category": "WORKOUT"}]
        )
        install_readback_builder(transport, self.proposal)

        receipt = publish_delivery(
            self.proposal,
            self.approval,
            load_current_plan=lambda: self.plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("upserted", receipt["operation"])
        self.assertEqual(1, len(transport.bulk_calls))

    def test_readback_mismatch_returns_no_accepted_receipt(self):
        transport = FakeTransport()
        install_readback_builder(transport, self.proposal)
        original = transport._readback

        def mismatch(event_id: str, event: dict[str, Any]) -> dict[str, Any]:
            readback = original(event_id, event)
            readback["workout_doc"]["steps"][1]["steps"][0]["pace"]["start"] += 30
            return readback

        transport._readback = mismatch  # type: ignore[method-assign]
        with self.assertRaisesRegex(DeliveryError, "pace target mismatch"):
            publish_delivery(
                self.proposal,
                self.approval,
                load_current_plan=lambda: self.plan,
                transport=transport,
                now=self.now,
            )

    def test_noncanonical_targets_fail_closed_before_preview(self):
        for mutation in ("percent", "unknown"):
            with self.subTest(mutation=mutation):
                plan = copy.deepcopy(self.plan)
                target = next(
                    item for item in plan["week"]["sessions"]
                    if item["session_id"] == "run-quality-01"
                )["structured_workout"]["steps"][1]["steps"][0]["target"]
                if mutation == "percent":
                    target["percent_of"] = "threshold_pace"
                else:
                    target["unit"] = "%pace"
                with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
                    prepare_delivery_proposal(plan, "run-quality-01", now=self.now)

    def test_non_positive_or_reversed_absolute_pace_is_blocked(self):
        for low, high in ((0, 320), (340, 320)):
            with self.subTest(low=low, high=high):
                plan = copy.deepcopy(self.plan)
                target = next(
                    item for item in plan["week"]["sessions"]
                    if item["session_id"] == "run-quality-01"
                )["structured_workout"]["steps"][1]["steps"][0]["target"]
                target["low_seconds_per_km"] = low
                target["high_seconds_per_km"] = high
                with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
                    prepare_delivery_proposal(plan, "run-quality-01", now=self.now)

    def test_absolute_pace_requires_measured_threshold_anchor_at_prepare_boundary(self):
        unknown = copy.deepcopy(self.plan)
        unknown["athlete_baseline"]["threshold_pace_sec_per_km"] = None
        with self.assertRaisesRegex(DeliveryError, "requires measured.*threshold_pace"):
            prepare_delivery_proposal(unknown, "run-quality-01", now=self.now)

        measured = prepare_delivery_proposal(self.plan, "run-quality-01", now=self.now)
        self.assertEqual("AWAITING_CONFIRMATION", measured["state"])

    def test_moved_and_replaced_running_sessions_remain_deliverable(self):
        for status in ("moved", "replaced"):
            with self.subTest(status=status):
                plan = copy.deepcopy(self.plan)
                next(
                    item for item in plan["week"]["sessions"]
                    if item["session_id"] == "run-quality-01"
                )["match_status"] = status
                proposal = prepare_delivery_proposal(plan, "run-quality-01", now=self.now)
                self.assertEqual("run-quality-01", proposal["session_id"])

    def _strength_plan(self) -> dict[str, Any]:
        plan = copy.deepcopy(self.plan)
        next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )["execution"]["publish_supported"] = True
        return plan

    def test_strength_publishes_a_titled_calendar_entry_carrying_no_structure(self):
        plan = self._strength_plan()
        proposal = prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        # Intervals echoes a workout_doc container with no steps for a calendar entry.
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            "workout_doc": {"steps": [], "description": event["description"]},
        }

        receipt = publish_delivery(
            proposal,
            approval,
            load_current_plan=lambda: plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("intervals_accepted", receipt["delivery_state"])
        written = transport.bulk_calls[0]
        self.assertEqual("WeightTraining", written["type"])
        self.assertEqual(
            "Maintain upper-body strength with low-volume lower accessory work",
            written["name"],
        )
        self.assertEqual("Bench press 5x5 @60kg; dumbbell fly 3x12", written["description"])
        self.assertNotIn("workout_doc", written)
        self.assertNotIn("steps", proposal["workout"])

    def test_strength_readback_carrying_workout_steps_fails_closed(self):
        plan = self._strength_plan()
        proposal = prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            "workout_doc": {"steps": [{"duration": 600}]},
        }

        with self.assertRaisesRegex(DeliveryError, "unexpectedly carries workout steps"):
            publish_delivery(
                proposal,
                approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_strength_without_a_purpose_cannot_title_a_calendar_entry(self):
        plan = self._strength_plan()
        next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )["purpose"] = "   "
        with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
            prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)

    def test_absolute_heart_rate_is_blocked_instead_of_converted_to_percent_hr(self):
        plan = copy.deepcopy(self.plan)
        next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )["structured_workout"]["steps"][0]["target"] = {
            "kind": "heart_rate",
            "unit": "bpm",
            "low_bpm": None,
            "high_bpm": 140,
        }
        with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
            prepare_delivery_proposal(plan, "run-quality-01", now=self.now)

    def test_hr_ceiling_workout_sends_workout_doc_and_prose_description(self):
        # #38: absolute bpm cannot ship via intervals workout text at all, so an
        # hr_ceiling workout's description must be plain prose (no `- ` prefix
        # that could look like parseable syntax) while the ceiling itself rides
        # the provider's workout_doc JSON field.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(plan, "run-quality-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        install_readback_builder(transport, proposal)

        receipt = publish_delivery(
            proposal,
            approval,
            load_current_plan=lambda: plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("intervals_accepted", receipt["delivery_state"])
        written = transport.bulk_calls[0]
        self.assertEqual(
            {"units": "bpm", "start": 0, "end": 140},
            written["workout_doc"]["steps"][0]["hr"],
        )
        for line in written["description"].splitlines():
            self.assertFalse(line.startswith("- "), line)

    def test_readback_percent_hr_instead_of_absolute_bpm_fails_closed(self):
        # The original dogfood failure (#38): 77-83 %hr was silently substituted
        # for the requested absolute bpm ceiling, so the watch enforced a floor
        # during a recovery run meant to stay under 140.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(plan, "run-quality-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            "workout_doc": {
                "steps": [
                    {"text": "Easy run", "duration": 1800,
                     "hr": {"units": "%hr", "start": 77, "end": 83}}
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "hr must remain absolute bpm"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_provider_added_floor_fails_closed(self):
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(plan, "run-quality-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            "workout_doc": {
                "steps": [
                    {"text": "Easy run", "duration": 1800,
                     "hr": {"units": "bpm", "start": 60, "end": 140}}
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "hr.start must remain 0"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_ceiling_mismatch_fails_closed(self):
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(plan, "run-quality-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            "workout_doc": {
                "steps": [
                    {"text": "Easy run", "duration": 1800,
                     "hr": {"units": "bpm", "start": 0, "end": 149}}
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "hr.end ceiling mismatch"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_hr_step_carrying_a_pace_target_fails_closed(self):
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(plan, "run-quality-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            "workout_doc": {
                "steps": [
                    {
                        "text": "Easy run", "duration": 1800,
                        "hr": {"units": "bpm", "start": 0, "end": 140},
                        "pace": {"start": 300, "end": 320, "units": "secs/km"},
                    }
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "unsupported target"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_hr_ceiling_workout_rejects_distance_step_and_repeat_at_prepare(self):
        # Fail closed rather than guess: the doc-JSON shape carrying a ceiling is
        # only verified for time-based, non-repeating work steps. A repeat is
        # already schema-blocked from *containing* hr_ceiling, but a workout
        # mixing a repeat block with a top-level hr_ceiling step must still be
        # rejected here.
        for mutation in ("distance", "repeat"):
            with self.subTest(mutation=mutation):
                plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
                session = next(
                    item for item in plan["week"]["sessions"]
                    if item["session_id"] == "run-quality-01"
                )
                if mutation == "distance":
                    session["structured_workout"]["steps"] = [
                        {
                            "kind": "work", "name": "Easy run",
                            "duration": {"kind": "distance", "meters": 5000},
                            "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140},
                        },
                    ]
                else:
                    session["structured_workout"]["steps"] = [
                        {
                            "kind": "work", "name": "Easy run",
                            "duration": {"kind": "time", "seconds": 1800},
                            "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140},
                        },
                        {
                            "kind": "repeat", "repetitions": 2, "steps": [
                                {
                                    "kind": "work", "name": "Strides",
                                    "duration": {"kind": "time", "seconds": 20},
                                    "target": {"kind": "open"},
                                },
                            ],
                        },
                    ]
                with self.assertRaisesRegex(
                    DeliveryError, "must use a time duration|must not contain a repeat"
                ):
                    prepare_delivery_proposal(plan, "run-quality-01", now=self.now)

    def test_4x800_cannot_be_bound_to_the_current_5x1000_session(self):
        forged = copy.deepcopy(self.proposal)
        forged["workout"]["name"] = "4x800 pace reps"
        forged["workout"]["steps"] = four_by_800_steps()
        forged["workout"]["description"] = "forged 4x800"
        material = {key: value for key, value in forged.items() if key != "proposal_hash"}
        forged["proposal_hash"] = canonical_hash(material)
        approval = approve_delivery_proposal(
            forged, approved_by="fixture-athlete", approved_at=self.now
        )
        with self.assertRaisesRegex(DeliveryError, "not the selected current PlanState workout"):
            publish_delivery(
                forged,
                approval,
                load_current_plan=lambda: self.plan,
                transport=FakeTransport(),
                now=self.now,
            )

    def test_4x800_cannot_be_reidentified_as_the_12km_easy_session(self):
        long_proposal = prepare_delivery_proposal(
            self.plan, "run-long-01", now=self.now
        )
        forged = copy.deepcopy(long_proposal)
        forged["workout"]["steps"] = four_by_800_steps()
        forged["workout"]["description"] = "forged 4x800"
        material = {key: value for key, value in forged.items() if key != "proposal_hash"}
        forged["proposal_hash"] = canonical_hash(material)
        approval = approve_delivery_proposal(
            forged, approved_by="fixture-athlete", approved_at=self.now
        )
        with self.assertRaisesRegex(DeliveryError, "not the selected current PlanState workout"):
            publish_delivery(
                forged,
                approval,
                load_current_plan=lambda: self.plan,
                transport=FakeTransport(),
                now=self.now,
            )

    def test_approval_and_current_plan_are_revalidated_at_write_boundary(self):
        changed_approval = copy.deepcopy(self.approval)
        changed_approval["proposal_hash"] = "wrong"
        with self.assertRaisesRegex(DeliveryError, "approval proposal_hash"):
            publish_delivery(
                self.proposal,
                changed_approval,
                load_current_plan=lambda: self.plan,
                transport=FakeTransport(),
                now=self.now,
            )

        changed_plan = copy.deepcopy(self.plan)
        changed_plan["version"] += 1
        with self.assertRaisesRegex(DeliveryError, "version changed"):
            publish_delivery(
                self.proposal,
                self.approval,
                load_current_plan=lambda: changed_plan,
                transport=FakeTransport(),
                now=self.now,
            )

        changed_session_plan = copy.deepcopy(self.plan)
        next(
            item for item in changed_session_plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )["prescription"] = "Changed without a version bump"
        with self.assertRaisesRegex(DeliveryError, "session changed"):
            publish_delivery(
                self.proposal,
                self.approval,
                load_current_plan=lambda: changed_session_plan,
                transport=FakeTransport(),
                now=self.now,
            )

        changed_workout_plan = copy.deepcopy(self.plan)
        next(
            item for item in changed_workout_plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )["structured_workout"]["steps"][1]["repetitions"] = 4
        with self.assertRaisesRegex(DeliveryError, "session changed"):
            publish_delivery(
                self.proposal,
                self.approval,
                load_current_plan=lambda: changed_workout_plan,
                transport=FakeTransport(),
                now=self.now,
            )

    def test_historical_plan_without_structured_workout_still_replays_but_cannot_publish(self):
        historical = copy.deepcopy(self.plan)
        for session in historical["week"]["sessions"]:
            session.pop("structured_workout", None)
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            init_store(state_dir, historical)
            self.assertEqual("passed", doctor_store(state_dir)["status"])
        with self.assertRaisesRegex(DeliveryError, "no canonical structured_workout"):
            prepare_delivery_proposal(historical, "run-quality-01", now=self.now)

    def test_reused_session_id_in_a_later_week_creates_a_distinct_owned_event(self):
        old_event = {
            "id": 8123,
            "external_id": self.proposal["owned_external_id"],
            "category": "WORKOUT",
            "type": "Run",
            "name": self.proposal["workout"]["name"],
            "start_date_local": "2026-08-13T00:00:00",
            "description": self.proposal["workout"]["description"],
        }
        later_plan = copy.deepcopy(self.plan)
        later_plan["version"] = 2
        later_plan["week"]["start"] = "2026-08-17"
        for candidate in later_plan["week"]["sessions"]:
            candidate["scheduled_date"] = (
                dt.date.fromisoformat(candidate["scheduled_date"]) + dt.timedelta(days=7)
            ).isoformat()
        later_session = next(
            item for item in later_plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        later_proposal = prepare_delivery_proposal(
            later_plan, "run-quality-01", now=self.now
        )

        self.assertNotEqual(
            self.proposal["owned_external_id"], later_proposal["owned_external_id"]
        )
        transport = FakeTransport(events=[old_event])
        install_readback_builder(transport, later_proposal)
        later_approval = approve_delivery_proposal(
            later_proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        receipt = publish_delivery(
            later_proposal,
            later_approval,
            load_current_plan=lambda: later_plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("upserted", receipt["operation"])
        self.assertEqual(1, len(transport.bulk_calls))
        self.assertEqual("8123", str(transport.events[0]["id"]))
        self.assertEqual("2026-08-13", transport.events[0]["start_date_local"][:10])
        self.assertEqual(2, len(transport.events))

    def test_same_week_move_updates_the_owned_event_without_changing_provider_id(self):
        old_event = {
            "id": 8123,
            "external_id": self.proposal["owned_external_id"],
            "category": "WORKOUT",
            "type": "Run",
            "name": self.proposal["workout"]["name"],
            "start_date_local": "2026-08-13T00:00:00",
            "description": self.proposal["workout"]["description"],
            "workout_doc": {
                "steps": [provider_step(step) for step in self.proposal["workout"]["steps"]]
            },
        }
        moved_plan = copy.deepcopy(self.plan)
        moved_plan["version"] = 2
        moved_session = next(
            item for item in moved_plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        moved_session["scheduled_date"] = "2026-08-14"
        moved_session["match_status"] = "moved"
        moved_session["structured_workout"]["name"] = "Moved quality run"
        moved_proposal = prepare_delivery_proposal(
            moved_plan, "run-quality-01", now=self.now
        )
        self.assertEqual(self.proposal["owned_external_id"], moved_proposal["owned_external_id"])

        transport = FakeTransport(events=[old_event])
        transport.readbacks["8123"] = copy.deepcopy(old_event)
        install_readback_builder(transport, moved_proposal)
        moved_approval = approve_delivery_proposal(
            moved_proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        receipt = publish_delivery(
            moved_proposal,
            moved_approval,
            load_current_plan=lambda: moved_plan,
            transport=transport,
            now=self.now,
        )

        self.assertEqual("upserted", receipt["operation"])
        self.assertEqual("8123", receipt["observation"]["external_id"])
        self.assertEqual(1, len(transport.bulk_calls))
        self.assertEqual(1, len(transport.events))
        self.assertEqual("2026-08-14", transport.events[0]["start_date_local"][:10])
        self.assertEqual("Moved quality run", transport.events[0]["name"])

    def test_verified_receipt_advances_the_same_append_only_current_state(self):
        transport = FakeTransport()
        install_readback_builder(transport, self.proposal)
        receipt = publish_delivery(
            self.proposal,
            self.approval,
            load_current_plan=lambda: self.plan,
            transport=transport,
            now=self.now,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            init_store(state_dir, self.plan)
            result = apply_delivery_observations(
                state_dir,
                observations=[receipt["observation"]],
            )
            self.assertEqual("passed", result["status"])
            current = status_store(state_dir)["current_plan"]
            session = next(
                item for item in current["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )
            self.assertEqual("intervals_accepted", session["execution"]["delivery_state"])
            self.assertEqual("9001", session["execution"]["external_id"])


class IntervalsTransportTests(unittest.TestCase):
    def test_concrete_transport_uses_get_post_get_and_never_logs_credentials(self):
        seen: list[tuple[str, str, Any]] = []
        responses = [[], [{"id": 44}], {"id": 44}]

        def fetch(request):
            body = json.loads(request.data) if request.data else None
            seen.append((request.get_method(), request.full_url, body))
            return json.dumps(responses.pop(0)).encode("utf-8")

        transport = IntervalsTransport(
            IntervalsCredentials(api_key="fake", athlete_id="i42"),
            fetch=fetch,
        )
        self.assertEqual([], transport.list_events("2026-08-13"))
        self.assertEqual([{"id": 44}], transport.bulk_upsert({"name": "Workout"}))
        self.assertEqual({"id": 44}, transport.get_event("44"))
        self.assertEqual(["GET", "POST", "GET"], [item[0] for item in seen])
        self.assertTrue(all("fake" not in item[1] for item in seen))


class DeliveryCliTests(unittest.TestCase):
    def test_one_cli_path_prepares_approves_publishes_and_updates_current_state(self):
        plan = plan_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            proposal_path = root / "proposal.json"
            approval_path = root / "approval.json"
            receipt_path = root / "receipt.json"
            init_store(state_dir, plan)

            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli_main([
                        "prepare-delivery", "--state-dir", str(state_dir),
                        "--session", "run-quality-01", "--out", str(proposal_path),
                        "--session", "run-long-01",
                    ]),
                )
                self.assertEqual(
                    0,
                    cli_main([
                        "approve-delivery", "--proposal", str(proposal_path),
                        "--approved-by", "fixture-athlete", "--out", str(approval_path),
                    ]),
                )

            proposal_set = json.loads(proposal_path.read_text(encoding="utf-8"))
            transport = FakeTransport()
            install_readback_builder(transport, proposal_set["items"])
            credentials = IntervalsCredentials(api_key="fake", athlete_id="i42")
            with (
                mock.patch("garmin_coach_loop.cli.resolve_credentials", return_value=credentials),
                mock.patch("garmin_coach_loop.cli.IntervalsTransport", return_value=transport),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    0,
                    cli_main([
                        "publish-delivery", "--state-dir", str(state_dir),
                        "--proposal", str(proposal_path), "--approval", str(approval_path),
                        "--receipt-out", str(receipt_path),
                    ]),
                )

            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual("intervals_accepted", receipt["delivery_state"])
            self.assertEqual(2, len(receipt["item_receipts"]))
            self.assertEqual(2, len(transport.bulk_calls))
            current = status_store(state_dir)["current_plan"]
            delivered = [
                item for item in current["week"]["sessions"]
                if item["session_id"] in {"run-quality-01", "run-long-01"}
            ]
            self.assertTrue(all(
                item["execution"]["delivery_state"] == "intervals_accepted"
                for item in delivered
            ))
            self.assertEqual(2, current["version"])


if __name__ == "__main__":
    unittest.main()
