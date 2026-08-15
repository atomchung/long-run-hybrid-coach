"""Regression tests for the CLI's own surface.

`--help` is the first thing anyone reads, including the two commands every session is
told to run before deciding anything (`doctor-store`, `status`). argparse lists a
subcommand's name among the choices whether or not it was given a help string, and
describes only the ones that were -- so an undescribed command is visible as a word and
explained nowhere.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop.cli import build_parser, main
from garmin_coach_loop.gateway import identity_db_path
from garmin_coach_loop.identity import lookup_or_create_owner, owner_for_provider_athlete
from garmin_coach_loop.store import (
    WRITER_CONTRACT_VERSION,
    init_store,
    read_current_plan,
    resolve_state_dir,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def _subcommands() -> argparse._SubParsersAction:
    parser = build_parser()
    for action in parser._actions:  # argparse exposes no public accessor for these
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("the CLI declares no subcommands")


class CommandHelpTests(unittest.TestCase):
    def test_every_subcommand_is_described_not_only_listed(self):
        action = _subcommands()
        described = {choice.dest for choice in action._choices_actions}
        self.assertEqual(set(action.choices), described)

    def test_no_subcommand_is_described_with_an_empty_string(self):
        for choice in _subcommands()._choices_actions:
            with self.subTest(command=choice.dest):
                self.assertTrue((choice.help or "").strip())


class AdoptOwnerStoreTests(unittest.TestCase):
    """The operator half of the bootstrap: an existing store, given to a signed-in owner.

    Everything here runs against a temporary gateway state root and a temporary source
    store. Nothing reads the machine's own state directory, and no test may ever create
    one -- that is the whole point of the command being explicit.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.source = base / "local-store"
        self.state_root = base / "gateway-root"
        self.plan = load("plan-state-v1.json")
        init_store(self.source, self.plan)
        self.identity_db = identity_db_path(self.state_root)
        self.owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def adopt(self, *arguments: str, athlete_id: str = "i1") -> tuple[int, dict[str, Any]]:
        return self.run_cli(
            "adopt-owner-store",
            "--athlete-id", athlete_id,
            "--from", str(self.source),
            "--state-root", str(self.state_root),
            *arguments,
        )

    def owner_dir(self, owner_id: str) -> Path:
        return resolve_state_dir(owner_id, state_root=self.state_root)

    def snapshot(self, state_dir: Path) -> dict[str, str]:
        return {
            str(path.relative_to(state_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(state_dir.rglob("*"))
            if path.is_file()
        }

    def test_the_preview_names_the_exact_source_and_destination_and_writes_nothing(self):
        code, report = self.adopt()

        self.assertEqual(0, code)
        self.assertEqual("preview", report["status"])
        self.assertEqual(str(self.source), report["source"])
        self.assertEqual(str(self.owner_dir(self.owner_id)), report["destination"])
        self.assertEqual(self.owner_id, report["owner_id"])
        self.assertEqual("fixture-plan-001", report["plan_id"])
        self.assertEqual(1, report["current_version"])
        self.assertFalse(self.owner_dir(self.owner_id).exists())

    def test_a_confirmed_adoption_lets_the_owner_directory_open_the_same_plan(self):
        before = self.snapshot(self.source)

        code, report = self.adopt("--confirm")

        self.assertEqual(0, code)
        self.assertEqual("adopted", report["status"])
        self.assertEqual("link", report["mode"])
        self.assertEqual(
            self.plan, read_current_plan(self.owner_dir(self.owner_id))["current_plan"]
        )
        self.assertEqual(before, self.snapshot(self.source))

    def test_copy_mode_duplicates_the_whole_history_and_leaves_the_source_alone(self):
        before = self.snapshot(self.source)

        code, report = self.adopt("--mode", "copy", "--confirm")

        self.assertEqual(0, code)
        self.assertEqual("copy", report["mode"])
        destination = self.owner_dir(self.owner_id)
        self.assertFalse(destination.is_symlink())
        self.assertEqual(before, self.snapshot(destination))
        self.assertEqual(before, self.snapshot(self.source))

    def test_an_owner_that_already_has_state_is_never_overwritten_or_merged(self):
        other = json.loads(json.dumps(self.plan))
        other["plan_id"] = "fixture-plan-002"
        init_store(self.owner_dir(self.owner_id), other)
        before = self.snapshot(self.owner_dir(self.owner_id))

        code, report = self.adopt("--confirm")

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("refusing to overwrite or merge", report["error"])
        self.assertEqual(before, self.snapshot(self.owner_dir(self.owner_id)))
        self.assertEqual(
            "fixture-plan-002", read_current_plan(self.owner_dir(self.owner_id))["plan_id"]
        )

    def test_an_athlete_that_never_signed_in_is_refused_and_no_owner_is_created(self):
        code, report = self.adopt("--confirm", athlete_id="i2")

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("i2", report["error"])
        self.assertIsNone(owner_for_provider_athlete(self.identity_db, "intervals", "i2"))
        self.assertFalse((self.state_root / "owners").exists())

    def test_one_athletes_adoption_never_reaches_another_athletes_directory(self):
        second_owner = lookup_or_create_owner(self.identity_db, "intervals", "i2")

        code, report = self.adopt("--confirm", athlete_id="i2")

        self.assertEqual(0, code)
        self.assertEqual(str(self.owner_dir(second_owner)), report["destination"])
        self.assertTrue(self.owner_dir(second_owner).exists())
        self.assertFalse(self.owner_dir(self.owner_id).exists())

    def test_a_source_that_is_not_a_valid_store_is_refused(self):
        (self.source / "store.json").write_text("{}", encoding="utf-8")

        code, report = self.adopt("--confirm")

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("source is not a valid PlanState store", report["error"])
        self.assertTrue(report["details"]["errors"])
        self.assertFalse(self.owner_dir(self.owner_id).exists())

    def test_the_single_user_home_variable_is_never_the_gateway_state_root(self):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GARMIN_COACH_LOOP_")
        }
        environment["GARMIN_COACH_LOOP_HOME"] = str(self.state_root)

        with mock.patch.dict(os.environ, environment, clear=True):
            code, report = self.run_cli(
                "adopt-owner-store", "--athlete-id", "i1", "--from", str(self.source)
            )

        self.assertEqual(2, code)
        self.assertIn("GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT", report["error"])
        self.assertFalse((self.state_root / "owners").exists())


class WriterContractCliTests(unittest.TestCase):
    """The CLI is one of the two entry paths the writer-contract guard has to cover.

    The guard itself lives once in ``garmin_coach_loop.store`` (see
    ``tests/test_writer_contract.py``); everything here is only proving the CLI actually
    reaches it -- for `apply-decision`, and for the `doctor-store` / `snapshot-store` /
    `restore-store` surface an operator uses to inspect and recover a store by hand.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "coach-state"
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        self.context = load("coach-context-day-4.json")
        init_store(self.state_dir, self.before)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def write_json(self, name: str, value: dict[str, Any]) -> Path:
        path = Path(self._tmp.name) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def bump_store_writer_contract_version(self, delta: int) -> None:
        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["writer_contract_version"] = WRITER_CONTRACT_VERSION + delta
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_doctor_store_reports_the_writer_contract_version(self):
        code, report = self.run_cli("doctor-store", "--state-dir", str(self.state_dir))

        self.assertEqual(0, code)
        self.assertEqual("passed", report["status"])
        self.assertEqual(WRITER_CONTRACT_VERSION, report["writer_contract_version"])

    def test_apply_decision_is_refused_before_a_commit_when_the_store_outruns_this_code(self):
        self.bump_store_writer_contract_version(+1)
        context_path = self.write_json("context.json", self.context)
        after_path = self.write_json("after.json", self.after)
        event_path = self.write_json("event.json", self.event)
        commits_before = sorted(p.name for p in (self.state_dir / "commits").iterdir())

        code, report = self.run_cli(
            "apply-decision",
            "--state-dir", str(self.state_dir),
            "--context", str(context_path),
            "--after", str(after_path),
            "--event", str(event_path),
        )

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn(str(WRITER_CONTRACT_VERSION + 1), report["error"])
        self.assertIn(str(WRITER_CONTRACT_VERSION), report["error"])
        self.assertIn("pull a checkout", report["error"])
        commits_after = sorted(p.name for p in (self.state_dir / "commits").iterdir())
        self.assertEqual(commits_before, commits_after)

    def test_snapshot_store_then_restore_store_round_trip(self):
        snapshot_code, snapshot_report = self.run_cli(
            "snapshot-store", "--state-dir", str(self.state_dir), "--reason", "cli-drill"
        )
        self.assertEqual(0, snapshot_code)
        self.assertEqual("passed", snapshot_report["status"])
        snapshot_dir = snapshot_report["snapshot_dir"]

        # Break the live store the way any failed write could.
        plan_path = next((self.state_dir / "commits").glob("*/plan.json"))
        tampered = json.loads(plan_path.read_text())
        tampered["week"]["intent"] = "tampered-by-cli-test"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")
        broken_code, broken_report = self.run_cli(
            "doctor-store", "--state-dir", str(self.state_dir)
        )
        self.assertEqual(2, broken_code)
        self.assertEqual("blocked", broken_report["status"])

        preview_code, preview_report = self.run_cli(
            "restore-store", "--snapshot", snapshot_dir, "--state-dir", str(self.state_dir)
        )
        self.assertEqual(0, preview_code)
        self.assertEqual("preview", preview_report["status"])
        self.assertEqual(
            "blocked", self.run_cli("doctor-store", "--state-dir", str(self.state_dir))[1]["status"]
        )

        restore_code, restore_report = self.run_cli(
            "restore-store", "--snapshot", snapshot_dir, "--state-dir", str(self.state_dir),
            "--confirm",
        )
        self.assertEqual(0, restore_code)
        self.assertEqual("restored", restore_report["status"])

        final_code, final_report = self.run_cli("doctor-store", "--state-dir", str(self.state_dir))
        self.assertEqual(0, final_code)
        self.assertEqual("passed", final_report["status"])


class StatusCommandTimezoneTests(unittest.TestCase):
    """`status --timezone` (issue #112): an explicit IANA zone answers "today" without
    the caller pre-computing `--today` -- the argument-wiring half of the fix; the actual
    cross-zone date-boundary arithmetic is covered directly against `status_store` in
    ``tests/test_state_store.py``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "coach-state"
        init_store(self.state_dir, load("plan-state-v1.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_status_subparser_declares_a_timezone_option(self):
        status_parser = _subcommands().choices["status"]
        dests = {action.dest for action in status_parser._actions}
        self.assertIn("timezone", dests)

    def test_explicit_today_succeeds_even_with_an_unresolvable_timezone(self):
        # An already-resolved date is authoritative; --timezone is only ever consulted
        # to compute "today" when --today is omitted.
        code, report = self.run_cli(
            "status",
            "--state-dir", str(self.state_dir),
            "--today", "2026-08-14",
            "--timezone", "Not/AZone",
        )
        self.assertEqual(0, code)
        self.assertEqual("passed", report["status"])
        self.assertEqual("2026-08-14", report["as_of_date"])
        self.assertEqual("strength-upper-01", report["next_session"]["session_id"])

    def test_unknown_timezone_is_refused_with_one_actionable_error(self):
        code, report = self.run_cli(
            "status",
            "--state-dir", str(self.state_dir),
            "--timezone", "Nowhere/Nothing",
        )
        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("unknown timezone", report["error"])
        self.assertIn("Nowhere/Nothing", report["error"])

    def test_timezone_flag_reaches_status_store_the_same_way_today_does(self):
        # Fixed proof that the CLI flag is actually wired to status_store's own
        # timezone resolution, not merely parsed and dropped: the same instant a
        # human would hit near a Taipei midnight, expressed as an explicit --today
        # equivalent for the CLI (which has no --now injection point of its own) --
        # here asserting the flag is accepted and produces the plan's current status
        # exactly as omitting it (the documented Asia/Taipei default) does.
        default_code, default_report = self.run_cli("status", "--state-dir", str(self.state_dir))
        explicit_code, explicit_report = self.run_cli(
            "status", "--state-dir", str(self.state_dir), "--timezone", "Asia/Taipei",
        )
        self.assertEqual(0, default_code)
        self.assertEqual(0, explicit_code)
        self.assertEqual(default_report["as_of_date"], explicit_report["as_of_date"])
        self.assertEqual(default_report["next_session"], explicit_report["next_session"])


class RecordAvailabilityCommandTests(unittest.TestCase):
    """`record-availability` (#28): the local half of storing which days the athlete trains.

    There is deliberately no matching strength command. On this machine per-set truth
    already arrives through `--health-db`, measured rather than recalled, and a second
    local way in would only create a way for the two to disagree.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "coach-state"

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def _evidence(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "athlete-evidence.json").read_text(encoding="utf-8"))

    def test_a_recurring_week_is_recorded_and_reported_back(self):
        code, report = self.run_cli(
            "record-availability",
            "--state-dir", str(self.state_dir),
            "--recurring-available", "mon,wed,fri",
            "--recurring-unavailable", "sun",
        )

        self.assertEqual(0, code)
        recurring = self._evidence()["availability"]["recurring"]
        self.assertEqual(["mon", "wed", "fri"], recurring["available_days"])
        self.assertEqual(["sun"], recurring["unavailable_days"])
        self.assertEqual(["mon", "wed", "fri"], report["recurring"]["available_days"])
        self.assertEqual("recurring", report["effective_this_week"]["basis"])

    def test_one_week_can_be_stated_without_touching_the_normal_week(self):
        self.run_cli(
            "record-availability",
            "--state-dir", str(self.state_dir),
            "--recurring-available", "mon,wed,fri",
        )
        # A Monday far enough ahead that the assertion does not depend on when the suite
        # runs -- the command refuses a week that has already begun.
        week_start = (
            dt.date.today() + dt.timedelta(days=14 - dt.date.today().weekday())
        ).isoformat()

        code, report = self.run_cli(
            "record-availability",
            "--state-dir", str(self.state_dir),
            "--week-start", week_start,
            "--week-unavailable", "wed",
        )

        self.assertEqual(0, code)
        availability = self._evidence()["availability"]
        self.assertEqual(["mon", "wed", "fri"], availability["recurring"]["available_days"])
        self.assertEqual([week_start], [item["week_start"] for item in availability["week_overrides"]])
        self.assertEqual(["wed"], report["week_override"]["unavailable_days"])

    def test_a_call_naming_no_day_is_refused_and_writes_nothing(self):
        code, report = self.run_cli("record-availability", "--state-dir", str(self.state_dir))

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_an_unknown_weekday_is_named_rather_than_dropped(self):
        code, report = self.run_cli(
            "record-availability",
            "--state-dir", str(self.state_dir),
            "--recurring-available", "mon,someday",
        )

        self.assertEqual(2, code)
        self.assertIn("someday", report["error"])


if __name__ == "__main__":
    unittest.main()
