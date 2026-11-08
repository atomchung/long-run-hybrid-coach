from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.identity import (
    IdentityError,
    claim_watch_pairing,
    delete_owner_identity,
    forget_watch_devices,
    lookup_or_create_owner,
    owner_for_watch_credential,
    owner_identity_row_counts,
    redeem_watch_pairing,
    start_watch_pairing,
    token_fingerprint,
    watch_device,
    _domain_fingerprint,
)

HMAC_KEY = b"unit-test-fingerprint-key-0000000"
SECRET = "s" * 40
OTHER_SECRET = "o" * 40
T0 = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


class WatchPairingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"
        self.owner = lookup_or_create_owner(self.db_path, "intervals", "i1")

    def tearDown(self):
        self._tmp.cleanup()

    def start(self, secret=SECRET, now=T0):
        return start_watch_pairing(self.db_path, secret, hmac_key=HMAC_KEY, now=now)

    def claim(self, code, owner=None, now=at(10), label="t***@gmail.com"):
        return claim_watch_pairing(
            self.db_path, code, owner or self.owner, hmac_key=HMAC_KEY, account_label=label, now=now
        )

    def redeem(self, code, secret=SECRET, now=at(20)):
        return redeem_watch_pairing(self.db_path, code, secret, hmac_key=HMAC_KEY, now=now)

    def rows(self, table):
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(f"SELECT * FROM {table}").fetchall()

    def test_start_claim_redeem_happy_path(self):
        code = self.start()
        self.assertEqual(8, len(code))
        self.assertEqual("pending", self.redeem(code).status)
        self.assertTrue(self.claim(code))
        result = self.redeem(code)
        self.assertEqual("issued", result.status)
        self.assertEqual("t***@gmail.com", result.account_label)
        self.assertTrue(result.credential)
        self.assertEqual(
            self.owner,
            owner_for_watch_credential(self.db_path, result.credential, hmac_key=HMAC_KEY, now=at(30)),
        )

    def test_nothing_raw_is_stored(self):
        code = self.start()
        self.claim(code)
        credential = self.redeem(code).credential
        stored = repr(self.rows("watch_pairings") + self.rows("watch_devices"))
        for raw in (code, SECRET, credential):
            self.assertNotIn(raw, stored)

    def test_wrong_secret_is_invalid_even_after_claim(self):
        code = self.start()
        self.assertEqual("invalid", self.redeem(code, OTHER_SECRET).status)
        self.claim(code)
        self.assertEqual("invalid", self.redeem(code, OTHER_SECRET).status)
        self.assertEqual("issued", self.redeem(code).status)

    def test_short_secret_is_refused_at_start_and_invalid_at_redeem(self):
        with self.assertRaises(IdentityError):
            self.start(secret="short")
        self.assertEqual("invalid", self.redeem("ABCD2345", "short").status)

    def test_expired_code_cannot_be_claimed_or_redeemed(self):
        code = self.start()
        self.assertFalse(self.claim(code, now=at(601)))
        self.assertEqual("invalid", self.redeem(code, now=at(601)).status)
        fresh = self.start(now=T0)
        self.assertTrue(self.claim(fresh, now=at(10)))
        self.assertEqual("invalid", self.redeem(fresh, now=at(601)).status)

    def test_double_claim_fails(self):
        code = self.start()
        other = lookup_or_create_owner(self.db_path, "intervals", "i2")
        self.assertTrue(self.claim(code))
        self.assertFalse(self.claim(code, owner=other))
        self.assertEqual(self.owner, self.rows("watch_pairings")[0][4])

    def test_code_is_normalized(self):
        code = self.start()
        messy = f"{code[:3].lower()}-{code[3:6].lower()} {code[6:].lower()}"
        self.assertTrue(self.claim(messy))
        self.assertEqual("issued", self.redeem(f" {code[:4]}-{code[4:]} ").status)

    def test_unknown_owner_and_unknown_code_fail(self):
        code = self.start()
        self.assertFalse(self.claim(code, owner="00000000-0000-0000-0000-000000000000"))
        self.assertFalse(self.claim("ZZZZZZZZ"))
        self.assertFalse(self.claim("short"))
        self.assertTrue(self.claim(code))

    def test_second_redeem_issues_nothing_new(self):
        code = self.start()
        self.claim(code)
        self.assertEqual("issued", self.redeem(code).status)
        self.assertEqual("invalid", self.redeem(code).status)
        self.assertEqual(1, len(self.rows("watch_devices")))
        # Said again by the account that holds it, the code is the same pairing; nobody
        # else can take it, and the watch is not issued a second credential either way.
        self.assertTrue(self.claim(code))
        other = lookup_or_create_owner(self.db_path, "intervals", "i2")
        self.assertFalse(self.claim(code, owner=other))
        self.assertEqual("invalid", self.redeem(code).status)
        self.assertEqual(1, len(self.rows("watch_devices")))

    def test_second_watch_replaces_the_first_credential(self):
        first_code = self.start()
        self.claim(first_code)
        first = self.redeem(first_code).credential
        second_code = self.start(OTHER_SECRET)
        self.claim(second_code)
        second = self.redeem(second_code, OTHER_SECRET).credential
        self.assertNotEqual(first, second)
        self.assertIsNone(owner_for_watch_credential(self.db_path, first, hmac_key=HMAC_KEY, now=at(30)))
        self.assertEqual(
            self.owner, owner_for_watch_credential(self.db_path, second, hmac_key=HMAC_KEY, now=at(30))
        )
        self.assertEqual(1, len(self.rows("watch_devices")))

    def test_last_seen_is_throttled(self):
        code = self.start()
        self.claim(code)
        credential = self.redeem(code, now=at(20)).credential

        def last_seen():
            return self.rows("watch_devices")[0][3]

        initial = last_seen()
        owner_for_watch_credential(self.db_path, credential, hmac_key=HMAC_KEY, now=at(26))
        self.assertEqual(initial, last_seen())
        owner_for_watch_credential(self.db_path, credential, hmac_key=HMAC_KEY, now=at(700))
        self.assertNotEqual(initial, last_seen())
        moved = last_seen()
        owner_for_watch_credential(self.db_path, credential, hmac_key=HMAC_KEY, now=at(706))
        self.assertEqual(moved, last_seen())

    def test_unknown_credential_and_missing_registry_resolve_to_none(self):
        self.assertIsNone(owner_for_watch_credential(self.db_path, "nope", hmac_key=HMAC_KEY))
        missing = Path(self._tmp.name) / "absent" / "identity.db"
        self.assertIsNone(owner_for_watch_credential(missing, "nope", hmac_key=HMAC_KEY))
        self.assertFalse(missing.exists())

    def test_fingerprint_domains_never_collide(self):
        value = "same-value-everywhere"
        prints = {
            _domain_fingerprint("code", value, hmac_key=HMAC_KEY),
            _domain_fingerprint("secret", value, hmac_key=HMAC_KEY),
            _domain_fingerprint("credential", value, hmac_key=HMAC_KEY),
            token_fingerprint(value, hmac_key=HMAC_KEY),
        }
        self.assertEqual(4, len(prints))

    def test_old_pairings_are_swept_on_start(self):
        old = self.start(now=T0)
        self.assertEqual(1, len(self.rows("watch_pairings")))
        self.start(OTHER_SECRET, now=T0 + dt.timedelta(days=2))
        self.assertEqual(1, len(self.rows("watch_pairings")))
        self.assertFalse(self.claim(old, now=T0 + dt.timedelta(days=2)))

    def test_forget_watch_devices(self):
        code = self.start()
        self.claim(code)
        self.redeem(code)
        pending = self.start(OTHER_SECRET)
        self.assertEqual(1, forget_watch_devices(self.db_path, self.owner))
        self.assertEqual([], self.rows("watch_devices"))
        self.assertEqual(1, len(self.rows("watch_pairings")))  # unclaimed one has no owner
        self.assertEqual(0, forget_watch_devices(self.db_path, self.owner))
        self.assertTrue(pending)

    def test_account_deletion_removes_every_watch_row(self):
        code = self.start()
        self.claim(code)
        self.redeem(code)
        counts = owner_identity_row_counts(self.db_path, self.owner)
        self.assertEqual(1, counts["watch_pairings"])
        self.assertEqual(1, counts["watch_devices"])

        removed = delete_owner_identity(self.db_path, self.owner)

        self.assertEqual(1, removed["watch_pairings"])
        self.assertEqual(1, removed["watch_devices"])
        self.assertEqual([], self.rows("watch_devices"))
        self.assertEqual([], self.rows("watch_pairings"))
        self.assertEqual(
            {name: 0 for name in counts}, owner_identity_row_counts(self.db_path, self.owner)
        )

    def test_cascade_clears_watch_rows_without_the_explicit_delete(self):
        code = self.start()
        self.claim(code)
        self.redeem(code)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM provider_identities")
            connection.execute("DELETE FROM owners WHERE owner_id = ?", (self.owner,))
        self.assertEqual([], self.rows("watch_devices"))
        self.assertEqual([], self.rows("watch_pairings"))

    def test_the_sweep_reads_the_expiry_index_not_the_table(self):
        self.start()
        with sqlite3.connect(self.db_path) as connection:
            plan = " ".join(
                str(row[-1]) for row in connection.execute(
                    "EXPLAIN QUERY PLAN DELETE FROM watch_pairings WHERE expires_at < ?", ("x",)
                )
            )
        self.assertIn("watch_pairings_expiry", plan)

    def test_the_same_account_repeating_a_code_is_the_same_pairing(self):
        code = self.start()
        self.assertTrue(self.claim(code))
        # A retried call: still the same pairing, and the first label stands.
        self.assertTrue(self.claim(code, label="x***@example.invalid", now=at(20)))
        other = lookup_or_create_owner(self.db_path, "intervals", "i2")
        self.assertFalse(self.claim(code, owner=other, now=at(30)))
        self.assertEqual("t***@gmail.com", self.redeem(code).account_label)
        # Past its ten minutes and never picked up, a code is nobody's any more.
        late = self.start(OTHER_SECRET)
        self.assertTrue(self.claim(late))
        self.assertFalse(self.claim(late, now=at(601)))

    def test_a_used_code_is_connected_only_while_its_watch_is_the_current_one(self):
        first = self.start()
        self.claim(first)
        self.redeem(first, now=at(20))
        self.assertTrue(self.claim(first, now=at(30)))
        second = self.start(OTHER_SECRET, now=at(40))
        self.claim(second, now=at(50))
        self.redeem(second, OTHER_SECRET, now=at(60))
        # The first watch was replaced: saying its code again must not report it connected.
        self.assertFalse(self.claim(first, now=at(70)))
        self.assertTrue(self.claim(second, now=at(70)))

    def test_watch_device_says_whether_and_when_this_account_paired(self):
        self.assertIsNone(watch_device(self.db_path, self.owner))
        self.assertIsNone(watch_device(Path(self._tmp.name) / "missing.db", self.owner))
        code = self.start()
        self.claim(code)
        self.redeem(code, now=at(40))
        device = watch_device(self.db_path, self.owner)
        self.assertEqual({"paired_at", "last_seen_at", "account_label"}, set(device))
        self.assertEqual("t***@gmail.com", device["account_label"])
        forget_watch_devices(self.db_path, self.owner)
        self.assertIsNone(watch_device(self.db_path, self.owner))


if __name__ == "__main__":
    unittest.main()
