from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.identity import (
    IdentityError,
    delete_owner_identity,
    ensure_registry,
    lookup_or_create_owner,
    owner_for_fingerprint,
    owner_for_provider_athlete,
    owner_identity_row_counts,
    record_token_fingerprint,
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

    def test_reauthorization_retires_the_previous_fingerprint(self):
        # Intervals invalidates the old access token when it issues a new one; a
        # fingerprint that still resolved would keep a dead token authenticating.
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        old = token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY)
        record_token_fingerprint(self.db_path, old, owner, "intervals")
        record_token_fingerprint(
            self.db_path, token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY), owner, "intervals"
        )
        self.assertIsNone(owner_for_fingerprint(self.db_path, old))

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
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1},
            counts,
        )

    def test_row_counts_for_an_unknown_owner_are_all_zero_without_creating_a_registry(self):
        missing_db = Path(self._tmp.name) / "absent" / "identity.db"

        counts = owner_identity_row_counts(missing_db, "11111111-2222-3333-4444-555555555555")

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0},
            counts,
        )
        self.assertFalse(missing_db.exists())

    def test_delete_removes_every_row_for_that_owner_and_reports_what_it_removed(self):
        owner = self._seeded_owner("i1", FIRST_TOKEN)

        removed = delete_owner_identity(self.db_path, owner)

        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1},
            removed,
        )
        self.assertIsNone(owner_for_provider_athlete(self.db_path, "intervals", "i1"))
        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0},
            owner_identity_row_counts(self.db_path, owner),
        )

    def test_delete_never_touches_another_owners_rows(self):
        first = self._seeded_owner("i1", FIRST_TOKEN)
        second = self._seeded_owner("i2", SECOND_TOKEN)

        delete_owner_identity(self.db_path, first)

        self.assertIsNone(owner_for_provider_athlete(self.db_path, "intervals", "i1"))
        self.assertEqual(second, owner_for_provider_athlete(self.db_path, "intervals", "i2"))
        self.assertEqual(
            {"owners": 1, "provider_identities": 1, "token_fingerprints": 1, "token_scopes": 1},
            owner_identity_row_counts(self.db_path, second),
        )

    def test_deleting_an_owner_never_seen_is_a_harmless_no_op(self):
        unknown = "99999999-8888-7777-6666-555544443333"

        removed = delete_owner_identity(self.db_path, unknown)

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0},
            removed,
        )

    def test_deleting_from_a_registry_that_does_not_exist_creates_nothing(self):
        missing_db = Path(self._tmp.name) / "absent" / "identity.db"

        removed = delete_owner_identity(missing_db, "11111111-2222-3333-4444-555555555555")

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0},
            removed,
        )
        self.assertFalse(missing_db.exists())

    def test_delete_is_idempotent_on_a_second_call(self):
        owner = self._seeded_owner("i1", FIRST_TOKEN)
        delete_owner_identity(self.db_path, owner)

        second_removal = delete_owner_identity(self.db_path, owner)

        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0, "token_scopes": 0},
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


if __name__ == "__main__":
    unittest.main()
