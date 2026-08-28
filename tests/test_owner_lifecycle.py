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

import contextlib
import dataclasses
import datetime as dt
import errno
import json
import types
import unittest
from typing import Any
from unittest import mock

from garmin_coach_loop import owner_data, store
from garmin_coach_loop.proposals import PROPOSAL_TTL_SECONDS
from garmin_coach_loop.identity import (
    IdentityError,
    owner_for_fingerprint,
    owner_identity_row_counts,
    record_token_fingerprint,
    revoke_owner_connections,
    token_fingerprint,
)
from garmin_coach_loop.store import (
    open_delivery_attempt,
    owner_maintenance_fence,
    read_current_plan,
    read_maintenance_fence,
)

from test_gateway import (
    CLIENT_SECRET_VALUE,
    HMAC_KEY,
    NOW,
    TOKEN_A,
    TOKEN_B,
    GatewayTestCase,
    RUN_SPORT_SETTINGS,
    publishable_plan,
    release_identity_for,
)


class OwnerDataTestCase(GatewayTestCase):
    """Two owners, so every cross-owner assertion has a second account to fail into."""

    def setUp(self):
        super().setUp()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]

    def export(self, *, token: str = TOKEN_A, **body: Any) -> tuple[int, Any]:
        return self.route(
            "data_export", body=body or None, token=token
        )

    def deletion_preview(self, *, token: str = TOKEN_A) -> tuple[int, Any]:
        return self.route("deletion_prepare", token=token)

    def delete(self, proposal: str, *, token: str = TOKEN_A, **overrides: Any):
        body: dict[str, Any] = {"proposal": proposal, "confirmed": True}
        body.update(overrides)
        return self.route("deletion_apply", body=body, token=token)

    def confirmed_deletion(self, *, token: str = TOKEN_A) -> tuple[int, Any]:
        status, preview = self.deletion_preview(token=token)
        self.assertEqual(200, status, preview)
        return self.delete(preview["proposal"], token=token)

    def _state_everything(self, *, token: str = TOKEN_A) -> None:
        """One statement of every kind the evidence layer holds, for this owner.

        Export and deletion are both whole-account operations, so the interesting
        assertion in either is that no group is missing -- which needs every group to be
        non-empty before the operation runs.
        """
        for kind, body in (
            ("profile_record", {"timezone": "Asia/Taipei"}),
            ("availability_record", {"recurring": {"available_days": ["mon", "wed", "fri"]}}),
            (
                "strength_report",
                {"exercise": "bench press", "sets": [{"reps": 5, "weight_kg": 60}]},
            ),
            ("body_measurement_record", {"weight_kg": 72.5}),
            ("activity_summary_record", {"sport": "running", "duration_minutes": 40}),
        ):
            status, payload = self.route(kind, body=body, token=token)
            self.assertEqual(200, status, payload)


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
        # The same five identity-table row counts, under the same key names, as
        # `owner_identity_row_counts` itself -- issue #139. No separate assertion of
        # coverage parity is enough on its own; see
        # test_export_identity_coverage_matches_the_deletion_previews_identity_rows for
        # the test that pins the two surfaces to each other rather than to a literal.
        self.assertEqual("intervals", payload["identity"]["provider"])
        self.assertEqual(1, payload["identity"]["owners"])
        self.assertEqual(1, payload["identity"]["provider_identities"])
        self.assertEqual(1, payload["identity"]["token_fingerprints"])
        self.assertEqual(0, payload["identity"]["token_scopes"])
        self.assertEqual(0, payload["identity"]["owner_revocations"])
        # Never revoked, and no scopes were recorded for this fixture's connection.
        self.assertIsNone(payload["identity"]["revoked_after"])
        self.assertEqual([], payload["identity"]["token_scope_names"])
        self.assertIn("athlete_evidence", payload)
        self.assertEqual([], payload["unknowns"])
        # Naming the omissions is part of the archive. "Here is everything" and "here is
        # everything we will show you" are different claims, and only one is true.
        self.assertEqual(list(owner_data.EXCLUDED), payload["excluded"])

    def test_it_carries_no_credential_identifier_or_path(self):
        # A past revocation and a scope-bearing reconnection, so this scan exercises
        # real content in every field issue #139 added -- not just the empty defaults
        # test_the_archive_is_this_athletes_and_states_what_it_leaves_out already covers.
        revoke_owner_connections(
            self.identity_db, self.owner_a, now=int(self.now.timestamp())
        )
        self.seed_owner(TOKEN_A, athlete_id="i1")
        record_token_fingerprint(
            self.identity_db,
            token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
            self.owner_a,
            "intervals",
            scope_names=("ACTIVITY:READ", "WELLNESS:READ"),
        )

        _, payload = self.export()

        self.assertIsNotNone(payload["identity"]["revoked_after"])
        self.assertEqual(
            [["ACTIVITY:READ", "WELLNESS:READ"]], payload["identity"]["token_scope_names"]
        )

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

    def test_export_identity_coverage_matches_the_deletion_previews_identity_rows(self):
        """Issue #139's acceptance test: the two surfaces must not be able to disagree.

        Not a comparison against a literal list of five names -- a comparison against
        whatever `owner_identity_row_counts` actually reports, the same function the
        deletion preview's own `identity_rows` calls. If a sixth identity table is ever
        added, this test keeps passing only if both surfaces picked it up; that is the
        property issue #139 asked for, not an incidental one.
        """
        _, exported = self.export()
        status, preview = self.deletion_preview()
        self.assertEqual(200, status, preview)

        identity_rows_keys = set(preview["removes"]["identity_rows"])
        exported_keys = set(exported["identity"])

        self.assertTrue(
            identity_rows_keys.issubset(exported_keys),
            f"export is missing identity_rows coverage: {identity_rows_keys - exported_keys}",
        )
        # What is left over is exactly the fields that are not an identity row count: the
        # provider name, the revocation instant the deletion preview has no equivalent
        # of, the scope-name content (as opposed to a count of it), and the usage counter
        # -- which is a number, but deliberately not one of these, because a deletion
        # proposal binds the hash of this preview and a usage count changes on the very
        # calls that confirm it. The preview states it as `usage_counters` instead.
        self.assertEqual(
            {
                "call_outcomes_recorded",
                "entry_origins",
                "provider",
                "revoked_after",
                "token_scope_names",
                "usage_days",
            },
            exported_keys - identity_rows_keys,
        )
        self.assertIn("usage_counters_removed", preview["removes"])

    def test_a_broken_usage_counter_cannot_block_an_erasure(self):
        """The failure mode that made the preview state its counters rather than count them.

        A counter write is swallowed when it fails, by design -- no statistic is worth a
        500 on somebody's coaching turn. But this preview is what the deletion proposal
        hashes, so anything derived from that counter can read one way while the athlete
        is looking at it and another way when they confirm. A derived `count > 0` did
        exactly that: broken at the preview, working at the confirmation, and the erasure
        came back `proposal_mismatch` over telemetry nobody can see.
        """
        with mock.patch(
            "garmin_coach_loop.gateway.record_activity",
            side_effect=IdentityError("registry is locked"),
        ):
            status, preview = self.deletion_preview()
        self.assertEqual(200, status, preview)
        self.assertTrue(preview["removes"]["usage_counters_removed"])

        # The condition clears between the preview and the confirmation.
        status, receipt = self.delete(preview["proposal"])

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])

    def test_revoked_after_is_null_before_a_revocation_and_set_after(self):
        _, before = self.export()
        self.assertIsNone(before["identity"]["revoked_after"])

        revoke_owner_connections(
            self.identity_db, self.owner_a, now=int(self.now.timestamp())
        )
        # Revoking forgets the fingerprint that just authenticated this call, so
        # reconnect first -- exactly what an athlete who revoked and reconsidered would
        # do -- before asking for another export.
        self.seed_owner(TOKEN_A, athlete_id="i1")

        _, after = self.export()

        # `self.now` (test_gateway.NOW) is 2026-08-13T00:00:00Z; the export reports the
        # instant as ISO-8601 UTC, not the epoch integer `owner_revocations` stores it as.
        self.assertEqual("2026-08-13T00:00:00Z", after["identity"]["revoked_after"])

    def test_the_owner_reference_is_stable_opaque_and_not_the_other_athletes(self):
        _, first = self.export()
        _, again = self.export()
        _, other = self.export(token=TOKEN_B)

        self.assertEqual(first["owner_reference"], again["owner_reference"])
        self.assertNotEqual(first["owner_reference"], other["owner_reference"])
        self.assertNotIn(self.owner_a, first["owner_reference"])

    def test_two_athletes_export_two_accounts(self):
        self._state_everything()

        _, mine = self.export()
        _, theirs = self.export(token=TOKEN_B)

        self.assertEqual(1, len(mine["athlete_evidence"]["strength_reports"]))
        self.assertEqual([], theirs["athlete_evidence"]["strength_reports"])

    def test_every_evidence_group_rides_along_in_the_archive(self):
        """A group the export forgot is a group the athlete's own copy is missing.

        The archive carries whatever ``load_evidence`` returns, so this is the check that
        a new evidence group cannot be added without appearing here -- the key set is
        compared against the loader's own, not against a literal list this test keeps in
        step by hand.
        """
        self._state_everything()

        _, payload = self.export()

        evidence = payload["athlete_evidence"]
        self.assertEqual(
            set(owner_data.athlete_evidence.load_evidence(self.owner_dir(self.owner_a))),
            set(evidence),
        )
        self.assertEqual(72.5, evidence["body_measurements"][0]["weight_kg"])
        self.assertEqual("running", evidence["reported_activities"][0]["sport"])

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
    def test_the_preview_counts_every_evidence_group_it_will_remove(self):
        """An athlete confirming an erasure should see everything it takes with it."""
        self._state_everything()

        status, payload = self.deletion_preview()

        self.assertEqual(200, status, payload)
        removes = payload["removes"]
        self.assertEqual(1, removes["reported_strength_sessions"])
        self.assertEqual(1, removes["body_measurements"])
        self.assertEqual(1, removes["reported_activities"])
        self.assertTrue(removes["reported_availability"])
        self.assertTrue(removes["stored_profile"])

    def test_a_confirmed_deletion_takes_every_evidence_group_with_it(self):
        self._state_everything()
        evidence_file = self.owner_dir(self.owner_a) / "athlete-evidence.json"
        self.assertTrue(evidence_file.is_file())

        status, receipt = self.confirmed_deletion()

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["removed"]["state_directory"])
        # The whole directory goes, so there is no group that could be left behind -- and
        # the other athlete's statements are untouched by it.
        self.assertFalse(evidence_file.exists())
        _, theirs = self.export(token=TOKEN_B)
        self.assertIn("athlete_evidence", theirs)

    def test_a_deletion_prepared_by_another_build_erases_nothing(self):
        """The shared proposal check on the one operation that cannot be taken back.

        An erasure is confirmed against a preview of what disappears. That preview is
        computed by the build that answered the prepare, and a build change between the
        two calls means the athlete confirmed one build's account of what they were about
        to lose and another build carried it out. Re-previewing costs one round trip and
        removes nothing, so the confirmation is refused and the account stays.
        """
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("a" * 40)
        )
        status, preview = self.deletion_preview()
        self.assertEqual(200, status, preview)
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("b" * 40)
        )

        status, payload = self.delete(preview["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertIn("different build of this gateway", payload["detail"])
        self.assertTrue((self.owner_dir(self.owner_a) / "store.json").is_file())

        # And the same athlete, previewing again on the build that is actually running,
        # still gets their erasure -- the refusal above is one re-preview, not a lock-out.
        status, receipt = self.confirmed_deletion()
        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["removed"]["state_directory"])

    def test_an_expired_proposal_erases_nothing(self):
        """The one operation that cannot be taken back was the one with no time bound.

        `applyCoachDecision` and `applyInitialization` both stop honouring a proposal
        after `PROPOSAL_TTL_SECONDS`; this route only ever compared the preview's
        content, so a confirmation from a conversation the athlete closed days ago still
        erased the account as long as nothing about it had moved (#208). The prepare has
        always told the client `expires_at`; now the apply means it.
        """
        status, preview = self.deletion_preview()
        self.assertEqual(200, status, preview)
        self.now = NOW + dt.timedelta(seconds=PROPOSAL_TTL_SECONDS + 1)

        status, payload = self.delete(preview["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_expired", payload["error"])
        # Refused, and nothing was touched on the way to refusing.
        self.assertTrue((self.owner_dir(self.owner_a) / "store.json").is_file())
        self.assertEqual(
            1, read_current_plan(self.owner_dir(self.owner_a))["current_version"]
        )

        # One re-preview, not a lock-out: the same athlete still gets their erasure.
        status, receipt = self.confirmed_deletion()
        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["removed"]["state_directory"])

    def test_a_proposal_still_inside_its_window_deletes(self):
        # The control for the refusal above: what is being refused is the age, so a
        # proposal a minute short of the same boundary has to go through. Without this,
        # a deletion route that refused everything would pass the test above.
        status, preview = self.deletion_preview()
        self.assertEqual(200, status, preview)
        self.now = NOW + dt.timedelta(seconds=PROPOSAL_TTL_SECONDS - 60)

        status, receipt = self.delete(preview["proposal"])

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])
        self.assertTrue(receipt["removed"]["state_directory"])

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
                "owner_revocations": 0,
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
                "owner_revocations": 0,
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
        for kind in ("session", "data_export", "deletion_prepare"):
            with self.subTest(kind=kind):
                status, payload = self.route(kind, token=TOKEN_A)
                self.assertEqual(401, status, payload)
                self.assertEqual("unauthorized", payload["error"])

    def test_an_athlete_who_signed_every_client_out_can_still_delete_their_account(self):
        """The one combination the two halves of this lifecycle could get wrong together.

        `revoke-connections` (#126) records a row keyed to `owners` by foreign key. A
        deletion that did not reach it would not leave an orphan -- it would fail, and it
        would fail for exactly the athlete who had already asked this product to stop
        letting anything reach their plan.
        """
        revoke_owner_connections(
            self.identity_db, self.owner_a, now=int(self.now.timestamp())
        )
        self.assertEqual(
            1, owner_identity_row_counts(self.identity_db, self.owner_a)["owner_revocations"]
        )
        # Revoking signed this connection out, so the deletion is driven by a new one --
        # which is what an athlete who revoked and then reconsidered actually has.
        self.seed_owner(TOKEN_A, athlete_id="i1")

        status, receipt = self.confirmed_deletion()

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])
        self.assertEqual(
            1, receipt["removed"]["identity_rows"]["owner_revocations"]
        )
        self.assertEqual(
            {
                "owners": 0,
                "provider_identities": 0,
                "token_fingerprints": 0,
                "token_scopes": 0,
                "owner_revocations": 0,
            },
            owner_identity_row_counts(self.identity_db, self.owner_a),
        )

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

    def test_state_written_while_the_account_was_being_deleted_is_swept_and_reported(self):
        """Review finding: the store lock does not cover the whole of a racing write.

        `athlete_evidence` creates the owner directory *before* it takes the lock, so a
        write already in flight can put the directory back after `rmtree` -- leaving a
        file for an owner whose identity rows are gone, which is data outliving every way
        of asking for it. Simulated directly here, because the real race needs two
        threads and the property under test is what happens afterwards.
        """
        state_dir = self.owner_dir(self.owner_a)
        original = owner_data.delete_owner_store
        raced = {"once": False}

        def racing_delete(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            # Only the first confirmed removal, so the sweep that follows it is the
            # thing under test rather than a second chance to lose.
            if kwargs.get("confirm") and not raced["once"]:
                raced["once"] = True
                # Exactly what a concurrent `recordStrengthExecution` leaves behind.
                state_dir.mkdir(parents=True, exist_ok=True)
                (state_dir / "athlete-evidence.json").write_text("{}", encoding="utf-8")
            return result

        with mock.patch.object(owner_data, "delete_owner_store", racing_delete):
            status, receipt = self.confirmed_deletion()

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["removed"]["state_written_during_deletion"])
        self.assertFalse(state_dir.exists())

    def test_an_ordinary_deletion_reports_no_racing_write(self):
        """The false-positive control: the flag is not simply always true."""
        _, receipt = self.confirmed_deletion()

        self.assertFalse(receipt["removed"]["state_written_during_deletion"])

    def test_a_fenced_deletion_tells_the_athlete_something_they_can_act_on(self):
        """Review finding: the refusal used to name the operator CLI command.

        A hosted athlete has no shell. What they can act on is the reservation, the
        sessions in it, and the Intervals calendar -- all of which the message keeps.
        """
        open_delivery_attempt(
            self.owner_dir(self.owner_a),
            plan_id="fixture-plan-001",
            plan_version=1,
            proposal_hash="h" * 64,
            kind="delivery",
            operations=[
                {"session_id": "run-long-01", "operation": "upsert", "external_id": None}
            ],
        )

        status, refused = self.deletion_preview()

        self.assertEqual(409, status, refused)
        self.assertNotIn("delete-owner", refused["detail"])
        self.assertIn("deleting your data", refused["detail"])
        self.assertIn("run-long-01", refused["detail"])

    def test_a_cutover_of_this_owners_store_refuses_the_preview_in_the_same_shape(self):
        """Issue #137: a deletion must not queue behind a cutover the athlete cannot see.

        The preview takes no lock -- it reads -- so without an explicit refusal here the
        erasure would be previewed as available and would then fail at the confirmation
        against a store that was being moved.
        """
        with owner_maintenance_fence(self.owner_dir(self.owner_a), operation="archive-store"):
            status, refused = self.deletion_preview()

        self.assertEqual(409, status, refused)
        self.assertEqual("state_conflict", refused["error"])
        self.assertIn("deleting your data is refused", refused["detail"])
        self.assertIn("archive-store", refused["detail"])
        # The control: the cutover ends, and the athlete's own deletion goes through.
        status, receipt = self.confirmed_deletion()
        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])

    def test_the_deleted_account_stays_fenced_after_the_receipt(self):
        """The receipt is not the end of the writers, so it is not the end of the fence.

        A request that authenticated before the deletion holds no credential this product
        can revoke -- it resolved its owner already -- and evidence writers create the
        owner directory before they take the store lock. The tombstone is what makes the
        receipt final for them too (``store.mark_owner_deleted``).
        """
        state_dir = self.owner_dir(self.owner_a)

        _, receipt = self.confirmed_deletion()

        self.assertTrue(receipt["deleted"])
        self.assertFalse(state_dir.exists())
        fence = read_maintenance_fence(state_dir)
        self.assertIs(True, fence["tombstone"])
        self.assertEqual("deleting your data", fence["operation"])
        # And the athlete who reconnects is a different owner id, so nothing legitimate
        # ever asks for this path again (test_repeating_it_is_safe).
        self.assertNotEqual(self.owner_a, self.seed_owner(TOKEN_A, athlete_id="i1"))

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
        _, session = self.route("session", body={}, token=TOKEN_A)
        _, delivery = self.route(
            "delivery_prepare",
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
        self.route(
            "strength_report",
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



class SharedVolumeLowWaterTests(OwnerDataTestCase):
    """One volume, two athletes, and what happens as it fills (issue #288 item 3).

    Every owner's store lives on the same volume, so the athlete who fills it is not the
    athlete whose next write meets the bottom of it. What this refuses is that write, a
    little early, with a sentence -- rather than letting it tear a commit in half and
    then having to be recovered from.

    Deliberately not a per-owner quota: this says nothing about who consumed the space,
    only that no one account can take the volume all the way down.
    """

    @staticmethod
    @contextlib.contextmanager
    def volume(*, free_bytes: int | None):
        """Report one free-space figure for every filesystem, or refuse to report any."""

        def statvfs(_path):
            if free_bytes is None:
                raise OSError(errno.EIO, "the volume did not answer")
            return types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1)

        with mock.patch.object(store.os, "statvfs", statvfs):
            yield

    NEARLY_FULL = store.VOLUME_LOW_WATER_BYTES - 1
    REFUSAL = "the state volume is nearly full, so nothing may be written to it right now"

    def test_a_write_is_refused_before_the_volume_runs_out_under_it(self):
        with self.volume(free_bytes=self.NEARLY_FULL):
            status, payload = self.route(
                "availability_record",
                body={"recurring": {"available_days": ["tue", "thu"]}},
                token=TOKEN_A,
            )

        self.assertEqual(409, status, payload)
        self.assertEqual(self.REFUSAL, payload["detail"])
        # Nothing half-written: the file the write would have created is not there.
        self.assertFalse((self.owner_dir(self.owner_a) / "athlete-evidence.json").exists())

    def test_the_other_athlete_meets_the_same_refusal_rather_than_a_torn_store(self):
        # The property that makes this a shared-volume guard rather than a per-account
        # one. Neither athlete filled it on their own; both are told the same thing.
        with self.volume(free_bytes=self.NEARLY_FULL):
            status, payload = self.route(
                "subjective_state_record",
                body={"note": "腿有點酸"},
                token=TOKEN_B,
            )

        self.assertEqual(409, status, payload)
        self.assertEqual(self.REFUSAL, payload["detail"])
        self.assertEqual(1, read_current_plan(self.owner_dir(self.owner_b))["current_version"])

    def test_one_byte_above_the_mark_is_an_ordinary_write(self):
        with self.volume(free_bytes=store.VOLUME_LOW_WATER_BYTES):
            status, payload = self.route(
                "availability_record",
                body={"recurring": {"available_days": ["tue", "thu"]}},
                token=TOKEN_A,
            )

        self.assertEqual(200, status, payload)

    def test_a_volume_that_cannot_be_measured_is_unknown_rather_than_full(self):
        # AGENTS.md invariant 3. Reading an unanswerable `statvfs` as a refusal would
        # take every store on the host offline at once, for a reason nobody could see.
        with self.volume(free_bytes=None):
            status, payload = self.route(
                "availability_record",
                body={"recurring": {"available_days": ["tue", "thu"]}},
                token=TOKEN_A,
            )

        self.assertEqual(200, status, payload)

    def test_reading_a_plan_still_works_when_the_volume_is_nearly_full(self):
        # A guard that stopped reads would turn "the volume is nearly full" into "the
        # service is down", which is worse than what it prevents.
        with self.volume(free_bytes=self.NEARLY_FULL):
            status, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual(1, payload["plan_version"])

    def test_deleting_an_account_still_finishes_when_the_volume_is_nearly_full(self):
        """The recovery path the guard must not close.

        Deletion is what gives space back, and its tombstone is written after the store
        is already gone -- so refusing that one write would leave an account erased from
        disk and not recorded as erased, which is the state the fence exists to prevent.
        """
        status, preview = self.deletion_preview(token=TOKEN_A)
        self.assertEqual(200, status, preview)

        with self.volume(free_bytes=self.NEARLY_FULL):
            status, receipt = self.delete(preview["proposal"], token=TOKEN_A)

        self.assertEqual(200, status, receipt)
        self.assertTrue(receipt["deleted"])
        fence = read_maintenance_fence(self.owner_dir(self.owner_a))
        self.assertIsNotNone(fence)
        self.assertIs(True, fence["tombstone"])


class ObservedRevocationTests(OwnerDataTestCase):
    def test_a_revoked_credential_is_forgotten_and_the_next_call_says_reconnect(self):
        self.fake.read_status = 401

        status, refused = self.route("session", body={}, token=TOKEN_A)
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
        status, again = self.route("session", body={}, token=TOKEN_A)
        self.assertEqual(401, status, again)
        self.assertEqual("unauthorized", again["error"])

    def test_the_plan_is_waiting_when_they_reconnect(self):
        self.fake.read_status = 401
        self.route("session", body={}, token=TOKEN_A)
        self.fake.read_status = None

        # Reconnecting is the ordinary token exchange, and identity is keyed on the
        # athlete rather than on the token, so it lands on the same owner and the same
        # plan mid-cycle (docs/account-lifecycle.md).
        self.fake.token_payload = {
            "access_token": "tok-alpha-2",
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1"},
        }
        self.gateway._redeem_intervals_code("fixture-code")
        self.assertEqual(
            self.owner_a,
            owner_for_fingerprint(
                self.identity_db, token_fingerprint("tok-alpha-2", hmac_key=HMAC_KEY)
            ),
        )
        status, session = self.route(
            "session", body={}, token="tok-alpha-2"
        )
        self.assertEqual(200, status, session)
        self.assertEqual(1, session["plan_state"]["plan_version"])

    def test_a_refused_scope_is_not_a_revocation(self):
        """403 is a token that works and a permission that was never granted.

        Forgetting it would send the athlete round an authorization loop that cannot
        grant a scope the application never asks for.
        """
        self.fake.read_status = 403

        status, refused = self.route("session", body={}, token=TOKEN_A)

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

        self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(before, self.snapshot(self.owner_dir(self.owner_a)))

    def test_one_revoked_connection_does_not_disconnect_the_athletes_other_one(self):
        """An athlete connected through two entries holds two tokens (issue #8).

        One of them being revoked says nothing about the other, and logging them out of
        both would be this product inventing a revocation the provider did not make.
        """
        self.seed_owner("tok-alpha-mcp", athlete_id="i1")
        self.fake.read_status = 401
        self.route("session", body={}, token=TOKEN_A)
        self.fake.read_status = None

        status, session = self.route(
            "session", body={}, token="tok-alpha-mcp"
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

        status, payload = self.route("permissions", token=TOKEN_A)

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
