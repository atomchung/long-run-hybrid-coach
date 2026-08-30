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

from tests import fit_fixtures
from garmin_coach_loop.cli import build_parser, main
from garmin_coach_loop.gateway import identity_db_path
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_for_provider_athlete,
    record_token_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.source_intervals import IntervalsCredentials, ProviderResponse
from garmin_coach_loop.store import (
    WRITER_CONTRACT_VERSION,
    init_store,
    open_delivery_attempt,
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


class ServeGatewayArgumentTests(unittest.TestCase):
    """``--host``/``--port`` must default to ``None``, not a hard-coded value.

    ``load_config`` (gateway.py) tells an omitted flag apart from an explicitly given one
    only by that ``None``, and uses the distinction to decide whether
    ``GARMIN_COACH_LOOP_GATEWAY_HOST``/``_PORT`` apply.
    """

    def test_host_and_port_default_to_none_so_the_environment_fallback_can_apply(self):
        args = build_parser().parse_args(["serve-gateway"])
        self.assertIsNone(args.host)
        self.assertIsNone(args.port)

    def test_an_explicit_host_and_port_are_still_parsed_normally(self):
        args = build_parser().parse_args(
            ["serve-gateway", "--host", "0.0.0.0", "--port", "9000"]
        )
        self.assertEqual("0.0.0.0", args.host)
        self.assertEqual(9000, args.port)


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


class DeleteOwnerCommandTests(unittest.TestCase):
    """The operator half of the owner-deletion workflow: end-to-end CLI coverage for `delete-owner`.

    Identity-table deletion and cross-owner protection at the SQL layer are covered
    directly in ``tests/test_identity.py``; store-directory deletion, the delivery
    fence, and the linked-owner guard are covered directly in
    ``tests/test_state_store.py::DeleteOwnerStoreTests``. Everything here proves the CLI
    wires ``--identity-db``/``--state-root``/``--owner-id``/``--confirm`` to those two
    functions correctly and reports the receipt this command actually promises.
    """

    HMAC_KEY = b"unit-test-fingerprint-key-0000000"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.state_root = base / "gateway-root"
        self.identity_db = identity_db_path(self.state_root)
        self.plan = load("plan-state-v1.json")
        self.owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        record_token_fingerprint(
            self.identity_db,
            token_fingerprint("tok-i1", hmac_key=self.HMAC_KEY),
            self.owner_id,
            "intervals",
            scope_names=("ACTIVITY:READ",),
        )
        init_store(self.owner_dir(self.owner_id), self.plan)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def delete(self, owner_id: str, *arguments: str) -> tuple[int, dict[str, Any]]:
        return self.run_cli(
            "delete-owner",
            "--identity-db", str(self.identity_db),
            "--state-root", str(self.state_root),
            "--owner-id", owner_id,
            *arguments,
        )

    def owner_dir(self, owner_id: str) -> Path:
        return resolve_state_dir(owner_id, state_root=self.state_root)

    def snapshot(self, directory: Path) -> dict[str, str]:
        return {
            str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    def test_dry_run_reports_the_exact_rows_and_directory_and_writes_nothing(self):
        before = self.snapshot(self.owner_dir(self.owner_id))

        code, report = self.delete(self.owner_id)

        self.assertEqual(0, code)
        self.assertEqual("preview", report["status"])
        self.assertEqual(self.owner_id, report["owner_id"])
        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1, "owner_revocations": 0},
            report["identity_rows"],
        )
        self.assertTrue(report["state_dir_exists"])
        self.assertFalse(report["state_dir_is_link"])
        self.assertIsNotNone(owner_for_provider_athlete(self.identity_db, "intervals", "i1"))
        self.assertTrue(self.owner_dir(self.owner_id).is_dir())
        self.assertEqual(before, self.snapshot(self.owner_dir(self.owner_id)))

    def test_confirm_deletes_identity_rows_and_the_state_directory(self):
        code, report = self.delete(self.owner_id, "--confirm")

        self.assertEqual(0, code)
        self.assertEqual("deleted", report["status"])
        self.assertEqual(self.owner_id, report["owner_id"])
        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1, "owner_revocations": 0},
            report["identity_rows_deleted"],
        )
        self.assertTrue(report["state_dir_removed"])
        # The receipt may name the tables it touched (the deletion contract's "minimal audit receipt"),
        # but never the raw token or the fingerprint derived from it.
        rendered = json.dumps(report)
        self.assertNotIn("tok-i1", rendered)
        self.assertNotIn(token_fingerprint("tok-i1", hmac_key=self.HMAC_KEY), rendered)
        self.assertIsNone(owner_for_provider_athlete(self.identity_db, "intervals", "i1"))
        self.assertFalse(self.owner_dir(self.owner_id).exists())

    def test_confirmed_deletion_never_touches_a_second_owners_rows_or_directory(self):
        second = lookup_or_create_owner(self.identity_db, "intervals", "i2")
        record_token_fingerprint(
            self.identity_db,
            token_fingerprint("tok-i2", hmac_key=self.HMAC_KEY),
            second,
            "intervals",
        )
        init_store(self.owner_dir(second), self.plan)
        before = self.snapshot(self.owner_dir(second))

        code, _ = self.delete(self.owner_id, "--confirm")

        self.assertEqual(0, code)
        self.assertEqual(second, owner_for_provider_athlete(self.identity_db, "intervals", "i2"))
        self.assertTrue(self.owner_dir(second).is_dir())
        self.assertEqual(before, self.snapshot(self.owner_dir(second)))

    def test_an_unresolved_delivery_attempt_blocks_deletion(self):
        session_id = self.plan["week"]["sessions"][0]["session_id"]
        attempt = open_delivery_attempt(
            self.owner_dir(self.owner_id),
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

        code, report = self.delete(self.owner_id, "--confirm")

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn(attempt["attempt_id"], report["error"])
        self.assertIsNotNone(owner_for_provider_athlete(self.identity_db, "intervals", "i1"))
        self.assertTrue(self.owner_dir(self.owner_id).is_dir())

    def test_deleting_an_owner_that_does_not_exist_is_idempotent(self):
        unknown = "99999999-8888-7777-6666-555544443333"

        code, report = self.delete(unknown, "--confirm")

        self.assertEqual(0, code)
        self.assertEqual("absent", report["status"])
        self.assertEqual(unknown, report["owner_id"])

    def test_a_second_confirmed_run_after_deletion_is_a_harmless_no_op(self):
        first_code, first_report = self.delete(self.owner_id, "--confirm")
        self.assertEqual(0, first_code)
        self.assertEqual("deleted", first_report["status"])

        second_code, second_report = self.delete(self.owner_id, "--confirm")

        self.assertEqual(0, second_code)
        self.assertEqual("absent", second_report["status"])

    def test_an_invalid_owner_id_is_refused_before_touching_anything(self):
        code, report = self.delete("not-a-uuid", "--confirm")

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("UUID", report["error"])
        self.assertIsNotNone(owner_for_provider_athlete(self.identity_db, "intervals", "i1"))
        self.assertTrue(self.owner_dir(self.owner_id).is_dir())


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
        self.assertEqual(["wed"], report["week"]["unavailable_days"])
        # The normal week is untouched, and the week that lost Wednesday keeps the rest of
        # it -- the losing day does not take Monday and Friday down with it.
        self.assertEqual(["mon", "wed", "fri"], report["effective_this_week"]["available_days"])

    def test_a_week_note_stands_alone_and_costs_no_training_day(self):
        """Issue #164: this week's constraint, expiring with the week that carries it."""
        self.run_cli(
            "record-availability",
            "--state-dir", str(self.state_dir),
            "--recurring-available", "mon,wed,fri",
        )

        code, report = self.run_cli(
            "record-availability",
            "--state-dir", str(self.state_dir),
            "--week-note", "出差，飯店只有啞鈴",
        )

        self.assertEqual(0, code)
        effective = report["effective_this_week"]
        self.assertEqual(["出差，飯店只有啞鈴"], effective["week_constraints"])
        # It named no day, so it adjusted none.
        self.assertEqual(["mon", "wed", "fri"], effective["available_days"])
        self.assertEqual("recurring", effective["basis"])

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


class RecordProfileCommandTests(unittest.TestCase):
    """`record-profile`: the local half of storing where the athlete is and what they read.

    The point of the command is that it is the *only* place either is said. Every other
    command reads it back instead of taking a flag, which is what these tests check.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "coach-state"
        init_store(self.state_dir, load("plan-state-v1.json"))

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def _record(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        return self.run_cli("record-profile", "--state-dir", str(self.state_dir), *arguments)

    def test_a_stated_timezone_and_language_round_trip(self):
        code, report = self._record("--timezone", "Europe/Berlin", "--language", "en")

        self.assertEqual(0, code)
        self.assertEqual("Europe/Berlin", report["profile"]["timezone"])
        self.assertEqual("en", report["profile"]["language"])
        self.assertEqual(
            {"timezone": "Europe/Berlin", "language": "en"}, report["effective"]
        )

    def test_stating_only_a_language_reports_the_default_timezone_still_standing_in(self):
        code, report = self._record("--language", "en")

        self.assertEqual(0, code)
        self.assertIsNone(report["profile"]["timezone"])
        self.assertEqual("Asia/Taipei", report["effective"]["timezone"])

    def test_a_call_stating_neither_is_refused_and_writes_nothing(self):
        code, report = self._record()

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_an_unknown_timezone_is_named_rather_than_stored(self):
        code, report = self._record("--timezone", "Nowhere/Nothing")

        self.assertEqual(2, code)
        self.assertIn("Nowhere/Nothing", report["error"])

    def test_status_answers_today_from_the_stored_timezone_without_being_told_again(self):
        """The acceptance case: said once, and the next command already knows.

        Kiritimati is UTC+14 and Baker Island UTC-12, so on the same instant they are
        never the same date. Asserting the two against each other proves `status` read the
        stored value rather than defaulting.
        """
        self._record("--timezone", "Pacific/Kiritimati")
        _, ahead = self.run_cli("status", "--state-dir", str(self.state_dir))
        self._record("--timezone", "Etc/GMT+12")
        _, behind = self.run_cli("status", "--state-dir", str(self.state_dir))

        self.assertNotEqual(ahead["as_of_date"], behind["as_of_date"])

    def test_a_request_timezone_overrides_the_stored_one_for_that_command_only(self):
        self._record("--timezone", "Pacific/Kiritimati")

        _, overridden = self.run_cli(
            "status", "--state-dir", str(self.state_dir), "--timezone", "Etc/GMT+12"
        )
        _, stored = self.run_cli("status", "--state-dir", str(self.state_dir))

        self.assertNotEqual(overridden["as_of_date"], stored["as_of_date"])

    def test_an_unmigrated_store_answers_exactly_as_it_did_before(self):
        # No profile was ever stated, so every command still runs on Asia/Taipei.
        _, without_profile = self.run_cli("status", "--state-dir", str(self.state_dir))
        _, explicit_taipei = self.run_cli(
            "status", "--state-dir", str(self.state_dir), "--timezone", "Asia/Taipei"
        )

        self.assertEqual(explicit_taipei["as_of_date"], without_profile["as_of_date"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_a_withdrawal_counts_a_past_day_from_the_same_stored_timezone(self):
        """`withdraw-delivery` decides what has already passed, so it needs the athlete's
        own day -- the one `status` answers with, not this code's default.

        The provider round trip is stubbed out: what is under test is which day the
        command hands to the withdrawal boundary, which is where the whole difference
        between two athletes' calendars lives.
        """
        self._record("--timezone", "Pacific/Kiritimati")
        _, expected = self.run_cli("status", "--state-dir", str(self.state_dir))
        proposal = Path(self._tmp.name) / "withdrawal.json"
        approval = Path(self._tmp.name) / "approval.json"
        for path in (proposal, approval):
            path.write_text("{}", encoding="utf-8")

        with mock.patch("garmin_coach_loop.cli.resolve_credentials", return_value=object()), \
                mock.patch("garmin_coach_loop.cli.IntervalsTransport"), \
                mock.patch("garmin_coach_loop.cli.withdraw_approved_set") as withdraw:
            withdraw.return_value = {"status": "passed"}
            code, _ = self.run_cli(
                "withdraw-delivery",
                "--state-dir", str(self.state_dir),
                "--proposal", str(proposal),
                "--approval", str(approval),
                "--receipt-out", str(Path(self._tmp.name) / "receipt.json"),
            )

        self.assertEqual(0, code)
        self.assertEqual(expected["as_of_date"], withdraw.call_args.kwargs["today"])

    def _withdrawal_day(self, *arguments: str) -> tuple[int, Any]:
        """Run `withdraw-delivery` with the provider stubbed, and report the day it used."""
        proposal = Path(self._tmp.name) / "withdrawal.json"
        approval = Path(self._tmp.name) / "approval.json"
        for path in (proposal, approval):
            path.write_text("{}", encoding="utf-8")
        with mock.patch("garmin_coach_loop.cli.resolve_credentials", return_value=object()), \
                mock.patch("garmin_coach_loop.cli.IntervalsTransport"), \
                mock.patch("garmin_coach_loop.cli.withdraw_approved_set") as withdraw:
            withdraw.return_value = {"status": "passed"}
            code, report = self.run_cli(
                "withdraw-delivery",
                "--state-dir", str(self.state_dir),
                "--proposal", str(proposal),
                "--approval", str(approval),
                "--receipt-out", str(Path(self._tmp.name) / "receipt.json"),
                *arguments,
            )
        if code != 0:
            return code, report
        return code, withdraw.call_args.kwargs["today"]

    def test_the_withdrawal_subparser_declares_a_timezone_option(self):
        withdraw_parser = _subcommands().choices["withdraw-delivery"]
        dests = {action.dest for action in withdraw_parser._actions}
        self.assertIn("timezone", dests)

    def test_a_requested_timezone_answers_the_same_day_for_a_withdrawal_as_for_status(self):
        """Issue #17: `status` took `--timezone` and withdrawal did not, so at one instant
        the two commands could disagree about which day had already passed.

        The stored profile is deliberately a fourth zone, so a command that ignored the
        request would answer that one and be caught rather than accidentally agreeing.
        """
        self._record("--timezone", "Pacific/Kiritimati")

        for timezone in ("Asia/Taipei", "UTC", "America/New_York"):
            with self.subTest(timezone=timezone):
                _, expected = self.run_cli(
                    "status", "--state-dir", str(self.state_dir), "--timezone", timezone
                )
                code, used = self._withdrawal_day("--timezone", timezone)

                self.assertEqual(0, code)
                self.assertEqual(expected["as_of_date"], used)

    def test_an_explicit_today_outranks_a_requested_timezone_on_a_withdrawal(self):
        # `status` does not refuse an unresolvable zone beside an explicit --today,
        # because the date is already resolved and the zone is never consulted. The
        # withdrawal path short-circuits on the same `or`, so it must not refuse either.
        code, used = self._withdrawal_day("--today", "2026-08-14", "--timezone", "Not/AZone")

        self.assertEqual(0, code)
        self.assertEqual("2026-08-14", used)

    def test_an_unknown_withdrawal_timezone_is_refused_rather_than_silently_replaced(self):
        # Falling back to the stored profile here would delete a day the athlete never
        # asked about, so the named zone is refused instead.
        code, report = self._withdrawal_day("--timezone", "Nowhere/Nothing")

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("unknown timezone", report["error"])


class HostedFirstGateTests(unittest.TestCase):
    """A machine whose plan lives on the hosted coach does not write locally by accident.

    The gate is one list checked in `main`, so what these prove is the two things a list
    can get wrong: that a writing command is on it, and that a reading command is not.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name).resolve() / "store"
        self.plan = load("plan-state-v1.json")
        init_store(self.state_dir, self.plan)

    def run_cli(self, *arguments: str, gateway: str | None = None) -> tuple[int, dict[str, Any]]:
        environment = {"GARMIN_COACH_LOOP_GATEWAY_URL": gateway} if gateway else {}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=False):
            if gateway is None:
                os.environ.pop("GARMIN_COACH_LOOP_GATEWAY_URL", None)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_a_local_write_is_refused_while_a_hosted_coach_is_configured(self):
        code, report = self.run_cli(
            "record-profile", "--state-dir", str(self.state_dir), "--timezone", "UTC",
            gateway="https://coach.example",
        )
        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("hosted coach at https://coach.example", report["error"])
        self.assertIn("--offline", report["error"])

    def test_saying_offline_out_loud_is_enough(self):
        code, report = self.run_cli(
            "record-profile", "--state-dir", str(self.state_dir), "--timezone", "UTC",
            "--offline",
            gateway="https://coach.example",
        )
        self.assertEqual(0, code)
        self.assertEqual("passed", report["status"])

    def test_reading_the_local_store_is_never_gated(self):
        for command in ("status", "doctor-store", "history"):
            with self.subTest(command=command):
                code, report = self.run_cli(
                    command, "--state-dir", str(self.state_dir),
                    gateway="https://coach.example",
                )
                self.assertEqual(0, code, report)

    def test_a_machine_with_no_hosted_coach_writes_locally_as_before(self):
        code, report = self.run_cli(
            "record-profile", "--state-dir", str(self.state_dir), "--timezone", "UTC"
        )
        self.assertEqual(0, code, report)

    def test_every_command_that_writes_a_local_store_carries_the_flag(self):
        """The list in `main` and the flags on the parsers have to agree.

        They are two statements of the same fact, and a command on the list without the
        flag refuses with no way to say --offline at all.
        """
        from garmin_coach_loop.cli import LOCAL_STORE_WRITERS

        choices = _subcommands().choices
        self.assertTrue(LOCAL_STORE_WRITERS <= set(choices))
        for command in sorted(LOCAL_STORE_WRITERS):
            with self.subTest(command=command):
                flags = {
                    option
                    for action in choices[command]._actions
                    for option in action.option_strings
                }
                self.assertIn("--offline", flags)


class HostedSummaryUnknownTests(unittest.TestCase):
    """A partial hosted reply is unknown, never an absence the gateway did not state."""

    def summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        from garmin_coach_loop.cli import _hosted_session_summary

        return _hosted_session_summary("https://coach.example", payload)

    def test_a_reply_without_a_plan_state_is_handed_back_whole(self):
        summary = self.summary({"status": "passed"})
        self.assertNotIn("session_count", summary)
        self.assertEqual("hosted", summary["entry"])

    def test_a_plan_state_with_no_week_counts_nothing_rather_than_zero(self):
        summary = self.summary(
            {
                "status": "passed",
                "plan_state": {"present": True, "plan_id": "p", "plan_version": 7},
            }
        )
        self.assertTrue(summary["plan_present"])
        self.assertIsNone(summary["session_count"])
        self.assertIsNone(summary["sessions"])
        self.assertIsNone(summary["week_start"])

    def test_an_account_the_gateway_says_has_no_plan_still_reads_as_that(self):
        summary = self.summary(
            {
                "status": "no_plan_state",
                "plan_state": {"present": False, "plan_id": None, "plan_version": None},
            }
        )
        self.assertFalse(summary["plan_present"])
        self.assertEqual("no_plan_state", summary["status"])


class ReconciliationStatementTests(unittest.TestCase):
    """hosted-session must state whether startCoachSession wrote, every time it runs.

    ``_reconciliation_statement`` is the one place that sentence is computed; these tests
    drive it directly rather than through a live gateway, the same way
    ``HostedSummaryUnknownTests`` proves ``_hosted_session_summary`` above it.
    """

    def statement(self, payload: dict[str, Any]) -> str:
        from garmin_coach_loop.cli import _reconciliation_statement

        return _reconciliation_statement(payload)

    def test_no_applied_entries_reads_as_no_change(self):
        payload = {
            "status": "passed",
            "plan_state": {"plan_version": 4},
            "reconciliation": {"status": "passed", "applied": []},
        }
        self.assertEqual("reconciliation: no change", self.statement(payload))

    def test_applied_entries_state_the_version_jump(self):
        payload = {
            "status": "passed",
            "plan_state": {"plan_version": 4},
            "reconciliation": {
                "status": "passed",
                "applied": [
                    {
                        "session_id": "run-quality-01",
                        "version": 3,
                        "idempotent_replay": False,
                    },
                    {
                        "session_id": "run-long-01",
                        "version": 4,
                        "idempotent_replay": False,
                    },
                ],
            },
        }
        self.assertEqual(
            "reconciliation applied: version 2 -> 4", self.statement(payload)
        )

    def test_a_pure_replay_still_reads_as_no_change(self):
        """A retry that only reconfirms an earlier commit wrote nothing new this call."""
        payload = {
            "status": "passed",
            "plan_state": {"plan_version": 4},
            "reconciliation": {
                "status": "passed",
                "applied": [
                    {
                        "session_id": "run-quality-01",
                        "version": 4,
                        "idempotent_replay": True,
                    }
                ],
            },
        }
        self.assertEqual("reconciliation: no change", self.statement(payload))

    def test_a_mixed_batch_counts_only_the_fresh_commits(self):
        """A retry after a partial failure: the first entry already landed, the second is new."""
        payload = {
            "status": "passed",
            "plan_state": {"plan_version": 4},
            "reconciliation": {
                "status": "passed",
                "applied": [
                    {
                        "session_id": "run-quality-01",
                        "version": 3,
                        "idempotent_replay": True,
                    },
                    {
                        "session_id": "run-long-01",
                        "version": 4,
                        "idempotent_replay": False,
                    },
                ],
            },
        }
        self.assertEqual(
            "reconciliation applied: version 3 -> 4", self.statement(payload)
        )

    def test_deferred_reconciliation_names_the_blocking_attempt(self):
        payload = {
            "status": "passed",
            "plan_state": {"plan_version": 4},
            "reconciliation": {
                "status": "deferred",
                "reason": "unresolved_delivery_attempt",
                "applied": [],
                "attempt_id": "delivery-attempt-abc123",
            },
        }
        statement = self.statement(payload)
        self.assertIn("deferred", statement)
        self.assertIn("delivery-attempt-abc123", statement)

    def test_no_plan_state_reads_as_not_applicable(self):
        payload = {"status": "no_plan_state", "plan_state": {"present": False}}
        self.assertEqual(
            "reconciliation: not applicable, no PlanState exists yet",
            self.statement(payload),
        )

    def test_a_reply_with_no_reconciliation_object_reads_as_unknown_not_no_change(self):
        """AGENTS.md invariant 3: an absent field is unknown, never coerced to zero."""
        payload = {"status": "blocked", "error": "invalid_request"}
        self.assertIn("unknown", self.statement(payload))

    def test_the_summary_carries_both_the_statement_and_the_raw_object(self):
        from garmin_coach_loop.cli import _hosted_session_summary

        payload = {
            "status": "passed",
            "plan_state": {"present": True, "plan_id": "p1", "plan_version": 4},
            "reconciliation": {
                "status": "passed",
                "applied": [
                    {
                        "session_id": "run-quality-01",
                        "version": 4,
                        "idempotent_replay": False,
                    }
                ],
            },
        }
        summary = _hosted_session_summary("https://coach.example", payload)
        self.assertEqual(
            "reconciliation applied: version 3 -> 4", summary["reconciliation_statement"]
        )
        self.assertEqual(payload["reconciliation"], summary["reconciliation"])

    def test_a_reply_without_a_plan_state_still_states_reconciliation(self):
        """The early-return branch of _hosted_session_summary must not skip it either."""
        from garmin_coach_loop.cli import _hosted_session_summary

        summary = _hosted_session_summary(
            "https://coach.example",
            {
                "status": "passed",
                "reconciliation": {"status": "passed", "applied": []},
            },
        )
        self.assertEqual("reconciliation: no change", summary["reconciliation_statement"])


class RefreshContextProviderReadTests(unittest.TestCase):
    """`refresh-context` reads the athlete's provider once, whether or not it reconciles.

    The command builds a context, writes back whatever reconciliation found, and rebuilds
    against the moved plan. The rebuild is answered from the snapshot the first build
    already read: reconciliation marks matched sessions completed and bumps the version,
    and neither reaches anything the provider read depends on.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "coach-state"
        plan = load("plan-state-v1.json")
        quality = next(
            session
            for session in plan["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        # Delivered, so the activity below can name it by the provider's own pairing --
        # which is what makes this refresh one that actually writes.
        quality["execution"] = {
            "publish_supported": True,
            "external_id": "ev-quality-01",
            "delivery_state": "intervals_accepted",
        }
        init_store(self.state_dir, plan)
        self.requested: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _fetch(self, request: Any) -> ProviderResponse:
        return ProviderResponse(self._fetch_body(request))

    def _fetch_body(self, request: Any) -> bytes:
        url = request.full_url
        self.requested.append(url)
        if "/activities?" in url:
            return json.dumps([
                {
                    "id": "i7001",
                    "type": "Run",
                    "start_date_local": "2026-08-13T07:00:00",
                    "moving_time": 3000,
                    "distance": 10000.0,
                    "average_speed": 3.33,
                    "average_heartrate": 160,
                    "paired_event_id": "ev-quality-01",
                }
            ]).encode("utf-8")
        if "/wellness?" in url:
            return json.dumps([]).encode("utf-8")
        if url.endswith("/sport-settings"):
            return json.dumps([{"types": ["Run"], "max_hr": 188}]).encode("utf-8")
        if "/streams?" in url:
            # Shorter than the drift reader's floor, so it reports nothing for this run.
            return json.dumps(fit_fixtures.STREAMS_TOO_SHORT_FOR_DRIFT).encode("utf-8")
        if url.endswith("/file"):
            # A session whose file carries no sets: not an error, and not a parse failure.
            return fit_fixtures.fit_file_without_sets()
        if url.endswith("/intervals"):
            return json.dumps({"icu_intervals": []}).encode("utf-8")
        raise AssertionError(f"unexpected intervals.icu URL in test: {url}")

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        credentials = IntervalsCredentials("synthetic-test-key-not-real", "i0")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "garmin_coach_loop.source_intervals.resolve_credentials",
            return_value=credentials,
        ), mock.patch(
            "garmin_coach_loop.source_intervals._default_fetch", new=self._fetch
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_a_refresh_that_reconciles_reads_each_endpoint_once(self):
        code, report = self.run_cli(
            "refresh-context",
            "--state-dir", str(self.state_dir),
            "--as-of", "2026-08-13T20:00:00+08:00",
        )

        self.assertEqual(0, code, report)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(
            ["run-quality-01"],
            [entry["session_id"] for entry in report["reconciliation"]["applied"]],
        )
        # The rebuilt context is the moved plan's, not the one the first build saw.
        self.assertEqual(2, report["context"]["goal_context"]["plan_version"])
        # Activities, wellness, the one structured day's segments, that same run's
        # per-sample series for its two ends, and the Run sport settings this plan's own
        # max HR gives something to disagree with -- each once.
        #
        # The streams read is the one this fixture's single run adds. It is per run and
        # capped at six, so a fortnight of daily running costs six of these and never a
        # seventh; a strength session in the window would add its uploaded file on the
        # same terms. Both are named here rather than counted loosely, because a read
        # that quietly doubles is invisible in every response it feeds.
        self.assertEqual(5, len(self.requested), self.requested)
        self.assertEqual(
            1, len([url for url in self.requested if "/streams?" in url])
        )
        self.assertEqual(
            1, len([url for url in self.requested if "/activities?" in url])
        )
        self.assertEqual(
            1, len([url for url in self.requested if url.endswith("/sport-settings")])
        )


class MigrationCommandTests(unittest.TestCase):
    """export -> import -> seal, as an operator runs it, including the refusals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.source = base / "local-store"
        self.bundle = base / "bundle.json"
        self.state_root = base / "gateway-root"
        self.plan = load("plan-state-v1.json")
        init_store(self.source, self.plan)
        self.identity_db = identity_db_path(self.state_root)
        self.owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def owner_dir(self) -> Path:
        return resolve_state_dir(self.owner_id, state_root=self.state_root)

    def export(self) -> dict[str, Any]:
        code, report = self.run_cli(
            "export-store", "--state-dir", str(self.source), "--out", str(self.bundle)
        )
        self.assertEqual(0, code, report)
        return report

    def test_export_import_and_seal_move_one_plan_to_the_owner_store(self):
        exported = self.export()
        self.assertEqual(self.plan["plan_id"], exported["plan_id"])
        self.assertEqual(0o600, os.stat(self.bundle).st_mode & 0o777)

        code, preview = self.run_cli(
            "import-store",
            "--bundle", str(self.bundle),
            "--athlete-id", "i1",
            "--state-root", str(self.state_root),
        )
        self.assertEqual(0, code, preview)
        self.assertEqual("preview", preview["status"])
        self.assertEqual(str(self.owner_dir()), preview["destination"])
        self.assertFalse(self.owner_dir().exists())

        code, imported = self.run_cli(
            "import-store",
            "--bundle", str(self.bundle),
            "--athlete-id", "i1",
            "--state-root", str(self.state_root),
            "--confirm",
        )
        self.assertEqual(0, code, imported)
        self.assertEqual(exported["bundle_digest"], imported["bundle_digest"])

        code, opened = self.run_cli("doctor-store", "--state-dir", str(self.owner_dir()))
        self.assertEqual(0, code, opened)
        self.assertEqual(self.plan["plan_id"], opened["plan_id"])

        code, sealed = self.run_cli(
            "seal-local-store",
            "--state-dir", str(self.source),
            "--hosted-entry", "https://coach.example",
            "--confirm",
        )
        self.assertEqual(0, code, sealed)
        code, blocked = self.run_cli(
            "record-profile", "--state-dir", str(self.source), "--timezone", "UTC",
            "--offline",
        )
        self.assertEqual(2, code)
        self.assertIn("handed off", blocked["error"])

    def test_an_athlete_who_never_signed_in_has_no_destination(self):
        self.export()
        code, report = self.run_cli(
            "import-store",
            "--bundle", str(self.bundle),
            "--athlete-id", "someone-else",
            "--state-root", str(self.state_root),
            "--confirm",
        )
        self.assertEqual(2, code)
        self.assertIn("no owner has connected", report["error"])

    def test_naming_both_a_directory_and_an_athlete_is_refused(self):
        self.export()
        code, report = self.run_cli(
            "import-store",
            "--bundle", str(self.bundle),
            "--athlete-id", "i1",
            "--state-dir", str(self.owner_dir()),
            "--state-root", str(self.state_root),
        )
        self.assertEqual(2, code)
        self.assertIn("exactly one", report["error"])

    def test_a_bundle_is_never_written_into_the_repository(self):
        code, report = self.run_cli(
            "export-store",
            "--state-dir", str(self.source),
            "--out", str(ROOT / "bundle.json"),
        )
        self.assertEqual(2, code)
        self.assertIn("outside the repository", report["error"])
        self.assertFalse((ROOT / "bundle.json").exists())

    def test_sealing_needs_somewhere_to_say_the_plan_went(self):
        code, report = self.run_cli(
            "seal-local-store", "--state-dir", str(self.source), "--confirm"
        )
        self.assertEqual(2, code)
        self.assertIn("hosted entry", report["error"])


class ExportBundlePrivateWriteTests(unittest.TestCase):
    """export-store carries the athlete's whole plan history in one file, so its write
    path is held to a stricter standard than an ordinary CLI output: private before the
    first byte regardless of umask, atomic, and closed to both an existing destination and
    a symlink one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.source = base / "local-store"
        self.bundle = base / "bundle.json"
        self.state_root = base / "gateway-root"
        init_store(self.source, load("plan-state-v1.json"))
        # Every test in this class runs under a permissive umask: the whole point of the
        # write path under test is that it must not matter.
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(arguments))
        return code, json.loads(out.getvalue() or err.getvalue())

    def export(self) -> tuple[int, dict[str, Any]]:
        return self.run_cli(
            "export-store", "--state-dir", str(self.source), "--out", str(self.bundle)
        )

    def test_the_temporary_file_is_never_observable_as_more_than_0600(self):
        """Not just the final mode: the mode at the moment `os.open` creates the file,
        which is the instant that matters and the one a chmod-after-write cannot cover."""
        observed_modes = []
        real_open = os.open

        def spying_open(path, flags, mode=0o777, **kwargs):
            descriptor = real_open(path, flags, mode, **kwargs)
            if flags & os.O_CREAT and Path(path).parent == self.bundle.parent:
                observed_modes.append(os.stat(descriptor).st_mode & 0o777)
            return descriptor

        with mock.patch("os.open", side_effect=spying_open):
            code, report = self.export()

        self.assertEqual(0, code, report)
        self.assertTrue(observed_modes, "expected a file created next to --out")
        self.assertTrue(all(mode == 0o600 for mode in observed_modes), observed_modes)
        self.assertEqual(0o600, os.stat(self.bundle).st_mode & 0o777)

    def test_a_successful_export_is_0600_and_imports_back_with_a_matching_digest(self):
        code, exported = self.export()
        self.assertEqual(0, code, exported)
        self.assertEqual(0o600, os.stat(self.bundle).st_mode & 0o777)

        identity_db = identity_db_path(self.state_root)
        lookup_or_create_owner(identity_db, "intervals", "i1")
        code, imported = self.run_cli(
            "import-store",
            "--bundle", str(self.bundle),
            "--athlete-id", "i1",
            "--state-root", str(self.state_root),
            "--confirm",
        )
        self.assertEqual(0, code, imported)
        self.assertEqual(exported["bundle_digest"], imported["bundle_digest"])

    def test_an_existing_destination_is_refused_without_touching_it(self):
        self.bundle.write_text("previous export, not to be touched\n", encoding="utf-8")
        before_bytes = self.bundle.read_bytes()
        before_mode = self.bundle.stat().st_mode & 0o777
        before_listing = sorted(p.name for p in self.bundle.parent.iterdir())

        code, report = self.export()

        self.assertEqual(2, code)
        self.assertIn("refusing to overwrite", report["error"])
        self.assertEqual(before_bytes, self.bundle.read_bytes())
        self.assertEqual(before_mode, self.bundle.stat().st_mode & 0o777)
        self.assertEqual(before_listing, sorted(p.name for p in self.bundle.parent.iterdir()))

    def test_a_symlink_destination_is_refused_without_following_it(self):
        # Dangling on purpose: a symlink whose target does not (yet) exist is the case a
        # plain existence check misses, since following it reports "nothing here".
        real_target = self.bundle.parent / "elsewhere.json"
        link = self.bundle.parent / "bundle-link.json"
        link.symlink_to(real_target)

        code, report = self.run_cli(
            "export-store", "--state-dir", str(self.source), "--out", str(link)
        )

        self.assertEqual(2, code)
        self.assertIn("symlink", report["error"])
        self.assertTrue(link.is_symlink())
        self.assertFalse(real_target.exists())

    def test_an_interrupted_serialization_leaves_no_temporary_or_final_file(self):
        before_listing = sorted(p.name for p in self.bundle.parent.iterdir())
        real_dumps = json.dumps

        def failing_dumps(*args, **kwargs):
            # Only the bundle's own pretty-printed serialization uses `indent`; the
            # digest hashing inside `export_bundle` and the CLI's own error reporting do
            # not, so this leaves both of those alone.
            if kwargs.get("indent") is not None:
                raise ValueError("simulated serialization failure")
            return real_dumps(*args, **kwargs)

        with mock.patch("json.dumps", side_effect=failing_dumps):
            code, report = self.export()

        self.assertEqual(2, code)
        self.assertIn("simulated serialization failure", report["error"])
        self.assertFalse(self.bundle.exists())
        self.assertEqual(before_listing, sorted(p.name for p in self.bundle.parent.iterdir()))

    def test_an_interrupted_install_leaves_no_temporary_or_final_file(self):
        before_listing = sorted(p.name for p in self.bundle.parent.iterdir())

        def failing_replace(src, dst):
            raise OSError("simulated install failure")

        with mock.patch("os.replace", side_effect=failing_replace):
            code, report = self.export()

        self.assertEqual(2, code)
        self.assertIn("simulated install failure", report["error"])
        self.assertFalse(self.bundle.exists())
        self.assertEqual(before_listing, sorted(p.name for p in self.bundle.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
