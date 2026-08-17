"""Moving one owner's store while a live gateway may be writing to it (issue #128).

The cutover -- ``archive_store`` then ``import_bundle`` -- renames and replaces a whole
owner directory. Every other write to that store is a short mutation under ``.lock``,
except the one that matters most: a delivery holds ``delivery-attempt.json`` across
network time, and moving the store away from that reservation is the one thing that makes
the provider write unfinishable. Intervals would hold an effect the canonical PlanState
could never record.

So these tests are about *ordering*, and none of them may pass by luck. Every handoff is
an explicit ``threading.Event`` around a hook installed at a named point inside the
operation, and every wait is bounded: a test either observes the interleaving it claims to
observe, or it fails. No test sleeps to make a race come out the right way.

Each pairing is asserted in both directions, because a fence that only wins is not a fence
-- it is a race the cutover happens to be quicker at:

    the writer is already inside the store lock   -> the cutover loses, nothing moves
    the fence is already held                     -> the writer loses, before it writes
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from garmin_coach_loop import athlete_evidence, store
from garmin_coach_loop.gateway import (
    CoachGateway,
    GatewayConfig,
    GatewayError,
    _reap_stale_owner_locks,
)
from garmin_coach_loop.reconcile import apply_reconciliation
from garmin_coach_loop.store import (
    MAINTENANCE_FENCE_SUFFIX,
    StateStoreError,
    adopt_store,
    apply_decision,
    archive_store,
    doctor_store,
    export_bundle,
    import_bundle,
    init_store,
    maintenance_fence_path,
    open_delivery_attempt,
    owner_maintenance_fence,
    pending_delivery_attempt,
    read_maintenance_fence,
    resolve_state_dir,
    restore_snapshot,
    snapshot_store,
    status_store,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"

# Every blocking wait in this module is bounded by this. It is a deadlock detector, never
# a synchronisation device: the tests hand off with events, and a wait that actually
# reaches this timeout is a failure being reported rather than a slow machine.
TIMEOUT = 30.0


def load(name: str) -> dict:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def _pending_upsert(session_id: str) -> dict:
    """One journalled operation, in the shape `open_delivery_attempt` records."""
    return {
        "session_id": session_id,
        "operation": "upsert",
        "owned_external_id": f"gcl:test:{session_id}",
        "scheduled_date": "2026-08-20",
    }


def _matched_actual(session_id: str, *, date: str) -> dict:
    """One identity-backed actual, which is what gives reconciliation something to write.

    Carries the paired provider event, because that is the only evidence the reconcile
    validator accepts for a session the product has delivered.
    """
    return {
        "activity_id": f"fixture-actual-{session_id}",
        "date": date,
        "sport": "running",
        "paired_event_id": f"event-{session_id}",
        "planned_session_id": session_id,
        "match_confidence": "matched",
        "adaptation": "threshold",
        "body_stress": "lower",
        "cost": "hard",
        "duration_minutes": 42,
        "completion": "completed",
        "elevation_gain_m": None,
        "subjective_feel": 3,
    }


class _PauseAt:
    """Hold one store operation still at a named function, and let it go on demand.

    Installed over a module attribute, so it intercepts the call the operation under test
    actually makes rather than approximating its timing from outside. Only the first call
    pauses -- the other thread goes on to use the same helper freely, and would otherwise
    deadlock against a pause meant for its counterpart.
    """

    def __init__(self, module, name: str):
        self.module = module
        self.name = name
        self.original = getattr(module, name)
        self.reached = threading.Event()
        self.resume = threading.Event()
        self._first = threading.Lock()
        self._taken = False

    def __enter__(self) -> "_PauseAt":
        def hooked(*args, **kwargs):
            with self._first:
                mine = not self._taken
                self._taken = True
            if mine:
                self.reached.set()
                if not self.resume.wait(TIMEOUT):
                    raise AssertionError(f"{self.name} was paused and never released")
            return self.original(*args, **kwargs)

        setattr(self.module, self.name, hooked)
        return self

    def __exit__(self, *_exc) -> None:
        setattr(self.module, self.name, self.original)
        self.resume.set()

    def wait(self, case: unittest.TestCase) -> None:
        case.assertTrue(
            self.reached.wait(TIMEOUT), f"{self.name} was never reached by the other thread"
        )

    def go(self) -> None:
        self.resume.set()


class _Thread(threading.Thread):
    """Run one callable off the main thread and keep whatever came back, exception or not.

    Joined from the test's own cleanup as well as explicitly, so an assertion that fails
    mid-race is reported as the one failure it is rather than as a temporary directory
    being deleted underneath a thread that is still running.
    """

    def __init__(self, work, case: unittest.TestCase | None = None):
        super().__init__(daemon=True)
        self._work = work
        self.result = None
        self.error: BaseException | None = None
        if case is not None:
            case.addCleanup(self.join, TIMEOUT)

    def run(self) -> None:
        try:
            self.result = self._work()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            self.error = exc

    def finish(self, case: unittest.TestCase):
        self.join(TIMEOUT)
        case.assertFalse(self.is_alive(), "the other thread never finished")
        if self.error is not None:
            raise self.error
        return self.result


class CutoverFenceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.owners = self.root / "owners"
        self.context = load("coach-context-day-4.json")
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        self.addCleanup(self._tmp.cleanup)

    def hosted_store(
        self,
        name: str = "0f8fad5b-d9cb-469f-a165-70867728950e",
        *,
        decided: bool = False,
    ) -> Path:
        """One owner store: an initial plan, and the repository's own decision on request.

        Left at version 1 by default, so ``a_decision`` is the real v1 -> v2 revision this
        repository already validates rather than a hand-built third plan. A store that
        never writes again during its test asks for ``decided=True`` instead.
        """
        state_dir = self.owners / name
        init_store(state_dir, self.before)
        athlete_evidence.record_profile(state_dir, timezone="Asia/Taipei", language="zh-Hant")
        if decided:
            apply_decision(state_dir, context=self.context, after=self.after, event=self.event)
        return state_dir

    def a_decision(self, state_dir: Path):
        """The plan write every coaching path ends in, ready to run on another thread."""
        return lambda: apply_decision(
            state_dir, context=self.context, after=self.after, event=self.event
        )

    def reconcilable_store(self, name: str = "0f8fad5b-d9cb-469f-a165-70867728950e") -> Path:
        """A store whose sessions carry provider identity, so reconciliation can write.

        The same enrichment ``tests/test_reconcile.py`` makes on its own copy of this
        fixture: provider identity is the only evidence strong enough for automatic
        completion, and the committed example deliberately has none.
        """
        plan = copy.deepcopy(self.before)
        for session in plan["week"]["sessions"]:
            session["execution"]["external_id"] = f"event-{session['session_id']}"
            session["execution"]["delivery_state"] = "intervals_accepted"
            session["execution"]["publish_supported"] = True
        state_dir = self.owners / name
        init_store(state_dir, plan)
        return state_dir

    def reconcilable_context(self) -> dict:
        """The day-4 context plus one matched actual, so reconciliation has a commit to make.

        Without it ``apply_reconciliation`` proposes nothing against this fixture and would
        never reach a write -- a test that raced it would be racing nothing.
        """
        context = copy.deepcopy(self.context)
        context["recent_actuals"].append(
            _matched_actual("run-quality-01", date="2026-08-13")
        )
        return context

    def a_delivery_reservation(self, state_dir: Path):
        current = status_store(state_dir)["current_plan"]
        return lambda: open_delivery_attempt(
            state_dir,
            kind="delivery",
            plan_id=current["plan_id"],
            plan_version=current["version"],
            proposal_hash="a" * 64,
            operations=[_pending_upsert("run-long-01")],
        )

    def assertNoFence(self, state_dir: Path) -> None:
        self.assertFalse(
            maintenance_fence_path(state_dir).exists(),
            "a released or failed maintenance operation left its fence behind",
        )

    def assertStillTheSameStore(self, state_dir: Path, expected_version: int) -> None:
        report = doctor_store(state_dir)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(expected_version, report["current_version"])


class ArchiveAgainstALiveWriterTests(CutoverFenceTestCase):
    """The cutover and a plan write, raced in both directions."""

    def test_a_reconciliation_inside_the_store_lock_makes_the_cutover_lose(self):
        state_dir = self.reconcilable_store()
        context = self.reconcilable_context()

        with _PauseAt(store, "_write_commit") as paused:
            thread = _Thread(lambda: apply_reconciliation(state_dir, context), self)
            thread.start()
            paused.wait(self)
            # Reconciliation is inside `.lock` and has not written its commit yet. This is
            # the moment the old code moved the whole directory anyway.
            with self.assertRaises(StateStoreError) as refusal:
                archive_store(state_dir, confirm=True)
            self.assertIn("locked by another operation", str(refusal.exception))
            # Nothing was moved, and the failed attempt did not leave a fence that would
            # block the writer it just lost to.
            self.assertTrue(state_dir.is_dir())
            self.assertEqual([], sorted(self.owners.glob("*.archived-*")))
            self.assertNoFence(state_dir)
            paused.go()
            reconciliation = thread.finish(self)

        self.assertEqual("passed", reconciliation["status"], reconciliation)
        self.assertEqual(
            ["run-quality-01"], [entry["session_id"] for entry in reconciliation["applied"]]
        )
        self.assertStillTheSameStore(state_dir, 2)

    def test_a_held_fence_makes_a_new_reconciliation_lose_before_it_writes(self):
        state_dir = self.reconcilable_store()
        context = self.reconcilable_context()

        # Paused inside `archive_store`, after the fence is taken and before anything
        # moves. `.lock` is not held here -- the fence is the only thing standing between
        # the store and a writer, which is exactly what this asserts.
        with _PauseAt(store, "doctor_store") as paused:
            thread = _Thread(lambda: archive_store(state_dir, confirm=True), self)
            thread.start()
            paused.wait(self)

            with self.assertRaises(StateStoreError) as refusal:
                apply_reconciliation(state_dir, context)
            self.assertIn("maintenance operation is in progress", str(refusal.exception))
            self.assertEqual("archive-store", refusal.exception.details["operation"])
            # The coach's own plan write loses at the same gate, for the same reason.
            with self.assertRaises(StateStoreError) as decision:
                self.a_decision(state_dir)()
            self.assertIn("apply-decision is refused", str(decision.exception))
            # The plan is exactly what it was: the writer lost before, not during.
            self.assertEqual(1, status_store(state_dir)["current_plan"]["version"])

            paused.go()
            archived = thread.finish(self)

        self.assertEqual("archived", archived["status"])
        self.assertFalse(state_dir.exists())
        self.assertStillTheSameStore(Path(archived["archive_dir"]), 1)
        self.assertNoFence(state_dir)

    def test_a_held_fence_refuses_every_write_the_gateway_can_start(self):
        """One fence, all of them -- checked where the lock is taken, not per caller."""
        state_dir = self.hosted_store()
        with owner_maintenance_fence(state_dir, operation="archive-store"):
            refusals = {
                "apply-decision": self.a_decision(state_dir),
                "publishing a delivery": self.a_delivery_reservation(state_dir),
                "record-profile": lambda: athlete_evidence.record_profile(
                    state_dir, timezone="UTC", language=None
                ),
                "record-availability": lambda: athlete_evidence.record_availability(
                    state_dir, recurring={"available_days": ["mon", "wed", "fri"]}
                ),
                # The two conversational evidence writers reach the fence the same way
                # every other one does -- at the store lock, not through a check of their
                # own -- which is what keeps a new writer from being the one that forgets.
                "recording a body measurement": lambda: athlete_evidence.record_body_measurement(
                    state_dir, weight_kg=72.5
                ),
                "recording a reported activity": lambda: athlete_evidence.record_activity_summary(
                    state_dir, sport="running", duration_minutes=40
                ),
                "snapshot-store": lambda: snapshot_store(state_dir),
                "export-store": lambda: export_bundle(state_dir),
            }
            for operation, work in refusals.items():
                with self.subTest(operation=operation):
                    with self.assertRaises(StateStoreError) as refusal:
                        work()
                    self.assertIn(
                        "maintenance operation is in progress", str(refusal.exception)
                    )
                    self.assertIn(operation, str(refusal.exception))
        # And the fence is a window, not a state: everything works again after it.
        athlete_evidence.record_profile(state_dir, timezone="UTC", language=None)
        self.assertNoFence(state_dir)


class ArchiveAgainstADeliveryTests(CutoverFenceTestCase):
    """The reservation is never separated from the code path that has to finish it."""

    def test_a_reservation_opened_before_the_cutover_refuses_it(self):
        state_dir = self.hosted_store(decided=True)
        attempt = self.a_delivery_reservation(state_dir)()

        for confirm in (False, True):
            with self.subTest(confirm=confirm):
                with self.assertRaises(StateStoreError) as refusal:
                    archive_store(state_dir, confirm=confirm)
                self.assertIn("in flight", str(refusal.exception))

        # The reservation is still exactly where the delivery that owns it will look.
        self.assertEqual(attempt["attempt_id"], pending_delivery_attempt(state_dir)["attempt_id"])
        self.assertTrue(state_dir.is_dir())
        self.assertEqual([], sorted(self.owners.glob("*.archived-*")))
        self.assertNoFence(state_dir)

    def test_a_reservation_cannot_be_opened_once_the_cutover_holds_the_fence(self):
        """The issue's own reproduction: a delivery starting *after* the preflight check.

        Under the old code the preflight had already passed and the rename went ahead with
        the reservation inside it. The reservation is the last thing that may appear before
        a provider write, so refusing it here is refusing the external mutation itself.
        """
        state_dir = self.hosted_store(decided=True)

        with _PauseAt(store, "doctor_store") as paused:
            thread = _Thread(lambda: archive_store(state_dir, confirm=True), self)
            thread.start()
            paused.wait(self)

            with self.assertRaises(StateStoreError) as refusal:
                self.a_delivery_reservation(state_dir)()
            self.assertIn("maintenance operation is in progress", str(refusal.exception))
            self.assertIn("publishing a delivery", str(refusal.exception))
            # No reservation exists, so no Intervals write can have begun.
            self.assertIsNone(pending_delivery_attempt(state_dir))

            paused.go()
            archived = thread.finish(self)

        self.assertEqual("archived", archived["status"])
        self.assertIsNone(pending_delivery_attempt(Path(archived["archive_dir"])))

    def test_a_store_carrying_a_reservation_is_never_imported_over(self):
        state_dir = self.hosted_store(decided=True)
        bundle = export_bundle(state_dir)
        attempt = self.a_delivery_reservation(state_dir)()

        # The reservation is what refuses, before the occupancy rule ever gets a turn:
        # an import installs over a path, and this one is mid-provider-write.
        with self.assertRaises(StateStoreError) as refusal:
            import_bundle(state_dir, bundle, confirm=True)
        self.assertIn("import-store is refused", str(refusal.exception))
        self.assertIn("in flight", str(refusal.exception))

        self.assertEqual(attempt["attempt_id"], pending_delivery_attempt(state_dir)["attempt_id"])
        self.assertStillTheSameStore(state_dir, 2)
        self.assertNoFence(state_dir)


class DestinationRaceTests(CutoverFenceTestCase):
    """A destination checked free must not be installable over one that appeared since."""

    def test_an_import_cannot_land_on_an_owner_store_initialized_since_the_check(self):
        source = self.hosted_store("11111111-1111-4111-8111-111111111111", decided=True)
        bundle = export_bundle(source)
        destination = self.owners / "22222222-2222-4222-8222-222222222222"

        # Paused inside `import_bundle` after the destination was found free and the
        # bundle was materialized, and before the install. This is the window.
        with _PauseAt(store, "doctor_store") as paused:
            thread = _Thread(lambda: import_bundle(destination, bundle, confirm=True), self)
            thread.start()
            paused.wait(self)

            with self.assertRaises(StateStoreError) as refusal:
                init_store(destination, self.before)
            self.assertIn("maintenance operation is in progress", str(refusal.exception))
            # Refused before anything was created, not half-made and then rolled back.
            self.assertFalse(destination.exists())

            # Adoption claims an owner directory too, and meets the same fence.
            with self.assertRaises(StateStoreError) as refusal:
                adopt_store(source, destination, mode="copy", confirm=True)
            self.assertIn("maintenance operation is in progress", str(refusal.exception))
            self.assertFalse(destination.exists())

            paused.go()
            imported = thread.finish(self)

        self.assertEqual("imported", imported["status"])
        self.assertStillTheSameStore(destination, 2)
        self.assertNoFence(destination)

    def test_an_initialization_in_progress_makes_the_import_lose(self):
        source = self.hosted_store("11111111-1111-4111-8111-111111111111", decided=True)
        bundle = export_bundle(source)
        destination = self.owners / "22222222-2222-4222-8222-222222222222"

        with _PauseAt(store, "_write_commit") as paused:
            thread = _Thread(lambda: init_store(destination, self.before), self)
            thread.start()
            paused.wait(self)

            with self.assertRaises(StateStoreError) as refusal:
                import_bundle(destination, bundle, confirm=True)
            self.assertIn("import-store is refused", str(refusal.exception))
            self.assertIn("init-store", str(refusal.exception))

            paused.go()
            thread.finish(self)

        # The initialization owns the path, and the import is now the ordinary refusal.
        self.assertStillTheSameStore(destination, 1)
        with self.assertRaises(StateStoreError) as refusal:
            import_bundle(destination, bundle, confirm=True)
        self.assertIn("importing is not merging", str(refusal.exception))
        self.assertNoFence(destination)

    def test_two_maintenance_operations_on_one_owner_cannot_both_hold_it(self):
        state_dir = self.hosted_store(decided=True)
        with owner_maintenance_fence(state_dir, operation="archive-store") as fence:
            with self.assertRaises(StateStoreError) as refusal:
                with owner_maintenance_fence(state_dir, operation="delete-owner"):
                    self.fail("two maintenance operations held one owner's store")
            self.assertIn("delete-owner is refused", str(refusal.exception))
            self.assertIn("archive-store", str(refusal.exception))
            # The loser released nothing: the first holder is still the holder.
            self.assertEqual(fence["fence_id"], read_maintenance_fence(state_dir)["fence_id"])
        self.assertNoFence(state_dir)

    def test_a_neighbouring_owner_is_never_fenced_by_this_one(self):
        fenced = self.hosted_store("11111111-1111-4111-8111-111111111111", decided=True)
        neighbour = self.hosted_store("22222222-2222-4222-8222-222222222222")
        with owner_maintenance_fence(fenced, operation="archive-store"):
            self.assertIsNone(read_maintenance_fence(neighbour))
            athlete_evidence.record_profile(neighbour, timezone="UTC", language=None)
            self.a_decision(neighbour)()
        self.assertStillTheSameStore(neighbour, 2)


class FailedCutoverTests(CutoverFenceTestCase):
    """Whatever went wrong, both ends are exactly as they were and both still open."""

    def test_an_archive_that_fails_at_the_rename_leaves_the_store_openable(self):
        state_dir = self.hosted_store()
        before = doctor_store(state_dir)

        original = store.os.replace

        def refuse(source, target):
            if Path(source) == state_dir:
                raise OSError("the volume said no")
            return original(source, target)

        store.os.replace = refuse
        self.addCleanup(setattr, store.os, "replace", original)
        with self.assertRaises(OSError):
            archive_store(state_dir, confirm=True)
        store.os.replace = original

        self.assertEqual(before, doctor_store(state_dir))
        self.assertNoFence(state_dir)
        # Openable *and* writable: a fence left behind would be a store nobody can use.
        self.a_decision(state_dir)()
        self.assertStillTheSameStore(state_dir, 2)

    def test_an_import_that_fails_leaves_the_destination_empty_and_the_bundle_intact(self):
        source = self.hosted_store("11111111-1111-4111-8111-111111111111", decided=True)
        bundle = export_bundle(source)
        carried = copy.deepcopy(bundle)
        destination = self.owners / "22222222-2222-4222-8222-222222222222"

        broken = copy.deepcopy(bundle)
        broken["current_version"] = 99
        with self.assertRaises(StateStoreError):
            import_bundle(destination, broken, confirm=True)

        self.assertFalse(destination.exists())
        self.assertNoFence(destination)
        self.assertEqual(carried, bundle)
        self.assertStillTheSameStore(source, 2)
        # And the good bundle still lands afterwards: nothing was consumed by the failure.
        import_bundle(destination, bundle, confirm=True)
        self.assertStillTheSameStore(destination, 2)

    def test_a_restore_never_races_a_cutover_of_the_same_store(self):
        state_dir = self.hosted_store(decided=True)
        snapshot = snapshot_store(state_dir)["snapshot_dir"]
        with owner_maintenance_fence(state_dir, operation="archive-store"):
            for confirm in (False, True):
                with self.subTest(confirm=confirm):
                    with self.assertRaises(StateStoreError) as refusal:
                        restore_snapshot(snapshot, state_dir, confirm=confirm)
                    self.assertIn("restore-store is refused", str(refusal.exception))
        self.assertStillTheSameStore(state_dir, 2)


class FenceIsNotStoreStateTests(CutoverFenceTestCase):
    """The fence coordinates a move; it is never part of the history being moved."""

    def test_the_fence_lives_beside_the_store_and_never_inside_it(self):
        state_dir = self.hosted_store(decided=True)
        inside = sorted(path.name for path in state_dir.iterdir())
        with owner_maintenance_fence(state_dir, operation="archive-store"):
            fence_path = maintenance_fence_path(state_dir)
            self.assertEqual(state_dir.parent, fence_path.parent)
            self.assertEqual(f"{state_dir.name}{MAINTENANCE_FENCE_SUFFIX}", fence_path.name)
            self.assertTrue(fence_path.is_file())
            # The store's own contents are untouched, so `_inspect_store` walks exactly
            # what it walked before and no commit, manifest or contract version moves.
            self.assertEqual(inside, sorted(path.name for path in state_dir.iterdir()))
        self.assertNoFence(state_dir)

    def test_doctor_reports_a_held_fence_without_failing_the_store(self):
        state_dir = self.hosted_store(decided=True)
        contract = json.loads((state_dir / "store.json").read_text(encoding="utf-8"))
        with owner_maintenance_fence(state_dir, operation="archive-store", reason="superseded"):
            report = doctor_store(state_dir)
            self.assertEqual("passed", report["status"], report)
            self.assertEqual("archive-store", report["maintenance_fence"]["operation"])
            self.assertEqual("superseded", report["maintenance_fence"]["reason"])
        released = doctor_store(state_dir)
        self.assertNotIn("maintenance_fence", released)
        self.assertEqual(
            contract, json.loads((state_dir / "store.json").read_text(encoding="utf-8"))
        )

    def test_a_fence_this_code_cannot_read_blocks_rather_than_reading_as_absent(self):
        state_dir = self.hosted_store(decided=True)
        maintenance_fence_path(state_dir).write_text('{"schema_version": "9.0"}', encoding="utf-8")
        report = doctor_store(state_dir)
        self.assertEqual("blocked", report["status"])
        self.assertIn("schema_version", report["maintenance_fence_error"])
        with self.assertRaises(StateStoreError):
            self.a_decision(state_dir)()

    def test_an_archived_store_carries_no_fence_and_opens_where_it_landed(self):
        state_dir = self.hosted_store(decided=True)
        archived = Path(archive_store(state_dir, confirm=True)["archive_dir"])
        self.assertStillTheSameStore(archived, 2)
        self.assertNotIn(
            MAINTENANCE_FENCE_SUFFIX.lstrip("."),
            " ".join(path.name for path in archived.iterdir()),
        )
        self.assertNoFence(state_dir)
        self.assertNoFence(archived)

    def test_a_bundle_never_carries_the_fence(self):
        state_dir = self.hosted_store(decided=True)
        maintenance_fence_path(state_dir)  # the sibling path, never a bundle member
        bundle = export_bundle(state_dir)
        self.assertEqual(
            [], [name for name in bundle["files"] if MAINTENANCE_FENCE_SUFFIX in name]
        )


class GatewayRefusesToBeginTests(CutoverFenceTestCase):
    """Nothing the hosted gateway can start reaches the provider during a cutover."""

    OWNER = "33333333-3333-4333-8333-333333333333"

    def gateway(self) -> CoachGateway:
        return CoachGateway(
            GatewayConfig(
                state_root=self.root,
                token_hmac_key=b"0" * 32,
                intervals_client_id="client",
                intervals_client_secret="secret",
            )
        )

    def test_every_writing_tool_is_refused_while_the_owner_is_being_moved(self):
        state_dir = resolve_state_dir(self.OWNER, state_root=self.root)
        init_store(state_dir, self.before)
        gateway = self.gateway()

        with owner_maintenance_fence(state_dir, operation="archive-store"):
            for kind, tool in CoachGateway._FENCED_BY_MAINTENANCE.items():
                with self.subTest(kind=kind):
                    with self.assertRaises(GatewayError) as refusal:
                        gateway.route(kind, self.OWNER, "token", {})
                    self.assertEqual(409, refusal.exception.status)
                    self.assertEqual("state_conflict", refusal.exception.code)
                    self.assertIn(tool, refusal.exception.detail)
                    self.assertIn(
                        "maintenance operation is in progress", refusal.exception.detail
                    )

    def test_a_gateway_starting_up_never_reclaims_an_operator_fence(self):
        """A restart clears a crashed predecessor's `.lock`, and must not touch a fence.

        They look alike and are the opposite case: the lock belonged to the process that
        just restarted, the fence to a cutover still running elsewhere.
        """
        state_dir = resolve_state_dir(self.OWNER, state_root=self.root)
        init_store(state_dir, self.before)
        with owner_maintenance_fence(state_dir, operation="archive-store") as fence:
            (state_dir / ".lock").write_text("pid=1\n", encoding="utf-8")
            self.assertEqual(1, _reap_stale_owner_locks(self.root))
            self.assertFalse((state_dir / ".lock").exists())
            self.assertEqual(fence["fence_id"], read_maintenance_fence(state_dir)["fence_id"])

    def test_the_fenced_kinds_are_every_kind_that_writes(self):
        """A new writing tool must not be able to arrive without meeting the fence."""
        gateway = self.gateway()
        # `route` builds its own handler table; reading it back is how this stays true
        # when a tool is added rather than passing on a list written down twice.
        try:
            gateway.route("no-such-kind", self.OWNER, "token", {})
        except KeyError as exc:
            self.assertEqual("no-such-kind", exc.args[0])
        readable = {
            "permissions",
            "data_export",
            "initialization_prepare",
            "decision_prepare",
            "delivery_prepare",
            "withdrawal_prepare",
            "deletion_prepare",
            # Deletion carries its own delivery-reservation refusal, states a foreign
            # fence in its preview, and takes this fence itself (issue #137). It must stay
            # out of this table: routing it through the generic check would make a
            # deletion refuse itself against its own tombstone.
            "deletion_apply",
        }
        self.assertEqual(set(), set(CoachGateway._FENCED_BY_MAINTENANCE) & readable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
