from __future__ import annotations

import copy
import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from garmin_coach_loop.cli import main as cli_main
from garmin_coach_loop.delivery import (
    DeliveryError,
    IntervalsTransport,
    _calendar_entry_from_session,
    _provider_payload,
    _resolved_ceiling_bpm,
    _workout_from_session,
    approve_delivery_proposal,
    approve_delivery_set,
    approve_withdrawal_set,
    deliver_approved_set,
    hr_ceiling_percent_lthr,
    prepare_delivery_proposal,
    prepare_delivery_set,
    prepare_withdrawal_set,
    publish_delivery,
    publish_delivery_set,
    withdraw_approved_set,
)
from garmin_coach_loop.delivery import (
    _reconcile_attempt,
    record_delivery_attempt_operation,
)
from garmin_coach_loop.plan_change import project_change_request
from garmin_coach_loop.validation import (
    validate_plan_state,
    _expected_context_baseline,
    _expected_current_calendar,
    _expected_goal_context,
)
from garmin_coach_loop.delivery_content import delivery_session_content
from garmin_coach_loop.plan_change import _publish_supported
from garmin_coach_loop.prescription import render_prescription, strength_title_suffix
from garmin_coach_loop.source_intervals import IntervalsCredentials
from garmin_coach_loop.store import (
    DELIVERY_ATTEMPT_FILE,
    WRITER_CONTRACT_VERSION,
    adopt_store,
    close_delivery_attempt,
    open_delivery_attempt,
    restore_snapshot,
    snapshot_store,
    unresolved_delivery_operations,
    StateStoreError,
    apply_decision,
    apply_delivery_observations,
    canonical_hash,
    doctor_store,
    init_store,
    pending_delivery_attempt,
    read_current_plan,
    status_store,
)

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # The runtime validators remain dependency-free.
    Draft202012Validator = None
    FormatChecker = None


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


# The Run threshold HR of the fixture account, matching the live account the `% LTHR`
# encoding was verified against on 2026-08-14: `50-86% LTHR` reached the watch as
# `81-140 bpm`, so a 140 bpm plan ceiling resolves to exactly 86%.
FIXTURE_RUN_THRESHOLD_HR = 163


def provider_step(
    step: dict[str, Any], resolution: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What Intervals echoes for one step it parsed out of the delivered workout text."""
    if step["kind"] == "repeat":
        return {
            "reps": step["repetitions"],
            "steps": [provider_step(child, resolution) for child in step["steps"]],
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
        assert resolution is not None, "an hr_ceiling read-back needs the confirmed resolution"
        result["hr"] = {
            "start": resolution["percent_lthr_low"],
            "end": resolution["percent_lthr_high"],
            "units": "%lthr",
        }
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
    session["plan"] = {
        "kind": "time_axis",
        "name": "Easy recovery run",
        "steps": hr_ceiling_steps(ceiling_bpm),
    }
    session["prescription"] = render_prescription(session["plan"])
    return plan


def rerendered(session: dict[str, Any]) -> dict[str, Any]:
    """Keep a hand-edited session's prescription the rendering of its own plan.

    Every production write path renders it; a test that edits a plan directly has to do
    the same, or the plan it built is refused for describing itself as what it used to be.
    """
    session["prescription"] = render_prescription(session["plan"])
    return session


class FakeTransport:
    def __init__(self, *, events: list[dict[str, Any]] | None = None):
        self.events = list(events or [])
        self.bulk_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.deleted: list[str] = []
        self.readbacks: dict[str, dict[str, Any]] = {}
        # The healthy provider state: settings readable, Run threshold pace configured
        # (m/s, the unit Intervals stores). Tests that need the other two answers set
        # `threshold_pace = None` (readable and unset) or `settings_readable = False`
        # (the hosted OAuth path, which may not read athlete settings at all).
        self.threshold_pace: Any = 2.7027
        self.threshold_hr: Any = FIXTURE_RUN_THRESHOLD_HR
        self.settings_readable = True
        self.threshold_pace_reads = 0
        self.threshold_hr_reads = 0

    def run_threshold_pace(self) -> tuple[bool, Any]:
        self.threshold_pace_reads += 1
        if not self.settings_readable:
            return (False, None)
        return (True, self.threshold_pace)

    def run_threshold_hr(self) -> tuple[bool, Any]:
        self.threshold_hr_reads += 1
        if not self.settings_readable:
            return (False, None)
        return (True, self.threshold_hr)

    def list_events(self, day: str) -> list[dict[str, Any]]:
        return copy.deepcopy([
            event for event in self.events
            if str(event.get("start_date_local", ""))[:10] == day
        ])

    def list_events_range(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        return copy.deepcopy([
            event for event in self.events
            if oldest <= str(event.get("start_date_local", ""))[:10] <= newest
        ])

    def find_event(self, event_id: str) -> dict[str, Any] | None:
        stored = next(
            (event for event in self.events if str(event.get("id")) == str(event_id)), None
        )
        return copy.deepcopy(stored) if stored is not None else None

    def delete_event(self, event_id: str) -> None:
        self.deleted.append(str(event_id))
        self.events = [
            event for event in self.events if str(event.get("id")) != str(event_id)
        ]

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
        resolution = (selected.get("preview") or {}).get("hr_ceiling_resolution")
        return {
            "id": int(event_id),
            **event,
            "workout_doc": {
                "steps": [
                    provider_step(step, resolution) for step in selected["workout"]["steps"]
                ]
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
                )["plan"]["steps"][1]["steps"][0]["target"]
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
                )["plan"]["steps"][1]["steps"][0]["target"]
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
        # The title appends the primary lift and its load to purpose: the athlete
        # effectively sees only this title on the watch, and 臥推 is movements[0].
        self.assertEqual(
            "Maintain upper-body strength with low-volume lower accessory work："
            "臥推 5x5 待確認",
            written["name"],
        )
        # The calendar description is the session's own prescription, which is now the
        # rendering of its movement list rather than a sentence written beside it.
        self.assertEqual("臥推 5x5 待確認\n伏地挺身 3x12 自重", written["description"])
        self.assertNotIn("workout_doc", written)
        self.assertNotIn("steps", proposal["workout"])

    def test_an_intent_line_that_counts_still_titles_the_calendar_entry(self):
        """The false-positive control for issue #99, end to end rather than in isolation.

        Refusing a prescribed number in `purpose` is only worth having if the field still
        does its job, and its job is titling the entry a strength day reaches the watch
        as. The title here carries a digit on purpose: it is the shape the athlete's own
        naming convention produces, it passes the refusal, and it survives validation,
        approval, the provider write and read-back unchanged.
        """
        plan = self._strength_plan()
        next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )["purpose"] = "本週第 2 次上肢，維持 Zone 2 以外的刺激"
        proposal = prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
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
        self.assertEqual(
            "本週第 2 次上肢，維持 Zone 2 以外的刺激：臥推 5x5 待確認",
            transport.bulk_calls[0]["name"],
        )

    def test_a_prescribed_pace_in_the_intent_line_never_reaches_the_calendar(self):
        # The harmful case at the delivery boundary: `_current_plan_is_valid` runs the
        # same validator, so a title carrying a number no baseline vouches for stops
        # before anything is written to the provider.
        plan = self._strength_plan()
        next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )["purpose"] = "臥推推到 80kg"
        with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
            prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)

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

    def test_strength_with_an_unstructured_plan_keeps_the_bare_purpose_as_title(self):
        """The athlete's own decision to decline quantification (2026-08-14) still titles.

        An unstructured strength plan has no primary movement to append, so the title
        stays exactly the purpose -- the bare title every strength entry carried before
        this feature, and the only shape available for a session with no movement list.
        """
        plan = self._strength_plan()
        session = next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )
        session["plan"] = {"kind": "unstructured"}
        rerendered(session)
        proposal = prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
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
        self.assertEqual(
            "Maintain upper-body strength with low-volume lower accessory work",
            transport.bulk_calls[0]["name"],
        )

    def test_strength_readback_name_mismatch_fails_closed(self):
        plan = self._strength_plan()
        proposal = prepare_delivery_proposal(plan, "strength-upper-01", now=self.now)
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        transport._readback = lambda event_id, event: {
            "id": int(event_id),
            **event,
            # The provider echoes back the bare purpose, without the primary lift and
            # load this delivery actually approved -- the same shape a pre-existing
            # event from before this feature would still carry.
            "name": "胸日",
            "workout_doc": {"steps": [], "description": event["description"]},
        }

        with self.assertRaisesRegex(DeliveryError, "read-back workout name mismatch"):
            publish_delivery(
                proposal,
                approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_absolute_heart_rate_is_blocked_instead_of_converted_to_percent_hr(self):
        plan = copy.deepcopy(self.plan)
        next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )["plan"]["steps"][0]["target"] = {
            "kind": "heart_rate",
            "unit": "bpm",
            "low_bpm": None,
            "high_bpm": 140,
        }
        with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
            prepare_delivery_proposal(plan, "run-quality-01", now=self.now)

    def test_hr_ceiling_is_delivered_as_percent_lthr_workout_text(self):
        # Issue #22, device-verified 2026-08-14: an absolute-bpm ceiling supplied through
        # `workout_doc` reached the watch as 1-252 bpm -- a target that displays and
        # constrains nothing. `% LTHR` is the encoding the provider parses and exports, so
        # the ceiling now ships as ordinary workout text and no `workout_doc` is sent.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
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
        self.assertNotIn("workout_doc", written)
        self.assertEqual("- Easy run 30m 50-86% LTHR", written["description"])

    def test_the_preview_states_the_bpm_the_confirmed_percentage_resolves_to(self):
        # The athlete confirms a ceiling in bpm; `86% LTHR` is one provider's way of
        # saying it. Both are bound by the proposal hash, so the number shown is the
        # number delivered -- and 86% of 163 is 140.18, which resolves to the 140 the
        # plan asked for and never above it.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )

        self.assertEqual(
            {
                "run_threshold_hr": 163,
                "percent_lthr_low": 50,
                "percent_lthr_high": 86,
                "resolved_ceiling_bpm": 140,
                "plan_ceiling_bpm": 140,
            },
            proposal["preview"]["hr_ceiling_resolution"],
        )

    def test_the_resolved_ceiling_never_rounds_up_past_the_plan_ceiling(self):
        # The one guarantee this encoding has to keep, over the whole range of ceilings a
        # recovery run can carry: a percentage that resolved even one bpm above the plan
        # would be the silent loosening the absolute-bpm path was removed for.
        for ceiling in range(90, 164):
            with self.subTest(ceiling=ceiling):
                low, high = hr_ceiling_percent_lthr(ceiling, FIXTURE_RUN_THRESHOLD_HR)
                resolved = _resolved_ceiling_bpm(high, FIXTURE_RUN_THRESHOLD_HR)
                self.assertLessEqual(resolved, ceiling)
                self.assertEqual(50, low)
                # ...and it is the tightest such percentage, not an over-cautious one.
                self.assertGreater(
                    _resolved_ceiling_bpm(high + 1, FIXTURE_RUN_THRESHOLD_HR), ceiling
                )

    def test_a_ceiling_with_no_readable_threshold_hr_blocks_at_preview(self):
        # Fail closed before the athlete confirms anything, not at publish and never by
        # silently downgrading to an open target.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        with self.assertRaisesRegex(DeliveryError, "Run threshold HR could not be read"):
            prepare_delivery_proposal(plan, "run-quality-01", now=self.now)

    def test_a_workout_with_no_ceiling_never_reads_the_threshold_hr(self):
        # The block is narrow by construction: only a ceiling needs the account's
        # threshold, so every other workout previews without touching the provider.
        reads: list[int] = []

        def reader() -> int | None:
            reads.append(1)
            return FIXTURE_RUN_THRESHOLD_HR

        prepare_delivery_proposal(
            self.plan, "run-quality-01", now=self.now, read_run_threshold_hr=reader
        )
        self.assertEqual([], reads)

    def test_a_ceiling_below_every_usable_percentage_blocks_rather_than_guesses(self):
        # 50% of threshold is the floor the grammar requires. A ceiling under it cannot be
        # expressed at all, and the message names both numbers that could be wrong.
        with self.assertRaisesRegex(DeliveryError, "cannot be delivered against"):
            hr_ceiling_percent_lthr(70, FIXTURE_RUN_THRESHOLD_HR)

    def test_readback_percent_of_max_hr_instead_of_threshold_fails_closed(self):
        # The original dogfood failure (#38): `77-83% HR` was resolved against max HR
        # rather than threshold, so a recovery run meant to stay under 140 enforced a
        # 139-149 bpm floor. A percentage is not enough; it has to be `%lthr`.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
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
        with self.assertRaisesRegex(DeliveryError, "must be resolved against threshold HR"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_absolute_bpm_fails_closed(self):
        # The shape this product used to send. It is now a failure, so the removed escape
        # hatch cannot come back through the provider either.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
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
                     "hr": {"units": "bpm", "start": 0, "end": 140}}
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "must be resolved against threshold HR"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_ceiling_resolving_above_the_plan_ceiling_fails_closed(self):
        # The check that carries the safety claim: whatever percentage the provider says
        # it parsed is resolved back into bpm and must land under the plan's ceiling. 87%
        # of 163 is 142, so this is the one-percent slip that would matter on the watch.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
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
                     "hr": {"units": "%lthr", "start": 50, "end": 87}}
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "is not the confirmed ceiling"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_provider_added_floor_fails_closed(self):
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
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
                     "hr": {"units": "%lthr", "start": 70, "end": 86}}
                ]
            },
        }
        with self.assertRaisesRegex(DeliveryError, "is not the confirmed floor"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )

    def test_readback_hr_step_carrying_a_pace_target_fails_closed(self):
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
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
                        "hr": {"units": "%lthr", "start": 50, "end": 86},
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

    def test_a_threshold_hr_that_moved_after_confirmation_blocks_the_publish(self):
        # The athlete confirmed 140 bpm. The same `86% LTHR` against a changed threshold
        # is a different ceiling, so the confirmation no longer describes the delivery.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
        approval = approve_delivery_proposal(
            proposal, approved_by="fixture-athlete", approved_at=self.now
        )
        transport = FakeTransport()
        install_readback_builder(transport, proposal)
        transport.threshold_hr = 171

        with self.assertRaisesRegex(DeliveryError, "changed from 163 to 171"):
            publish_delivery(
                proposal, approval,
                load_current_plan=lambda: plan,
                transport=transport,
                now=self.now,
            )
        self.assertEqual([], transport.bulk_calls)

    def test_a_ceiling_on_a_distance_step_delivers_as_workout_text(self):
        # The old prose path could not carry a distance step or a repeat, because the
        # doc-JSON shape it used was only verified for time-based single steps. Workout
        # text has no such limit -- it renders a heart-rate target exactly as it renders
        # a pace one -- so the restriction goes with the path that needed it.
        plan = hr_ceiling_plan_fixture(ceiling_bpm=140)
        session = next(
            item for item in plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        session["plan"]["steps"] = [
            {
                "kind": "work", "name": "Easy run",
                "duration": {"kind": "distance", "meters": 5000},
                "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140},
            },
        ]
        rerendered(session)

        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )

        self.assertEqual("- Easy run 5km 50-86% LTHR", proposal["workout"]["description"])

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
        session = next(
            item for item in changed_session_plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        session["plan"]["name"] = "Changed without a version bump"
        rerendered(session)
        with self.assertRaisesRegex(DeliveryError, "session changed"):
            publish_delivery(
                self.proposal,
                self.approval,
                load_current_plan=lambda: changed_session_plan,
                transport=FakeTransport(),
                now=self.now,
            )

        changed_workout_plan = copy.deepcopy(self.plan)
        changed_workout = next(
            item for item in changed_workout_plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        changed_workout["plan"]["steps"][1]["repetitions"] = 4
        rerendered(changed_workout)
        with self.assertRaisesRegex(DeliveryError, "session changed"):
            publish_delivery(
                self.proposal,
                self.approval,
                load_current_plan=lambda: changed_workout_plan,
                transport=FakeTransport(),
                now=self.now,
            )

    def test_a_plan_predating_session_plans_does_not_open_at_all(self):
        """No compatibility layer, deliberately (issue #93).

        A session that carried its structure in `structured_workout` used to keep
        validating, and delivery refused it one step later. That "optional because
        history lacks it" reasoning is what kept the free-text path alive through five
        repairs, so a plan without `plan` is now refused where it is read, not where it
        is sent: the store will not take it, and the athlete regenerates once.
        """
        historical = copy.deepcopy(self.plan)
        for session in historical["week"]["sessions"]:
            session["structured_workout"] = session.pop("plan")
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            with self.assertRaises(Exception) as raised:
                init_store(state_dir, historical)
            self.assertIn("initial PlanState is invalid", str(raised.exception))
            self.assertFalse(state_dir.exists())
        with self.assertRaisesRegex(DeliveryError, "current PlanState is invalid"):
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
        later_plan["cycle"]["outlook"] = later_plan["cycle"]["outlook"][1:]
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
        moved_session["plan"]["name"] = "Moved quality run"
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


class StrengthTitleSuffixTests(unittest.TestCase):
    """The primary-lift-and-load rendering in isolation, one plan shape per case.

    `DeliveryFlowTests` already covers this wired into a real delivery end to end; these
    cases are the movement shapes `_validate_strength_movement` allows (see
    `validation.STRENGTH_LOAD_BASES` and its bodyweight/pending_confirmation/
    measured_baseline coherence rule), checked directly against the pure renderer so each
    is one assertion rather than a full plan built to reach it.
    """

    @staticmethod
    def _movement(**fields: Any) -> dict[str, Any]:
        movement = {
            "exercise": "bench-press",
            "display_name": "臥推",
            "sets": 5,
            "reps": 5,
            "load_kg": None,
            "assist_kg": None,
            "load_basis": "pending_confirmation",
        }
        movement.update(fields)
        return movement

    def test_load_kg_renders_kg_not_the_chinese_unit_prescription_uses(self):
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(load_kg=65, load_basis="measured_baseline")],
        }
        self.assertEqual("臥推 5x5 65kg", strength_title_suffix(plan))

    def test_load_kg_keeps_a_fraction_and_drops_a_whole_numbers_point(self):
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(load_kg=62.5, load_basis="measured_baseline")],
        }
        self.assertEqual("臥推 5x5 62.5kg", strength_title_suffix(plan))

    def test_assist_kg_renders_the_assisted_load(self):
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(assist_kg=24, load_basis="measured_baseline")],
        }
        self.assertEqual("臥推 5x5 輔助24kg", strength_title_suffix(plan))

    def test_bodyweight_basis_with_no_load_figure(self):
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(load_basis="bodyweight")],
        }
        self.assertEqual("臥推 5x5 自重", strength_title_suffix(plan))

    def test_pending_confirmation_basis_with_no_load_figure(self):
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(load_basis="pending_confirmation")],
        }
        self.assertEqual("臥推 5x5 待確認", strength_title_suffix(plan))

    def test_a_set_taken_to_failure_has_no_rep_count(self):
        plan = {
            "kind": "movement_list",
            "movements": [
                self._movement(reps=None, load_kg=40, load_basis="measured_baseline")
            ],
        }
        self.assertEqual("臥推 5組力竭 40kg", strength_title_suffix(plan))

    def test_the_first_movement_is_the_primary_lift(self):
        plan = {
            "kind": "movement_list",
            "movements": [
                self._movement(
                    display_name="臥推", load_kg=65, load_basis="measured_baseline"
                ),
                self._movement(
                    display_name="肩推", sets=3, reps=10, load_kg=30,
                    load_basis="measured_baseline",
                ),
            ],
        }
        self.assertEqual("臥推 5x5 65kg", strength_title_suffix(plan))

    def test_an_unstructured_plan_has_no_suffix(self):
        self.assertIsNone(strength_title_suffix({"kind": "unstructured"}))

    def test_a_time_axis_plan_has_no_suffix(self):
        # A running plan reaching this function would be a caller bug, not a strength
        # session -- checked because `None` is exactly the fallback the caller titles
        # bare purpose from, and a wrong branch here would silently mistitle a run.
        plan = {"kind": "time_axis", "name": "輕鬆跑 30分", "steps": []}
        self.assertIsNone(strength_title_suffix(plan))

    def test_the_title_is_written_in_the_language_the_session_was_written_in(self):
        """The title and the description ride on one calendar entry, so they are one
        language -- read off the session's own sentence rather than passed in beside it.
        """
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(reps=None, load_basis="bodyweight")],
        }
        session = {
            "session_id": "strength-upper-01",
            "sport": "strength",
            "scheduled_date": "2026-08-14",
            "purpose": "Upper body",
            "plan": plan,
            "prescription": render_prescription(plan, "en"),
        }

        entry = _calendar_entry_from_session(session)

        self.assertEqual("Upper body: 臥推 5 sets to failure bodyweight", entry["name"])
        self.assertEqual(entry["description"], session["prescription"])

    def test_a_chinese_session_keeps_the_title_it_always_had(self):
        plan = {
            "kind": "movement_list",
            "movements": [self._movement(load_kg=65, load_basis="measured_baseline")],
        }
        session = {
            "session_id": "strength-upper-01",
            "sport": "strength",
            "scheduled_date": "2026-08-14",
            "purpose": "上肢",
            "plan": plan,
            "prescription": render_prescription(plan),
        }

        self.assertEqual(
            "上肢：臥推 5x5 65kg", _calendar_entry_from_session(session)["name"]
        )


class PublishSupportMatchesWhatDeliveryCanBuildTests(unittest.TestCase):
    """The writers' flag and this boundary have to answer the same question.

    They are two expressions of one rule -- what delivery could send -- and they have
    already drifted apart twice. Both times the flag narrowed to running while
    `_workout_from_session` kept building strength calendar entries, and both times the
    result was a session the product refused to deliver at all rather than a session it
    delivered wrongly, which is why no other test noticed. Reading the flag off the plan
    is what the delivery boundary does, so a flag that is wrong is not a flag anybody can
    override.
    """

    CASES = (
        ("running planned along a time axis", {
            "sport": "running", "purpose": "輕鬆跑",
            "plan": {"kind": "time_axis", "name": "輕鬆跑 30分", "steps": [
                {"kind": "work", "name": "輕鬆跑",
                 "duration": {"kind": "time", "seconds": 1800},
                 "target": {"kind": "open"}},
            ]},
        }),
        ("running with no time axis to send", {
            "sport": "running", "purpose": "輕鬆跑", "plan": {"kind": "unstructured"},
        }),
        ("strength with a purpose to title the entry", {
            "sport": "strength", "purpose": "胸日",
            "plan": {"kind": "movement_list", "movements": []},
        }),
        ("strength with no purpose to title the entry", {
            "sport": "strength", "purpose": "   ",
            "plan": {"kind": "movement_list", "movements": []},
        }),
        ("a rest day", {"sport": "rest", "purpose": "休息", "plan": {"kind": "unstructured"}}),
        ("mobility", {"sport": "mobility", "purpose": "活動度", "plan": {"kind": "unstructured"}}),
    )

    def test_the_flag_is_true_exactly_when_a_payload_can_be_built(self):
        for label, session in self.CASES:
            with self.subTest(label):
                session = {**session, "scheduled_date": "2026-08-20", "prescription": "x"}
                try:
                    _workout_from_session(session)
                except DeliveryError:
                    buildable = False
                else:
                    buildable = True
                self.assertEqual(buildable, _publish_supported(session), label)


class ProjectionWithdrawsWhateverChangesThePayloadTests(unittest.TestCase):
    """A receipt may survive a change only while the payload it was written from does.

    `delivery_session_content` decides that, and it is read from three places that never
    see the payload: the writers' stale-delivery reset, validation's refusal to carry a
    receipt across a content change, and the store's compare-and-commit hash. A field the
    payload is built from and the projection does not list is a delivered entry that goes
    stale in silence -- Intervals keeps what it was sent, the plan keeps calling it
    delivered, and `not_published` is the only state redelivery accepts, so the wrong entry
    is the one the athlete keeps.

    `purpose` was that field until #105, found by reading rather than by any test: the two
    are enumerated separately, so a payload gaining an input is a green build. This asserts
    the relation instead. It says nothing about which sport reads which field -- only that
    a change the provider would see is a change the projection reports -- so the third
    execution model is covered by the case it adds here, not by a rule remembered later.
    """

    @staticmethod
    def _session(**fields: Any) -> dict[str, Any]:
        session = {
            "session_id": "session-01",
            "scheduled_date": "2026-08-20",
            "adaptation": "aerobic_base",
            "planned_minutes": 45,
            "hard": False,
            "execution": {
                "publish_supported": True,
                "external_id": None,
                "delivery_state": "not_published",
            },
            **fields,
        }
        # Rendered rather than written, exactly as the writers leave it: a prescription
        # free to differ from its plan would be a second thing to project.
        session["prescription"] = render_prescription(session["plan"])
        return session

    RUN = {
        "sport": "running",
        "purpose": "輕鬆跑",
        "plan": {
            "kind": "time_axis",
            "name": "輕鬆跑 30分",
            "steps": [
                {
                    "kind": "work",
                    "name": "輕鬆跑",
                    "duration": {"kind": "time", "seconds": 1800},
                    "target": {"kind": "open"},
                }
            ],
        },
    }
    STRENGTH = {
        "sport": "strength",
        "purpose": "背日",
        "plan": {
            "kind": "movement_list",
            "movements": [
                {
                    "name": "引體向上",
                    "sets": 3,
                    "reps": "8",
                    "load": {"kind": "assisted_kg", "kg": 15},
                }
            ],
        },
    }

    CHANGES = (
        ("a run moved to another day", RUN, {"scheduled_date": "2026-08-21"}),
        ("a run given different work", RUN, {"plan": {**RUN["plan"], "name": "節奏跑 30分"}}),
        ("a strength day renamed", STRENGTH, {"purpose": "肩日"}),
        ("a strength day moved", STRENGTH, {"scheduled_date": "2026-08-21"}),
        (
            "a strength day given different movements",
            STRENGTH,
            {"plan": {**STRENGTH["plan"], "movements": [{"name": "臥推", "sets": 5, "reps": "5"}]}},
        ),
    )

    def test_every_change_the_provider_would_see_withdraws_the_delivery(self):
        for label, base, change in self.CHANGES:
            with self.subTest(label):
                before = self._session(**base)
                after = self._session(**{**base, **change})
                self.assertNotEqual(
                    _workout_from_session(before),
                    _workout_from_session(after),
                    f"{label}: the payload has to differ for this case to mean anything",
                )
                self.assertNotEqual(
                    delivery_session_content(before), delivery_session_content(after), label
                )


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


class ProviderBoundaryTests(unittest.TestCase):
    """Issue #131: one target vocabulary, and one documented provider input.

    PlanState is the canonical executable workout and Intervals workout text is a
    rendering of it. There is no longer a second outbound representation: the
    `workout_doc` escape hatch that carried an absolute-bpm ceiling was removed in
    issue #22 after the device showed it arriving as 1-252 bpm. Each of those sentences
    is asserted here, because each has already drifted once.
    """

    def setUp(self):
        self.now = dt.datetime(2026, 8, 12, 10, 0, tzinfo=dt.timezone.utc)

    def _payload(self, plan: dict[str, Any]) -> dict[str, Any]:
        proposal = prepare_delivery_proposal(
            plan, "run-quality-01", now=self.now,
            run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR,
        )
        return _provider_payload(proposal)

    def test_the_published_schema_and_the_runtime_accept_the_same_targets(self):
        # The drift this closes: runtime delivery understood hr_ceiling while the
        # published contract could only express open and pace, so a legal plan could not
        # describe a workout the product delivers live.
        if Draft202012Validator is None:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((ROOT / "contracts" / "plan-state.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for label, plan in (
            ("pace", plan_fixture()),
            ("hr_ceiling", hr_ceiling_plan_fixture(140)),
        ):
            with self.subTest(target=label):
                self.assertEqual([], [error.message for error in validator.iter_errors(plan)])
                self.assertEqual("passed", validate_plan_state(plan)["status"])

    def test_both_layers_refuse_an_hr_ceiling_inside_a_repeat(self):
        if Draft202012Validator is None:
            self.skipTest("jsonschema is not installed")
        plan = hr_ceiling_plan_fixture(140)
        session = next(
            item for item in plan["week"]["sessions"] if item["session_id"] == "run-quality-01"
        )
        session["plan"]["steps"] = [
            {
                "kind": "repeat",
                "repetitions": 2,
                "steps": [
                    {
                        "kind": "work",
                        "name": "區段",
                        "duration": {"kind": "time", "seconds": 600},
                        "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140},
                    }
                ],
            }
        ]
        rerendered(session)
        schema = json.loads((ROOT / "contracts" / "plan-state.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())

        self.assertNotEqual([], [error.message for error in validator.iter_errors(plan)])
        self.assertEqual("blocked", validate_plan_state(plan)["status"])

    def test_an_hr_ceiling_plan_reaches_intervals_through_the_real_delivery_path(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state_dir = Path(temporary.name) / "state"
        plan = hr_ceiling_plan_fixture(140)
        init_store(state_dir, plan)
        proposal_set, approval = _confirmed_set(plan, ["run-quality-01"])
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        result = deliver_approved_set(
            state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertNotIn("workout_doc", transport.bulk_calls[0])
        self.assertEqual(
            "- Easy run 30m 50-86% LTHR", transport.bulk_calls[0]["description"]
        )

    def test_no_outbound_payload_ever_carries_a_supplied_workout_doc(self):
        # Issue #22's first acceptance line. A supplied `workout_doc` is not a documented
        # provider input, and the one path that used it delivered a ceiling the watch did
        # not enforce. Every execution model now leaves through workout text alone.
        self.assertNotIn("workout_doc", self._payload(plan_fixture()))
        self.assertNotIn("workout_doc", self._payload(hr_ceiling_plan_fixture(140)))

        strength = plan_fixture()
        session = next(
            item for item in strength["week"]["sessions"]
            if item["session_id"] == "strength-full-01"
        )
        session["match_status"] = "planned"
        session["execution"]["publish_supported"] = True
        proposal = prepare_delivery_proposal(strength, "strength-full-01", now=self.now)
        payload = _provider_payload(proposal)
        self.assertNotIn("workout_doc", payload)
        self.assertEqual("WeightTraining", payload["type"])

    def test_no_provider_representation_leaks_back_into_planstate(self):
        # A provider payload must never become a second source of workout truth: the plan
        # that produced it carries none of its fields.
        plan = hr_ceiling_plan_fixture(140)
        payload = self._payload(plan)
        session = next(
            item for item in plan["week"]["sessions"] if item["session_id"] == "run-quality-01"
        )
        for provider_field in ("external_id", "category", "start_date_local"):
            self.assertIn(provider_field, payload)
            self.assertNotIn(provider_field, session)
            self.assertNotIn(provider_field, session["plan"])
        self.assertEqual({"kind", "name", "steps"}, set(session["plan"]))


class PaceExportPrerequisiteTests(unittest.TestCase):
    """Issue #131: a pace target Intervals would accept and then export without it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.plan = plan_fixture()
        init_store(self.state_dir, self.plan)
        self.proposal_set, self.approval = _confirmed_set(self.plan, ["run-quality-01"])
        self.transport = FakeTransport()
        install_readback_builder(self.transport, self.proposal_set["items"])

    def _deliver(self) -> dict[str, Any]:
        return deliver_approved_set(
            self.state_dir, self.proposal_set, self.approval,
            transport=self.transport, now=BOUNDARY_NOW,
        )

    def test_a_configured_threshold_pace_delivers_normally(self):
        result = self._deliver()

        self.assertEqual("passed", result["status"])
        self.assertEqual(1, len(self.transport.bulk_calls))

    def test_an_observed_missing_threshold_pace_blocks_before_any_write(self):
        self.transport.threshold_pace = None

        with self.assertRaises(DeliveryError) as blocked:
            self._deliver()

        message = str(blocked.exception)
        self.assertIn("Run threshold pace", message)
        # Named apart from the coaching evidence of the same name, which is present.
        self.assertIn("athlete_baseline.threshold_pace_sec_per_km", message)
        self.assertEqual([], self.transport.bulk_calls)
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_an_unreadable_setting_is_not_read_as_a_missing_one(self):
        # The hosted path may not read athlete settings at all. "I could not look" must
        # never become "it is not there" -- and must not become a refusal either.
        self.transport.settings_readable = False

        result = self._deliver()

        self.assertEqual("passed", result["status"])
        self.assertEqual(1, len(self.transport.bulk_calls))

    def test_the_prerequisite_is_only_read_when_a_pace_target_is_delivered(self):
        state_dir = Path(self.temporary.name) / "hr-state"
        plan = hr_ceiling_plan_fixture(140)
        init_store(state_dir, plan)
        proposal_set, approval = _confirmed_set(plan, ["run-quality-01"])
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])
        transport.threshold_pace = None

        result = deliver_approved_set(
            state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertEqual(0, transport.threshold_pace_reads)

    def test_an_event_already_correct_still_verifies_while_the_setting_is_missing(self):
        # Convergence must not be held hostage by the guard: the provider already holds
        # this workout, so nothing new is being written and nothing new can be stripped.
        self._deliver()
        self.transport.threshold_pace = None
        writes = len(self.transport.bulk_calls)
        state_dir = Path(self.temporary.name) / "second"
        init_store(state_dir, self.plan)
        proposal_set, approval = _confirmed_set(self.plan, ["run-quality-01"])

        result = deliver_approved_set(
            state_dir, proposal_set, approval, transport=self.transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertEqual("deduplicated_existing", result["item_receipts"][0]["operation"])
        self.assertEqual(writes, len(self.transport.bulk_calls))


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

    def test_the_cli_reports_and_clears_the_same_recovery_state_the_gateway_sees(self):
        # Both entry points run the one delivery boundary, so an unresolved provider write
        # has to be as visible and as clearable from a terminal as it is over HTTP.
        plan = plan_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            proposal_path = root / "proposal.json"
            approval_path = root / "approval.json"
            init_store(state_dir, plan)
            proposal_set, approval = _confirmed_set(plan, ["run-quality-01"])
            for path, value in ((proposal_path, proposal_set), (approval_path, approval)):
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            transport = BreakOneReadbackTransport(
                corrupt_external_id=proposal_set["items"][0]["owned_external_id"]
            )
            transport.install(proposal_set["items"])
            credentials = IntervalsCredentials(api_key="fake", athlete_id="i42")

            with (
                mock.patch("garmin_coach_loop.cli.resolve_credentials", return_value=credentials),
                mock.patch("garmin_coach_loop.cli.IntervalsTransport", return_value=transport),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    2,
                    cli_main([
                        "publish-delivery", "--state-dir", str(state_dir),
                        "--proposal", str(proposal_path), "--approval", str(approval_path),
                        "--receipt-out", str(root / "receipt.json"),
                    ]),
                )

            # An in-flight delivery is not a broken store, so doctor still opens it -- and
            # says, in its own output, which operation is unreconciled and under which id.
            printed = io.StringIO()
            with redirect_stdout(printed):
                self.assertEqual(
                    0, cli_main(["doctor-store", "--state-dir", str(state_dir)])
                )
            reported = json.loads(printed.getvalue())["unresolved_delivery_operations"]
            self.assertEqual(
                ["run-quality-01"], [item["session_id"] for item in reported]
            )
            self.assertEqual(str(transport.events[0]["id"]), reported[0]["external_id"])

            cleared = io.StringIO()
            with redirect_stdout(cleared):
                self.assertEqual(
                    0,
                    cli_main([
                        "clear-delivery-attempt", "--state-dir", str(state_dir), "--confirm",
                    ]),
                )
            self.assertIn("run-quality-01", cleared.getvalue())
            self.assertIsNone(pending_delivery_attempt(state_dir))


BOUNDARY_NOW = dt.datetime(2026, 8, 12, 10, 0, tzinfo=dt.timezone.utc)
BOTH_SESSIONS = ["run-quality-01", "run-long-01"]


class BreakOneReadbackTransport(FakeTransport):
    """A provider that accepts every write but returns a corrupted doc for one event.

    The shape of the real 2026-08-13 failure: the write lands, the read-back does not
    match, and the event stays on the calendar. ``heal`` is the operator fixing whatever
    made it fail; the events already written stay where they are.
    """

    def __init__(self, *, corrupt_external_id: str | None = None):
        super().__init__()
        self.corrupt_external_id = corrupt_external_id
        self._honest: Any = None

    def install(self, proposals: list[dict[str, Any]]) -> None:
        install_readback_builder(self, proposals)
        self._honest = self._readback

        def build(event_id: str, event: dict[str, Any]) -> dict[str, Any]:
            readback = self._honest(event_id, event)
            if event["external_id"] == self.corrupt_external_id:
                readback["workout_doc"]["steps"] = []
            return readback

        self._readback = build  # type: ignore[method-assign]

    def heal(self, proposals: list[dict[str, Any]]) -> None:
        # Already-written events keep the body the provider last returned for them --
        # including the corrupted one, until something rewrites it.
        self.corrupt_external_id = None
        self.install(proposals)


def _confirmed_set(plan: dict[str, Any], session_ids: list[str]) -> tuple[dict, dict]:
    proposal_set = prepare_delivery_set(
        plan, session_ids, now=BOUNDARY_NOW, run_threshold_hr=FIXTURE_RUN_THRESHOLD_HR
    )
    approval = approve_delivery_set(
        proposal_set, approved_by="fixture-athlete", approved_at=BOUNDARY_NOW
    )
    return proposal_set, approval


class AmbiguousStepNameTests(unittest.TestCase):
    """Issue #75: the provider grammar must never read a name as executable meaning."""

    def _plan_with_step_name(self, name: str) -> dict[str, Any]:
        plan = plan_fixture()
        session = next(
            item
            for item in plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        session["plan"]["steps"][0]["name"] = name
        session["prescription"] = render_prescription(session["plan"])
        return plan

    def test_a_name_the_provider_reads_as_a_duration_is_refused_before_any_write(self):
        # The harmful case, exactly as it happened live: `1000m` in the name was read as
        # 1000 minutes and replaced the real distance.
        plan = self._plan_with_step_name("門檻 1000m")
        with self.assertRaises(DeliveryError) as blocked:
            prepare_delivery_proposal(plan, "run-quality-01", now=BOUNDARY_NOW)
        message = str(blocked.exception)
        self.assertIn("門檻 1000m", message)
        self.assertIn("1000m", message)
        self.assertIn("rename", message)

    def test_every_grammar_token_class_is_refused(self):
        # One case per token class the provider's published syntax guide defines. The
        # short forms below are the class the guide named and this guard did not: `5'` is
        # five minutes and `30"` is thirty seconds, exactly as `5m` and `30s` are.
        for name in (
            "門檻 5km", "間歇 400mtr", "恢復 30s", "巡航 1h",
            "節奏 5'", "衝刺 30\"", "組合 1'30\"",
            "節奏 5x", "強度 85%", "有氧 Z2", "區段 5:30",
        ):
            with self.subTest(name=name):
                plan = self._plan_with_step_name(name)
                with self.assertRaises(DeliveryError):
                    prepare_delivery_proposal(plan, "run-quality-01", now=BOUNDARY_NOW)

    def test_names_the_published_grammar_leaves_alone_still_pass(self):
        # The false-positive control: none of these carries a token the guide defines,
        # and every one is a name this product actually produces.
        for name in ("門檻節奏", "熱身輕鬆跑", "第 3 趟", "收操慢跑", "5 minutes easy"):
            with self.subTest(name=name):
                plan = self._plan_with_step_name(name)
                proposal = prepare_delivery_proposal(plan, "run-quality-01", now=BOUNDARY_NOW)
                self.assertIn(name, proposal["workout"]["description"])

    def test_the_names_the_athlete_actually_uses_still_publish(self):
        # False-positive control: purpose-first Chinese names, including one carrying a
        # digit with no unit, prepare, publish and read back clean. This is the live v27
        # workaround the issue records, kept as the control it was.
        plan = self._plan_with_step_name("門檻節奏")
        session = next(
            item
            for item in plan["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        session["plan"]["steps"][1]["steps"][0]["name"] = "第 3 趟"
        session["prescription"] = render_prescription(session["plan"])
        proposal = prepare_delivery_proposal(plan, "run-quality-01", now=BOUNDARY_NOW)
        self.assertIn("門檻節奏", proposal["workout"]["description"])
        self.assertIn("第 3 趟", proposal["workout"]["description"])

    def test_a_name_added_after_the_preview_still_cannot_reach_the_provider(self):
        plan = plan_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            init_store(state_dir, plan)
            proposal_set, approval = _confirmed_set(plan, ["run-quality-01"])

            edited = copy.deepcopy(plan)
            session = next(
                item
                for item in edited["week"]["sessions"]
                if item["session_id"] == "run-quality-01"
            )
            session["plan"]["steps"][0]["name"] = "門檻 1000m"
            session["prescription"] = render_prescription(session["plan"])
            transport = FakeTransport()
            install_readback_builder(transport, proposal_set["items"])

            # The store still holds the previewed plan, so only the derivation performed
            # at write time can catch this.
            with self.assertRaises(DeliveryError):
                publish_delivery_set(
                    proposal_set,
                    approval,
                    load_current_plan=lambda: edited,
                    transport=transport,
                    now=BOUNDARY_NOW,
                )
            self.assertEqual([], transport.bulk_calls)


class PartialDeliveryTests(unittest.TestCase):
    """Issue #110: what Intervals already accepted must never be reported as unpublished."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.plan = plan_fixture()
        init_store(self.state_dir, self.plan)

    def _current(self) -> dict[str, Any]:
        return read_current_plan(self.state_dir)["current_plan"]

    def _delivery_states(self) -> dict[str, str]:
        return {
            session["session_id"]: session["execution"]["delivery_state"]
            for session in self._current()["week"]["sessions"]
        }

    def test_a_verified_item_survives_a_later_item_failing(self):
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        second = proposal_set["items"][1]
        transport = BreakOneReadbackTransport(
            corrupt_external_id=second["owned_external_id"]
        )
        transport.install(proposal_set["items"])

        result = deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(result["item_receipts"]))
        first_session = proposal_set["items"][0]["session_id"]
        self.assertEqual(
            "intervals_accepted", self._delivery_states()[first_session]
        )
        self.assertEqual(
            "not_published", self._delivery_states()[second["session_id"]]
        )
        self.assertEqual([second["session_id"]], [
            item["session_id"] for item in result["unresolved"]
        ])
        # The failure names the event that exists but does not match (#75), so the
        # divergence is cleanable rather than invisible.
        self.assertIn("Intervals event", result["unresolved"][0]["error"])

        # Issue #121: the item that was written and failed its read-back is a provider
        # effect nothing has reconciled, so the reservation stays -- naming the session,
        # the operation and the exact event id it left on the calendar.
        self.assertTrue(result["attempt_open"])
        attempt = pending_delivery_attempt(self.state_dir)
        self.assertIsNotNone(attempt)
        outstanding = unresolved_delivery_operations(attempt)
        self.assertEqual([second["session_id"]], [item["session_id"] for item in outstanding])
        self.assertEqual("mutated_unverified", outstanding[0]["state"])
        self.assertEqual("upsert", outstanding[0]["operation"])
        self.assertEqual(second["owned_external_id"], outstanding[0]["owned_external_id"])
        self.assertEqual(
            str(transport.events[1]["id"]), outstanding[0]["external_id"]
        )
        # And it keeps fencing the plan, so nothing can move underneath the event.
        with self.assertRaises(StateStoreError):
            apply_decision(
                self.state_dir,
                context=load("coach-context-day-4.json"),
                after=load("plan-state-v2-day-4.json"),
                event=load("decision-event-day-4.json"),
            )

    def test_a_failure_that_never_reached_the_provider_releases_the_reservation(self):
        # The other half of the same rule: a reservation exists to protect a provider
        # effect. When the provider was never even read, holding it would fence the
        # athlete's next plan change behind a delivery that did not happen.
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        def refuse_to_read(day: str) -> list[dict[str, Any]]:
            raise DeliveryError("Intervals GET failed: connection refused")

        transport.list_events = refuse_to_read  # type: ignore[method-assign]
        with self.assertRaises(DeliveryError):
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        self.assertEqual([], transport.bulk_calls)
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_an_unexpected_crash_that_touched_nothing_also_releases_it(self):
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        def crash(day: str) -> list[dict[str, Any]]:
            raise RuntimeError("the transport died")

        transport.list_events = crash  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_the_unresolved_item_survives_the_process_that_found_it(self):
        # The response is lost and a new process arrives with no memory of the run.
        # Everything it needs to act is on disk.
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        second = proposal_set["items"][1]
        transport = BreakOneReadbackTransport(
            corrupt_external_id=second["owned_external_id"]
        )
        transport.install(proposal_set["items"])
        deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        reported = doctor_store(self.state_dir)["unresolved_delivery_operations"]

        self.assertEqual([second["session_id"]], [item["session_id"] for item in reported])
        self.assertEqual(
            reported, status_store(self.state_dir)["unresolved_delivery_operations"]
        )

    def test_the_retry_converges_the_unresolved_item_without_a_second_event(self):
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        first, second = proposal_set["items"]
        transport = BreakOneReadbackTransport(
            corrupt_external_id=second["owned_external_id"]
        )
        transport.install(proposal_set["items"])
        deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )
        self.assertEqual(2, len(transport.bulk_calls))

        # A retry is the same approved set. A freshly bound one is refused: the
        # reservation does not accept a second binding while this one is unreconciled.
        rebound_set, rebound_approval = _confirmed_set(
            self._current(), [second["session_id"]]
        )
        with self.assertRaises(StateStoreError) as fenced:
            deliver_approved_set(
                self.state_dir, rebound_set, rebound_approval,
                transport=transport, now=BOUNDARY_NOW,
            )
        self.assertIn("in flight", str(fenced.exception))

        transport.heal(proposal_set["items"])
        result = deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertFalse(result["attempt_open"])
        # The session that already landed is never rewritten; the unresolved one is
        # corrected in place, so Intervals still holds exactly two events.
        self.assertEqual(
            1,
            len([call for call in transport.bulk_calls
                 if call["external_id"] == first["owned_external_id"]]),
        )
        self.assertEqual(2, len(transport.events))
        self.assertEqual(
            {"intervals_accepted", "intervals_accepted"},
            {self._delivery_states()[session_id] for session_id in BOTH_SESSIONS},
        )
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_a_single_item_written_but_unverified_keeps_its_event_id(self):
        # Issue #121's first case: the only item's write lands and its read-back does not
        # match. There is no receipt to return, so the reservation is the only place the
        # event id can survive -- and releasing it here is what lost it.
        proposal_set, approval = _confirmed_set(self.plan, ["run-quality-01"])
        only = proposal_set["items"][0]
        transport = BreakOneReadbackTransport(
            corrupt_external_id=only["owned_external_id"]
        )
        transport.install(proposal_set["items"])

        with self.assertRaises(DeliveryError) as blocked:
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        self.assertIn("stays open", str(blocked.exception))
        attempt = pending_delivery_attempt(self.state_dir)
        outstanding = unresolved_delivery_operations(attempt)
        self.assertEqual("mutated_unverified", outstanding[0]["state"])
        self.assertEqual(str(transport.events[0]["id"]), outstanding[0]["external_id"])
        # PlanState stays honest about what it can observe.
        self.assertEqual("not_published", self._delivery_states()["run-quality-01"])

        # The same approved set converges it, rewriting that one event rather than adding
        # another.
        transport.heal(proposal_set["items"])
        result = deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertEqual(1, len(transport.events))
        self.assertEqual("intervals_accepted", self._delivery_states()["run-quality-01"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_a_retry_refuses_when_the_marker_now_names_a_different_event(self):
        # Only the journal can see this: the id this attempt wrote is not the id the day
        # now carries under the same marker, so two events exist and the list shows one.
        proposal_set, approval = _confirmed_set(self.plan, ["run-quality-01"])
        only = proposal_set["items"][0]
        transport = BreakOneReadbackTransport(
            corrupt_external_id=only["owned_external_id"]
        )
        transport.install(proposal_set["items"])
        with self.assertRaises(DeliveryError):
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )
        transport.heal(proposal_set["items"])
        transport.events[0]["id"] = "7777"

        with self.assertRaises(DeliveryError) as blocked:
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        self.assertIn("7777", str(blocked.exception))
        self.assertIn("same product-owned marker", str(blocked.exception))
        self.assertEqual(1, len(transport.bulk_calls))
        self.assertIsNotNone(pending_delivery_attempt(self.state_dir))

    def test_a_retry_after_the_commit_but_before_the_release_reports_the_success(self):
        # Issue #121's follow-up: state and provider are already correct, and only the
        # reservation outlived the run. The retry must not re-write, and must not report
        # a stale plan version as a failed delivery.
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])
        with mock.patch(
            "garmin_coach_loop.delivery.close_delivery_attempt",
            side_effect=RuntimeError("the process died before the release"),
        ):
            with self.assertRaises(RuntimeError):
                deliver_approved_set(
                    self.state_dir, proposal_set, approval,
                    transport=transport, now=BOUNDARY_NOW,
                )
        self.assertIsNotNone(pending_delivery_attempt(self.state_dir))
        writes = len(transport.bulk_calls)

        result = deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertFalse(result["attempt_open"])
        self.assertEqual(writes, len(transport.bulk_calls))
        self.assertEqual(sorted(BOTH_SESSIONS), sorted(result["state_update"]["session_ids"]))
        self.assertTrue(result["state_update"]["idempotent_replay"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_an_interruption_leaves_the_accepted_event_ids_on_disk(self):
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        class Interrupted(RuntimeError):
            pass

        original = transport.bulk_upsert
        state = {"writes": 0}

        def interrupt_after_the_first(event: dict[str, Any]) -> list[dict[str, Any]]:
            state["writes"] += 1
            if state["writes"] > 1:
                raise Interrupted("the process died between two items")
            return original(event)

        transport.bulk_upsert = interrupt_after_the_first  # type: ignore[method-assign]
        with self.assertRaises(Interrupted):
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        attempt = pending_delivery_attempt(self.state_dir)
        self.assertIsNotNone(attempt)
        recovered = {item["session_id"]: item for item in attempt["operations"]}
        first, second = (item["session_id"] for item in proposal_set["items"])
        self.assertEqual("verified", recovered[first]["state"])
        self.assertTrue(recovered[first]["external_id"])
        # The item the process died inside is journalled as a mutation that started and
        # was never answered -- not as an item nothing was said about.
        self.assertEqual("mutation_started", recovered[second]["state"])
        # doctor and status report it, so the interruption is visible without reading
        # the store's files by hand.
        self.assertEqual(attempt, status_store(self.state_dir)["pending_delivery_attempt"])

        # The retry writes nothing new for the item Intervals already holds.
        transport.bulk_upsert = original  # type: ignore[method-assign]
        writes_before = len(transport.bulk_calls)
        result = deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )
        self.assertEqual("passed", result["status"])
        self.assertEqual(writes_before + 1, len(transport.bulk_calls))
        self.assertEqual("deduplicated_existing", result["item_receipts"][0]["operation"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))


    def test_a_reservation_is_kept_when_the_journal_write_itself_fails(self):
        # Intervals accepted the item; only recording that it verified failed. Releasing
        # the reservation here would let the next plan change race an event nothing
        # recorded, so the mutation this run already journalled is what holds it.
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        class JournalFailure(RuntimeError):
            pass

        original = record_delivery_attempt_operation

        def refuse_the_verification(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("state") == "verified":
                raise JournalFailure("the store could not be written")
            return original(*args, **kwargs)

        with mock.patch(
            "garmin_coach_loop.delivery.record_delivery_attempt_operation",
            refuse_the_verification,
        ):
            with self.assertRaises(JournalFailure):
                deliver_approved_set(
                    self.state_dir,
                    proposal_set,
                    approval,
                    transport=transport,
                    now=BOUNDARY_NOW,
                )

        self.assertEqual(1, len(transport.bulk_calls))
        attempt = pending_delivery_attempt(self.state_dir)
        self.assertIsNotNone(attempt)
        self.assertEqual(
            ["mutated_unverified"],
            [item["state"] for item in unresolved_delivery_operations(attempt)],
        )
        with self.assertRaises(StateStoreError):
            apply_decision(
                self.state_dir,
                context=load("coach-context-day-4.json"),
                after=load("plan-state-v2-day-4.json"),
                event=load("decision-event-day-4.json"),
            )


class DeliveryFencesStoreMaintenanceTests(unittest.TestCase):
    """Issue #122: nothing may fork, replace or advertise state across a provider write."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.plan = plan_fixture()
        init_store(self.state_dir, self.plan)
        self.proposal_set, _ = _confirmed_set(self.plan, ["run-quality-01"])

    def _open(self) -> dict[str, Any]:
        return open_delivery_attempt(
            self.state_dir,
            kind="delivery",
            plan_id=self.proposal_set["plan_id"],
            plan_version=self.proposal_set["plan_version"],
            proposal_hash=self.proposal_set["proposal_hash"],
            operations=[
                {
                    "session_id": "run-quality-01",
                    "operation": "upsert",
                    "owned_external_id": self.proposal_set["items"][0]["owned_external_id"],
                    "scheduled_date": "2026-08-13",
                }
            ],
        )

    def test_a_snapshot_is_refused_while_a_delivery_is_in_flight(self):
        attempt = self._open()

        with self.assertRaises(StateStoreError) as blocked:
            snapshot_store(self.state_dir, reason="test")

        self.assertIn(attempt["attempt_id"], str(blocked.exception))
        self.assertIn("run-quality-01", str(blocked.exception))
        self.assertFalse((self.root / "state.snapshots").exists())

    def test_a_confirmed_restore_is_refused_before_either_directory_moves(self):
        snapshot = snapshot_store(self.state_dir, reason="before")
        attempt = self._open()
        before = sorted(path.name for path in self.root.iterdir())

        with self.assertRaises(StateStoreError) as blocked:
            restore_snapshot(snapshot["snapshot_dir"], self.state_dir, confirm=True)

        self.assertIn(attempt["attempt_id"], str(blocked.exception))
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))
        self.assertEqual(attempt, pending_delivery_attempt(self.state_dir))

    def test_copy_adoption_is_refused_and_creates_no_destination(self):
        attempt = self._open()
        destination = self.root / "owners" / "copied"

        with self.assertRaises(StateStoreError) as blocked:
            adopt_store(self.state_dir, destination, mode="copy", confirm=True)

        self.assertIn(attempt["attempt_id"], str(blocked.exception))
        self.assertFalse(destination.exists())

    def test_link_adoption_keeps_referring_to_the_same_reservation(self):
        attempt = self._open()
        destination = self.root / "owners" / "linked"

        adopted = adopt_store(self.state_dir, destination, mode="link", confirm=True)

        self.assertEqual(attempt["attempt_id"], adopted["pending_delivery_attempt"])
        # One reservation, not two: releasing it through the adopted path releases it.
        self.assertEqual(attempt, pending_delivery_attempt(destination))
        close_delivery_attempt(destination, attempt_id=attempt["attempt_id"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_maintenance_resumes_once_the_reservation_is_settled(self):
        self._open()
        close_delivery_attempt(self.state_dir)

        snapshot = snapshot_store(self.state_dir, reason="test")
        restored = restore_snapshot(snapshot["snapshot_dir"], self.state_dir, confirm=True)
        adopted = adopt_store(
            self.state_dir, self.root / "owners" / "copied", mode="copy", confirm=True
        )

        self.assertEqual("restored", restored["status"])
        self.assertEqual("adopted", adopted["status"])
        self.assertIsNone(pending_delivery_attempt(Path(snapshot["snapshot_dir"])))

    def test_a_snapshot_does_not_carry_this_machine_s_delivery_reservation(self):
        # A restored store must not arrive fenced behind a delivery that is not happening.
        snapshot = snapshot_store(self.state_dir, reason="test")

        self.assertIsNone(pending_delivery_attempt(Path(snapshot["snapshot_dir"])))

    def test_an_unreadable_reservation_blocks_doctor_with_a_way_out(self):
        self._open()
        (self.state_dir / DELIVERY_ATTEMPT_FILE).write_text("{oh no", encoding="utf-8")

        report = doctor_store(self.state_dir)

        self.assertEqual("blocked", report["status"])
        self.assertIn("clear-delivery-attempt", report["delivery_attempt_error"])
        # It fences every maintenance command too, rather than reading as "no delivery".
        for operation in (
            lambda: snapshot_store(self.state_dir, reason="test"),
            lambda: adopt_store(
                self.state_dir, self.root / "owners" / "copied", mode="copy", confirm=True
            ),
        ):
            with self.assertRaises(StateStoreError):
                operation()

        cleared = close_delivery_attempt(self.state_dir)

        self.assertTrue(cleared["cleared"])
        self.assertIn("unreadable", cleared)
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])

    def test_a_reservation_written_under_the_old_schema_is_refused_not_guessed(self):
        self._open()
        (self.state_dir / DELIVERY_ATTEMPT_FILE).write_text(
            json.dumps({"schema_version": "1.0", "attempt_id": "old", "verified": []}),
            encoding="utf-8",
        )

        with self.assertRaises(StateStoreError) as blocked:
            pending_delivery_attempt(self.state_dir)

        self.assertIn("schema_version", str(blocked.exception))

    def test_clearing_a_reservation_reports_what_it_abandoned(self):
        attempt = self._open()
        record_delivery_attempt_operation(
            self.state_dir,
            attempt_id=attempt["attempt_id"],
            session_id="run-quality-01",
            state="mutated_unverified",
            external_id="9001",
        )

        cleared = close_delivery_attempt(self.state_dir)

        self.assertEqual(
            [
                {
                    "session_id": "run-quality-01",
                    "operation": "upsert",
                    "state": "mutated_unverified",
                    "external_id": "9001",
                    "owned_external_id": self.proposal_set["items"][0]["owned_external_id"],
                }
            ],
            cleared["abandoned"],
        )
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_abandoning_unresolved_operations_has_to_name_the_reservation(self):
        """Issue #16: the same recovery, from a caller with no filesystem to point at.

        A delivery closing its own reservation still may not abandon anything, so the
        flag exists to let a person do it -- and a person who cannot say which reservation
        they were looking at has not identified one.
        """
        attempt = self._open()
        record_delivery_attempt_operation(
            self.state_dir,
            attempt_id=attempt["attempt_id"],
            session_id="run-quality-01",
            state="mutated_unverified",
            external_id="9001",
        )

        with self.assertRaises(StateStoreError):
            close_delivery_attempt(self.state_dir, abandon_unresolved=True)
        with self.assertRaises(StateStoreError):
            close_delivery_attempt(
                self.state_dir, attempt_id="delivery-attempt-other", abandon_unresolved=True
            )
        # And without the flag the delivery's own close is still refused, unchanged.
        with self.assertRaises(StateStoreError):
            close_delivery_attempt(self.state_dir, attempt_id=attempt["attempt_id"])
        self.assertEqual(attempt["attempt_id"], pending_delivery_attempt(self.state_dir)["attempt_id"])

        cleared = close_delivery_attempt(
            self.state_dir, attempt_id=attempt["attempt_id"], abandon_unresolved=True
        )

        self.assertTrue(cleared["cleared"])
        self.assertEqual(
            ["run-quality-01"], [item["session_id"] for item in cleared["abandoned"]]
        )
        self.assertIsNone(pending_delivery_attempt(self.state_dir))


class DeliveryFencesPlanWritesTests(unittest.TestCase):
    """A plan revision may not land in the middle of a provider write, and vice versa."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.plan = plan_fixture()
        init_store(self.state_dir, self.plan)

    def _apply_a_real_decision(self) -> None:
        after = load("plan-state-v2-day-4.json")
        for session in after["week"]["sessions"]:
            if session["session_id"] in BOTH_SESSIONS:
                session["execution"]["publish_supported"] = True
        apply_decision(
            self.state_dir,
            context=load("coach-context-day-4.json"),
            after=after,
            event=load("decision-event-day-4.json"),
        )

    def test_a_plan_change_before_the_first_write_produces_zero_provider_writes(self):
        proposal_set, approval = _confirmed_set(self.plan, ["run-quality-01"])
        self._apply_a_real_decision()
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        with self.assertRaises(DeliveryError) as blocked:
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        self.assertIn("version changed", str(blocked.exception))
        self.assertEqual([], transport.bulk_calls)
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_a_plan_change_is_refused_while_a_delivery_is_in_flight(self):
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])
        original = transport.bulk_upsert
        blocked_change: dict[str, Any] = {}

        def change_the_plan_mid_set(event: dict[str, Any]) -> list[dict[str, Any]]:
            if not blocked_change:
                try:
                    self._apply_a_real_decision()
                except StateStoreError as exc:
                    blocked_change["error"] = str(exc)
            return original(event)

        transport.bulk_upsert = change_the_plan_mid_set  # type: ignore[method-assign]
        result = deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )

        self.assertEqual("passed", result["status"])
        self.assertIn("in flight", blocked_change.get("error", ""))

    def test_reads_continue_while_a_delivery_is_in_flight(self):
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])
        original = transport.bulk_upsert
        seen: list[int] = []

        def read_mid_set(event: dict[str, Any]) -> list[dict[str, Any]]:
            seen.append(read_current_plan(self.state_dir)["current_version"])
            return original(event)

        transport.bulk_upsert = read_mid_set  # type: ignore[method-assign]
        deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )
        self.assertEqual([1, 1], seen)

    def test_an_older_checkout_meeting_a_newer_contract_writes_nothing(self):
        proposal_set, approval = _confirmed_set(self.plan, ["run-quality-01"])
        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["writer_contract_version"] = WRITER_CONTRACT_VERSION + 1
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        with self.assertRaises(StateStoreError) as blocked:
            deliver_approved_set(
                self.state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
            )

        self.assertIn("writer-contract", str(blocked.exception))
        self.assertEqual([], transport.bulk_calls)


# --------------------------------------------------------------------------------------
# Issue #113: a confirmed change must not leave the superseded workout live on Intervals
# --------------------------------------------------------------------------------------


def coaching_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "summary": "調整本週安排",
        "reason_codes": ["schedule_or_equipment_changed"],
        "evidence": [{"field": "constraints", "observation": "本週行程改變"}],
        "goal_effect": {"week": "本週安排調整", "cycle": "28 天方向不變"},
        "next_review_condition": "下一次 anchor 前重新評估",
        "sessions": [],
    }
    request.update(overrides)
    return request


class WithdrawalCliTests(unittest.TestCase):
    """The same withdrawal boundary, reached the way a terminal reaches it."""

    def test_one_cli_path_prepares_approves_withdraws_and_updates_current_state(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state_dir = root / "state"
        plan = plan_fixture()
        init_store(state_dir, plan)

        transport = FakeTransport()
        proposal_set, approval = _confirmed_set(plan, ["run-quality-01"])
        install_readback_builder(transport, proposal_set["items"])
        deliver_approved_set(
            state_dir, proposal_set, approval, transport=transport, now=BOUNDARY_NOW
        )
        delivered_id = str(transport.events[0]["id"])

        # A confirmed change replaces the delivered run with something unpublishable.
        current = read_current_plan(state_dir)["current_plan"]
        context = load("coach-context-day-4.json")
        context["goal_context"] = _expected_goal_context(current)
        context["athlete_baseline"] = _expected_context_baseline(current)
        context["current_calendar"] = _expected_current_calendar(current)
        projection = project_change_request(
            current,
            coaching_request(sessions=[{
                "operation": "replace",
                "session_id": "run-quality-01",
                "sport": "rest",
                "purpose": "完全休息",
                "adaptation": "recovery",
                "cost": "easy",
                "planned_minutes": 0,
                "plan": {"kind": "unstructured"},
            }]),
            context=context,
            issued_at=dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc),
        )
        apply_decision(
            state_dir,
            context=context,
            after=projection["after_plan"],
            event=projection["decision_event"],
        )

        proposal_path = root / "withdrawal.json"
        approval_path = root / "withdrawal-approval.json"
        receipt_path = root / "withdrawal-receipt.json"
        credentials = IntervalsCredentials(api_key="fake", athlete_id="i42")
        with (
            mock.patch("garmin_coach_loop.cli.resolve_credentials", return_value=credentials),
            mock.patch("garmin_coach_loop.cli.IntervalsTransport", return_value=transport),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, cli_main([
                "prepare-withdrawal", "--state-dir", str(state_dir),
                "--session", "run-quality-01", "--out", str(proposal_path),
            ]))
            self.assertEqual(0, cli_main([
                "approve-withdrawal", "--proposal", str(proposal_path),
                "--approved-by", "fixture-athlete", "--out", str(approval_path),
            ]))
            self.assertEqual(0, cli_main([
                "withdraw-delivery", "--state-dir", str(state_dir),
                "--proposal", str(proposal_path), "--approval", str(approval_path),
                "--receipt-out", str(receipt_path), "--today", "2026-08-13",
            ]))

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("passed", receipt["status"])
        self.assertEqual([delivered_id], transport.deleted)
        self.assertEqual([], transport.events)
        session = next(
            item
            for item in read_current_plan(state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertNotIn("superseded_external_id", session["execution"])


class SupersededDeliveryTests(unittest.TestCase):
    """The calendar and the plan may diverge, but never silently."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.plan = plan_fixture()
        init_store(self.state_dir, self.plan)
        self.transport = FakeTransport()
        self.session_id = "run-quality-01"
        proposal_set, approval = _confirmed_set(self.plan, [self.session_id])
        install_readback_builder(self.transport, proposal_set["items"])
        deliver_approved_set(
            self.state_dir,
            proposal_set,
            approval,
            transport=self.transport,
            now=BOUNDARY_NOW,
        )
        self.owned_external_id = proposal_set["items"][0]["owned_external_id"]

    def current(self) -> dict[str, Any]:
        return read_current_plan(self.state_dir)["current_plan"]

    def session(self, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        sessions = (plan or self.current())["week"]["sessions"]
        return next(item for item in sessions if item["session_id"] == self.session_id)

    def change(self, operation: dict[str, Any]) -> dict[str, Any]:
        """Run one real coaching change through the same path the gateway uses."""
        before = self.current()
        context = load("coach-context-day-4.json")
        # The context has to project the plan the change is made against, which by now
        # carries the recorded delivery.
        context["goal_context"] = _expected_goal_context(before)
        context["athlete_baseline"] = _expected_context_baseline(before)
        context["current_calendar"] = _expected_current_calendar(before)
        projection = project_change_request(
            before,
            coaching_request(sessions=[operation]),
            context=context,
            issued_at=dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc),
        )
        apply_decision(
            self.state_dir,
            context=context,
            after=projection["after_plan"],
            event=projection["decision_event"],
        )
        return self.current()

    def test_a_change_records_the_event_it_superseded(self):
        delivered_event_id = self.session()["execution"]["external_id"]
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        execution = self.session()["execution"]
        self.assertEqual("not_published", execution["delivery_state"])
        self.assertIsNone(execution["external_id"])
        # The event still exists on Intervals, so the plan says so.
        self.assertEqual(delivered_event_id, execution["superseded_external_id"])
        self.assertEqual(1, len(self.transport.events))

    def test_republishing_a_moved_session_leaves_exactly_one_event_on_the_new_date(self):
        delivered_event_id = self.session()["execution"]["external_id"]
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        proposal_set, approval = _confirmed_set(self.current(), [self.session_id])
        install_readback_builder(self.transport, proposal_set["items"])
        deliver_approved_set(
            self.state_dir, proposal_set, approval, transport=self.transport, now=BOUNDARY_NOW
        )

        owned = [
            event for event in self.transport.events
            if event.get("external_id") == self.owned_external_id
        ]
        self.assertEqual(1, len(owned))
        self.assertEqual("2026-08-15", str(owned[0]["start_date_local"])[:10])
        self.assertEqual(delivered_event_id, str(owned[0]["id"]))
        execution = self.session()["execution"]
        self.assertEqual("intervals_accepted", execution["delivery_state"])
        # The replacement overwrote the very event that was superseded, so nothing is
        # outstanding any more.
        self.assertNotIn("superseded_external_id", execution)

    def _withdraw(self, **kwargs: Any) -> dict[str, Any]:
        return self._bind_withdrawal(**kwargs)()

    def _bind_withdrawal(self, **kwargs: Any) -> Callable[[], dict[str, Any]]:
        """One confirmed withdrawal, callable more than once.

        A retry re-sends the set the athlete confirmed rather than preparing a new one --
        which is also the only way to retry, since a proposal carries its own creation
        time and a fresh one would not hash to what is bound.
        """
        proposal_set = prepare_withdrawal_set(self.current(), [self.session_id])
        approval = approve_withdrawal_set(proposal_set, approved_by="fixture-athlete")
        today = kwargs.pop("today", "2026-08-13")
        return lambda: withdraw_approved_set(
            self.state_dir,
            proposal_set,
            approval,
            transport=self.transport,
            now=BOUNDARY_NOW,
            today=today,
            **kwargs,
        )

    def test_a_session_that_can_no_longer_be_published_is_withdrawn_after_confirmation(self):
        self.change({
            "operation": "replace",
            "session_id": self.session_id,
            "sport": "rest",
            "purpose": "完全休息",
            "adaptation": "recovery",
            "cost": "easy",
            "planned_minutes": 0,
            "plan": {"kind": "unstructured"},
        })
        # Nothing publishable can replace the old event, which is what made this
        # unrecoverable before: prepare-delivery refuses.
        with self.assertRaises(DeliveryError):
            prepare_delivery_set(self.current(), [self.session_id])

        superseded = self.session()["execution"]["superseded_external_id"]
        result = self._withdraw()

        self.assertEqual("passed", result["status"])
        self.assertEqual([superseded], self.transport.deleted)
        self.assertEqual([], self.transport.events)
        self.assertNotIn("superseded_external_id", self.session()["execution"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_a_delete_whose_confirmation_fails_stays_recoverable(self):
        # Issue #121's third case: the event may be gone and the product cannot prove it.
        # The delete happened, so the reservation must survive the failure that follows.
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        superseded = self.session()["execution"]["superseded_external_id"]
        # One confirmed withdrawal, run twice: the retry is the same approved set, exactly
        # as it is for a delivery.
        retry = self._bind_withdrawal()
        confirmations = {"count": 0}
        original = self.transport.find_event

        def fail_the_confirmation(event_id: str) -> dict[str, Any] | None:
            confirmations["count"] += 1
            if confirmations["count"] == 2:
                raise DeliveryError("Intervals GET failed: connection reset")
            return original(event_id)

        self.transport.find_event = fail_the_confirmation  # type: ignore[method-assign]
        with self.assertRaises(DeliveryError) as blocked:
            retry()

        self.assertIn("stays open", str(blocked.exception))
        self.assertEqual([superseded], self.transport.deleted)
        attempt = pending_delivery_attempt(self.state_dir)
        outstanding = unresolved_delivery_operations(attempt)
        self.assertEqual("delete", outstanding[0]["operation"])
        self.assertEqual("mutated_unverified", outstanding[0]["state"])
        self.assertEqual(superseded, outstanding[0]["external_id"])
        # The plan still says the event is outstanding, and stays fenced until it is not.
        self.assertEqual(superseded, self.session()["execution"]["superseded_external_id"])
        with self.assertRaises(StateStoreError):
            self.change({
                "operation": "move",
                "session_id": self.session_id,
                "scheduled_date": "2026-08-16",
            })

        # The retry asks Intervals again, finds the event already gone, and records it --
        # without a second delete.
        self.transport.find_event = original  # type: ignore[method-assign]
        result = retry()

        self.assertEqual("passed", result["status"])
        self.assertEqual([superseded], self.transport.deleted)
        self.assertNotIn("superseded_external_id", self.session()["execution"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_a_withdrawal_interrupted_after_recording_recovers_by_itself(self):
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        self._withdraw()
        # The reservation outlived the run that recorded its result -- the shape a crash
        # between the commit and the release leaves behind. The plan already says the
        # superseded event is gone, so reconciliation settles it without asking Intervals.
        proposal_set = prepare_delivery_set(self.current(), [self.session_id])
        attempt = open_delivery_attempt(
            self.state_dir,
            kind="withdrawal",
            plan_id=proposal_set["plan_id"],
            plan_version=proposal_set["plan_version"],
            proposal_hash="a-stale-withdrawal",
            operations=[
                {
                    "session_id": self.session_id,
                    "operation": "delete",
                    "owned_external_id": "gcl:stale",
                    "scheduled_date": "2026-08-15",
                }
            ],
        )
        record_delivery_attempt_operation(
            self.state_dir,
            attempt_id=attempt["attempt_id"],
            session_id=self.session_id,
            state="verified",
            external_id="129007775",
            result={"withdrawal": {"session_id": self.session_id}},
        )
        stale = pending_delivery_attempt(self.state_dir)

        settled = _reconcile_attempt(self.state_dir, stale)

        self.assertEqual(
            ["recorded"], [item["state"] for item in settled["operations"]]
        )

    def test_a_withdrawal_retried_after_its_commit_reports_the_success(self):
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        superseded = self.session()["execution"]["superseded_external_id"]
        # The retry is the same confirmed withdrawal. Re-preparing one is not possible:
        # the plan already stopped holding the superseded event when the commit landed.
        retry = self._bind_withdrawal()
        with mock.patch(
            "garmin_coach_loop.delivery.close_delivery_attempt",
            side_effect=RuntimeError("the process died before the release"),
        ):
            with self.assertRaises(RuntimeError):
                retry()
        self.assertIsNotNone(pending_delivery_attempt(self.state_dir))

        result = retry()

        self.assertEqual("passed", result["status"])
        self.assertFalse(result["attempt_open"])
        # One delete, not two, and the plan already agreed before the retry ran.
        self.assertEqual([superseded], self.transport.deleted)
        self.assertTrue(result["state_update"]["idempotent_replay"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_a_withdrawal_never_touches_an_event_this_product_does_not_own(self):
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        someone_elses = {
            "id": "555001",
            "external_id": "strava:not-ours",
            "start_date_local": "2026-08-15T00:00:00",
            "category": "WORKOUT",
        }
        self.transport.events.append(someone_elses)
        self._withdraw()
        self.assertEqual([someone_elses], self.transport.events)

    def test_two_events_carrying_the_owned_marker_are_refused_rather_than_guessed_between(self):
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        duplicate = copy.deepcopy(self.transport.events[0])
        duplicate["id"] = "999999"
        duplicate["start_date_local"] = "2026-08-16T00:00:00"
        self.transport.events.append(duplicate)
        with self.assertRaises(DeliveryError) as blocked:
            self._withdraw()
        self.assertIn("multiple Intervals events", str(blocked.exception))
        self.assertEqual([], self.transport.deleted)

    def test_a_past_day_s_record_is_not_deleted_by_editing_a_future_plan(self):
        # The session moves forward; the event it superseded stays on the day it was
        # delivered for. What is being deleted is that event, so its date decides -- the
        # session's own date is the new one and would wave this through.
        delivered_day = self.session()["scheduled_date"]
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        self.assertEqual(
            delivered_day, str(self.transport.events[0]["start_date_local"])[:10]
        )

        with self.assertRaises(DeliveryError) as blocked:
            self._withdraw(today="2026-08-14")

        self.assertIn("has passed", str(blocked.exception))
        self.assertIn(delivered_day, str(blocked.exception))
        self.assertEqual([], self.transport.deleted)
        self.assertEqual(1, len(self.transport.events))

    def test_absence_is_only_ever_the_provider_saying_this_event_is_gone(self):
        # A list that comes back empty is also what a provider returns when it cannot
        # answer. Recording that as "withdrawn" would tell the athlete their calendar
        # matches the plan while the superseded workout is still on their watch.
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        superseded = self.session()["execution"]["superseded_external_id"]
        hidden = [event for event in self.transport.events]
        self.transport.events = []          # the range read answers nothing...
        self.transport.readbacks.pop(superseded, None)

        def still_there(event_id: str) -> dict[str, Any] | None:
            return next(
                (event for event in hidden if str(event.get("id")) == str(event_id)), None
            )

        self.transport.find_event = still_there  # type: ignore[method-assign]

        with self.assertRaises(DeliveryError) as blocked:
            self._withdraw()

        # The empty list decides nothing: the event id is looked up directly, and while
        # the provider still reports it, no withdrawal is recorded.
        self.assertIn("still holds", str(blocked.exception))
        self.assertEqual(
            superseded, self.session()["execution"].get("superseded_external_id")
        )

    def test_a_tampered_withdrawal_commit_is_caught_when_the_store_is_reopened(self):
        # The write path holds a withdrawal to one narrow mutation. Replay has to hold it
        # to the same one, or the fence only exists for as long as the process that wrote
        # the commit -- which is what `doctor-store` re-reading all of history is for.
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        self._withdraw()
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])

        commits = sorted((self.state_dir / "commits").iterdir())
        withdrawal_commit = commits[-1]
        plan = json.loads((withdrawal_commit / "plan.json").read_text(encoding="utf-8"))
        session = next(
            item for item in plan["week"]["sessions"] if item["session_id"] == self.session_id
        )
        # A change no withdrawal may make, smuggled into a commit that claims to be one.
        session["planned_minutes"] = session["planned_minutes"] + 5
        (withdrawal_commit / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt = json.loads((withdrawal_commit / "receipt.json").read_text(encoding="utf-8"))
        receipt["plan_hash"] = canonical_hash(plan)
        receipt.pop("receipt_hash")
        receipt["receipt_hash"] = canonical_hash(receipt)
        (withdrawal_commit / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = doctor_store(self.state_dir)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("withdrawal event must not change session field" in error for error in report["errors"]),
            report["errors"],
        )

    def test_an_event_already_gone_from_the_calendar_still_records_cleanly(self):
        self.change({
            "operation": "move",
            "session_id": self.session_id,
            "scheduled_date": "2026-08-15",
        })
        # The athlete deleted it in Intervals themselves.
        self.transport.events = []
        result = self._withdraw()
        self.assertEqual("passed", result["status"])
        self.assertEqual([], self.transport.deleted)
        self.assertNotIn("superseded_external_id", self.session()["execution"])


if __name__ == "__main__":
    unittest.main()
