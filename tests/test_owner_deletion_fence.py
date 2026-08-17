"""Deleting one athlete's account while a request of theirs is still running (issue #137).

Deletion used to remove the store, remove the identity rows, and then sweep once. The
sweep closes the window for *new* requests -- their credential no longer resolves -- and
says nothing about a request that resolved a moment earlier. Evidence writers create the
owner directory *before* they take the store lock, so one already in flight could put the
directory back after the sweep and write a file into it, and no credential could ever
reach that directory again to delete it: the athlete's training data outliving their own
deletion receipt.

The fix is the fence issue #128 already built, taken around the whole erasure, and then
kept: a deletion tombstone rather than a released window. So these tests are about
*ordering*, and none of them may pass by luck. Every handoff is an explicit
``threading.Event`` around a hook installed at a named point inside the operation, and
every wait is bounded -- the same discipline, and the same helpers, as
``tests/test_store_cutover_fence.py``.

The three interleavings that matter, each asserted in the direction that could lose:

    a writer is inside the store lock        -> the deletion loses, nothing is removed
    a writer arrives while the fence is held -> an empty shell at most, and it is swept
    a writer arrives after the receipt       -> the tombstone refuses it, forever
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import athlete_evidence, owner_data
from garmin_coach_loop.gateway import _reap_stale_owner_locks, identity_db_path
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_identity_row_counts,
    record_token_fingerprint,
)
from garmin_coach_loop.store import (
    StateStoreError,
    doctor_store,
    init_store,
    maintenance_fence_path,
    owner_maintenance_fence,
    read_maintenance_fence,
    resolve_state_dir,
)

from test_store_cutover_fence import TIMEOUT, _PauseAt, _Thread, load


NOW = dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc)

# What `owner_data` names this operation to the athlete, and therefore what the fence,
# the tombstone and every refusal derived from them carry.
HOSTED_DELETION = "deleting your data"


class OwnerDeletionFenceTestCase(unittest.TestCase):
    """One connected athlete with a plan, deleted directly rather than over HTTP.

    Directly, because what is under test is the interleaving of two threads inside one
    process against one directory. The transport is covered where it belongs, in
    ``tests/test_owner_lifecycle.py``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.state_root = self.root / "gateway-root"
        self.owners = self.state_root / "owners"
        self.identity_db = identity_db_path(self.state_root)
        self.plan = load("plan-state-v1.json")
        self.owner_id = self.connect("i1", "tok-i1")
        self.state_dir = resolve_state_dir(self.owner_id, state_root=self.state_root)
        init_store(self.state_dir, self.plan)
        self.addCleanup(self._tmp.cleanup)

    def connect(self, athlete_id: str, token: str) -> str:
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", athlete_id)
        record_token_fingerprint(self.identity_db, f"fp-{token}", owner_id, "intervals")
        return owner_id

    # -- the two operations being raced ------------------------------------------------

    def delete(self) -> dict[str, Any]:
        return owner_data.delete_owner(
            self.state_dir,
            identity_db=self.identity_db,
            owner_id=self.owner_id,
            owner_reference="ref-alpha",
            now=NOW,
        )

    def preview(self) -> dict[str, Any]:
        return owner_data.deletion_preview(
            self.state_dir, identity_db=self.identity_db, owner_id=self.owner_id
        )

    def an_evidence_write(self, timezone: str = "UTC"):
        """One reported-evidence write: the writer that creates the directory itself.

        `record_profile` is the shortest of the three, and every one of them has the same
        shape -- `mkdir` outside the lock, then the lock -- which is the shape this whole
        module is about.
        """
        return lambda: athlete_evidence.record_profile(
            self.state_dir, timezone=timezone, language=None
        )

    # -- assertions ---------------------------------------------------------------------

    def assertNothingWasWritten(self) -> None:
        """No content for this owner, whether or not an empty shell is standing there."""
        if not self.state_dir.exists():
            return
        self.assertEqual(
            [],
            sorted(path.name for path in self.state_dir.iterdir()),
            "a refused writer left content behind for a deleted owner",
        )

    def assertTombstone(self) -> dict[str, Any]:
        fence = read_maintenance_fence(self.state_dir)
        self.assertIsNotNone(fence, "the deletion released its fence instead of keeping it")
        self.assertIs(True, fence["tombstone"])
        self.assertEqual(HOSTED_DELETION, fence["operation"])
        self.assertTrue(fence["deleted_at"])
        return fence

    def assertNoFence(self) -> None:
        self.assertFalse(
            maintenance_fence_path(self.state_dir).exists(),
            "a deletion that removed nothing left a fence behind",
        )

    def assertStillConnected(self) -> None:
        """The account is exactly as the deletion found it: store on disk, rows resolving.

        Deliberately not a ``doctor_store`` call: this also runs while a writer that just
        won the race is still holding the store lock, which doctor reports as a blocked
        store and is not what is being asserted here.
        """
        self.assertTrue((self.state_dir / "store.json").is_file())
        self.assertEqual(
            1, owner_identity_row_counts(self.identity_db, self.owner_id)["owners"]
        )

    def assertErased(self) -> None:
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(
            0, owner_identity_row_counts(self.identity_db, self.owner_id)["owners"]
        )


class DeletionAgainstALiveWriterTests(OwnerDeletionFenceTestCase):
    """The deletion and one reported-evidence write, raced in both directions."""

    def test_a_writer_inside_the_store_lock_makes_the_deletion_lose(self):
        # Paused inside `record_profile`'s own lock, after the evidence file has been read
        # and before it is written back. This is the moment a deletion must not remove the
        # directory: the write is halfway through it.
        with _PauseAt(athlete_evidence, "_atomic_json") as paused:
            writer = _Thread(self.an_evidence_write("Asia/Taipei"), self)
            writer.start()
            paused.wait(self)

            with self.assertRaises(StateStoreError) as refusal:
                self.delete()
            self.assertIn("locked by another operation", str(refusal.exception))
            # Nothing was removed, and the attempt left no fence -- a deletion that lost
            # must not leave the store fenced against the writer it lost to.
            self.assertStillConnected()
            self.assertNoFence()

            paused.go()
            writer.finish(self)

        # The write it lost to actually landed, rather than being half-applied.
        stored = athlete_evidence.load_evidence(self.state_dir)["profile"]
        self.assertEqual("Asia/Taipei", stored["timezone"])
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])
        # And the deletion works on the next attempt: this is a race, not a state.
        receipt = self.delete()
        self.assertTrue(receipt["deleted"])
        self.assertErased()

    def test_a_writer_arriving_during_the_deletion_leaves_a_shell_that_is_swept(self):
        """The writer that resolved its owner before the deletion, and lands mid-way.

        It recreates the owner directory -- that much no lock can prevent, because the
        `mkdir` comes first -- and then meets the fence at the lock and writes nothing.
        The sweep runs after it, with the fence still held, so the receipt can say both
        halves of the truth: something came back, and nothing is there now.
        """
        released: dict[str, Any] = {}

        # Paused before `record_profile` resolves the store root, which is before its
        # `mkdir`: exactly an in-flight request that has authenticated and not yet touched
        # the filesystem.
        with _PauseAt(athlete_evidence, "resolve_state_root") as paused:
            writer = _Thread(self.an_evidence_write(), self)
            writer.start()
            paused.wait(self)

            original = owner_data.delete_owner_identity

            def hand_over(*args: Any, **kwargs: Any) -> Any:
                """Let the in-flight writer run in the gap the old sweep could not cover.

                Between the identity rows going and the sweep: under the old code this is
                where the directory came back for good, because nothing looked again.
                """
                counts = original(*args, **kwargs)
                paused.go()
                writer.join(TIMEOUT)
                released["writer_finished"] = not writer.is_alive()
                released["shell"] = self.state_dir.is_dir()
                return counts

            with mock.patch.object(owner_data, "delete_owner_identity", hand_over):
                receipt = self.delete()

        self.assertTrue(released["writer_finished"], "the racing writer never finished")
        self.assertTrue(released["shell"], "the writer never recreated the directory")
        # The writer lost at the lock, and said why.
        self.assertIsInstance(writer.error, StateStoreError)
        self.assertIn("was deleted", str(writer.error))
        # The receipt reports the resurrection rather than hiding it, and the directory
        # really is gone at the end of the fenced deletion.
        self.assertTrue(receipt["removed"]["state_written_during_deletion"])
        self.assertTrue(receipt["deleted"])
        self.assertErased()
        self.assertTombstone()

    def test_a_writer_arriving_after_the_receipt_is_refused_by_the_tombstone(self):
        """The order the one-time sweep could never have caught: mkdir after the receipt.

        No credential check stands in this writer's way -- it resolved its owner while the
        account still existed -- and no further sweep is ever going to run. The tombstone
        is the only thing between it and a resurrected directory full of training data.
        """
        with _PauseAt(athlete_evidence, "resolve_state_root") as paused:
            writer = _Thread(self.an_evidence_write(), self)
            writer.start()
            paused.wait(self)

            receipt = self.delete()
            self.assertFalse(receipt["removed"]["state_written_during_deletion"])
            self.assertErased()

            paused.go()
            with self.assertRaises(StateStoreError) as refusal:
                writer.finish(self)

        self.assertIn("was deleted", str(refusal.exception))
        self.assertNotIn("in progress", str(refusal.exception))
        # At most an empty directory, never a file: the athlete's data did not come back.
        self.assertNothingWasWritten()
        self.assertTombstone()

    def test_a_writer_whose_directory_is_removed_under_it_is_refused_not_crashed(self):
        """The narrowest window of all: between the writer's `mkdir` and its lock.

        Deletion is the one operation that removes an owner directory out from under a
        live writer, so it is the one that can make the lock file's own parent disappear.
        That is a lost race like any other and is reported as one -- a bare OSError here
        would surface to the athlete as an unhandled failure rather than a conflict.
        """
        with _PauseAt(athlete_evidence, "_exclusive_lock") as paused:
            writer = _Thread(self.an_evidence_write(), self)
            writer.start()
            paused.wait(self)

            receipt = self.delete()
            self.assertTrue(receipt["deleted"])

            paused.go()
            with self.assertRaises(StateStoreError) as refusal:
                writer.finish(self)

        self.assertIn("record-profile is refused", str(refusal.exception))
        self.assertIn("no longer exists", str(refusal.exception))
        self.assertNothingWasWritten()

    def test_every_writer_that_creates_the_directory_itself_meets_the_tombstone(self):
        """One tombstone, all three of them -- checked at the lock, not per caller."""
        self.delete()

        refusals = {
            "record-profile": self.an_evidence_write(),
            "record-availability": lambda: athlete_evidence.record_availability(
                self.state_dir, recurring={"available_days": ["mon", "wed", "fri"]}
            ),
            "recording reported strength": lambda: athlete_evidence.record_strength_report(
                self.state_dir, exercise="bench press", sets=[{"reps": 5, "weight_kg": 60}]
            ),
        }
        for operation, work in refusals.items():
            with self.subTest(operation=operation):
                with self.assertRaises(StateStoreError) as refusal:
                    work()
                self.assertIn(operation, str(refusal.exception))
                self.assertIn("was deleted", str(refusal.exception))
        self.assertNothingWasWritten()

    def test_a_deleted_owner_id_can_never_be_initialized_again(self):
        """The tombstone is only safe because ids are not reused, and it enforces that.

        A reconnecting athlete registers a fresh owner id
        (``test_owner_lifecycle.py::test_repeating_it_is_safe``), so nothing legitimate
        ever asks for this path again -- and anything that does is the resurrection this
        fence exists to refuse, not a returning athlete.
        """
        self.delete()

        with self.assertRaises(StateStoreError) as refusal:
            init_store(self.state_dir, self.plan)
        self.assertIn("init-store is refused", str(refusal.exception))
        self.assertIn("was deleted", str(refusal.exception))
        self.assertFalse(self.state_dir.exists())


class DeletionReceiptTests(OwnerDeletionFenceTestCase):
    """A receipt claims the account is gone, so it is issued only when that is checkable."""

    def test_an_ordinary_deletion_reports_no_resurrection_and_keeps_its_tombstone(self):
        receipt = self.delete()

        self.assertTrue(receipt["deleted"])
        self.assertTrue(receipt["removed"]["state_directory"])
        self.assertFalse(receipt["removed"]["state_written_during_deletion"])
        self.assertEqual(1, receipt["removed"]["identity_rows"]["owners"])
        self.assertErased()
        self.assertTombstone()

    def test_no_receipt_is_issued_while_a_directory_is_still_standing(self):
        """The one case where the sweep does not win: it is reported as a failure.

        Nothing can be *in* that directory -- the tombstone is already sealed -- so this
        is not a data leak; it is the receipt refusing to claim something it cannot see.
        Asking again is what finishes it.
        """
        original = owner_data.delete_owner_store

        def never_stays_removed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if kwargs.get("confirm"):
                # A writer that keeps winning, including the very last race: the directory
                # is back after the removal that was meant to be the final one.
                self.state_dir.mkdir(parents=True, exist_ok=True)
            return result

        with mock.patch.object(owner_data, "delete_owner_store", never_stays_removed):
            with self.assertRaises(StateStoreError) as refusal:
                self.delete()

        self.assertIn("did not finish", str(refusal.exception))
        # Everything the deletion did finish, it kept: the rows are gone and the account
        # is fenced, so the retry has nothing to undo and nothing can be written meanwhile.
        self.assertEqual(
            0, owner_identity_row_counts(self.identity_db, self.owner_id)["owners"]
        )
        self.assertTombstone()
        self.assertNothingWasWritten()

        receipt = self.delete()
        self.assertTrue(receipt["deleted"])
        self.assertErased()


class InterruptedDeletionTests(OwnerDeletionFenceTestCase):
    """A failure between the two removals is finished by asking again, not by an operator."""

    def test_a_retry_finishes_the_job_under_this_deletions_own_tombstone(self):
        def crash(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the identity registry went away")

        with mock.patch.object(owner_data, "delete_owner_identity", crash):
            with self.assertRaises(RuntimeError):
                self.delete()

        # The half-done state issue #137 has to survive: store gone, rows still resolving.
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(
            1, owner_identity_row_counts(self.identity_db, self.owner_id)["owners"]
        )
        # And the fence stayed, because the store is already gone -- releasing it here
        # would reopen the window for exactly as long as the retry takes.
        first = self.assertTombstone()

        # The retry goes through the preview, because that is how the confirmation is
        # rebound. It must not refuse against this deletion's own tombstone.
        preview = self.preview()
        self.assertEqual(1, preview["removes"]["identity_rows"]["owners"])

        receipt = self.delete()

        self.assertTrue(receipt["deleted"])
        self.assertFalse(receipt["removed"]["state_directory"])
        self.assertEqual(1, receipt["removed"]["identity_rows"]["owners"])
        self.assertErased()
        # The same tombstone, re-entered rather than replaced.
        self.assertEqual(first["fence_id"], self.assertTombstone()["fence_id"])

    def test_a_failure_before_anything_is_removed_leaves_no_tombstone(self):
        """The control the tombstone would otherwise be catastrophic without.

        A deletion that lost a transient race must leave the account exactly as writable
        as it found it. A tombstone written on the way out of *that* would fence a live
        athlete's store forever, on the strength of a lock somebody held for a
        millisecond.
        """
        with _PauseAt(athlete_evidence, "_atomic_json") as paused:
            writer = _Thread(self.an_evidence_write(), self)
            writer.start()
            paused.wait(self)

            with self.assertRaises(StateStoreError):
                self.delete()

            paused.go()
            writer.finish(self)

        self.assertNoFence()
        self.assertStillConnected()
        # Writable, not merely present.
        athlete_evidence.record_profile(self.state_dir, timezone="UTC", language=None)


class DeletionUnderSomebodyElsesFenceTests(OwnerDeletionFenceTestCase):
    """A deletion must fail loudly during a cutover, never queue behind one invisibly."""

    def test_the_preview_refuses_while_another_operation_holds_the_fence(self):
        with owner_maintenance_fence(self.state_dir, operation="archive-store"):
            with self.assertRaises(StateStoreError) as refusal:
                self.preview()
            self.assertIn(f"{HOSTED_DELETION} is refused", str(refusal.exception))
            self.assertIn("archive-store", str(refusal.exception))
            self.assertIn("maintenance operation is in progress", str(refusal.exception))
            self.assertEqual("archive-store", refusal.exception.details["operation"])

            # And a confirmation that got past an earlier preview is refused too, without
            # removing anything on the way.
            with self.assertRaises(StateStoreError):
                self.delete()
            self.assertStillConnected()

        # The control: the cutover ends and the athlete's deletion goes through.
        self.assertEqual(1, self.preview()["removes"]["identity_rows"]["owners"])
        self.assertTrue(self.delete()["deleted"])

    def test_a_neighbouring_owner_is_never_fenced_by_this_deletion(self):
        neighbour_id = self.connect("i2", "tok-i2")
        neighbour = resolve_state_dir(neighbour_id, state_root=self.state_root)
        init_store(neighbour, self.plan)

        self.delete()

        self.assertIsNone(read_maintenance_fence(neighbour))
        athlete_evidence.record_profile(neighbour, timezone="UTC", language=None)
        self.assertEqual("passed", doctor_store(neighbour)["status"])


class TombstoneAgainstTheFenceContractTests(OwnerDeletionFenceTestCase):
    """What issue #128 promised about a held fence, checked against a permanent one."""

    def test_doctor_names_a_deletion_tombstone_instead_of_maintenance_in_progress(self):
        self.delete()

        report = doctor_store(self.state_dir)

        # Reported, and reported as what it is: "maintenance in progress" would send an
        # operator away to wait for an operation that finished and is never coming back.
        self.assertIs(True, report["deletion_tombstone"]["tombstone"])
        self.assertEqual(HOSTED_DELETION, report["deletion_tombstone"]["operation"])
        self.assertNotIn("maintenance_fence", report)
        # Informational, exactly as a held fence is: the store is blocked because the
        # store is gone, and the tombstone contributes no error of its own.
        self.assertEqual(["state directory does not exist"], report["errors"])

    def test_a_restart_reclaims_a_stale_lock_and_never_the_tombstone(self):
        """Startup clears a crashed predecessor's `.lock` and must not touch a fence.

        Under a tombstone the two arrive together: a late writer that was killed between
        `mkdir` and its refusal leaves both an empty shell and a lock inside it. The lock
        is the restarting process's to clear; the tombstone is not, and reclaiming it
        would unfence a deleted account.
        """
        self.delete()
        self.state_dir.mkdir(parents=True)
        (self.state_dir / ".lock").write_text("pid=1\n", encoding="utf-8")

        self.assertEqual(1, _reap_stale_owner_locks(self.state_root))

        self.assertFalse((self.state_dir / ".lock").exists())
        self.assertTombstone()
        # Still refused afterwards, which is the property the count above only implies.
        with self.assertRaises(StateStoreError):
            self.an_evidence_write()()
        self.assertNothingWasWritten()

    def test_the_tombstone_never_enters_the_store_and_never_moves_with_one(self):
        """A sibling file, the same as every other fence: it is not the athlete's history."""
        self.delete()

        fence_path = maintenance_fence_path(self.state_dir)
        self.assertEqual(self.state_dir.parent, fence_path.parent)
        self.assertTrue(fence_path.is_file())
        self.assertEqual([fence_path.name], sorted(path.name for path in self.owners.iterdir()))


class LinkedOwnerDeletionTests(OwnerDeletionFenceTestCase):
    """An owner directory that is a link to somebody else's store: the one unfenceable shape."""

    def setUp(self):
        super().setUp()
        # Replace this owner's real directory with a link to a store it does not own --
        # what `adopt-owner-store --mode link` leaves behind.
        self.target = self.root / "operator-store"
        init_store(self.target, load("plan-state-v1.json"))
        for path in sorted(self.state_dir.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.state_dir.rmdir()
        self.state_dir.symlink_to(self.target)

    def test_the_link_goes_the_target_stays_and_the_freed_path_is_tombstoned(self):
        receipt = self.delete()

        self.assertTrue(receipt["deleted"])
        self.assertTrue(receipt["removed"]["state_directory"])
        self.assertFalse(self.state_dir.exists())
        # The store the link pointed at is not this owner's to delete, and it opens
        # exactly as it did before.
        self.assertEqual("passed", doctor_store(self.target)["status"])
        # The path the link occupied is fenced, so a writer that arrives afterwards --
        # resolving to the freed path now that the link is gone -- writes nothing there.
        self.assertTombstone()
        with self.assertRaises(StateStoreError) as refusal:
            self.an_evidence_write()()
        self.assertIn("was deleted", str(refusal.exception))
        self.assertNothingWasWritten()
        self.assertEqual("passed", doctor_store(self.target)["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
