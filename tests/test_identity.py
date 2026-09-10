from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop.identity import (
    IdentityError,
    activity_report,
    client_origin_disclosed,
    delete_owner_identity,
    owner_active_day_count,
    owner_call_outcome_count,
    owner_client_disclosures,
    record_client_origin_disclosure,
    owner_entry_origins,
    record_entry_origin,
    ensure_registry,
    lookup_or_create_owner,
    owner_for_fingerprint,
    owner_for_provider_athlete,
    owner_identity_row_counts,
    provider_athlete_for_owner,
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

    def test_an_owner_resolves_back_to_the_athlete_it_was_created_for(self):
        """The inverse lookup a delivery uses to say whose calendar it is about to write.

        Re-authorizing does not move it: the same athlete keeps the same owner, so the
        answer is the same before and after a second token is recorded.
        """
        owner = lookup_or_create_owner(self.db_path, "intervals", "i1")
        record_token_fingerprint(
            self.db_path, token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), owner, "intervals"
        )
        self.assertEqual("i1", provider_athlete_for_owner(self.db_path, owner, "intervals"))

        record_token_fingerprint(
            self.db_path, token_fingerprint(SECOND_TOKEN, hmac_key=HMAC_KEY), owner, "intervals"
        )
        self.assertEqual("i1", provider_athlete_for_owner(self.db_path, owner, "intervals"))

    def test_two_owners_resolve_to_their_own_athletes_and_not_each_others(self):
        first = lookup_or_create_owner(self.db_path, "intervals", "i1")
        second = lookup_or_create_owner(self.db_path, "intervals", "i2")

        self.assertEqual("i1", provider_athlete_for_owner(self.db_path, first, "intervals"))
        self.assertEqual("i2", provider_athlete_for_owner(self.db_path, second, "intervals"))
        self.assertIsNone(provider_athlete_for_owner(self.db_path, first, "strava"))

    def test_an_owner_nobody_authorized_resolves_to_nothing(self):
        # Including before the registry file exists at all, which is what a first request
        # against a fresh deployment finds.
        unknown = "99999999-8888-7777-6666-555544443333"
        self.assertIsNone(provider_athlete_for_owner(self.db_path, unknown, "intervals"))
        lookup_or_create_owner(self.db_path, "intervals", "i1")
        self.assertIsNone(provider_athlete_for_owner(self.db_path, unknown, "intervals"))

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


class UsageHistoryTests(unittest.TestCase):
    """What the registry can still answer about use, now that nothing counts it.

    The two counter tables were written on every dispatched call until 1.4.3, when the
    writers were removed so that a read could be annotated as one (issue #408). What is
    pinned here is the half that stayed: rows written before that are still readable,
    still an account's own data, and still erased with the account. The rows below are
    inserted directly, because no product code path can produce one any more -- and a
    test that reached for a writer would be pinning code that no longer exists.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"
        self.owner = lookup_or_create_owner(self.db_path, "intervals", "athlete-1")

    def tearDown(self):
        self._tmp.cleanup()

    def historical_call(self, owner: str, tool: str, day: str, *, calls: int = 1) -> None:
        """One pre-1.4.3 usage row, written the way the removed counter would have."""
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO activity_days (owner_id, day, tool, calls) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner_id, day, tool) DO UPDATE SET calls = calls + ?",
                (owner, day, tool, calls, calls),
            )

    def historical_outcome(
        self, owner: str, tool: str, day: str, outcome: str, refusal: str = ""
    ) -> None:
        """One pre-1.4.3 outcome row, same terms as ``historical_call``."""
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO call_outcomes (owner_id, day, tool, outcome, refusal, calls) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(owner_id, day, tool, outcome, refusal) "
                "DO UPDATE SET calls = calls + 1",
                (owner, day, tool, outcome, refusal),
            )

    def test_no_writer_for_either_counter_survives_in_the_product(self):
        """The removal, checked against the module rather than against a call site."""
        import garmin_coach_loop.identity as identity_module

        for name in ("record_activity", "record_call_outcome"):
            with self.subTest(writer=name):
                self.assertFalse(hasattr(identity_module, name))
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(Path("garmin_coach_loop").rglob("*.py"))
        )
        self.assertNotIn("INSERT INTO activity_days", sources)
        self.assertNotIn("INSERT INTO call_outcomes", sources)

    def test_days_recorded_before_the_writers_went_are_still_readable(self):
        self.historical_call(self.owner, "session", "2026-08-18")
        self.historical_call(self.owner, "session", "2026-08-19", calls=4)
        entry = activity_report(self.db_path)["owners"][0]
        self.assertEqual(2, entry["active_days"])
        self.assertEqual(5, entry["calls"])
        self.assertEqual("2026-08-18", entry["first_active_day"])
        self.assertEqual("2026-08-19", entry["last_active_day"])
        self.assertEqual(2, owner_active_day_count(self.db_path, self.owner))

    def test_two_tools_on_one_day_are_one_active_day_not_two(self):
        """The rows are per tool; the number an athlete is shown must be per day."""
        self.historical_call(self.owner, "session", "2026-08-19")
        self.historical_call(self.owner, "delivery_apply", "2026-08-19")
        self.historical_call(self.owner, "state", "2026-08-19")
        self.historical_call(self.owner, "session", "2026-08-20")
        self.assertEqual(2, owner_active_day_count(self.db_path, self.owner))
        self.assertEqual({"session": 2, "delivery_apply": 1, "state": 1},
                         activity_report(self.db_path)["owners"][0]["tools"])

    def test_an_account_that_only_ever_used_this_release_reports_zeroes(self):
        """The normal shape from now on: registered, connected, and counted nowhere."""
        report = activity_report(self.db_path)
        self.assertEqual(1, report["registered"])
        self.assertEqual(0, report["active"])
        entry = report["owners"][0]
        self.assertEqual(0, entry["active_days"])
        self.assertIsNone(entry["last_active_day"])
        self.assertEqual({}, entry["tools"])
        self.assertEqual(0, owner_call_outcome_count(self.db_path, self.owner))

    def test_a_window_narrows_who_was_active_never_who_exists(self):
        self.historical_call(self.owner, "session", "2026-08-01")
        other = lookup_or_create_owner(self.db_path, "intervals", "athlete-2")
        self.historical_call(other, "session", "2026-08-19")
        report = activity_report(self.db_path, since="2026-08-15")
        self.assertEqual(2, report["registered"])
        self.assertEqual(1, report["active"])

    def test_a_window_that_is_not_a_date_is_refused_rather_than_compared(self):
        """`2026-8-1` sorts after `2026-12-31`, so a lexical window on it silently omits."""
        self.historical_call(self.owner, "session", "2026-08-19")
        for malformed in ("2026-8-1", "08-19-2026", "yesterday", "2026-08"):
            with self.subTest(since=malformed):
                with self.assertRaises(IdentityError):
                    activity_report(self.db_path, since=malformed)
        self.assertEqual(
            1, activity_report(self.db_path, since="2026-08-19")["active"]
        )

    def test_an_answered_call_and_a_refused_one_are_still_told_apart_in_history(self):
        self.historical_outcome(self.owner, "session", "2026-08-19", "accepted")
        self.historical_outcome(self.owner, "session", "2026-08-19", "accepted")
        self.historical_outcome(
            self.owner, "decision_apply", "2026-08-19", "refused", "plan_state_exists"
        )
        entry = activity_report(self.db_path)["owners"][0]
        self.assertEqual(2, entry["accepted"])
        self.assertEqual({"plan_state_exists": 1}, entry["refused"])
        # Rows, not calls: the two accepted ones share a row and a count of 2. The export
        # asks this only whether anything is held at all.
        self.assertEqual(2, owner_call_outcome_count(self.db_path, self.owner))

    def test_the_first_entry_an_athlete_arrived_through_is_kept_beside_a_later_one(self):
        record_entry_origin(self.db_path, self.owner, "https://claude.ai")
        record_entry_origin(self.db_path, self.owner, "https://claude.ai")
        record_entry_origin(self.db_path, self.owner, "https://chatgpt.com")
        self.assertEqual(
            ["https://chatgpt.com", "https://claude.ai"],
            owner_entry_origins(self.db_path, self.owner),
        )

    def test_an_account_that_never_called_anything_still_says_where_it_came_from(self):
        """Issue #209: that account is exactly the one worth asking about."""
        record_entry_origin(self.db_path, self.owner, "https://claude.ai")
        entry = activity_report(self.db_path)["owners"][0]
        self.assertEqual(0, entry["active_days"])
        self.assertEqual(["https://claude.ai"], entry["entries"])

    def test_a_window_never_hides_which_entry_somebody_arrived_through(self):
        record_entry_origin(self.db_path, self.owner, "https://claude.ai")
        self.historical_call(self.owner, "session", "2026-08-19")
        entry = activity_report(self.db_path, since="2026-08-19")["owners"][0]
        self.assertEqual(["https://claude.ai"], entry["entries"])

    def test_deleting_an_account_removes_its_entry_its_days_and_its_outcomes(self):
        record_entry_origin(self.db_path, self.owner, "https://claude.ai")
        self.historical_call(self.owner, "session", "2026-08-19")
        self.historical_outcome(self.owner, "session", "2026-08-19", "accepted")
        self.assertEqual(1, owner_active_day_count(self.db_path, self.owner))

        delete_owner_identity(self.db_path, self.owner)

        self.assertEqual([], owner_entry_origins(self.db_path, self.owner))
        self.assertEqual(0, owner_active_day_count(self.db_path, self.owner))
        self.assertEqual(0, owner_call_outcome_count(self.db_path, self.owner))
        self.assertEqual([], activity_report(self.db_path)["owners"])

    def test_history_is_never_one_of_the_hashed_identity_row_counts(self):
        """A deletion proposal binds this preview, so its numbers must not move under it."""
        self.historical_call(self.owner, "session", "2026-08-19")
        self.historical_outcome(self.owner, "session", "2026-08-19", "accepted")
        counts = owner_identity_row_counts(self.db_path, self.owner)
        self.assertNotIn("activity_days", counts)
        self.assertNotIn("call_outcomes", counts)

    def test_a_usage_count_for_an_unknown_owner_is_zero_without_creating_a_registry(self):
        missing = Path(self._tmp.name) / "absent.db"
        self.assertEqual(0, owner_active_day_count(missing, self.owner))
        self.assertEqual(0, owner_call_outcome_count(missing, self.owner))
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
        self.historical_call(self.owner, "session", "2026-08-19")
        rendered = repr(activity_report(self.db_path))
        self.assertNotIn("athlete-1", rendered)
        self.assertNotIn(FIRST_TOKEN, rendered)
        self.assertNotIn(token_fingerprint(FIRST_TOKEN, hmac_key=HMAC_KEY), rendered)


class ClientDisclosureTests(unittest.TestCase):
    """One row per athlete per unverified origin, and it goes with the account."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"
        self.owner = lookup_or_create_owner(self.db_path, "intervals", "athlete-1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_origin_is_disclosed_once_however_many_times_it_is_recorded(self):
        self.assertFalse(
            client_origin_disclosed(self.db_path, self.owner, "https://new-agent.example")
        )

        for _ in range(3):
            record_client_origin_disclosure(
                self.db_path, self.owner, "https://new-agent.example"
            )

        self.assertTrue(
            client_origin_disclosed(self.db_path, self.owner, "https://new-agent.example")
        )
        self.assertEqual(
            ["https://new-agent.example"],
            owner_client_disclosures(self.db_path, self.owner),
        )

    def test_two_sources_are_two_notices_and_two_athletes_are_told_separately(self):
        other = lookup_or_create_owner(self.db_path, "intervals", "athlete-2")
        record_client_origin_disclosure(self.db_path, self.owner, "https://one.example")
        record_client_origin_disclosure(self.db_path, self.owner, "https://two.example")
        record_client_origin_disclosure(self.db_path, other, "https://one.example")

        self.assertEqual(
            ["https://one.example", "https://two.example"],
            owner_client_disclosures(self.db_path, self.owner),
        )
        self.assertFalse(
            client_origin_disclosed(self.db_path, other, "https://two.example")
        )

    def test_deleting_an_account_removes_what_it_was_told(self):
        record_client_origin_disclosure(self.db_path, self.owner, "https://one.example")

        delete_owner_identity(self.db_path, self.owner)

        self.assertEqual([], owner_client_disclosures(self.db_path, self.owner))
        self.assertFalse(
            client_origin_disclosed(self.db_path, self.owner, "https://one.example")
        )

    def test_asking_a_registry_that_does_not_exist_creates_nothing(self):
        missing = Path(self._tmp.name) / "absent.db"

        self.assertFalse(client_origin_disclosed(missing, self.owner, "https://x.example"))
        self.assertEqual([], owner_client_disclosures(missing, self.owner))
        self.assertFalse(missing.exists())


# Exactly what 1.4.2's `delete_owner_identity` executes, in its order, taken from that
# release's source (`fe5fd249`). It is the rollback target this release may be asked to
# go back to, and the point of writing it out is that it has never heard of
# `client_disclosures`: if clearing those rows depended on this list, the athlete's own
# account deletion would be the thing that failed.
_ROLLBACK_TARGET_DELETION = (
    "DELETE FROM token_scopes WHERE fingerprint IN "
    "(SELECT fingerprint FROM token_fingerprints WHERE owner_id = ?)",
    "DELETE FROM token_fingerprints WHERE owner_id = ?",
    "DELETE FROM owner_revocations WHERE owner_id = ?",
    "DELETE FROM activity_days WHERE owner_id = ?",
    "DELETE FROM call_outcomes WHERE owner_id = ?",
    "DELETE FROM entry_origins WHERE owner_id = ?",
    "DELETE FROM provider_identities WHERE owner_id = ?",
    "DELETE FROM owners WHERE owner_id = ?",
)


def _delete_as_rollback_target(db_path: Path, owner_id: str) -> None:
    """Erase one account the way the release before this one does, and no other way."""
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _ROLLBACK_TARGET_DELETION:
            connection.execute(statement, (owner_id,))
        connection.execute("COMMIT")
    finally:
        connection.close()


class ClientDisclosureRollbackTests(unittest.TestCase):
    """A notice row must not be able to stop an athlete deleting their account.

    `client_disclosures` is the only identity table an athlete owns rows in that the
    previous release does not clear. Rolling a deployment back to it must not turn
    `applyOwnerDeletion` into a `FOREIGN KEY constraint failed`, so the clearing lives in
    the schema -- `ON DELETE CASCADE` -- where a release that has never heard of the
    table still performs it (follow-up review of PR #411).
    """

    # The shape `client_disclosures` was first written with: same columns, same key, no
    # cascade. A registry created by that build is what the migration has to answer,
    # because `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it is.
    _FIRST_SHAPE = """
        CREATE TABLE client_disclosures (
            owner_id TEXT NOT NULL REFERENCES owners(owner_id),
            origin TEXT NOT NULL,
            disclosed_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, origin)
        )
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "identity.db"
        ensure_registry(self.db_path)
        self.owner = lookup_or_create_owner(self.db_path, "intervals", "athlete-1")
        self.other = lookup_or_create_owner(self.db_path, "intervals", "athlete-2")

    def tearDown(self):
        self._tmp.cleanup()

    def _revert_to_first_shape(self, *rows: tuple[str, str]) -> None:
        """Put the table back in its first shape, with rows written straight into it.

        The rows go in here, not through `record_client_origin_disclosure`: that opens
        the registry with `create=True`, which migrates the empty table before the first
        row is written -- leaving the copy step nothing to carry, and leaving a test
        unable to notice if the copy were removed.
        """
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            connection.execute("DROP TABLE client_disclosures")
            connection.execute(self._FIRST_SHAPE)
            for owner_id, origin in rows:
                connection.execute(
                    "INSERT INTO client_disclosures (owner_id, origin, disclosed_at)"
                    " VALUES (?, ?, ?)",
                    (owner_id, origin, "2026-09-10T00:00:00Z"),
                )
        finally:
            connection.close()

    def _stored_ddl(self) -> str:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'client_disclosures'"
            ).fetchone()[0]
        finally:
            connection.close()

    def _foreign_key_violations(self) -> list:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()

    def _disclosure_rows(self, owner_id: str) -> list[str]:
        return owner_client_disclosures(self.db_path, owner_id)

    def test_the_release_before_this_one_can_still_erase_an_account_that_was_told(self):
        """Upgrade, be told once, roll back, delete: the whole hazard in one test."""
        record_client_origin_disclosure(self.db_path, self.owner, "https://new-agent.example")
        record_client_origin_disclosure(self.db_path, self.other, "https://new-agent.example")

        _delete_as_rollback_target(self.db_path, self.owner)

        self.assertEqual([], self._disclosure_rows(self.owner))
        self.assertEqual(
            {"owners": 0, "provider_identities": 0, "token_fingerprints": 0,
             "token_scopes": 0, "owner_revocations": 0},
            owner_identity_row_counts(self.db_path, self.owner),
        )
        # The other athlete is untouched: their notice, their identity, their account.
        self.assertEqual(["https://new-agent.example"], self._disclosure_rows(self.other))
        self.assertEqual(
            1, owner_identity_row_counts(self.db_path, self.other)["owners"]
        )

    def test_a_registry_written_before_this_fix_is_migrated_rows_and_all(self):
        """`CREATE TABLE IF NOT EXISTS` does not reshape a table; the rebuild does."""
        self._revert_to_first_shape(
            (self.owner, "https://one.example"),
            (self.owner, "https://three.example"),
            (self.other, "https://two.example"),
        )

        ensure_registry(self.db_path)

        self.assertIn("ON DELETE CASCADE", self._stored_ddl())
        # Migrating is not losing: every athlete keeps every notice they were already
        # given, so nobody is told a second time by the release that fixed the schema.
        self.assertEqual(
            ["https://one.example", "https://three.example"],
            self._disclosure_rows(self.owner),
        )
        self.assertEqual(["https://two.example"], self._disclosure_rows(self.other))

    def test_the_rebuild_leaves_behind_a_row_whose_athlete_no_longer_exists(self):
        """Foreign keys are off for the copy, so it must not carry a row that breaks them.

        Nothing should ever have written one. If something did, the athlete it named is
        gone and the row is unreachable -- carrying it through would rebuild the table
        into a state its own constraint forbids.
        """
        self._revert_to_first_shape(
            (self.owner, "https://one.example"),
            ("an-owner-that-was-deleted", "https://ghost.example"),
        )

        ensure_registry(self.db_path)

        self.assertEqual(["https://one.example"], self._disclosure_rows(self.owner))
        self.assertEqual([], self._disclosure_rows("an-owner-that-was-deleted"))
        self.assertEqual([], self._foreign_key_violations())

    def test_the_migrated_table_erases_with_the_account_on_the_release_before_it(self):
        """The migration is only worth anything if the rollback delete then works."""
        self._revert_to_first_shape(
            (self.owner, "https://one.example"), (self.other, "https://two.example")
        )
        ensure_registry(self.db_path)

        _delete_as_rollback_target(self.db_path, self.owner)

        self.assertEqual([], self._disclosure_rows(self.owner))
        self.assertEqual(["https://two.example"], self._disclosure_rows(self.other))
        self.assertEqual(
            1, owner_identity_row_counts(self.db_path, self.other)["owners"]
        )

    def test_this_release_still_deletes_the_rows_itself_and_only_this_owner(self):
        """The cascade is the floor, not the plan: the current path still names the table.

        Two assertions, because the behavioural one alone cannot tell the statement from
        the cascade that would cover for it -- the second reads the statement out of the
        module, the way the removed counters are held gone.
        """
        record_client_origin_disclosure(self.db_path, self.owner, "https://one.example")
        record_client_origin_disclosure(self.db_path, self.other, "https://two.example")

        delete_owner_identity(self.db_path, self.owner)

        self.assertEqual([], self._disclosure_rows(self.owner))
        self.assertEqual(["https://two.example"], self._disclosure_rows(self.other))
        self.assertIn(
            "DELETE FROM client_disclosures WHERE owner_id = ?",
            Path("garmin_coach_loop/identity.py").read_text(encoding="utf-8"),
        )

    def test_a_migration_runs_once_and_leaves_a_current_registry_alone(self):
        """The ordinary path is one read of `sqlite_master` and no write."""
        record_client_origin_disclosure(self.db_path, self.owner, "https://one.example")
        before = self._stored_ddl()

        ensure_registry(self.db_path)
        ensure_registry(self.db_path)

        self.assertEqual(before, self._stored_ddl())
        self.assertEqual(["https://one.example"], self._disclosure_rows(self.owner))

    def test_a_second_open_of_a_migrated_registry_rebuilds_nothing_and_keeps_the_rows(self):
        """A rolling deploy opens this many times; the second open must be a no-op."""
        self._revert_to_first_shape((self.owner, "https://one.example"))
        ensure_registry(self.db_path)
        migrated = self._stored_ddl()

        ensure_registry(self.db_path)
        record_client_origin_disclosure(self.db_path, self.other, "https://two.example")

        self.assertEqual(migrated, self._stored_ddl())
        self.assertEqual(["https://one.example"], self._disclosure_rows(self.owner))
        self.assertEqual(["https://two.example"], self._disclosure_rows(self.other))
        self.assertEqual([], self._foreign_key_violations())


if __name__ == "__main__":
    unittest.main()
