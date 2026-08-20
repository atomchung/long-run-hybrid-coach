from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.identity import (
    IdentityError,
    activity_report,
    delete_owner_identity,
    owner_usage_row_count,
    record_activity,
    ensure_registry,
    lookup_or_create_owner,
    owner_for_fingerprint,
    owner_for_provider_athlete,
    owner_identity_row_counts,
    owner_scope_name_sets,
    record_token_fingerprint,
    revoke_owner_connections,
    scopes_for_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.store import StateStoreError, resolve_state_dir


# Short synthetic values only. Never real key material, and deliberately far too short to
# resemble a token any scanner should worry about.
HMAC_KEY = b"unit-test-fingerprint-key-0000000"
FIRST_TOKEN = "tok-alpha-1"
SECOND_TOKEN = "tok-alpha-2"


class IdentityRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_athlete_reauthorizing_maps_to_the_same_owner(self):
        # The whole point of the registry: a new access token must not mean a new store.
        first = lookup_or_create_owner(self.db_path, "intervals", "i1")
        second = lookup_or_create_owner(self.db_path, "intervals", "i1")
        self.assertEqual(first, second)

        record_token_fingerprint(
            self.db_path, token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), first, "intervals"
        )
        record_token_fingerprint(
            self.db_path, token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY), second, "intervals"
        )
        self.assertEqual(
            first,
            owner_for_fingerprint(
                self.db_path, token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY)
            ),
        )

    def test_a_second_authorization_leaves_the_first_one_working(self):
        # Intervals keeps earlier access tokens valid, and an athlete connects this
        # product from more than one client. Retiring the previous fingerprint logged
        # them out of one entry every time they connected another.
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        first = token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY)
        second = token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY)
        record_token_fingerprint(self.db_path, first, owner, "intervals")
        record_token_fingerprint(self.db_path, second, owner, "intervals")
        self.assertEqual(owner, owner_for_fingerprint(self.db_path, first))
        self.assertEqual(owner, owner_for_fingerprint(self.db_path, second))

    def test_recording_the_same_fingerprint_twice_changes_nothing(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        fingerprint = token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY)
        record_token_fingerprint(
            self.db_path, fingerprint, owner, "intervals", scope_names=("ACTIVITY:READ",)
        )
        record_token_fingerprint(
            self.db_path,
            fingerprint,
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ", "WELLNESS:READ"),
        )
        self.assertEqual(owner, owner_for_fingerprint(self.db_path, fingerprint))
        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1, "owner_revocations": 0},
            owner_identity_row_counts(self.db_path, owner),
        )
        self.assertEqual(
            ("ACTIVITY:READ", "WELLNESS:READ"),
            scopes_for_fingerprint(self.db_path, fingerprint),
        )

    def test_different_athletes_get_different_owners_and_disjoint_state_dirs(self):
        first = lookup_or_create_owner(self.db_path, "intervals", "i1")
        second = lookup_or_create_owner(self.db_path, "intervals", "i2")
        self.assertNotEqual(first, second)

        root = Path(self._tmp.name) / "state"
        first_dir = resolve_state_dir(first, state_root=root)
        second_dir = resolve_state_dir(second, state_root=root)
        self.assertNotEqual(first_dir, second_dir)
        self.assertEqual(first_dir.parent, second_dir.parent)

    def test_database_never_contains_the_plaintext_token(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path, token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), owner, "intervals"
        )
        raw = self.db_path.read_bytes()
        self.assertNotIn(FIRST_TOKEN.encode("utf-8"), raw)
        self.assertNotIn(HMAC_KEY, raw)
        self.assertIn(
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY).encode("ascii"), raw
        )

    def test_registry_file_is_private_to_its_owner(self):
        lookup_or_create_owner(self.db_path, "intervals", "i1")
        self.assertEqual(0o600, os.stat(self.db_path).st_mode & 0o777)

    def test_unknown_fingerprint_resolves_to_nothing_without_creating_a_registry(self):
        missing = Path(self._tmp.name) / "absent" / "identity.db"
        self.assertIsNone(owner_for_fingerprint(missing, "0" * 64))
        self.assertFalse(missing.exists())

    def test_fingerprint_requires_a_key_and_a_token(self):
        with self.assertRaises(IdentityError):
            token_fingerprint("", hmac_key=HMAC_KEY)
        with self.assertRaises(IdentityError):
            token_fingerprint(FIRST_TOKEN, hmac_key=b"")

    def test_fingerprint_cannot_be_recorded_for_an_unknown_owner(self):
        with self.assertRaises(IdentityError):
            record_token_fingerprint(
                self.db_path,
                token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
                "11111111-2222-3333-4444-555555555555",
                "intervals",
            )


class OwnerScopeNameSetsTests(unittest.TestCase):
    """``owner_scope_name_sets``: the export archive's non-secret view of what an
    owner's live connections may do (issue #139), without exposing which connection."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_registry_that_does_not_exist_yet_is_the_empty_tuple(self):
        missing = Path(self._tmp.name) / "absent" / "identity.db"
        self.assertEqual((), owner_scope_name_sets(missing, "11111111-2222-3333-4444-555555555555"))
        self.assertFalse(missing.exists())

    def test_an_owner_with_no_recorded_scopes_is_the_empty_tuple(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path, token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), owner, "intervals"
        )
        self.assertEqual((), owner_scope_name_sets(self.db_path, owner))

    def test_one_connection_reports_its_one_set(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ", "WELLNESS:READ"),
        )
        self.assertEqual(
            (("ACTIVITY:READ", "WELLNESS:READ"),), owner_scope_name_sets(self.db_path, owner)
        )

    def test_two_connections_with_different_grants_report_both_sets_once_each(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ",),
        )
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ", "CALENDAR:WRITE", "WELLNESS:READ"),
        )
        self.assertEqual(
            (("ACTIVITY:READ",), ("ACTIVITY:READ", "CALENDAR:WRITE", "WELLNESS:READ")),
            owner_scope_name_sets(self.db_path, owner),
        )

    def test_the_same_grant_on_two_connections_is_reported_once(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        for token in (FIRST_TOKEN, SECOND_TOKEN):
            record_token_fingerprint(
                self.db_path,
                token_fingerprint(token, hmac_key=HMAC_KEY),
                owner,
                "intervals",
                scope_names=("ACTIVITY:READ", "WELLNESS:READ"),
            )
        self.assertEqual(
            (("ACTIVITY:READ", "WELLNESS:READ"),), owner_scope_name_sets(self.db_path, owner)
        )

    def test_storage_order_does_not_split_one_granted_set_into_two(self):
        # `record_token_fingerprint` stores whatever order it is given; this function's
        # contract is a *set* of names, so two recordings of the same names in a
        # different order must collapse to one entry, not two.
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("WELLNESS:READ", "ACTIVITY:READ"),
        )
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ", "WELLNESS:READ"),
        )
        self.assertEqual(
            (("ACTIVITY:READ", "WELLNESS:READ"),), owner_scope_name_sets(self.db_path, owner)
        )

    def test_a_forgotten_connections_scopes_are_not_reported(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ",),
        )
        revoke_owner_connections(self.db_path, owner)
        self.assertEqual((), owner_scope_name_sets(self.db_path, owner))

    def test_another_owners_scopes_never_appear(self):
        mine = lookup_or_create_owner(self.db_path, "intervals", "i1")
        theirs = lookup_or_create_owner(self.db_path, "intervals", "i2")
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
            mine,
            "intervals",
            scope_names=("ACTIVITY:READ",),
        )
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY),
            theirs,
            "intervals",
            scope_names=("CALENDAR:WRITE",),
        )
        self.assertEqual((("ACTIVITY:READ",),), owner_scope_name_sets(self.db_path, mine))
        self.assertEqual((("CALENDAR:WRITE",),), owner_scope_name_sets(self.db_path, theirs))

    def test_never_returns_a_fingerprint_value(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        fingerprint = token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY)
        record_token_fingerprint(
            self.db_path, fingerprint, owner, "intervals", scope_names=("ACTIVITY:READ",)
        )
        blob = repr(owner_scope_name_sets(self.db_path, owner))
        self.assertNotIn(fingerprint, blob)


class EnsureRegistryTests(unittest.TestCase):
    """``ensure_registry``: the gateway's startup preflight opens this once, before it
    binds a socket, so a missing or broken registry is a refused boot (see gateway.py's
    ``run_preflight``), not a 500 on somebody's first sign-in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_the_file_and_schema_when_nothing_exists_yet(self):
        self.assertFalse(self.db_path.exists())
        ensure_registry(self.db_path)
        self.assertTrue(self.db_path.is_file())
        # Usable immediately -- the schema this call created is the real one.
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        self.assertTrue(owner)

    def test_is_a_no_op_against_an_already_initialized_registry(self):
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path, token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), owner, "intervals"
        )

        ensure_registry(self.db_path)  # must not raise, must not disturb existing rows

        self.assertEqual(owner, lookup_or_create_owner(self.db_path, "intervals", "i1"))
        self.assertEqual(
            owner,
            owner_for_fingerprint(self.db_path, token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY)),
        )

    def test_a_path_that_cannot_become_a_database_is_refused_as_an_identity_error(self):
        # A directory where the file should be: guaranteed unusable regardless of which
        # user or permissions the test happens to run under.
        self.db_path.mkdir()
        with self.assertRaises(IdentityError):
            ensure_registry(self.db_path)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses directory permissions"
    )
    def test_an_unwritable_parent_directory_is_refused_as_an_identity_error(self):
        # No permission-restoring cleanup needed: the directory stays empty (the write
        # that would have populated it is exactly what failed), and removing an empty
        # directory needs write access to its parent, not to itself.
        locked = Path(self._tmp.name) / "locked"
        locked.mkdir(mode=0o500)
        with self.assertRaises(IdentityError):
            ensure_registry(locked / "identity.db")


class DeleteOwnerIdentityTests(unittest.TestCase):
    """Operator-run owner deletion, identity half: every row for one owner goes,
    no other owner's rows move."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _seeded_owner(self, athlete_id: str, token: str) -> str:
        owner = lookup_or_create_owner(self.db_path, "intervals", athlete_id)
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(token, hmac_key=HMAC_KEY),
            owner,
            "intervals",
            scope_names=("ACTIVITY:READ",),
        )
        return owner

    def test_row_counts_match_what_was_actually_recorded(self):
        owner = self._seeded_owner("i1", FIRST_TOKEN)

        counts = owner_identity_row_counts(self.db_path, owner)

        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1, "owner_revocations": 0},
            counts,
        )

    def test_row_counts_for_an_unknown_owner_are_all_zero_without_creating_a_registry(self):
        missing_db = Path(self._tmp.name) / "absent" / "identity.db"

        counts = owner_identity_row_counts(missing_db, "11111111-2222-3333-4444-555555555555")

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0, "owner_revocations": 0},
            counts,
        )
        self.assertFalse(missing_db.exists())

    def test_delete_removes_every_row_for_that_owner_and_reports_what_it_removed(self):
        owner = self._seeded_owner("i1", FIRST_TOKEN)

        removed = delete_owner_identity(self.db_path, owner)

        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1, "owner_revocations": 0},
            removed,
        )
        self.assertIsNone(owner_for_provider_athlete(self.db_path, "intervals", "i1"))
        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0, "owner_revocations": 0},
            owner_identity_row_counts(self.db_path, owner),
        )

    def test_delete_never_touches_another_owners_rows(self):
        first = self._seeded_owner("i1", FIRST_TOKEN)
        second = self._seeded_owner("i2", SECOND_TOKEN)

        delete_owner_identity(self.db_path, first)

        self.assertIsNone(owner_for_provider_athlete(self.db_path, "intervals", "i1"))
        self.assertEqual(second, owner_for_provider_athlete(self.db_path, "intervals", "i2"))
        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1, "owner_revocations": 0},
            owner_identity_row_counts(self.db_path, second),
        )

    def test_deleting_an_owner_never_seen_is_a_harmless_no_op(self):
        unknown = "99999999-8888-7777-6666-555544443333"

        removed = delete_owner_identity(self.db_path, unknown)

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0, "owner_revocations": 0},
            removed,
        )

    def test_deleting_from_a_registry_that_does_not_exist_creates_nothing(self):
        missing_db = Path(self._tmp.name) / "absent" / "identity.db"

        removed = delete_owner_identity(missing_db, "11111111-2222-3333-4444-555555555555")

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0, "owner_revocations": 0},
            removed,
        )
        self.assertFalse(missing_db.exists())

    def test_delete_is_idempotent_on_a_second_call(self):
        owner = self._seeded_owner("i1", FIRST_TOKEN)
        delete_owner_identity(self.db_path, owner)

        second_removal = delete_owner_identity(self.db_path, owner)

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0, "owner_revocations": 0},
            second_removal,
        )


class OwnerStateDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def test_owner_id_must_be_a_canonical_uuid(self):
        # Everything that is not one fixed-shape string is refused, so no owner id can
        # ever escape the root or name another owner's directory.
        for candidate in (
            "../../etc",
            "/absolute",
            "not-a-uuid",
            "",
            "{11111111-2222-3333-4444-555555555555}",
            "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
            "urn:uuid:11111111-2222-3333-4444-555555555555",
            "11111111222233334444555555555555",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(StateStoreError):
                    resolve_state_dir(candidate, state_root=self.root)

    def test_owner_directory_lives_under_the_root(self):
        owner = "11111111-2222-3333-4444-555555555555"
        resolved = resolve_state_dir(owner, state_root=self.root)
        self.assertEqual(self.root.resolve() / "owners" / owner, resolved)


class UsageCounterTests(unittest.TestCase):
    """The operator's usage question, and the two things it must never turn into.

    What is being pinned here is a boundary as much as a feature: the counter answers how
    many accounts exist and how often each is used, and stays incapable of answering who
    they are or what they did.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"
        self.owner = lookup_or_create_owner(self.db_path, "intervals", "athlete-1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_repeated_calls_on_one_day_are_one_row_and_a_rising_count(self):
        for _ in range(5):
            record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        report = activity_report(self.db_path)
        self.assertEqual(1, report["registered"])
        self.assertEqual(1, report["active"])
        entry = report["owners"][0]
        self.assertEqual(1, entry["active_days"])
        self.assertEqual(5, entry["calls"])
        self.assertEqual(1, owner_usage_row_count(self.db_path, self.owner))

    def test_distinct_days_are_counted_separately_from_calls(self):
        record_activity(self.db_path, self.owner, "session", day="2026-08-18")
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        entry = activity_report(self.db_path)["owners"][0]
        self.assertEqual(2, entry["active_days"])
        self.assertEqual(3, entry["calls"])
        self.assertEqual("2026-08-18", entry["first_active_day"])
        self.assertEqual("2026-08-19", entry["last_active_day"])

    def test_each_tool_is_counted_under_its_own_name(self):
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        record_activity(self.db_path, self.owner, "delivery_apply", day="2026-08-19")
        entry = activity_report(self.db_path)["owners"][0]
        self.assertEqual({"session": 2, "delivery_apply": 1}, entry["tools"])

    def test_an_account_that_never_came_back_is_reported_with_zeroes(self):
        report = activity_report(self.db_path)
        self.assertEqual(1, report["registered"])
        self.assertEqual(0, report["active"])
        entry = report["owners"][0]
        self.assertEqual(0, entry["active_days"])
        self.assertIsNone(entry["last_active_day"])

    def test_a_window_narrows_who_was_active_never_who_exists(self):
        record_activity(self.db_path, self.owner, "session", day="2026-08-01")
        other = lookup_or_create_owner(self.db_path, "intervals", "athlete-2")
        record_activity(self.db_path, other, "session", day="2026-08-19")
        report = activity_report(self.db_path, since="2026-08-15")
        self.assertEqual(2, report["registered"])
        self.assertEqual(1, report["active"])

    def test_deleting_an_account_removes_its_counters_in_the_same_call(self):
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        record_activity(self.db_path, self.owner, "delivery_apply", day="2026-08-19")
        self.assertEqual(2, owner_usage_row_count(self.db_path, self.owner))
        delete_owner_identity(self.db_path, self.owner)
        self.assertEqual(0, owner_usage_row_count(self.db_path, self.owner))
        self.assertEqual([], activity_report(self.db_path)["owners"])

    def test_usage_counters_are_never_one_of_the_hashed_identity_row_counts(self):
        """A deletion proposal binds this preview, and the two calls that confirm it count."""
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        counts = owner_identity_row_counts(self.db_path, self.owner)
        self.assertNotIn("activity_days", counts)
        record_activity(self.db_path, self.owner, "deletion_prepare", day="2026-08-19")
        self.assertEqual(counts, owner_identity_row_counts(self.db_path, self.owner))

    def test_a_usage_count_for_an_unknown_owner_is_zero_without_creating_a_registry(self):
        missing = Path(self._tmp.name) / "absent.db"
        self.assertEqual(0, owner_usage_row_count(missing, self.owner))
        self.assertFalse(missing.exists())

    def test_a_registry_that_does_not_exist_yet_reports_nothing_and_stays_absent(self):
        missing = Path(self._tmp.name) / "absent.db"
        self.assertEqual(
            {"registered": 0, "active": 0, "since": None, "owners": []},
            activity_report(missing),
        )
        self.assertFalse(missing.exists())

    def test_the_report_never_carries_an_athlete_id_a_token_or_a_fingerprint(self):
        record_token_fingerprint(
            self.db_path,
            token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY),
            self.owner,
            "intervals",
        )
        record_activity(self.db_path, self.owner, "session", day="2026-08-19")
        rendered = repr(activity_report(self.db_path))
        self.assertNotIn("athlete-1", rendered)
        self.assertNotIn(FIRST_TOKEN, rendered)
        self.assertNotIn(token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), rendered)

    def test_a_counter_cannot_be_recorded_for_an_unknown_owner(self):
        with self.assertRaises(IdentityError):
            record_activity(self.db_path, "11111111-2222-3333-4444-555555555555", "session")

    def test_an_empty_owner_or_tool_is_refused(self):
        for owner, tool in ((" ", "session"), (self.owner, " ")):
            with self.subTest(owner=owner, tool=tool):
                with self.assertRaises(IdentityError):
                    record_activity(self.db_path, owner, tool)


if __name__ == "__main__":
    unittest.main()
