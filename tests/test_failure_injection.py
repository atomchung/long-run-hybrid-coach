"""Failure-injection regressions for the transaction and provider boundary.

Each class is a real failure this product has hit — a restart between preview and
confirm, a process death inside a store commit, a calendar write that landed and a
second that did not, a duplicate confirm, a proposal presented as the wrong athlete,
a plan that moved underneath a yes, and a corrupted hold. The matching control is
the legitimate look-alike that must still succeed.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop.delivery import DeliveryError, deliver_approved_set
from garmin_coach_loop.store import (
    StateStoreError,
    apply_decision,
    doctor_store,
    init_store,
    pending_delivery_attempt,
    read_current_plan,
    restore_snapshot,
    snapshot_store,
    unresolved_delivery_operations,
)
from failure_injection import (
    ProcessDeath,
    assert_payload_hides,
    assert_unreconciled_delivery_fences,
    corrupt_held_transaction,
    crash_during_store_commit,
    fail_nth_calendar_write,
    fail_nth_event_delete,
    isolated_product_home,
    restart_gateway,
)
from test_delivery import (
    BOUNDARY_NOW,
    BOTH_SESSIONS,
    FakeTransport,
    _confirmed_set,
    install_readback_builder,
    plan_fixture,
)
from test_gateway import (
    ONBOARDING,
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    TOKEN_B,
    WEEKLY_CHANGE,
    GatewayTestCase,
    as_change_request,
    coaching_request,
    load,
    publishable_plan,
)


class IsolatedHomeMixin:
    def setUp(self):
        super().setUp()
        self._home_guard = isolated_product_home(getattr(self, "state_root", None))
        self._home_guard.__enter__()
        self.addCleanup(self._home_guard.__exit__, None, None, None)


# --------------------------------------------------------------------------------------
# 1. Restart between prepare and apply
# --------------------------------------------------------------------------------------


class RestartBetweenPrepareAndApplyTests(IsolatedHomeMixin, GatewayTestCase):
    """A deploy or crash forgets the hold; the signed proposal still opens."""

    def setUp(self):
        super().setUp()
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

    def _apply(self, kind: str, proposal: str, *, token: str = TOKEN_A) -> tuple[int, Any]:
        return self.route(
            kind, body={"proposal": proposal, "confirmed": True}, token=token
        )

    def test_uncommitted_first_plan_apply_after_restart_requires_prepare(self):
        """Railway redeploy between preview and yes must not invent a first plan."""
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)
        status, prepared = self.route(
            "decision_prepare",
            body={"change_request": as_change_request(ONBOARDING)},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        preview_goal = prepared["preview"]["goal"]["outcome"]
        restart_gateway(self)

        status, payload = self._apply("decision_apply", prepared["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_expired", payload["error"])
        self.assertIn("prepare again", payload["detail"])
        self.assertNotIn("prepared", payload)
        self.assertFalse((self.state_dir / "store.json").exists())
        assert_payload_hides(self, payload, preview_goal)

    def test_uncommitted_decision_apply_after_restart_requires_prepare(self):
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        self.assertEqual(200, status, session)
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        before = self.snapshot(self.state_dir)
        restart_gateway(self)

        status, payload = self._apply("decision_apply", prepared["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_expired", payload["error"])
        self.assertIn("prepare again", payload["detail"])
        self.assertNotIn("prepared", payload)
        self.assertEqual(before, self.snapshot(self.state_dir))
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_uncommitted_delivery_apply_after_restart_requires_prepare(self):
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)
        current = read_current_plan(self.state_dir)
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        before = self.snapshot(self.state_dir)
        restart_gateway(self)

        status, payload = self._apply("delivery_apply", prepared["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_expired", payload["error"])
        self.assertIn("prepare again", payload["detail"])
        self.assertNotIn("resume_attempt_id", payload.get("detail") or "")
        self.assertEqual(before, self.snapshot(self.state_dir))
        self.assertEqual([], self.fake.events)
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_committed_decision_replay_after_restart_is_identical(self):
        """False-positive control: a yes that already landed still converges after a deploy."""
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        status, applied = self._apply("decision_apply", prepared["proposal"])
        self.assertEqual(200, status, applied)
        committed = self.snapshot(self.state_dir)
        restart_gateway(self)

        status, replayed = self._apply("decision_apply", prepared["proposal"])

        self.assertEqual(200, status, replayed)
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(applied["plan_version"], replayed["plan_version"])
        self.assertEqual(committed, self.snapshot(self.state_dir))


# --------------------------------------------------------------------------------------
# 2. Crash mid-commit
# --------------------------------------------------------------------------------------


class CrashMidCommitTests(unittest.TestCase):
    """A death inside the store writer must not leave an unopenable history without a way back."""

    def setUp(self):
        self._home_guard = isolated_product_home()
        self.home = self._home_guard.__enter__()
        self.addCleanup(self._home_guard.__exit__, None, None, None)
        self.state_dir = self.home / "state"
        init_store(self.state_dir, load("plan-state-v1.json"))
        self.context = load("coach-context-day-4.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")

    def _apply(self) -> Any:
        return apply_decision(
            self.state_dir,
            context=self.context,
            after=self.after,
            event=self.event,
        )

    def _restore_from(self, snapshot_dir: Path) -> None:
        restored = restore_snapshot(snapshot_dir, self.state_dir, confirm=True)
        self.assertEqual("restored", restored["status"])
        report = doctor_store(self.state_dir)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(1, report["current_version"])
        self.assertEqual(
            load("plan-state-v1.json"),
            read_current_plan(self.state_dir)["current_plan"],
        )

    def test_crash_while_pending_files_are_written_blocks_doctor_and_restore_recovers(self):
        """Process dies after writing the pending commit directory, before it is named."""
        snapshot = snapshot_store(self.state_dir, reason="before-crash")
        with crash_during_store_commit(after="pending-files"):
            with self.assertRaises(ProcessDeath):
                self._apply()

        pending = list((self.state_dir / "commits").glob(".pending-*"))
        self.assertEqual(1, len(pending), pending)
        report = doctor_store(self.state_dir)
        self.assertEqual("blocked", report["status"], report)
        self.assertTrue(
            any("incomplete pending commit" in error for error in report["errors"]),
            report["errors"],
        )
        with self.assertRaises(StateStoreError):
            apply_decision(
                self.state_dir,
                context=self.context,
                after=self.after,
                event=self.event,
            )
        self._restore_from(Path(snapshot["snapshot_dir"]))

    def test_crash_after_commit_dir_before_manifest_blocks_doctor_and_restore_recovers(self):
        """Process dies after the commit directory is renamed, before store.json moves."""
        snapshot = snapshot_store(self.state_dir, reason="before-crash")
        with crash_during_store_commit(after="commit-dir-before-manifest"):
            with self.assertRaises(ProcessDeath):
                self._apply()

        self.assertEqual([], list((self.state_dir / "commits").glob(".pending-*")))
        commits = [
            path
            for path in (self.state_dir / "commits").iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
        self.assertEqual(2, len(commits), [path.name for path in commits])
        manifest = json.loads((self.state_dir / "store.json").read_text(encoding="utf-8"))
        self.assertEqual(1, manifest["current_version"])
        json.loads((self.state_dir / "store.json").read_text(encoding="utf-8"))
        report = doctor_store(self.state_dir)
        self.assertEqual("blocked", report["status"], report)
        self.assertTrue(
            any("does not match immutable history" in error for error in report["errors"]),
            report["errors"],
        )
        self._restore_from(Path(snapshot["snapshot_dir"]))

    def test_handled_exception_during_commit_leaves_the_previous_version_openable(self):
        """False-positive control: a caught writer failure is atomic, not a blocked store."""
        with crash_during_store_commit(after="handled-exception"):
            with self.assertRaises(RuntimeError):
                self._apply()

        self.assertEqual([], list((self.state_dir / "commits").glob(".pending-*")))
        report = doctor_store(self.state_dir)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(1, report["current_version"])
        self.assertEqual(
            load("plan-state-v1.json"),
            read_current_plan(self.state_dir)["current_plan"],
        )

    def test_a_completed_commit_still_opens(self):
        """False-positive control: the writer that finished is still a valid store."""
        result = self._apply()
        self.assertEqual("passed", result["status"])
        report = doctor_store(self.state_dir)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(2, report["current_version"])
        self.assertEqual(self.after, read_current_plan(self.state_dir)["current_plan"])


# --------------------------------------------------------------------------------------
# 3. Partial provider effect
# --------------------------------------------------------------------------------------


class PartialProviderEffectTests(unittest.TestCase):
    """The first calendar write landed; the second did not."""

    def setUp(self):
        self._home_guard = isolated_product_home()
        self.home = self._home_guard.__enter__()
        self.addCleanup(self._home_guard.__exit__, None, None, None)
        self.state_dir = self.home / "state"
        self.plan = plan_fixture()
        init_store(self.state_dir, self.plan)

    def _open_partial(self) -> tuple[dict[str, Any], dict[str, Any], FakeTransport]:
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])
        restore = fail_nth_calendar_write(transport, 2)
        result = deliver_approved_set(
            self.state_dir,
            proposal_set,
            approval,
            transport=transport,
            now=BOUNDARY_NOW,
        )
        restore()
        self.assertTrue(result["attempt_open"], result)
        self.assertEqual(1, len(transport.events), transport.events)
        return proposal_set, approval, transport

    def test_second_calendar_write_failure_journals_the_first_and_retry_converges(self):
        snapshot = snapshot_store(self.state_dir, reason="before-partial")
        proposal_set, approval, transport = self._open_partial()

        self.assertEqual(1, len(transport.events))
        attempt = pending_delivery_attempt(self.state_dir)
        self.assertIsNotNone(attempt)
        outstanding = unresolved_delivery_operations(attempt)
        self.assertTrue(outstanding, attempt)
        first_id = proposal_set["items"][0]["session_id"]
        recovered = {item["session_id"]: item for item in attempt["operations"]}
        self.assertEqual("recorded", recovered[first_id]["state"])
        self.assertTrue(recovered[first_id]["external_id"])

        assert_unreconciled_delivery_fences(
            self,
            self.state_dir,
            snapshot_dir=Path(snapshot["snapshot_dir"]),
            copy_destination=self.home / "copied-owner",
        )

        writes_before = len(transport.bulk_calls)
        result = deliver_approved_set(
            self.state_dir,
            proposal_set,
            approval,
            transport=transport,
            now=BOUNDARY_NOW,
        )
        self.assertEqual("passed", result["status"], result)
        self.assertFalse(result["attempt_open"])
        self.assertEqual(2, len(transport.events))
        self.assertEqual(writes_before + 1, len(transport.bulk_calls))
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_failure_before_any_provider_write_releases_the_reservation(self):
        """False-positive control: nothing reached Intervals, so the fence must not stay."""
        proposal_set, approval = _confirmed_set(self.plan, BOTH_SESSIONS)
        transport = FakeTransport()
        install_readback_builder(transport, proposal_set["items"])

        def refuse_to_read(day: str) -> list[dict[str, Any]]:
            raise DeliveryError("Intervals GET failed: connection refused")

        transport.list_events = refuse_to_read  # type: ignore[method-assign]
        with self.assertRaises(DeliveryError):
            deliver_approved_set(
                self.state_dir,
                proposal_set,
                approval,
                transport=transport,
                now=BOUNDARY_NOW,
            )
        self.assertEqual([], transport.events)
        self.assertEqual([], transport.bulk_calls)
        self.assertIsNone(pending_delivery_attempt(self.state_dir))


# --------------------------------------------------------------------------------------
# 4. Replay
# --------------------------------------------------------------------------------------


class ReplayApplyTests(IsolatedHomeMixin, GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def test_duplicate_decision_apply_converges_to_one_version(self):
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        status, first = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, first)
        committed = self.snapshot(self.state_dir)

        status, second = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, second)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["plan_version"], second["plan_version"])
        self.assertEqual(committed, self.snapshot(self.state_dir))
        self.assertEqual(2, read_current_plan(self.state_dir)["current_version"])

    def test_duplicate_delivery_apply_converges_to_one_event(self):
        current = read_current_plan(self.state_dir)
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, first = self.route(
            "delivery_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, first)
        committed = self.snapshot(self.state_dir)
        events = copy.deepcopy(self.fake.events)

        status, second = self.route(
            "delivery_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

        # A finished standalone delivery advances PlanState, so a same-process
        # retry currently refuses rather than returning idempotent_replay. Either
        # answer is only legal if it does not add a second calendar event.
        if status == 200:
            self.assertEqual(events, self.fake.events)
        else:
            self.assertEqual(409, status, second)
            self.assertEqual(committed, self.snapshot(self.state_dir))
        self.assertEqual(1, len(self.fake.events))
        execution = next(
            item["execution"]
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("intervals_accepted", execution["delivery_state"])


# --------------------------------------------------------------------------------------
# 5. Wrong owner
# --------------------------------------------------------------------------------------


class WrongOwnerTests(IsolatedHomeMixin, GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

    def test_owner_b_cannot_apply_owner_a_decision_and_sees_no_preview(self):
        plan_a = publishable_plan()
        owner_a = self.seed_owner(TOKEN_A, plan=plan_a)
        owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        dir_a, dir_b = self.owner_dir(owner_a), self.owner_dir(owner_b)
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        before_a, before_b = self.snapshot(dir_a), self.snapshot(dir_b)
        needles = (
            WEEKLY_CHANGE["summary"],
            WEEKLY_CHANGE["sessions"][0]["purpose"],
            prepared["preview"]["sessions"][0]["after"]["prescription"],
        )

        status, payload = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_B,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        assert_payload_hides(self, payload, *needles)
        self.assertEqual(before_a, self.snapshot(dir_a))
        self.assertEqual(before_b, self.snapshot(dir_b))

    def test_owner_b_cannot_apply_owner_a_delivery_and_neither_store_moves(self):
        owner_a = self.seed_owner(TOKEN_A, plan=publishable_plan())
        owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        dir_a, dir_b = self.owner_dir(owner_a), self.owner_dir(owner_b)
        current = read_current_plan(dir_a)
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        before_a, before_b = self.snapshot(dir_a), self.snapshot(dir_b)
        preview_name = prepared["preview"][0]["delivered_description"]

        status, payload = self.route(
            "delivery_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_B,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        assert_payload_hides(self, payload, preview_name)
        self.assertEqual(before_a, self.snapshot(dir_a))
        self.assertEqual(before_b, self.snapshot(dir_b))
        self.assertEqual([], self.fake.events)

    def test_owner_a_can_still_apply_their_own_proposal(self):
        """False-positive control: the same token that previewed still confirms."""
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        status, applied = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        self.assertEqual(2, applied["plan_version"])


# --------------------------------------------------------------------------------------
# 6. Stale state
# --------------------------------------------------------------------------------------


class StalePlanVersionTests(IsolatedHomeMixin, GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=load("plan-state-v1.json"))
        self.state_dir = self.owner_dir(self.owner_id)

    def test_proposal_for_version_n_refuses_when_the_store_is_n_plus_1(self):
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        apply_decision(
            self.state_dir,
            context=load("coach-context-day-4.json"),
            after=load("plan-state-v2-day-4.json"),
            event=load("decision-event-day-4.json"),
        )
        moved = self.snapshot(self.state_dir)

        status, payload = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_superseded", payload["error"])
        self.assertIn("plan has moved", payload["detail"])
        self.assertEqual(moved, self.snapshot(self.state_dir))
        quality = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        # N's preview was 45 minutes. N+1 is 35. Committing N onto N+1 would show 45.
        self.assertEqual(35, quality["planned_minutes"])
        self.assertNotEqual(45, quality["planned_minutes"])

    def test_proposal_for_the_current_version_still_commits(self):
        """False-positive control: a preview against the head is still a valid yes."""
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        status, applied = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        quality = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual(45, quality["planned_minutes"])


# --------------------------------------------------------------------------------------
# 7. Corrupted retained material
# --------------------------------------------------------------------------------------


class CorruptedRetainedMaterialTests(IsolatedHomeMixin, GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def _prepare(self) -> dict[str, Any]:
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        return prepared

    def test_corrupted_held_record_is_reprepare_not_500_or_commit_or_rest(self):
        prepared = self._prepare()
        before = self.snapshot(self.state_dir)

        def force_rest(record: dict[str, Any]) -> None:
            for item in record["effect"]["after_plan"]["week"]["sessions"]:
                if item["session_id"] == "run-quality-01":
                    item["sport"] = "rest"
                    item["planned_minutes"] = 0
                    item["purpose"] = "forced rest from corrupted hold"
                    return
            raise AssertionError("missing quality session")

        corrupt_held_transaction(
            self.gateway, self.owner_id, prepared["proposal"], force_rest
        )

        status, payload = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertIn("integrity mismatch", payload["detail"])
        self.assertNotEqual(500, status)
        self.assertEqual(before, self.snapshot(self.state_dir))
        quality = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertNotEqual("rest", quality["sport"])
        self.assertEqual(60, quality["planned_minutes"])

    def test_uncorrupted_held_record_commits_the_previewed_session_not_rest(self):
        """False-positive control: hash equality still permits the previewed change."""
        prepared = self._prepare()
        status, applied = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        quality = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("running", quality["sport"])
        self.assertEqual(45, quality["planned_minutes"])
        self.assertNotEqual("rest", quality["sport"])


# --------------------------------------------------------------------------------------
# 8. publish_new_workouts:false — already-delivered replacements and withdrawals
# --------------------------------------------------------------------------------------


class PublishFalseAlreadyDeliveredTests(IsolatedHomeMixin, GatewayTestCase):
    """Issue #410: false suppresses NEW publications, not replacements or withdrawals.

    The contract (contracts/decision-delivery.md) says the default is false and that
    changed previously delivered future sessions still ride along automatically.
    These tests execute that mixed path; they do not change the confirmation UX.
    """

    REST_QUALITY = coaching_request(sessions=[{
        "operation": "replace", "session_id": "run-quality-01", "sport": "rest",
        "purpose": "今天休息", "adaptation": "recovery", "cost": "easy", "planned_minutes": 0,
        "plan": {"kind": "unstructured"},
    }])

    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.plan)
        self.state_dir = self.owner_dir(self.owner_id)
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

    def _session(self) -> dict[str, Any]:
        status, session = self.route(
            "session", body={"read": "all", "all_clear": True}, token=TOKEN_A
        )
        self.assertEqual(200, status, session)
        return session

    def _deliver(self, session_ids: list[str]) -> dict[str, Any]:
        current = read_current_plan(self.state_dir)
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": session_ids,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.route(
            "delivery_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        return applied

    def _prepare(self, change: dict[str, Any]) -> dict[str, Any]:
        session = self._session()
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": change,
                "publish_new_workouts": False,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        return prepared

    def _apply(self, prepared: dict[str, Any]) -> tuple[int, Any]:
        return self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

    def test_false_replaces_already_delivered_and_does_not_publish_the_undelivered_sibling(self):
        self._deliver(["run-quality-01"])
        quality_id = self.fake.events[0]["id"]
        writes_before = len(self.fake.bulk_calls)
        change = copy.deepcopy(WEEKLY_CHANGE)
        self.fake.steps_by_name[change["sessions"][0]["plan"]["name"]] = (
            change["sessions"][0]["plan"]["steps"]
        )
        prepared = self._prepare(change)
        calendar = prepared["preview"]["calendar_delivery"]
        operations = {row["session_id"]: row["operation"] for row in calendar["workouts"]}
        self.assertEqual("replace", operations["run-quality-01"])
        self.assertNotIn("run-long-01", operations)
        self.assertEqual([], calendar.get("withdrawals") or [])

        status, applied = self._apply(prepared)
        self.assertEqual(200, status, applied)
        self.assertEqual("passed", applied["calendar_delivery"]["status"], applied)
        self.assertEqual(1, len(self.fake.events))
        self.assertEqual(quality_id, self.fake.events[0]["id"])
        self.assertEqual(writes_before + 1, len(self.fake.bulk_calls))
        long_row = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-long-01"
        )
        self.assertEqual("not_published", long_row["execution"]["delivery_state"])

    def test_false_withdraws_already_delivered_rest_and_the_preview_names_the_event(self):
        self._deliver(["run-quality-01"])
        event = self.fake.events[0]
        prepared = self._prepare(self.REST_QUALITY)
        withdrawals = prepared["preview"]["calendar_delivery"]["withdrawals"]
        self.assertEqual(1, len(withdrawals), withdrawals)
        row = withdrawals[0]
        self.assertEqual("run-quality-01", row["session_id"])
        self.assertTrue(row["event_present"])
        self.assertEqual(str(event.get("start_date_local", ""))[:10], row["event_date"])
        self.assertEqual(event.get("name"), row["event_name"])
        self.assertEqual([], prepared["preview"]["calendar_delivery"].get("workouts") or [])

        status, applied = self._apply(prepared)
        self.assertEqual(200, status, applied)
        self.assertEqual(
            [{"session_id": "run-quality-01"}],
            applied["calendar_delivery"]["withdrawn"],
        )
        self.assertEqual([], self.fake.events)
        rest = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("rest", rest["sport"])

    def test_past_and_non_owned_events_survive_the_false_withdrawal_path(self):
        self._deliver(["run-quality-01", "run-long-01"])
        quality = next(
            event for event in self.fake.events if event["start_date_local"][:10] == "2026-08-13"
        )
        quality["start_date_local"] = "2026-08-01T06:00:00"
        bystander = {
            "id": "bystander-1",
            "external_id": "not-product-owned",
            "name": "Someone else's workout",
            "start_date_local": "2026-08-13T07:00:00",
        }
        self.fake.events.append(bystander)
        prepared = self._prepare(self.REST_QUALITY)
        unresolved = prepared["preview"]["calendar_delivery"].get("unresolved") or []
        self.assertTrue(
            any(item.get("session_id") == "run-quality-01" for item in unresolved),
            prepared["preview"]["calendar_delivery"],
        )

        status, applied = self._apply(prepared)
        self.assertEqual(200, status, applied)
        remaining_ids = {str(event["id"]) for event in self.fake.events}
        self.assertIn("bystander-1", remaining_ids)
        self.assertIn(str(quality["id"]), remaining_ids)

    def test_another_owner_cannot_spend_this_false_path_withdrawal(self):
        self._deliver(["run-quality-01"])
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        prepared = self._prepare(self.REST_QUALITY)
        events = copy.deepcopy(self.fake.events)
        before = self.snapshot(self.state_dir)

        status, payload = self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_B,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertEqual(events, self.fake.events)
        self.assertEqual(before, self.snapshot(self.state_dir))

    def test_partial_withdrawal_on_the_false_path_converges_without_deleting_twice(self):
        self._deliver(["run-quality-01", "run-long-01"])
        change = coaching_request(sessions=[{
            "operation": "replace", "session_id": sid, "sport": "rest",
            "purpose": "今天休息", "adaptation": "recovery", "cost": "easy",
            "planned_minutes": 0, "plan": {"kind": "unstructured"},
        } for sid in ("run-quality-01", "run-long-01")])
        prepared = self._prepare(change)
        self.assertEqual(2, len(prepared["preview"]["calendar_delivery"]["withdrawals"]))
        with fail_nth_event_delete(2):
            status, first = self._apply(prepared)
        self.assertEqual(200, status, first)
        self.assertEqual("partial", first["calendar_delivery"]["status"], first)
        self.assertTrue(first["calendar_delivery"]["attempt_open"])
        deleted_once = list(self.fake.deleted)
        self.assertEqual(1, len(deleted_once))

        status, resumed = self._apply(prepared)
        self.assertEqual(200, status, resumed)
        self.assertEqual("passed", resumed["calendar_delivery"]["status"], resumed)
        self.assertEqual(deleted_once, self.fake.deleted[:1])
        self.assertEqual(2, len(self.fake.deleted))
        self.assertEqual([], self.fake.events)
        self.assertIsNone(pending_delivery_attempt(self.state_dir))


if __name__ == "__main__":
    unittest.main()

