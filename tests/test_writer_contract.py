"""The writer-contract guard: detection before a write, and a real way back (issue #88).

CLAUDE.md has long warned that once newer code writes a field, older code cannot open the
store at all -- not just the newest commit, the whole thing -- and that the fix has been
"merge the schema change before writing real state." That rule only ever worked if a human
remembered it every time. These tests are the machine-checked version: the store records
the writer-contract version it was last written under, every write path compares its own
supported version against that before touching anything, and a verified snapshot exists to
recover from whatever a mismatch (or anything else) leaves behind.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import store as store_module
from garmin_coach_loop.store import (
    StateStoreError,
    WRITER_CONTRACT_VERSION,
    apply_decision,
    doctor_store,
    init_store,
    restore_snapshot,
    snapshot_store,
)
from garmin_coach_loop.validation import validate_plan_state


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"
FROZEN = Path(__file__).resolve().parent / "fixtures" / "frozen_stores"


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


class FrozenStoreFixtureTests(unittest.TestCase):
    """Current code must open and fully validate stores produced by earlier versions.

    ``_state_root`` refuses any path inside this repository, so a frozen fixture is never
    opened in place -- it is copied to a scratch directory first, exactly the way a real
    store would live outside the repository too.
    """

    def _copy_fixture(self, name: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        destination = Path(tmp.name) / name
        shutil.copytree(FROZEN / name, destination)
        return destination

    def test_the_current_contract_fixture_opens_and_validates(self):
        state_dir = self._copy_fixture("current_contract")

        report = doctor_store(state_dir)

        self.assertEqual("passed", report["status"], report)
        self.assertEqual([], report["errors"])
        # The literal, not the constant: a frozen fixture asserting WRITER_CONTRACT_VERSION
        # would agree with any value and so would check nothing. Moving the contract means
        # regenerating this store and moving this number with it.
        self.assertEqual(5, report["writer_contract_version"])
        self.assertEqual(2, report["current_version"])
        self.assertEqual(1, report["event_count"])

    def test_an_unknown_execution_key_is_refused_which_is_why_the_contract_moved(self):
        """The coupling that made `superseded_external_id` a contract change (issue #113).

        The field is optional and additive, which on its own would not need a bump. But
        `execution` refuses a key it was not taught, so a store where one session carries
        it does not open at all under a checkout that predates it -- not that field, the
        whole store. This pins the rule the bump was reasoning from.
        """
        plan = load("plan-state-v1.json")
        plan["week"]["sessions"][0]["execution"]["a_field_from_the_future"] = "x"

        report = validate_plan_state(plan)

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any("a_field_from_the_future" in error for error in report["errors"]),
            report["errors"],
        )

    def test_the_pre_writer_contract_field_fixture_opens_and_validates(self):
        # The manifest shape every real store had before issue #88 landed: no such field.
        state_dir = self._copy_fixture("pre_writer_contract_field")
        manifest = json.loads((state_dir / "store.json").read_text())
        self.assertNotIn("writer_contract_version", manifest)

        report = doctor_store(state_dir)

        self.assertEqual("passed", report["status"], report)
        self.assertEqual([], report["errors"])
        # Never having recorded a version defaults to 1 -- the contract in force before
        # the field existed, which is the only thing such a store can have been written
        # under. An honest default, not an error and not left blank.
        self.assertEqual(1, report["writer_contract_version"])
        self.assertEqual(1, report["current_version"])
        self.assertEqual(0, report["event_count"])

    def test_a_legacy_store_is_snapshotted_before_the_first_write_that_outruns_it(self):
        """A store predating the field is one contract behind, and #93 is why.

        This test used to assert the opposite, and correctly: while
        ``WRITER_CONTRACT_VERSION`` was 1, a store that never recorded one was at the
        same contract as the code, so its next write was ordinary. Issue #93 moved the
        contract to 2, which is precisely the situation the guard was built for -- so the
        first write takes the backup first, and the athlete keeps a verified copy of the
        store as it was before this code touched it.

        The equal-version case it used to cover has not gone anywhere; it is the test
        below, held against a store this checkout wrote itself.
        """
        state_dir = self._copy_fixture("pre_writer_contract_field")
        after = load("plan-state-v2-day-4.json")
        event = load("decision-event-day-4.json")
        context = load("coach-context-day-4.json")

        result = apply_decision(state_dir, context=context, after=after, event=event)

        self.assertEqual("passed", result["status"])
        self.assertIn("writer_contract_snapshot", result)
        snapshot = doctor_store(Path(result["writer_contract_snapshot"]))
        # The backup is the store as it was: still on the old contract, still at v1.
        self.assertEqual("passed", snapshot["status"], snapshot)
        self.assertEqual(1, snapshot["writer_contract_version"])
        self.assertEqual(1, snapshot["current_version"])
        # And the live store moved on, now recording the contract that wrote it.
        manifest = json.loads((state_dir / "store.json").read_text())
        self.assertEqual(WRITER_CONTRACT_VERSION, manifest["writer_contract_version"])

    def test_an_equal_contract_store_takes_an_ordinary_write_with_no_backup(self):
        # The other half, and the common one: writing to a store this checkout itself
        # wrote must cost nothing an operator would notice.
        state_dir = self._copy_fixture("current_contract")
        self.assertEqual(
            WRITER_CONTRACT_VERSION, doctor_store(state_dir)["writer_contract_version"]
        )
        before = doctor_store(state_dir)["current_version"]

        result = apply_decision(
            state_dir,
            context=load("coach-context-day-4.json"),
            after=load("plan-state-v2-day-4.json"),
            event=load("decision-event-day-4.json"),
        )

        self.assertEqual("passed", result["status"])
        self.assertNotIn("writer_contract_snapshot", result)
        resolved = state_dir.resolve()
        self.assertFalse((resolved.parent / f"{resolved.name}.snapshots").exists())
        self.assertEqual(before, doctor_store(state_dir)["current_version"])


class SnapshotAndRestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "coach-state"
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        self.context = load("coach-context-day-4.json")
        init_store(self.state_dir, self.before)

    def commit_names(self) -> list[str]:
        return sorted(path.name for path in (self.state_dir / "commits").iterdir())

    def snapshots_root(self) -> Path:
        resolved = self.state_dir.resolve()
        return resolved.parent / f"{resolved.name}.snapshots"

    # -- the guard itself ---------------------------------------------------------------

    def test_a_manual_snapshot_is_verified_and_discoverable(self):
        result = snapshot_store(self.state_dir, reason="before-risky-change")

        self.assertEqual("passed", result["status"])
        snapshot_dir = Path(result["snapshot_dir"])
        self.assertTrue(snapshot_dir.is_dir())
        self.assertIn("before-risky-change", snapshot_dir.name)
        self.assertEqual("passed", doctor_store(snapshot_dir)["status"])
        self.assertEqual(self.snapshots_root(), snapshot_dir.parent)

    def test_code_newer_than_store_snapshots_before_the_first_write_and_records_the_new_version(self):
        with mock.patch.object(store_module, "WRITER_CONTRACT_VERSION", WRITER_CONTRACT_VERSION + 1):
            result = apply_decision(
                self.state_dir, context=self.context, after=self.after, event=self.event
            )
            # Still "code version +1" here: the live store it just wrote opens cleanly.
            self.assertEqual("passed", doctor_store(self.state_dir)["status"])

        self.assertIn("writer_contract_snapshot", result)
        snapshot_dir = Path(result["writer_contract_snapshot"])
        snapshot_doctor = doctor_store(snapshot_dir)
        self.assertEqual("passed", snapshot_doctor["status"])
        # The snapshot is the store exactly as it was *before* this write: still on the
        # old contract, still at version 1, with no decision applied yet.
        self.assertEqual(WRITER_CONTRACT_VERSION, snapshot_doctor["writer_contract_version"])
        self.assertEqual(1, snapshot_doctor["current_version"])
        self.assertEqual(0, snapshot_doctor["event_count"])
        # The live store moved on and now records the new contract.
        manifest = json.loads((self.state_dir / "store.json").read_text())
        self.assertEqual(WRITER_CONTRACT_VERSION + 1, manifest["writer_contract_version"])
        # Back to this checkout's real, unmocked version: forward-only means this store
        # is now unreadable by it too, exactly like a store any newer release wrote.
        reverted = doctor_store(self.state_dir)
        self.assertEqual("blocked", reverted["status"])
        self.assertEqual(WRITER_CONTRACT_VERSION + 1, reverted["writer_contract_version"])

    def test_an_idempotent_replay_never_triggers_a_snapshot(self):
        apply_decision(self.state_dir, context=self.context, after=self.after, event=self.event)

        with mock.patch.object(store_module, "WRITER_CONTRACT_VERSION", WRITER_CONTRACT_VERSION + 1):
            # Same event_id, same content: a retry, not a new write. Nothing about the
            # writer-contract guard should fire for a request that writes nothing.
            replay = apply_decision(
                self.state_dir, context=self.context, after=self.after, event=self.event
            )

        self.assertTrue(replay["idempotent_replay"])
        self.assertNotIn("writer_contract_snapshot", replay)
        self.assertFalse(self.snapshots_root().exists())

    def test_store_newer_than_code_is_refused_before_creating_a_commit(self):
        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["writer_contract_version"] = WRITER_CONTRACT_VERSION + 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before_commits = self.commit_names()

        with self.assertRaisesRegex(StateStoreError, "writer-contract version") as cm:
            apply_decision(self.state_dir, context=self.context, after=self.after, event=self.event)

        message = str(cm.exception)
        self.assertIn(str(WRITER_CONTRACT_VERSION + 1), message)
        self.assertIn(str(WRITER_CONTRACT_VERSION), message)
        self.assertIn("pull a checkout", message)
        self.assertEqual(before_commits, self.commit_names())
        self.assertFalse(self.snapshots_root().exists())

    def test_doctor_store_surfaces_the_newer_store_version_and_a_remedy(self):
        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["writer_contract_version"] = WRITER_CONTRACT_VERSION + 5
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = doctor_store(self.state_dir)

        self.assertEqual("blocked", report["status"])
        self.assertEqual(WRITER_CONTRACT_VERSION + 5, report["writer_contract_version"])
        self.assertEqual(1, len(report["errors"]))
        self.assertIn("pull a checkout", report["errors"][0])

    def test_doctor_store_surfaces_the_version_on_an_ordinary_healthy_store(self):
        report = doctor_store(self.state_dir)

        self.assertEqual("passed", report["status"])
        self.assertEqual(WRITER_CONTRACT_VERSION, report["writer_contract_version"])

    def test_a_non_integer_writer_contract_version_is_a_validation_error_not_a_crash(self):
        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["writer_contract_version"] = "not-a-version"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = doctor_store(self.state_dir)

        self.assertEqual("blocked", report["status"])
        self.assertIn(
            "store.json writer_contract_version must be a positive integer", report["errors"]
        )
        self.assertEqual("not-a-version", report["writer_contract_version"])

    # -- the ordering fix (issue #101) ----------------------------------------------------

    def _break_initial_commit_to_an_incompatible_older_shape(self) -> None:
        """Stand in for a real commit an older checkout made, under a shape issue #93 no
        longer accepts -- without also tripping an unrelated integrity-hash mismatch.

        ``init_store`` refuses to persist an invalid PlanState by design (AGENTS.md 6), so
        no valid call in this checkout can produce a store like this on its own; that is
        exactly why it has to be built by hand here. It stands in for history no API in
        this checkout can write anymore, not for something going wrong in this checkout's
        own write path -- the receipt is recomputed around the edit so the only thing
        wrong with the store is the shape, the same as a genuine pre-#93 commit.
        """
        commit_dir = next((self.state_dir / "commits").glob("00000001-*"))
        plan_path = commit_dir / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        del plan["week"]["sessions"][0]["plan"]
        del plan["week"]["sessions"][0]["prescription"]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        receipt_path = commit_dir / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["plan_hash"] = store_module.canonical_hash(plan)
        receipt.pop("receipt_hash", None)
        receipt["receipt_hash"] = store_module.canonical_hash(receipt)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["writer_contract_version"] = WRITER_CONTRACT_VERSION - 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_a_store_incompatible_with_the_current_contract_leads_with_the_mismatch(self):
        # The scenario issue #101 is about: not just an old version number, but stored
        # history that genuinely does not fit what this code now requires. The wall of
        # schema errors #94 was built to replace never stopped being possible -- it just
        # stopped being the *first* thing read.
        self._break_initial_commit_to_an_incompatible_older_shape()

        report = doctor_store(self.state_dir)

        self.assertEqual("blocked", report["status"])
        self.assertEqual(WRITER_CONTRACT_VERSION - 1, report["writer_contract_version"])
        # The mismatch and its remedy lead: both versions and a way forward, in the first
        # error -- not somewhere an operator has to scroll a schema-error wall to find.
        leading = report["errors"][0]
        self.assertIn(f"writer-contract version {WRITER_CONTRACT_VERSION - 1}", leading)
        self.assertIn(f"writer-contract version {WRITER_CONTRACT_VERSION}", leading)
        self.assertIn("older", leading)
        self.assertIn("regenerate", leading)
        # Doctor's full pass still ran -- the schema errors it found are still reported,
        # just not first.
        self.assertGreater(len(report["errors"]), 1)
        self.assertTrue(
            any("sessions[0].plan is required" in error for error in report["errors"][1:])
        )

        before_commits = self.commit_names()
        with self.assertRaisesRegex(StateStoreError, "older than this code") as cm:
            apply_decision(
                self.state_dir, context=self.context, after=self.after, event=self.event
            )
        # The exception's own top-level message -- what the gateway's generic handler
        # actually forwards -- is the same leading sentence, not the generic fallback.
        self.assertEqual(leading, str(cm.exception))
        self.assertNotIn("state store failed doctor", str(cm.exception))
        # Refused before anything was written: no new commit, no snapshot either.
        self.assertEqual(before_commits, self.commit_names())
        self.assertFalse(self.snapshots_root().exists())

    def test_a_torn_store_at_the_current_contract_is_still_refused_and_not_written(self):
        # The pre-check must not become a way to bypass doctor: a store that claims the
        # exact version this code writes, but is torn for an unrelated reason, still has
        # to fail closed -- with the same generic message as before, since there is no
        # version mismatch to lead with.
        plan_path = next((self.state_dir / "commits").glob("*/plan.json"))
        tampered = json.loads(plan_path.read_text())
        tampered["week"]["intent"] = "tampered-while-versions-match"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")
        before_commits = self.commit_names()
        self.assertEqual(
            WRITER_CONTRACT_VERSION, doctor_store(self.state_dir)["writer_contract_version"]
        )

        with self.assertRaisesRegex(StateStoreError, "state store failed doctor"):
            apply_decision(
                self.state_dir, context=self.context, after=self.after, event=self.event
            )

        self.assertEqual(before_commits, self.commit_names())
        self.assertFalse(self.snapshots_root().exists())

    # -- the restore drill ----------------------------------------------------------------

    def test_restore_drill_full_cycle(self):
        """snapshot -> a write leaves the store broken -> restore -> doctor-store green."""
        snapshot = snapshot_store(self.state_dir, reason="pre-risky-op")
        snapshot_dir = Path(snapshot["snapshot_dir"])

        # Stand in for whatever a snapshot exists to survive: the live store ends up
        # broken after the snapshot was taken.
        plan_path = next((self.state_dir / "commits").glob("*/plan.json"))
        tampered = json.loads(plan_path.read_text())
        tampered["week"]["intent"] = "tampered-for-drill"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertEqual("blocked", doctor_store(self.state_dir)["status"])

        preview = restore_snapshot(snapshot_dir, self.state_dir, confirm=False)
        self.assertEqual("preview", preview["status"])
        self.assertEqual(1, preview["current_version"])
        # A preview writes nothing: the store is still broken.
        self.assertEqual("blocked", doctor_store(self.state_dir)["status"])

        restored = restore_snapshot(snapshot_dir, self.state_dir, confirm=True)

        self.assertEqual("restored", restored["status"])
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])
        self.assertEqual(WRITER_CONTRACT_VERSION, doctor_store(self.state_dir)["writer_contract_version"])
        # The broken store was preserved, not discarded.
        displaced = Path(restored["displaced_previous_store"])
        self.assertTrue(displaced.is_dir())
        displaced_plan = json.loads(next(displaced.glob("commits/*/plan.json")).read_text())
        self.assertEqual("tampered-for-drill", displaced_plan["week"]["intent"])

    def test_restoring_from_a_snapshot_that_does_not_open_is_refused(self):
        snapshot = snapshot_store(self.state_dir, reason="will-be-corrupted")
        snapshot_dir = Path(snapshot["snapshot_dir"])
        plan_path = next(snapshot_dir.glob("commits/*/plan.json"))
        corrupted = json.loads(plan_path.read_text())
        corrupted["week"]["intent"] = "corrupted-snapshot"
        plan_path.write_text(json.dumps(corrupted), encoding="utf-8")

        with self.assertRaisesRegex(StateStoreError, "snapshot does not open"):
            restore_snapshot(snapshot_dir, self.state_dir, confirm=True)

        # The live (good) store is untouched by the refused restore.
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])

    def test_snapshotting_a_store_that_already_fails_doctor_is_refused(self):
        plan_path = next((self.state_dir / "commits").glob("*/plan.json"))
        tampered = json.loads(plan_path.read_text())
        tampered["week"]["intent"] = "already-broken"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaisesRegex(StateStoreError, "state store failed doctor"):
            snapshot_store(self.state_dir, reason="should-not-happen")

        self.assertFalse(self.snapshots_root().exists())


if __name__ == "__main__":
    unittest.main()
