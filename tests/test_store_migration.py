"""Carrying one store to the hosted gateway, and stopping the local one afterwards.

Two mechanisms, tested together because they only make sense together: a bundle is how a
history moves between two machines that share no filesystem, and the handoff seal is what
stops the machine it came from becoming a second writer for the same athlete. Without the
seal the migration is a copy; without the bundle the seal fences a store nobody carried.

Every refusal here is deliberately a refusal rather than a merge. Which of two divergent
plans is the athlete's real one is a training judgement made by looking at both (issue
#40); anything this code decided on its own would be silently picking one.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop import athlete_evidence
from garmin_coach_loop.store import (
    HOSTED_HANDOFF_FILE,
    StateStoreError,
    _bundle_digest,
    adopt_store,
    apply_decision,
    archive_store,
    doctor_store,
    export_bundle,
    history_store,
    import_bundle,
    init_store,
    open_delivery_attempt,
    read_handoff,
    restore_snapshot,
    seal_store,
    snapshot_store,
    status_store,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"
HOSTED_ENTRY = "https://coach.example"


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


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.context = load("coach-context-day-4.json")
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        self.addCleanup(self._tmp.cleanup)

    def local_store(self, *, decided: bool = True) -> Path:
        """A store with a real commit chain: an initial plan, and one applied decision."""
        state_dir = self.root / "local"
        init_store(state_dir, self.before)
        athlete_evidence.record_profile(state_dir, timezone="Asia/Taipei", language="zh-Hant")
        if decided:
            apply_decision(
                state_dir, context=self.context, after=self.after, event=self.event
            )
        return state_dir


class BundleRoundTripTests(MigrationTestCase):
    def test_an_exported_bundle_opens_as_the_same_store_somewhere_else(self):
        source = self.local_store()
        bundle = export_bundle(source)
        self.assertEqual(self.before["plan_id"], bundle["plan_id"])
        self.assertEqual(2, bundle["current_version"])
        self.assertEqual(1, bundle["event_count"])
        # The evidence the athlete stated travels with the plan; the transient files do not.
        self.assertIn("athlete-evidence.json", bundle["files"])
        self.assertNotIn(".lock", bundle["files"])
        self.assertNotIn(HOSTED_HANDOFF_FILE, bundle["files"])

        destination = self.root / "owners" / "0f8fad5b-d9cb-469f-a165-70867728950e"
        preview = import_bundle(destination, bundle)
        self.assertEqual("preview", preview["status"])
        self.assertFalse(destination.exists())

        imported = import_bundle(destination, bundle, confirm=True)
        self.assertEqual("imported", imported["status"])
        self.assertEqual(bundle["bundle_digest"], imported["bundle_digest"])

        opened = doctor_store(destination)
        self.assertEqual("passed", opened["status"], opened)
        self.assertEqual(2, opened["current_version"])
        self.assertEqual(1, opened["event_count"])
        # Every revision, not only the current one: what a migration carries is history.
        self.assertEqual(
            [entry["event_id"] for entry in history_store(source)["revisions"]],
            [entry["event_id"] for entry in history_store(destination)["revisions"]],
        )
        self.assertEqual(
            "Asia/Taipei",
            athlete_evidence.resolve_settings(destination)[0],
        )

    def test_the_source_store_is_left_exactly_as_it_was(self):
        source = self.local_store()
        before = doctor_store(source)
        export_bundle(source)
        self.assertEqual(before, doctor_store(source))

    def test_a_bundle_that_changed_in_transit_is_refused(self):
        bundle = export_bundle(self.local_store())
        tampered = copy.deepcopy(bundle)
        edited = json.loads(tampered["files"]["store.json"])
        edited["current_version"] = 99
        tampered["files"]["store.json"] = json.dumps(edited)
        with self.assertRaises(StateStoreError) as refusal:
            import_bundle(self.root / "owners" / "dest", tampered, confirm=True)
        self.assertIn("digest", str(refusal.exception))

    def test_a_bundle_whose_header_disagrees_with_its_chain_is_refused(self):
        bundle = export_bundle(self.local_store())
        bundle["current_version"] = 99
        destination = self.root / "owners" / "dest"
        with self.assertRaises(StateStoreError) as refusal:
            import_bundle(destination, bundle, confirm=True)
        self.assertIn("summary", str(refusal.exception))
        self.assertFalse(destination.exists())

    def test_a_bundle_carrying_an_unexpected_path_is_refused(self):
        exported = export_bundle(self.local_store())
        for name in ("../escape.json", "commits/../../escape.json", "notes.txt"):
            with self.subTest(name=name):
                bundle = copy.deepcopy(exported)
                bundle["files"][name] = "{}"
                # The digest is recomputed, so this is not the tampering case: it is a
                # well-formed bundle asking for a file this store format has no place for.
                bundle["bundle_digest"] = _bundle_digest(bundle["files"])
                with self.assertRaises(StateStoreError) as refusal:
                    import_bundle(self.root / "owners" / "dest", bundle, confirm=True)
                self.assertIn("unexpected file", str(refusal.exception))
                self.assertFalse((self.root / "owners" / "dest").exists())

    def test_a_symlink_inside_a_store_is_never_carried_out_in_a_bundle(self):
        source = self.local_store()
        outside = self.root / "outside.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        # Planted where the store's own checks do not look: doctor replays the commit
        # chain, so a link swapped in there is already refused as a malformed commit. The
        # evidence file is a legitimate store member, which is exactly why the export has
        # to decide about links itself.
        planted = source / "athlete-evidence.json"
        planted.unlink()
        planted.symlink_to(outside)
        with self.assertRaises(StateStoreError) as refusal:
            export_bundle(source)
        self.assertIn("symlink", str(refusal.exception))

    def test_a_store_that_does_not_open_is_never_exported(self):
        source = self.local_store()
        (source / "store.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(StateStoreError):
            export_bundle(source)

    def test_exporting_holds_the_store_lock_and_releases_it(self):
        """The chain is checked and read as one step, and normal writing resumes after."""
        source = self.local_store()
        export_bundle(source)
        self.assertFalse((source / ".lock").exists())
        # Still writable: the lock was released, not left behind.
        athlete_evidence.record_profile(source, timezone="UTC", language=None)

        (source / ".lock").write_text("pid=1\n", encoding="utf-8")
        with self.assertRaises(StateStoreError) as refusal:
            export_bundle(source)
        self.assertIn("locked", str(refusal.exception))

    def test_a_store_with_a_delivery_in_flight_is_never_exported(self):
        source = self.local_store()
        current = status_store(source)["current_plan"]
        open_delivery_attempt(
            source,
            kind="delivery",
            plan_id=current["plan_id"],
            plan_version=current["version"],
            proposal_hash="a" * 64,
            operations=[_pending_upsert("run-long-01")],
        )
        with self.assertRaises(StateStoreError) as refusal:
            export_bundle(source)
        self.assertIn("in flight", str(refusal.exception))


class ImportingIsNotMergingTests(MigrationTestCase):
    def test_a_destination_that_already_holds_a_plan_is_refused(self):
        bundle = export_bundle(self.local_store())
        destination = self.root / "owners" / "hosted"
        init_store(destination, self.before)
        occupied = doctor_store(destination)

        for confirm in (False, True):
            with self.subTest(confirm=confirm):
                with self.assertRaises(StateStoreError) as refusal:
                    import_bundle(destination, bundle, confirm=confirm)
                self.assertIn("importing is not merging", str(refusal.exception))
        # Refused in both, and the destination is byte-for-byte what it was.
        self.assertEqual(occupied, doctor_store(destination))

    def test_archiving_the_destination_is_the_way_past_it_and_keeps_both(self):
        bundle = export_bundle(self.local_store())
        destination = self.root / "owners" / "hosted"
        init_store(destination, self.before)

        preview = archive_store(destination, reason="stale-hosted-plan")
        self.assertEqual("preview", preview["status"])
        self.assertTrue(destination.exists())

        archived = archive_store(destination, reason="stale-hosted-plan", confirm=True)
        self.assertEqual("archived", archived["status"])
        self.assertFalse(destination.exists())
        # Moved, not destroyed: the archived store still opens on its own.
        self.assertEqual("passed", doctor_store(archived["archive_dir"])["status"])

        imported = import_bundle(destination, bundle, confirm=True)
        self.assertEqual("imported", imported["status"])
        self.assertEqual(2, doctor_store(destination)["current_version"])

    def test_a_destination_that_exists_but_holds_nothing_is_imported_into(self):
        bundle = export_bundle(self.local_store())
        destination = self.root / "owners" / "empty"
        destination.mkdir(parents=True)
        imported = import_bundle(destination, bundle, confirm=True)
        self.assertEqual("imported", imported["status"])
        self.assertEqual("passed", doctor_store(destination)["status"])

    def test_archiving_a_sealed_store_says_what_moving_it_frees_up(self):
        source = self.local_store()
        seal_store(source, hosted_entry=HOSTED_ENTRY, confirm=True)
        preview = archive_store(source, reason="tidy-up")
        self.assertEqual(HOSTED_ENTRY, preview["hosted_handoff"]["hosted_entry"])
        self.assertIn("second plan for the same athlete", preview["warning"])

    def test_a_destination_that_is_a_link_to_another_store_is_refused(self):
        bundle = export_bundle(self.local_store())
        real = self.root / "somewhere-else"
        init_store(real, self.before)
        destination = self.root / "owners" / "linked"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(real, target_is_directory=True)
        with self.assertRaises(StateStoreError) as refusal:
            import_bundle(destination, bundle, confirm=True)
        self.assertIn("link", str(refusal.exception))
        self.assertEqual("passed", doctor_store(real)["status"])


class HandoffSealTests(MigrationTestCase):
    def sealed(self) -> Path:
        state_dir = self.local_store()
        seal_store(state_dir, hosted_entry=HOSTED_ENTRY, confirm=True)
        return state_dir

    def test_sealing_previews_before_it_writes(self):
        state_dir = self.local_store()
        preview = seal_store(state_dir, hosted_entry=HOSTED_ENTRY)
        self.assertEqual("preview", preview["status"])
        self.assertIsNone(read_handoff(state_dir))
        self.assertEqual(2, preview["current_version"])

        sealed = seal_store(state_dir, hosted_entry=HOSTED_ENTRY, confirm=True)
        self.assertEqual("sealed", sealed["status"])
        self.assertEqual(HOSTED_ENTRY, read_handoff(state_dir)["hosted_entry"])
        # Re-running the last step of an interrupted migration says what is already true.
        self.assertEqual(
            "already_sealed",
            seal_store(state_dir, hosted_entry=HOSTED_ENTRY, confirm=True)["status"],
        )

    def test_a_sealed_store_refuses_every_write(self):
        state_dir = self.sealed()
        third = copy.deepcopy(self.after)
        third["version"] = 3

        with self.assertRaises(StateStoreError) as refusal:
            apply_decision(
                state_dir, context=self.context, after=third, event=self.event
            )
        self.assertIn("handed off", str(refusal.exception))
        self.assertIn(HOSTED_ENTRY, str(refusal.exception))

        with self.assertRaises(StateStoreError):
            athlete_evidence.record_availability(
                state_dir,
                recurring={"available_days": ["mon"], "unavailable_days": []},
                week=None,
                timezone_name="Asia/Taipei",
            )
        with self.assertRaises(StateStoreError):
            athlete_evidence.record_profile(state_dir, timezone="UTC", language=None)
        current = status_store(state_dir)["current_plan"]
        with self.assertRaises(StateStoreError):
            open_delivery_attempt(
                state_dir,
                kind="delivery",
                plan_id=current["plan_id"],
                plan_version=current["version"],
                proposal_hash="b" * 64,
                operations=[_pending_upsert("run-long-01")],
            )

    def test_a_sealed_store_stays_readable_and_exportable(self):
        state_dir = self.sealed()
        opened = doctor_store(state_dir)
        self.assertEqual("passed", opened["status"], opened)
        self.assertEqual(HOSTED_ENTRY, opened["hosted_handoff"]["hosted_entry"])
        self.assertEqual(HOSTED_ENTRY, status_store(state_dir)["hosted_handoff"]["hosted_entry"])
        self.assertEqual(2, history_store(state_dir)["revision_count"])
        self.assertEqual(2, export_bundle(state_dir)["current_version"])
        # A backup of an archive is still a backup.
        self.assertEqual("passed", snapshot_store(state_dir, reason="after-handoff")["status"])

    def test_a_sealed_store_is_never_restored_over_or_adopted_from(self):
        state_dir = self.local_store()
        snapshot = snapshot_store(state_dir, reason="before-handoff")["snapshot_dir"]
        seal_store(state_dir, hosted_entry=HOSTED_ENTRY, confirm=True)

        with self.assertRaises(StateStoreError) as refusal:
            restore_snapshot(snapshot, state_dir, confirm=True)
        self.assertIn("handed off", str(refusal.exception))
        with self.assertRaises(StateStoreError) as refusal:
            adopt_store(state_dir, self.root / "owners" / "adopted", mode="link")
        self.assertIn("handed off", str(refusal.exception))
        self.assertFalse((self.root / "owners" / "adopted").exists())

    def test_a_sealed_store_is_never_re_initialized_into_a_fresh_plan(self):
        state_dir = self.sealed()
        for path in sorted((state_dir / "commits").iterdir()):
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
        (state_dir / "store.json").unlink()
        with self.assertRaises(StateStoreError) as refusal:
            init_store(state_dir, self.before)
        self.assertIn("handed off", str(refusal.exception))

    def test_a_marker_this_code_cannot_read_blocks_rather_than_reading_as_absent(self):
        state_dir = self.sealed()
        (state_dir / HOSTED_HANDOFF_FILE).write_text('{"schema_version": "9.9"}', encoding="utf-8")
        report = doctor_store(state_dir)
        self.assertEqual("blocked", report["status"])
        self.assertIn("hosted_handoff_error", report)
        with self.assertRaises(StateStoreError):
            apply_decision(
                state_dir, context=self.context, after=self.after, event=self.event
            )

    def test_releasing_the_seal_is_explicit_and_says_what_it_costs(self):
        state_dir = self.sealed()
        preview = seal_store(state_dir, hosted_entry=HOSTED_ENTRY, release=True)
        self.assertEqual("preview", preview["status"])
        self.assertIn("second writable plan", preview["warning"])
        self.assertIsNotNone(read_handoff(state_dir))

        released = seal_store(
            state_dir, hosted_entry=HOSTED_ENTRY, release=True, confirm=True
        )
        self.assertEqual("released", released["status"])
        self.assertIsNone(read_handoff(state_dir))
        # Writable again: the store is back to refusing nothing on account of the handoff.
        recorded = athlete_evidence.record_profile(
            state_dir, timezone="Europe/Berlin", language=None
        )
        self.assertEqual("Europe/Berlin", recorded["profile"]["timezone"])
        self.assertNotIn("hosted_handoff", doctor_store(state_dir))

    def test_sealing_a_store_with_a_delivery_in_flight_is_refused(self):
        state_dir = self.local_store()
        current = status_store(state_dir)["current_plan"]
        open_delivery_attempt(
            state_dir,
            kind="delivery",
            plan_id=current["plan_id"],
            plan_version=current["version"],
            proposal_hash="c" * 64,
            operations=[_pending_upsert("run-long-01")],
        )
        with self.assertRaises(StateStoreError) as refusal:
            seal_store(state_dir, hosted_entry=HOSTED_ENTRY, confirm=True)
        self.assertIn("in flight", str(refusal.exception))
        self.assertIsNone(read_handoff(state_dir))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
