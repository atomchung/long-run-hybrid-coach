"""What an athlete can do about their own account: read it, delete it, lose access to it.

Four states that get confused with one another, and the whole point of the code under
test is that they stay apart (docs/account-lifecycle.md):

- **connected** -- a credential this deployment recognises;
- **revoked** -- the athlete withdrew the grant at Intervals, learned here on the next
  failed call, after which the plan waits for them unchanged (#8);
- **exported** -- they hold a copy, which changes nothing (#7);
- **deleted** -- the plan and the identity rows that reach it are gone, and no credential
  resolves them again (#6).

Every dangerous path here is written twice: once as the harmful case, and once as the
control that must still work. A deletion that refuses everything would pass a test suite
made only of the first half.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from garmin_coach_loop import owner_data
from garmin_coach_loop.identity import (
    owner_for_fingerprint,
    owner_identity_row_counts,
    token_fingerprint,
)
from garmin_coach_loop.store import read_current_plan

from test_gateway import (
    CLIENT_SECRET_VALUE,
    HMAC_KEY,
    TOKEN_A,
    TOKEN_B,
    GatewayTestCase,
    publishable_plan,
)


class OwnerDataTestCase(GatewayTestCase):
    """Two owners, so every cross-owner assertion has a second account to fail into."""

    def setUp(self):
        super().setUp()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())

    def export(self, *, token: str = TOKEN_A, **body: Any) -> tuple[int, Any]:
        return self.call(
            "GET", "/v1/coach/data/export", body=body or None, token=token
        )

    def deletion_preview(self, *, token: str = TOKEN_A) -> tuple[int, Any]:
        return self.call("GET", "/v1/coach/data/deletion/prepare", token=token)

    def delete(self, proposal: str, *, token: str = TOKEN_A, **overrides: Any):
        body: dict[str, Any] = {"proposal": proposal, "confirmed": True}
        body.update(overrides)
        return self.call("POST", "/v1/coach/data/deletion/apply", body=body, token=token)

    def confirmed_deletion(self, *, token: str = TOKEN_A) -> tuple[int, Any]:
        status, preview = self.deletion_preview(token=token)
        self.assertEqual(200, status, preview)
        return self.delete(preview["proposal"], token=token)


# --------------------------------------------------------------------------------------
# Export (#7)
# --------------------------------------------------------------------------------------


class OwnerExportTests(OwnerDataTestCase):
    def test_the_archive_is_this_athletes_and_states_what_it_leaves_out(self):
        status, payload = self.export()

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual(owner_data.ARCHIVE_VERSION, payload["archive_version"])
        self.assertEqual("fixture-plan-001", payload["plan_state"]["plan_id"])
        self.assertEqual(1, payload["plan_state"]["plan_version"])
        self.assertEqual(1, payload["identity"]["provider_connections"])
        self.assertEqual(1, payload["identity"]["live_token_fingerprints"])
        self.assertIn("athlete_evidence", payload)
        self.assertEqual([], payload["unknowns"])
        # Naming the omissions is part of the archive. "Here is everything" and "here is
        # everything we will show you" are different claims, and only one is true.
        self.assertEqual(list(owner_data.EXCLUDED), payload["excluded"])

    def test_it_carries_no_credential_identifier_or_path(self):
        _, payload = self.export()

        blob = json.dumps(payload, ensure_ascii=False) + "\n".join(
            self.log_handler.records
        )
        for secret in (
            TOKEN_A,
            TOKEN_B,
            CLIENT_SECRET_VALUE,
            HMAC_KEY.decode("ascii"),
            token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
            self.owner_a,
            str(self.state_root),
            # The provider athlete id: theirs, but not something this archive needs to
            # restate, and the one identifier an operator could pivot on.
            "i1",
        ):
            self.assertNotIn(secret, blob, secret[:12])

    def test_the_owner_reference_is_stable_opaque_and_not_the_other_athletes(self):
        _, first = self.export()
        _, again = self.export()
        _, other = self.export(token=TOKEN_B)

        self.assertEqual(first["owner_reference"], again["owner_reference"])
        self.assertNotEqual(first["owner_reference"], other["owner_reference"])
        self.assertNotIn(self.owner_a, first["owner_reference"])

    def test_two_athletes_export_two_accounts(self):
        self.call(
            "POST",
            "/v1/coach/strength-report",
            body={"exercise": "bench press", "sets": [{"reps": 5, "weight_kg": 60}]},
            token=TOKEN_A,
        )

        _, mine = self.export()
        _, theirs = self.export(token=TOKEN_B)

        self.assertEqual(1, len(mine["athlete_evidence"]["strength_reports"]))
        self.assertEqual([], theirs["athlete_evidence"]["strength_reports"])

    def test_no_request_field_can_name_a_different_account(self):
        """There is no athlete parameter, so there is nothing to traverse with.

        The check is that the surplus field is *refused* rather than ignored: a body
        quietly dropped is a body somebody keeps trying.
        """
        for attempt in (
            {"owner_id": self.owner_b},
            {"athlete_id": "i2"},
            {"state_dir": "../" + self.owner_b},
        ):
            with self.subTest(attempt=attempt):
                status, payload = self.export(**attempt)
                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])

    def test_an_account_with_no_plan_says_so_rather_than_exporting_an_empty_one(self):
        owner = self.seed_owner("tok-charlie-1", athlete_id="i3")

        status, payload = self.export(token="tok-charlie-1")

        self.assertEqual(200, status, payload)
        self.assertIsNone(payload["plan_state"])
        self.assertEqual([], payload["decision_history"])
        self.assertIn("no PlanState exists for this account", payload["unknowns"])
        self.assertEqual(1, owner_identity_row_counts(self.identity_db, owner)["owners"])

    def test_a_store_that_cannot_be_read_refuses_rather_than_exporting_nothing(self):
        """AGENTS.md 3: a failed read is unknown, never an empty successful one.

        An archive that turned an unreadable store into ``plan_state: null`` would hand
        the athlete a blank copy of a plan that is still sitting on disk -- and it is
        exactly the archive somebody would then act on.
        """
        (self.owner_dir(self.owner_a) / "store.json").write_text("{}", encoding="utf-8")

        status, payload = self.export()

        self.assertEqual(409, status, payload)
        self.assertEqual("state_conflict", payload["error"])


# --------------------------------------------------------------------------------------
# Deletion (#6)
# --------------------------------------------------------------------------------------


class OwnerDeletionTests(OwnerDataTestCase):
    def test_the_preview_says_what_goes_and_what_it_cannot_reach(self):
        status, payload = self.deletion_preview()

        self.assertEqual(200, status, payload)
        self.assertTrue(payload["confirmation_required"])
        self.assertFalse(payload["reversible"])
        self.assertEqual("fixture-plan-001", payload["removes"]["plan_id"])
        self.assertEqual(1, payload["removes"]["identity_rows"]["owners"])
        self.assertEqual(1, payload["removes"]["identity_rows"]["token_fingerprints"])
        self.assertEqual(list(owner_data.NOT_REMOVED), payload["not_removed"])
        # The two boundaries an athlete would otherwise assume this crossed.
        joined = " ".join(payload["not_removed"])
        self.assertIn("Intervals.icu calendar", joined)
        self.assertIn("Revoke it in Intervals.icu Settings", joined)
        # And previewing removed nothing.
        self.assertEqual(1, read_current_plan(self.owner_dir(self.owner_a))["current_version"])

    def test_a_confirmed_deletion_removes_the_store_and_every_identity_row(self):
        status, receipt = self.confirmed_deletion()

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])
        self.assertTrue(receipt["receipt_id"].startswith("gcd-"))
        self.assertTrue(receipt["removed"]["state_directory"])
        self.assertEqual(
            {
                "owners": 1,
                "provider_identities": 1,
                "token_fingerprints": 1,
                "token_scopes": 0,
            },
            receipt["removed"]["identity_rows"],
        )
        self.assertFalse(self.owner_dir(self.owner_a).exists())
        self.assertEqual(
            {
                "owners": 0,
                "provider_identities": 0,
                "token_fingerprints": 0,
                "token_scopes": 0,
            },
            owner_identity_row_counts(self.identity_db, self.owner_a),
        )

    def test_the_credential_that_deleted_the_account_no_longer_resolves_it(self):
        self.confirmed_deletion()

        self.assertIsNone(
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            )
        )
        for method, path in (
            ("POST", "/v1/coach/session"),
            ("GET", "/v1/coach/data/export"),
            ("GET", "/v1/coach/data/deletion/prepare"),
        ):
            with self.subTest(path=path):
                status, payload = self.call(method, path, token=TOKEN_A)
                self.assertEqual(401, status, payload)
                self.assertEqual("unauthorized", payload["error"])

    def test_the_other_athlete_is_untouched(self):
        self.confirmed_deletion()

        status, theirs = self.export(token=TOKEN_B)
        self.assertEqual(200, status, theirs)
        self.assertEqual("fixture-plan-001", theirs["plan_state"]["plan_id"])
        self.assertTrue(self.owner_dir(self.owner_b).exists())

    def test_repeating_it_is_safe(self):
        self.confirmed_deletion()
        # The credential is gone with the account, so a repeat is a fresh connection of
        # the same athlete -- which is what a real second attempt looks like.
        owner = self.seed_owner(TOKEN_A, athlete_id="i1")
        self.assertNotEqual(self.owner_a, owner)

        status, receipt = self.confirmed_deletion()

        self.assertEqual(200, status, receipt)
        self.assertFalse(receipt["removed"]["state_directory"])
        self.assertEqual(list(owner_data.NOT_REMOVED), receipt["not_removed"])

    def test_the_receipt_carries_no_owner_id_credential_or_training_content(self):
        _, receipt = self.confirmed_deletion()

        blob = json.dumps(receipt) + "\n".join(self.log_handler.records)
        for secret in (
            TOKEN_A,
            self.owner_a,
            str(self.state_root),
            token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
            "fixture-plan-001",
            "run-long-01",
        ):
            self.assertNotIn(secret, blob, secret[:12])

    # -- the harmful cases -------------------------------------------------------------

    def test_another_athletes_confirmation_deletes_nothing(self):
        status, preview = self.deletion_preview(token=TOKEN_A)
        self.assertEqual(200, status, preview)

        status, refused = self.delete(preview["proposal"], token=TOKEN_B)

        self.assertEqual(409, status, refused)
        self.assertEqual("proposal_mismatch", refused["error"])
        self.assertTrue(self.owner_dir(self.owner_a).exists())
        self.assertTrue(self.owner_dir(self.owner_b).exists())

    def test_an_ordinary_confirmation_cannot_be_spent_as_an_erasure(self):
        """#6: erasure must not be reachable from the decision or delivery path.

        The two are bound by different claims, so a confirmation the athlete gave for a
        plan change or a delivery opens nothing here -- which is what stops "confirm this
        week's change" from ever being the click that deleted the account.
        """
        _, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        _, delivery = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-long-01"],
            },
            token=TOKEN_A,
        )

        status, refused = self.delete(delivery["proposal_hash"])

        self.assertEqual(409, status, refused)
        self.assertEqual("proposal_mismatch", refused["error"])
        self.assertTrue(self.owner_dir(self.owner_a).exists())

    def test_without_the_confirmation_nothing_is_removed(self):
        _, preview = self.deletion_preview()

        status, refused = self.delete(preview["proposal"], confirmed=False)

        self.assertEqual(409, status, refused)
        self.assertEqual("confirmation_required", refused["error"])
        self.assertTrue(self.owner_dir(self.owner_a).exists())

    def test_an_account_that_changed_since_the_preview_is_refused(self):
        """The confirmation is for the account they were shown, not for the name of it."""
        _, preview = self.deletion_preview()
        self.call(
            "POST",
            "/v1/coach/strength-report",
            body={"exercise": "bench press", "sets": [{"reps": 5, "weight_kg": 60}]},
            token=TOKEN_A,
        )

        status, refused = self.delete(preview["proposal"])

        self.assertEqual(409, status, refused)
        self.assertEqual("proposal_mismatch", refused["error"])
        self.assertTrue(self.owner_dir(self.owner_a).exists())

        # The control: re-previewing and confirming that works.
        status, receipt = self.confirmed_deletion()
        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])


# --------------------------------------------------------------------------------------
# Revocation (#8)
# --------------------------------------------------------------------------------------


class ObservedRevocationTests(OwnerDataTestCase):
    def test_a_revoked_credential_is_forgotten_and_the_next_call_says_reconnect(self):
        self.fake.read_status = 401

        status, refused = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(502, status, refused)
        self.assertEqual("provider_error", refused["error"])

        # The fingerprint is the only thing this product holds that a revocation can
        # invalidate, so revocation means forgetting it. The next call is then a plain
        # 401 the client can act on rather than a provider error it can only report.
        self.assertIsNone(
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            )
        )
        self.fake.read_status = None
        status, again = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(401, status, again)
        self.assertEqual("unauthorized", again["error"])

    def test_the_plan_is_waiting_when_they_reconnect(self):
        self.fake.read_status = 401
        self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.fake.read_status = None

        # Reconnecting is the ordinary token exchange, and identity is keyed on the
        # athlete rather than on the token, so it lands on the same owner and the same
        # plan mid-cycle (docs/account-lifecycle.md).
        self.fake.token_payload = {
            "access_token": "tok-alpha-2",
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1"},
        }
        status, _ = self.call(
            "POST",
            "/oauth/intervals/token",
            raw=b"grant_type=authorization_code&code=fixture-code",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(200, status)
        self.assertEqual(
            self.owner_a,
            owner_for_fingerprint(
                self.identity_db, token_fingerprint("tok-alpha-2", hmac_key=HMAC_KEY)
            ),
        )
        status, session = self.call(
            "POST", "/v1/coach/session", body={}, token="tok-alpha-2"
        )
        self.assertEqual(200, status, session)
        self.assertEqual(1, session["plan_state"]["plan_version"])

    def test_a_refused_scope_is_not_a_revocation(self):
        """403 is a token that works and a permission that was never granted.

        Forgetting it would send the athlete round an authorization loop that cannot
        grant a scope the application never asks for.
        """
        self.fake.read_status = 403

        status, refused = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(502, status, refused)
        self.assertEqual(
            self.owner_a,
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            ),
        )

    def test_the_failed_call_left_the_plan_exactly_where_it_was(self):
        before = self.snapshot(self.owner_dir(self.owner_a))
        self.fake.read_status = 401

        self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(before, self.snapshot(self.owner_dir(self.owner_a)))

    def test_one_revoked_connection_does_not_disconnect_the_athletes_other_one(self):
        """An athlete connected through two entries holds two tokens (issue #8).

        One of them being revoked says nothing about the other, and logging them out of
        both would be this product inventing a revocation the provider did not make.
        """
        self.seed_owner("tok-alpha-mcp", athlete_id="i1")
        self.fake.read_status = 401
        self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.fake.read_status = None

        status, session = self.call(
            "POST", "/v1/coach/session", body={}, token="tok-alpha-mcp"
        )

        self.assertEqual(200, status, session)
        self.assertEqual(1, session["plan_state"]["plan_version"])

    def test_the_diagnostic_still_answers_instead_of_becoming_a_401(self):
        """`inspectIntervalsPermissions` exists to report the state of a connection.

        Forgetting the credential from inside it would replace the one answer the athlete
        asked for -- ``invalid_or_expired``, which names the fix -- with a bare 401 that
        names nothing.
        """
        self.fake.read_status = 401

        status, payload = self.call("GET", "/v1/coach/permissions", token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("invalid_or_expired", payload["settings_read"])
        self.assertEqual(
            self.owner_a,
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            ),
        )


if __name__ == "__main__":
    unittest.main()
