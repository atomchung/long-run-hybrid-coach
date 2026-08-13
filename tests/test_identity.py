from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.identity import (
    IdentityError,
    lookup_or_create_owner,
    owner_for_fingerprint,
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
