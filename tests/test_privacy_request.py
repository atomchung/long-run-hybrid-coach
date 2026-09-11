"""An export or deletion request that arrives by email, walked end to end.

The in-conversation route has its own suite (``test_owner_lifecycle.py``), and this one
exists because that route could not be verified in every client for 1.4 (issue #417). What
is under test here is the operator-run path that replaces the promise: the check that the
requester controls the account, the archive they then receive, the scope they confirm, and
the erasure bound to it.

Every releasing path is written twice. The verified athlete gets their data; the
impersonator gets a refusal and leaves the account exactly as it was. A suite made only of
the second half would pass with a route that refuses everybody, which is not a data
request route at all.

The second account is not decoration: it is the thing an over-broad deletion or a
confused verification would fall into, and it is asserted unchanged after every operation.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
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


def _utc(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.timezone.utc)


class PrivacyRequestTestCase(unittest.TestCase):
    """Two synthetic accounts, both real stores, neither belonging to anybody.

    Windows are built around the wall clock rather than a frozen one, because the instant
    under test is written by ``record_token_fingerprint`` itself. Recording first and
    closing the window afterwards is what a real request does, in the same order.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name) / "coach-state"
        self.state_root.mkdir(parents=True)
        self.identity_db = self.state_root / "identity.db"

        self.requester = self._seed(REQUESTER_ATHLETE)
        self.bystander = self._seed(BYSTANDER_ATHLETE)
        self.bystander_fingerprints = self._snapshot(self.bystander)

    # -- fixtures ---------------------------------------------------------------------

    def _seed(self, athlete_id: str) -> str:
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", athlete_id)
        record_token_fingerprint(
            self.identity_db, f"fp-first-{athlete_id}", owner_id, "intervals"
        )
        # A seeded account connected when it was created, not a moment ago. Backdated so
        # the windows below are the ones a real request uses -- a fixture whose only
        # authorization sits inside every window would verify every request and prove
        # nothing.
        self._backdate(owner_id, days=30)
        state_dir = resolve_state_dir(owner_id, state_root=self.state_root)
        init_store(state_dir, publishable_plan())
        athlete_evidence.record_profile(
            state_dir, timezone="Asia/Taipei", language="zh-Hant"
        )
        athlete_evidence.record_body_measurement(
            state_dir, weight_kg=72.5, timezone_name="Asia/Taipei"
        )
        return owner_id

    def _backdate(self, owner_id: str, *, days: int) -> None:
        stamp = (
            _utc(dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)
        ).isoformat().replace("+00:00", "Z")
        # `closing` as well as the transaction: sqlite3's own context manager commits
        # and leaves the handle open, and the ResourceWarning that follows is printed on
        # stderr -- which is where these tests read a command's JSON from.
        with contextlib.closing(sqlite3.connect(self.identity_db)) as connection:
            with connection:
                connection.execute(
                    "UPDATE token_fingerprints SET created_at = ? WHERE owner_id = ?",
                    (stamp, owner_id),
                )

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
        self.assertEqual(self._snapshot(self.bystander), self.bystander_fingerprints)

    def rebaseline_bystander(self) -> None:
        """After the bystander legitimately reconnects, their own new row is expected.

        Called only where the test itself made that happen. Everything the requester's
        request could have moved is still compared against the baseline it takes here.
        """
        self.bystander_fingerprints = self._snapshot(self.bystander)

    def reauthorize(self, owner_id: str, *, label: str) -> None:
        """What the athlete does in their AI client: consent again, get a new token."""
        record_token_fingerprint(self.identity_db, f"fp-{label}", owner_id, "intervals")

    def window(self, *, span_minutes: int = 30) -> dict[str, dt.datetime]:
        """A window that closed a moment ago, around whatever was just recorded."""
        until = _utc(dt.datetime.now(dt.timezone.utc))
        return {
            "since": until - dt.timedelta(minutes=span_minutes),
            "until": until,
            "now": until + dt.timedelta(seconds=1),
        }

    # -- helpers that mirror the three operator commands ------------------------------

    def verify(self, athlete_id: str, **window: Any) -> dict[str, Any]:
        return privacy_request.verify_account_control(
            self.identity_db, athlete_id=athlete_id, **window
        )

    def export(self, athlete_id: str, **window: Any) -> dict[str, Any]:
        return privacy_request.export_request(
            self.state_root,
            self.identity_db,
            athlete_id=athlete_id,
            hmac_key=HMAC_KEY,
            **window,
        )

    def scope(self, athlete_id: str, **window: Any) -> dict[str, Any]:
        return privacy_request.deletion_scope(
            self.state_root, self.identity_db, athlete_id=athlete_id, **window
        )

    def delete(
        self, athlete_id: str, *, scope_digest: str, confirmed: bool = True, **window: Any
    ) -> dict[str, Any]:
        return privacy_request.apply_deletion(
            self.state_root,
            self.identity_db,
            athlete_id=athlete_id,
            hmac_key=HMAC_KEY,
            scope_digest=scope_digest,
            confirmed=confirmed,
            **window,
        )


class OpeningARequest(PrivacyRequestTestCase):
    """The first reply, which goes out before anything about the account is known."""

    def test_reply_is_identical_whether_or_not_the_account_exists(self):
        known = privacy_request.open_request(
            self.identity_db,
            athlete_id=REQUESTER_ATHLETE,
            now=dt.datetime.now(dt.timezone.utc),
        )
        unknown = privacy_request.open_request(
            self.identity_db,
            athlete_id="i-nobody-here",
            now=dt.datetime.now(dt.timezone.utc),
        )
        self.assertEqual(known["reply"], unknown["reply"])
        self.assertTrue(known["operator_only"]["account_exists"])
        self.assertFalse(unknown["operator_only"]["account_exists"])
        self.assertIsNone(unknown["operator_only"]["owner_id"])

    def test_the_reply_asks_for_nothing_secret(self):
        opened = privacy_request.open_request(
            self.identity_db,
            athlete_id=REQUESTER_ATHLETE,
            now=dt.datetime.now(dt.timezone.utc),
        )
        reply = opened["reply"].lower()
        self.assertIn("re-authorize", reply)
        # Named in the reply only as things that will never be read, which is the
        # opposite of asking for them -- so the assertion is on the sentence, not the word.
        self.assertIn("will not be read", reply)
        self.assertNotIn("owner id", reply)

    def test_the_window_it_names_excludes_the_connection_already_recorded(self):
        """The bound the operator copies has to leave the standing connection outside.

        Timestamps in the fulfilment commands are second-precision, so a bound truncated
        to the second the request was opened in would include an authorization recorded
        moments earlier -- and "this account has a connection" is true of every account.
        """
        # Not backdated: an authorization from this very second, the hardest case.
        self.reauthorize(self.requester, label="just-before-the-request")
        opened = privacy_request.open_request(
            self.identity_db,
            athlete_id=REQUESTER_ATHLETE,
            now=dt.datetime.now(dt.timezone.utc),
        )
        since = privacy_request.parse_instant(
            opened["authorize_after"], field="authorize_after"
        )
        with self.assertRaises(PrivacyRequestError):
            self.verify(
                REQUESTER_ATHLETE,
                since=since,
                until=since + dt.timedelta(minutes=10),
                now=since + dt.timedelta(minutes=11),
            )

    def test_opening_a_request_reads_no_store(self):
        before = self._snapshot(self.requester)
        privacy_request.open_request(
            self.identity_db,
            athlete_id=REQUESTER_ATHLETE,
            now=dt.datetime.now(dt.timezone.utc),
        )
        self.assertEqual(self._snapshot(self.requester), before)


class VerifyingControlOfTheAccount(PrivacyRequestTestCase):
    """The one check that separates the athlete from somebody who knows their id."""

    def test_a_fresh_authorization_inside_the_window_verifies(self):
        self.reauthorize(self.requester, label="reconnect")
        verified = self.verify(REQUESTER_ATHLETE, **self.window())
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["athlete_id"], REQUESTER_ATHLETE)
        self.assertEqual(verified["authorizations_in_window"], 1)
        # The evidence an operator quotes back must not carry the internal storage name.
        self.assertNotIn("owner_id", verified)

    def test_the_connection_they_already_had_does_not_verify(self):
        """A route that counted the standing connection would verify every account.

        This account is connected -- it was connected a month ago, and the athlete has
        done nothing since. Naming its id is still not a reason to release anything.
        """
        with self.assertRaises(PrivacyRequestError) as caught:
            self.verify(REQUESTER_ATHLETE, **self.window())
        self.assertIn("no authorization", str(caught.exception))

    def test_somebody_elses_authorization_does_not_verify_this_account(self):
        self.reauthorize(self.bystander, label="bystander-reconnect")
        with self.assertRaises(PrivacyRequestError):
            self.verify(REQUESTER_ATHLETE, **self.window())

    def test_an_unknown_athlete_id_refuses_in_the_same_words(self):
        """The refusal must not be a membership oracle.

        "No such account" and "you did not authorize" have to read identically, or an
        operator pasting the refusal into a reply tells a stranger which athlete ids are
        registered here.
        """
        window = self.window()
        with self.assertRaises(PrivacyRequestError) as unknown:
            self.verify("i-nobody-here", **window)
        with self.assertRaises(PrivacyRequestError) as unverified:
            self.verify(REQUESTER_ATHLETE, **window)
        self.assertEqual(str(unknown.exception), str(unverified.exception))

    def test_a_window_wider_than_a_day_is_refused(self):
        self.reauthorize(self.requester, label="reconnect")
        window = self.window()
        window["since"] = window["until"] - dt.timedelta(hours=25)
        with self.assertRaises(PrivacyRequestError) as caught:
            self.verify(REQUESTER_ATHLETE, **window)
        self.assertIn("verification window spans", str(caught.exception))

    def test_a_window_still_open_is_refused(self):
        self.reauthorize(self.requester, label="reconnect")
        window = self.window()
        window["now"] = window["until"] - dt.timedelta(minutes=1)
        with self.assertRaises(PrivacyRequestError) as caught:
            self.verify(REQUESTER_ATHLETE, **window)
        self.assertIn("still open", str(caught.exception))

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        window = self.window()
        window["since"], window["until"] = window["until"], window["since"]
        with self.assertRaises(PrivacyRequestError):
            self.verify(REQUESTER_ATHLETE, **window)

    def test_a_bound_without_a_timezone_is_refused(self):
        with self.assertRaises(PrivacyRequestError) as caught:
            privacy_request.parse_instant("2026-09-11T14:00:00", field="--authorized-after")
        self.assertIn("timezone", str(caught.exception))


class ServingAVerifiedExport(PrivacyRequestTestCase):
    """What the athlete receives, and what an unverified requester does not."""

    def test_the_archive_is_the_one_the_coach_would_have_handed_over(self):
        self.reauthorize(self.requester, label="reconnect")
        served = self.export(REQUESTER_ATHLETE, **self.window())
        archive = served["archive"]
        expected = owner_data.export_archive(
            self._state_dir(self.requester),
            identity_db=self.identity_db,
            owner_id=self.requester,
            owner_reference=binding(self.requester, key=HMAC_KEY),
        )
        self.assertEqual(archive, expected)
        self.assertEqual(
            archive["owner_reference"], binding(self.requester, key=HMAC_KEY)
        )
        self.assertEqual(archive["excluded"], list(owner_data.EXCLUDED))
        self.assertIsNotNone(archive["plan_state"])
        self.assert_bystander_untouched()

    def test_the_archive_carries_no_owner_id_or_athlete_id(self):
        self.reauthorize(self.requester, label="reconnect")
        served = self.export(REQUESTER_ATHLETE, **self.window())
        rendered = json.dumps(served["archive"], ensure_ascii=False)
        self.assertNotIn(self.requester, rendered)
        self.assertNotIn(REQUESTER_ATHLETE, rendered)

    def test_an_unverified_request_exports_nothing(self):
        with self.assertRaises(PrivacyRequestError):
            self.export(REQUESTER_ATHLETE, **self.window())
        self.assert_bystander_untouched()

    def test_naming_another_athletes_id_exports_nothing(self):
        """The impersonation case, in the shape it actually arrives in.

        The bystander reconnects for their own reasons and then claims the requester's
        athlete id. Their authorization is real; it is evidence about their account and
        about no other.
        """
        self.reauthorize(self.bystander, label="bystander-reconnect")
        with self.assertRaises(PrivacyRequestError):
            self.export(REQUESTER_ATHLETE, **self.window())
        self.assertEqual(self._snapshot(self.requester)["rows"]["owners"], 1)


class DeletingAgainstAConfirmedScope(PrivacyRequestTestCase):
    """The erasure, and the three ways it refuses instead."""

    def verified_scope(self) -> tuple[dict[str, Any], dict[str, Any]]:
        self.reauthorize(self.requester, label="reconnect")
        window = self.window()
        return window, self.scope(REQUESTER_ATHLETE, **window)

    def test_a_confirmed_scope_erases_the_account_and_says_so(self):
        window, scope = self.verified_scope()
        self.assertEqual(scope["scope_digest"], canonical_hash(
            {key: scope[key] for key in ("removes", "not_removed", "reversible")}
        ))
        self.assertFalse(scope["reversible"])
        self.assertEqual(scope["not_removed"], list(owner_data.NOT_REMOVED))

        erased = self.delete(
            REQUESTER_ATHLETE, scope_digest=scope["scope_digest"], **window
        )
        self.assertTrue(erased["deleted"])
        self.assertTrue(erased["receipt_id"].startswith("gcd-"))

        checked = erased["verified_after_deletion"]
        self.assertTrue(checked["state_directory_absent"])
        self.assertTrue(checked["deletion_tombstone"])
        self.assertEqual(
            checked["identity_rows_remaining"],
            {key: 0 for key in checked["identity_rows_remaining"]},
        )
        self.assertEqual(checked["accounts_before"], 2)
        self.assertEqual(checked["accounts_after"], 1)
        self.assertTrue(checked["other_accounts_unchanged"])

        fence = read_maintenance_fence(self._state_dir(self.requester))
        self.assertTrue(fence and fence["tombstone"])
        self.assert_bystander_untouched()

    def test_the_receipt_carries_nothing_that_was_deleted(self):
        window, scope = self.verified_scope()
        erased = self.delete(
            REQUESTER_ATHLETE, scope_digest=scope["scope_digest"], **window
        )
        rendered = json.dumps(erased, ensure_ascii=False)
        self.assertNotIn(self.requester, rendered)
        self.assertNotIn("72.5", rendered)

    def test_a_scope_the_requester_never_saw_is_refused(self):
        window, scope = self.verified_scope()
        # Between preview and confirmation the athlete weighs in for another day. One
        # more record goes, so the scope moved and the confirmation is about a preview
        # nobody read.
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
            self.delete(REQUESTER_ATHLETE, scope_digest=scope["scope_digest"], **window)
        self.assertIn("changed after the scope", str(caught.exception))
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())
        self.assert_bystander_untouched()

    def test_a_deletion_without_a_confirmation_is_refused(self):
        window, scope = self.verified_scope()
        with self.assertRaises(PrivacyRequestError):
            self.delete(
                REQUESTER_ATHLETE,
                scope_digest=scope["scope_digest"],
                confirmed=False,
                **window,
            )
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())

    def test_a_deletion_without_a_scope_digest_is_refused(self):
        window, _ = self.verified_scope()
        with self.assertRaises(PrivacyRequestError):
            self.delete(REQUESTER_ATHLETE, scope_digest="", **window)
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())

    def test_an_unverified_request_deletes_nothing(self):
        window = self.window()
        with self.assertRaises(PrivacyRequestError):
            self.scope(REQUESTER_ATHLETE, **window)
        with self.assertRaises(PrivacyRequestError):
            self.delete(REQUESTER_ATHLETE, scope_digest="anything", **window)
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())
        self.assert_bystander_untouched()

    def test_an_impersonators_own_authorization_deletes_nothing(self):
        """The one that would be a disaster: a deletion triggered by a stranger.

        The bystander reconnects, then names the requester's athlete id and asks for a
        deletion. Nothing about the requester's account moves, and the bystander's own
        account is not deleted either -- the request named somebody else.
        """
        self.reauthorize(self.bystander, label="bystander-reconnect")
        self.rebaseline_bystander()
        window = self.window()
        with self.assertRaises(PrivacyRequestError):
            self.scope(REQUESTER_ATHLETE, **window)
        with self.assertRaises(PrivacyRequestError):
            self.delete(REQUESTER_ATHLETE, scope_digest="anything", **window)
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())
        self.assert_bystander_untouched()


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
        self.assertIn("re-authorize", opened["reply"].lower())

        self.reauthorize(self.requester, label="reconnect")
        window = self.window()
        bounds = [
            "--authorized-after", window["since"].isoformat().replace("+00:00", "Z"),
            "--authorized-before", window["until"].isoformat().replace("+00:00", "Z"),
        ]

        archive_path = Path(self._tmp.name) / "archive.json"
        code, exported = self.run_cli(
            "privacy-request-export",
            "--athlete-id", REQUESTER_ATHLETE,
            *bounds,
            "--out", str(archive_path),
        )
        self.assertEqual(code, 0)
        self.assertEqual(exported["status"], "passed")
        self.assertTrue(archive_path.is_file())
        # The athlete's whole history in one file: never group- or world-readable.
        self.assertEqual(archive_path.stat().st_mode & 0o777, 0o600)
        written = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertEqual(
            written["owner_reference"], binding(self.requester, key=HMAC_KEY)
        )

        code, scope = self.run_cli(
            "privacy-request-delete", "--athlete-id", REQUESTER_ATHLETE, *bounds
        )
        self.assertEqual(code, 0)
        self.assertEqual(scope["status"], "preview")
        self.assertTrue((self._state_dir(self.requester) / "store.json").is_file())

        code, erased = self.run_cli(
            "privacy-request-delete",
            "--athlete-id", REQUESTER_ATHLETE,
            *bounds,
            "--scope-digest", scope["scope_digest"],
            "--confirm",
        )
        self.assertEqual(code, 0)
        self.assertEqual(erased["status"], "deleted")
        self.assertTrue(erased["verified_after_deletion"]["other_accounts_unchanged"])
        self.assert_bystander_untouched()

    def test_an_unverified_request_exits_blocked_and_writes_no_file(self):
        window = self.window()
        archive_path = Path(self._tmp.name) / "never-written.json"
        code, blocked = self.run_cli(
            "privacy-request-export",
            "--athlete-id", REQUESTER_ATHLETE,
            "--authorized-after", window["since"].isoformat().replace("+00:00", "Z"),
            "--authorized-before", window["until"].isoformat().replace("+00:00", "Z"),
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
