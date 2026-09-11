"""An export or deletion request that arrives by email, walked end to end.

The in-conversation route has its own suite (``test_owner_lifecycle.py``), and this one
exists because that route could not be verified in every client for 1.4 (issue #417). What
is under test here is the operator-run path that replaces the promise: the archive the
requester receives, the scope they confirm, the erasure bound to it, and the record of
what the operator looked at first.

**What is deliberately not under test is the identity check.** By the owner's decision on
2026-09-11 it is a person's judgement -- a screenshot of the requester's Intervals.icu
Settings page, or the athlete id alone when they cannot reach it -- so there is nothing
here that could refuse a request for naming the wrong athlete, and a test asserting
otherwise would be asserting a gate the product does not have. What the tests do hold is
that the basis is recorded and travels into the receipt.

The second account is not decoration: it is the thing an over-broad deletion would fall
into, and it is asserted unchanged after every operation.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import athlete_evidence, cli, owner_data, privacy_request
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_identity_row_counts,
    record_token_fingerprint,
)
from garmin_coach_loop.privacy_request import PrivacyRequestError
from garmin_coach_loop.proposals import binding
from garmin_coach_loop.store import (
    canonical_hash,
    init_store,
    read_maintenance_fence,
    resolve_state_dir,
)

from test_gateway import publishable_plan


HMAC_KEY = b"privacy-request-unit-test-key-0001"
REQUESTER_ATHLETE = "i-requester"
BYSTANDER_ATHLETE = "i-bystander"
SCREENSHOT = "settings-screenshot"
ID_ONLY = "athlete-id-only"


class PrivacyRequestTestCase(unittest.TestCase):
    """Two synthetic accounts, both real stores, neither belonging to anybody."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name) / "coach-state"
        self.state_root.mkdir(parents=True)
        self.identity_db = self.state_root / "identity.db"

        self.requester = self._seed(REQUESTER_ATHLETE)
        self.bystander = self._seed(BYSTANDER_ATHLETE)
        self.bystander_baseline = self._snapshot(self.bystander)

    # -- fixtures ---------------------------------------------------------------------

    def _seed(self, athlete_id: str) -> str:
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", athlete_id)
        record_token_fingerprint(
            self.identity_db, f"fp-{athlete_id}", owner_id, "intervals"
        )
        state_dir = resolve_state_dir(owner_id, state_root=self.state_root)
        init_store(state_dir, publishable_plan())
        athlete_evidence.record_profile(
            state_dir, timezone="Asia/Taipei", language="zh-Hant"
        )
        athlete_evidence.record_body_measurement(
            state_dir, weight_kg=72.5, timezone_name="Asia/Taipei"
        )
        return owner_id

    def _state_dir(self, owner_id: str) -> Path:
        return resolve_state_dir(owner_id, state_root=self.state_root)

    def _snapshot(self, owner_id: str) -> dict[str, Any]:
        """Everything about one account that a wrong deletion or export would move."""
        state_dir = self._state_dir(owner_id)
        return {
            "rows": owner_identity_row_counts(self.identity_db, owner_id),
            "files": sorted(
                str(path.relative_to(state_dir))
                for path in state_dir.rglob("*")
                if path.is_file()
            ),
            "store": (state_dir / "store.json").read_text(encoding="utf-8"),
        }

    def assert_bystander_untouched(self) -> None:
        self.assertEqual(self._snapshot(self.bystander), self.bystander_baseline)

    # -- helpers that mirror the three operator commands ------------------------------

    def export(self, athlete_id: str, evidence: str = SCREENSHOT) -> dict[str, Any]:
        return privacy_request.export_request(
            self.state_root,
            self.identity_db,
            athlete_id=athlete_id,
            identity_evidence=evidence,
            hmac_key=HMAC_KEY,
        )

    def scope(self, athlete_id: str, evidence: str = SCREENSHOT) -> dict[str, Any]:
        return privacy_request.deletion_scope(
            self.state_root,
            self.identity_db,
            athlete_id=athlete_id,
            identity_evidence=evidence,
        )

    def delete(
        self,
        athlete_id: str,
        *,
        scope_digest: str,
        evidence: str = SCREENSHOT,
        confirmed: bool = True,
    ) -> dict[str, Any]:
        return privacy_request.apply_deletion(
            self.state_root,
            self.identity_db,
            athlete_id=athlete_id,
            identity_evidence=evidence,
            now=dt.datetime.now(dt.timezone.utc),
            hmac_key=HMAC_KEY,
            scope_digest=scope_digest,
            confirmed=confirmed,
        )


class OpeningARequest(PrivacyRequestTestCase):
    """The first reply, which goes out before anybody has looked at anything."""

    def open(self, athlete_id: str) -> dict[str, Any]:
        return privacy_request.open_request(
            self.identity_db,
            athlete_id=athlete_id,
            now=dt.datetime.now(dt.timezone.utc),
        )

    def test_the_reply_asks_for_the_screenshot_and_nothing_secret(self):
        reply = self.open(REQUESTER_ATHLETE)["reply"]
        self.assertIn("screenshot", reply.lower())
        self.assertIn("Settings", reply)
        # The three are named only as things that will never be read, which is the
        # opposite of asking for them -- so the assertion is on the sentence.
        self.assertIn("will not be read", reply)

    def test_the_reply_is_identical_whether_or_not_the_account_exists(self):
        known = self.open(REQUESTER_ATHLETE)
        unknown = self.open("i-nobody-here")
        self.assertEqual(known["reply"], unknown["reply"])
        self.assertTrue(known["operator_only"]["account_exists"])
        self.assertFalse(unknown["operator_only"]["account_exists"])
        self.assertIsNone(unknown["operator_only"]["owner_id"])

    def test_opening_a_request_reads_no_store(self):
        before = self._snapshot(self.requester)
        self.open(REQUESTER_ATHLETE)
        self.assertEqual(self._snapshot(self.requester), before)


class RecordingWhatWasChecked(PrivacyRequestTestCase):
    """The identity check refuses nothing; what it must do is leave a record."""

    def test_both_accepted_answers_serve_the_request(self):
        for evidence in (SCREENSHOT, ID_ONLY):
            with self.subTest(evidence=evidence):
                served = self.export(REQUESTER_ATHLETE, evidence)
                self.assertEqual(served["identity"]["evidence"], evidence)
                self.assertEqual(
                    served["identity"]["checked"],
                    privacy_request.IDENTITY_EVIDENCE[evidence],
                )

    def test_an_unrecognised_answer_is_refused_before_anything_is_read(self):
        for evidence in ("", "trust me", "screenshot"):
            with self.subTest(evidence=evidence):
                with self.assertRaises(PrivacyRequestError) as caught:
                    self.export(REQUESTER_ATHLETE, evidence)
                self.assertIn("identity evidence must be one of", str(caught.exception))

    def test_an_athlete_id_that_never_connected_is_refused(self):
        with self.assertRaises(PrivacyRequestError) as caught:
            self.export("i-nobody-here")
        self.assertIn("nothing here to export or delete", str(caught.exception))


class ServingAnExport(PrivacyRequestTestCase):
    """What the athlete receives."""

    def test_the_archive_is_the_one_the_coach_would_have_handed_over(self):
        archive = self.export(REQUESTER_ATHLETE)["archive"]
        self.assertEqual(
            archive,
            owner_data.export_archive(
                self._state_dir(self.requester),
                identity_db=self.identity_db,
                owner_id=self.requester,
                owner_reference=binding(self.requester, key=HMAC_KEY),
            ),
        )
        self.assertEqual(archive["excluded"], list(owner_data.EXCLUDED))
        self.assertIsNotNone(archive["plan_state"])
        self.assert_bystander_untouched()

    def test_the_archive_carries_no_owner_id_or_athlete_id(self):
        rendered = json.dumps(self.export(REQUESTER_ATHLETE)["archive"], ensure_ascii=False)
        self.assertNotIn(self.requester, rendered)
        self.assertNotIn(REQUESTER_ATHLETE, rendered)

    def test_exporting_one_account_reads_no_other(self):
        self.export(REQUESTER_ATHLETE)
        self.assert_bystander_untouched()


class DeletingAgainstAConfirmedScope(PrivacyRequestTestCase):
    """The erasure, and the three ways it refuses instead."""

    def test_a_confirmed_scope_erases_the_account_and_says_so(self):
        scope = self.scope(REQUESTER_ATHLETE)
        self.assertEqual(
            scope["scope_digest"],
            canonical_hash(
                {key: scope[key] for key in ("removes", "not_removed", "reversible")}
            ),
        )
        self.assertFalse(scope["reversible"])
        self.assertEqual(scope["not_removed"], list(owner_data.NOT_REMOVED))

        erased = self.delete(REQUESTER_ATHLETE, scope_digest=scope["scope_digest"])
        self.assertTrue(erased["deleted"])
        self.assertTrue(erased["receipt_id"].startswith("gcd-"))
        self.assertEqual(erased["identity"]["evidence"], SCREENSHOT)

        checked = erased["verified_after_deletion"]
        self.assertTrue(checked["state_directory_absent"])
        self.assertTrue(checked["deletion_tombstone"])
        self.assertEqual(
            checked["identity_rows_remaining"],
            {key: 0 for key in checked["identity_rows_remaining"]},
        )
        self.assertEqual((checked["accounts_before"], checked["accounts_after"]), (2, 1))
        self.assertTrue(checked["other_accounts_unchanged"])

        fence = read_maintenance_fence(self._state_dir(self.requester))
        self.assertTrue(fence and fence["tombstone"])
        self.assert_bystander_untouched()

    def test_deletion_states_what_it_cannot_reach_at_intervals(self):
        """The scope is the conversation's: Intervals keeps what belongs to Intervals."""
        not_removed = " ".join(self.scope(REQUESTER_ATHLETE)["not_removed"])
        self.assertIn("Intervals.icu calendar", not_removed)
        self.assertIn("Intervals.icu authorization", not_removed)

    def test_the_receipt_carries_nothing_that_was_deleted(self):
        scope = self.scope(REQUESTER_ATHLETE)
        rendered = json.dumps(
            self.delete(REQUESTER_ATHLETE, scope_digest=scope["scope_digest"]),
            ensure_ascii=False,
        )
        self.assertNotIn(self.requester, rendered)
        self.assertNotIn("72.5", rendered)

    def test_a_scope_the_requester_never_saw_is_refused(self):
        scope = self.scope(REQUESTER_ATHLETE)
        # Between preview and confirmation the athlete weighs in for another day. One
        # more record goes, so the confirmation is about a preview nobody read.
        yesterday = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        ).date().isoformat()
        athlete_evidence.record_body_measurement(
            self._state_dir(self.requester),
            weight_kg=72.1,
            date=yesterday,
            timezone_name="Asia/Taipei",
        )
        with self.assertRaises(PrivacyRequestError) as caught:
            self.delete(REQUESTER_ATHLETE, scope_digest=scope["scope_digest"])
        self.assertIn("changed after the scope", str(caught.exception))
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())
        self.assert_bystander_untouched()

    def test_a_deletion_without_a_confirmation_is_refused(self):
        scope = self.scope(REQUESTER_ATHLETE)
        with self.assertRaises(PrivacyRequestError):
            self.delete(
                REQUESTER_ATHLETE, scope_digest=scope["scope_digest"], confirmed=False
            )
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())

    def test_a_deletion_without_a_scope_digest_is_refused(self):
        self.scope(REQUESTER_ATHLETE)
        with self.assertRaises(PrivacyRequestError):
            self.delete(REQUESTER_ATHLETE, scope_digest="")
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())

    def test_deleting_one_account_leaves_the_other_whole(self):
        scope = self.scope(REQUESTER_ATHLETE)
        self.delete(REQUESTER_ATHLETE, scope_digest=scope["scope_digest"])
        self.assert_bystander_untouched()
        self.assertTrue((self._state_dir(self.bystander) / "store.json").is_file())


class TheOperatorCommands(PrivacyRequestTestCase):
    """The same walkthrough through the entry an operator actually types."""

    def run_cli(self, *argv: str) -> tuple[int, dict[str, Any]]:
        printed: list[str] = []
        with mock.patch.dict(
            os.environ,
            {
                "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.state_root),
                "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode("utf-8"),
            },
        ), mock.patch.object(
            sys, "stdout", new=_Collector(printed)
        ), mock.patch.object(
            sys, "stderr", new=_Collector(printed)
        ):
            code = cli.main(list(argv))
        # The first complete JSON object, not the whole stream: a warning printed on the
        # same descriptor would otherwise be read as the command's answer.
        captured = "".join(printed)
        start = captured.find("{")
        self.assertNotEqual(start, -1, f"no JSON report; the command printed {captured!r}")
        report, _ = json.JSONDecoder().raw_decode(captured[start:])
        return code, report

    def test_the_whole_request_runs_from_the_command_line(self):
        code, opened = self.run_cli(
            "privacy-request-open", "--athlete-id", REQUESTER_ATHLETE
        )
        self.assertEqual(code, 0)
        self.assertEqual(opened["status"], "passed")
        self.assertIn("screenshot", opened["reply"].lower())

        archive_path = Path(self._tmp.name) / "archive.json"
        code, exported = self.run_cli(
            "privacy-request-export",
            "--athlete-id", REQUESTER_ATHLETE,
            "--identity-evidence", SCREENSHOT,
            "--out", str(archive_path),
        )
        self.assertEqual(code, 0)
        self.assertEqual(exported["status"], "passed")
        self.assertEqual(exported["identity"]["evidence"], SCREENSHOT)
        self.assertTrue(archive_path.is_file())
        # The athlete's whole history in one file: never group- or world-readable.
        self.assertEqual(archive_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(archive_path.read_text(encoding="utf-8"))["owner_reference"],
            binding(self.requester, key=HMAC_KEY),
        )

        code, scope = self.run_cli(
            "privacy-request-delete",
            "--athlete-id", REQUESTER_ATHLETE,
            "--identity-evidence", SCREENSHOT,
        )
        self.assertEqual(code, 0)
        self.assertEqual(scope["status"], "preview")
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())

        code, erased = self.run_cli(
            "privacy-request-delete",
            "--athlete-id", REQUESTER_ATHLETE,
            "--identity-evidence", SCREENSHOT,
            "--scope-digest", scope["scope_digest"],
            "--confirm",
        )
        self.assertEqual(code, 0)
        self.assertEqual(erased["status"], "deleted")
        self.assertTrue(erased["verified_after_deletion"]["other_accounts_unchanged"])
        self.assert_bystander_untouched()

    def test_the_athlete_id_only_answer_runs_the_same_way(self):
        archive_path = Path(self._tmp.name) / "id-only.json"
        code, exported = self.run_cli(
            "privacy-request-export",
            "--athlete-id", REQUESTER_ATHLETE,
            "--identity-evidence", ID_ONLY,
            "--out", str(archive_path),
        )
        self.assertEqual(code, 0)
        self.assertEqual(exported["identity"]["evidence"], ID_ONLY)
        self.assertTrue(archive_path.is_file())

    def test_an_unknown_athlete_id_exits_blocked_and_writes_no_file(self):
        archive_path = Path(self._tmp.name) / "never-written.json"
        code, blocked = self.run_cli(
            "privacy-request-export",
            "--athlete-id", "i-nobody-here",
            "--identity-evidence", SCREENSHOT,
            "--out", str(archive_path),
        )
        self.assertEqual(code, 2)
        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse(archive_path.exists())


class _Collector:
    """A stdout/stderr stand-in that keeps what a command printed."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, text: str) -> int:
        self._sink.append(text)
        return len(text)

    def flush(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
