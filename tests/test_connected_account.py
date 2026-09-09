"""Which Intervals account a connection reaches, and what that has to guarantee (#396).

The gateway has always keyed identity by `(provider, provider_athlete_id) -> owner`, and
that key is not the subject here -- it is unchanged. What is under test is the part a
person can act on: one athlete holding a normal account and a separate review account
could authorize either, and nothing in any response said which one they had. A workout
was prepared against one and pushed to the other.

So two claims, and the second is the one that matters:

1. **A connection can name itself.** `inspectIntervalsPermissions` reports the account
   behind the bearer as a label a person recognises -- the email address first, because
   two accounts of one person can carry one display name -- or says explicitly that it
   could not. Never somebody else's name, and never a raw athlete id.
2. **Every preview that can write is bound to the account it named.** The account is
   carried inside the opaque set, and the apply refuses a bearer that resolves to a
   different one. That covers all three paths a workout reaches Intervals by:
   `prepareWorkoutDelivery`/`applyWorkoutDelivery` in both directions, the same pair
   resuming an interrupted reservation, and the calendar half of
   `prepareCoachDecision`/`applyCoachDecision`. Preview account A, write account B is not
   reachable on any of them.

Third, quieter, and asserted throughout: the label is read live and kept nowhere. It is
not in the store, not in the identity registry, not in a log line, not in the delivery
receipt, not in the export and not in the usage counters.
"""

from __future__ import annotations

import copy
import json
import unittest
from typing import Any
from unittest import mock

from garmin_coach_loop import connected_account
from garmin_coach_loop import gateway as gw
from garmin_coach_loop.delivery import (
    DeliveryError,
    owned_external_id_for,
    prepare_delivery_set,
    prepare_withdrawal_set,
)
from garmin_coach_loop.gateway import GatewayError
from garmin_coach_loop.identity import activity_report
from garmin_coach_loop.proposals import open_proposal
from garmin_coach_loop.store import pending_delivery_attempt, read_current_plan

from test_gateway import (
    FIXTURE_EMAIL,
    HMAC_KEY,
    RUN_SPORT_SETTINGS,
    SECOND_FIXTURE_EMAIL,
    TOKEN_A,
    TOKEN_B,
    WEEKLY_CHANGE,
    GatewayTestCase,
    publishable_plan,
)


FIRST_LABEL = {"id": "i1", "name": "Fixture Athlete", "email": FIXTURE_EMAIL}
SECOND_LABEL = {"id": "i2", "name": "Review Account", "email": SECOND_FIXTURE_EMAIL}

# The one session of the fixture plan a delivery is allowed to publish.
DELIVERABLE_SESSION = "run-quality-01"


class AccountLabelShapeTests(unittest.TestCase):
    """`describe` alone: what each branch says, and what it refuses to say."""

    def test_a_matching_profile_becomes_a_label_a_person_recognises(self):
        described = connected_account.describe(
            registered_athlete_id="i1", profile=dict(FIRST_LABEL)
        )
        self.assertEqual(connected_account.RESOLVED, described["resolution"])
        self.assertEqual(FIXTURE_EMAIL, described["email"])
        self.assertEqual("Fixture Athlete", described["name"])
        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete", described["label"]
        )

    def test_the_email_leads_the_label_and_the_object(self):
        """Which half of the label a person is meant to read first, pinned.

        One person's own account and their review account routinely carry one display
        name; the address never does. So the address is what a reader meets first -- in
        the label, and in the order of the object the model reads out of.
        """
        described = connected_account.describe(
            registered_athlete_id="i1", profile=dict(FIRST_LABEL)
        )

        self.assertLess(
            described["label"].index(FIXTURE_EMAIL),
            described["label"].index("Fixture Athlete"),
        )
        self.assertLess(list(described).index("email"), list(described).index("name"))

    def test_two_accounts_sharing_a_name_are_still_told_apart(self):
        # The case the ordering exists for: identical display names, and the label still
        # answers which account this is.
        shared = "Fixture Athlete"
        first = connected_account.describe(
            registered_athlete_id="i1",
            profile={"id": "i1", "name": shared, "email": FIXTURE_EMAIL},
        )
        second = connected_account.describe(
            registered_athlete_id="i2",
            profile={"id": "i2", "name": shared, "email": SECOND_FIXTURE_EMAIL},
        )

        self.assertEqual(first["name"], second["name"])
        self.assertNotEqual(first["label"], second["label"])
        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — " + shared, first["label"]
        )

    def test_a_profile_naming_a_different_athlete_carries_no_name_at_all(self):
        """The one branch this whole surface exists for.

        A profile whose id is not the athlete this bearer is registered as is exactly the
        wrong identity, so it does not get to supply the name shown before a write.
        """
        described = connected_account.describe(
            registered_athlete_id="i1", profile={**SECOND_LABEL}
        )
        self.assertEqual(connected_account.MISMATCH, described["resolution"])
        self.assertEqual(connected_account.DIFFERENT_ATHLETE, described["reason"])
        self.assertNotIn("name", described)
        self.assertNotIn("email", described)
        self.assertNotIn("label", described)
        self.assertNotIn(SECOND_FIXTURE_EMAIL, json.dumps(described))

    def test_an_athlete_id_the_provider_sent_as_a_number_is_the_same_athlete(self):
        """The registry stores what the authorization coerced; reading it back agrees.

        `_redeem_intervals_code` registers `str(athlete_id).strip()`, so a provider that
        answers with a number is registered as its digits. Comparing the live profile
        under a stricter rule made the two halves of one product disagree about one field
        -- and this half failed closed, refusing every write for an account that is in
        fact the registered one.
        """
        described = connected_account.describe(
            registered_athlete_id="12345",
            profile={"id": 12345, "name": "Fixture Athlete", "email": FIXTURE_EMAIL},
        )

        self.assertEqual(connected_account.RESOLVED, described["resolution"])
        self.assertEqual(FIXTURE_EMAIL, described["email"])

    def test_a_number_that_is_a_different_athlete_is_still_a_mismatch(self):
        """Coercing the shape does not soften the comparison it exists to enable."""
        described = connected_account.describe(
            registered_athlete_id="12345", profile={"id": 99999, "name": "Someone Else"}
        )

        self.assertEqual(connected_account.MISMATCH, described["resolution"])
        self.assertEqual(connected_account.DIFFERENT_ATHLETE, described["reason"])

    def test_a_profile_with_no_athlete_id_cannot_be_verified_and_is_not_an_accusation(self):
        """"Which account is this?" unanswered is not "this is the wrong account".

        `mismatch` is the one resolution that refuses a write, and it means the provider
        answered for somebody else. A response this code could not read an id out of said
        nothing about who it is, so it costs the label and nothing more.
        """
        for profile in ({"name": "Fixture Athlete"}, {"id": None}, {"id": "   "}):
            with self.subTest(profile=profile):
                described = connected_account.describe(
                    registered_athlete_id="i1", profile=dict(profile)
                )

                self.assertEqual(connected_account.UNAVAILABLE, described["resolution"])
                self.assertEqual(
                    connected_account.PROFILE_WITHOUT_ATHLETE_ID, described["reason"]
                )
                self.assertNotIn("label", described)

    def test_an_unreadable_profile_says_so_rather_than_guessing(self):
        described = connected_account.describe(registered_athlete_id="i1", profile=None)
        self.assertEqual(connected_account.UNAVAILABLE, described["resolution"])
        self.assertEqual(connected_account.PROFILE_UNREADABLE, described["reason"])
        self.assertNotIn("label", described)

    def test_a_missing_identity_row_is_reported_and_never_filled_in(self):
        described = connected_account.describe(
            registered_athlete_id=None, profile=dict(FIRST_LABEL)
        )
        self.assertEqual(connected_account.UNAVAILABLE, described["resolution"])
        self.assertEqual(connected_account.NO_IDENTITY_ROW, described["reason"])
        self.assertNotIn(FIXTURE_EMAIL, json.dumps(described))

    def test_a_profile_with_only_a_name_still_labels_the_account(self):
        """A weaker answer than usual, and still the answer -- not a fourth resolution.

        The athlete is told which account this is as far as the provider allowed, which is
        what `resolved` means. What such a label cannot do is separate two accounts that
        share a display name, and the object says so by carrying `email: null` rather than
        by inventing a state every reader would then have to branch on. The tool
        descriptions carry the rest: say the label cannot tell them apart.
        """
        described = connected_account.describe(
            registered_athlete_id="i1", profile={"id": "i1", "name": "Fixture Athlete"}
        )
        self.assertEqual(connected_account.RESOLVED, described["resolution"])
        self.assertIsNone(described["email"])
        self.assertEqual("Intervals.icu — Fixture Athlete", described["label"])
        self.assertEqual(
            {"provider", "resolution", "email", "name", "label"}, set(described)
        )

    def test_a_profile_with_only_an_email_still_labels_the_account(self):
        described = connected_account.describe(
            registered_athlete_id="i1", profile={"id": "i1", "email": FIXTURE_EMAIL}
        )
        self.assertEqual(connected_account.RESOLVED, described["resolution"])
        self.assertIsNone(described["name"])
        self.assertEqual("Intervals.icu — " + FIXTURE_EMAIL, described["label"])

    def test_a_first_and_last_name_stand_in_for_a_missing_display_name(self):
        described = connected_account.describe(
            registered_athlete_id="i1",
            profile={"id": "i1", "firstname": "Fixture", "lastname": "Athlete"},
        )
        self.assertEqual("Fixture Athlete", described["name"])

    def test_a_profile_with_nothing_to_show_is_unavailable_not_a_blank_label(self):
        described = connected_account.describe(
            registered_athlete_id="i1", profile={"id": "i1", "sex": "M"}
        )
        self.assertEqual(connected_account.UNAVAILABLE, described["resolution"])
        self.assertEqual(connected_account.PROFILE_WITHOUT_LABEL, described["reason"])


class AccountRefTests(unittest.TestCase):
    """The handle a delivery set carries instead of the athlete id."""

    def test_the_same_account_always_produces_the_same_handle(self):
        first = connected_account.account_ref("intervals", "i1", hmac_key=HMAC_KEY)
        second = connected_account.account_ref("intervals", "i1", hmac_key=HMAC_KEY)
        self.assertEqual(first, second)

    def test_two_accounts_never_share_a_handle(self):
        self.assertNotEqual(
            connected_account.account_ref("intervals", "i1", hmac_key=HMAC_KEY),
            connected_account.account_ref("intervals", "i2", hmac_key=HMAC_KEY),
        )

    def test_the_handle_never_carries_the_athlete_id_it_stands_for(self):
        # The orchestration prompt tells the model never to display an athlete id, and a
        # delivery set is model-visible bytes. A handle keeps both true at once.
        ref = connected_account.account_ref("intervals", "i669399", hmac_key=HMAC_KEY)
        self.assertNotIn("i669399", ref)
        self.assertNotIn("669399", ref)

    def test_a_different_deployment_key_produces_a_different_handle(self):
        self.assertNotEqual(
            connected_account.account_ref("intervals", "i1", hmac_key=HMAC_KEY),
            connected_account.account_ref("intervals", "i1", hmac_key=b"another-key-000"),
        )

    def test_a_binding_is_exactly_a_provider_and_a_handle(self):
        binding = connected_account.binding("intervals", "i1", hmac_key=HMAC_KEY)
        self.assertEqual({"provider", "account_ref"}, set(binding))
        self.assertTrue(connected_account.is_binding(binding))

    def test_anything_that_is_not_that_shape_is_not_a_binding(self):
        for value in (
            None,
            "intervals",
            {},
            {"provider": "intervals"},
            {"provider": "intervals", "account_ref": ""},
            {"provider": "intervals", "account_ref": "abc", "email": FIXTURE_EMAIL},
        ):
            self.assertFalse(connected_account.is_binding(value), value)


class DeliverySetBindingTests(unittest.TestCase):
    """The set itself: the account is hashed material, not a label beside it."""

    def setUp(self):
        self.plan = publishable_plan()
        self.session_id = DELIVERABLE_SESSION
        self.binding = connected_account.binding("intervals", "i1", hmac_key=HMAC_KEY)

    def prepared(self, **kwargs: Any) -> dict[str, Any]:
        return prepare_delivery_set(self.plan, [self.session_id], **kwargs)

    def withdrawal(self, **kwargs: Any) -> dict[str, Any]:
        """One withdrawal set over a session whose delivered event was superseded."""
        plan = publishable_plan()
        session = next(
            item
            for item in plan["week"]["sessions"]
            if item["session_id"] == self.session_id
        )
        session["execution"]["superseded_external_id"] = "9001"
        owned = owned_external_id_for(plan, self.session_id)
        return prepare_withdrawal_set(
            plan,
            [self.session_id],
            read_event=lambda event_id: {
                "id": event_id,
                "name": "superseded",
                "start_date_local": "2026-08-13T06:00:00",
                "external_id": owned,
            },
            **kwargs,
        )

    def test_the_account_changes_the_proposal_hash(self):
        """Swapping the account after confirmation must break the approval, not pass it.

        This is the same property `direction` has: a field inside the hash cannot be
        edited into a set that still validates.
        """
        first = self.prepared(target_account=self.binding)
        second = self.prepared(
            target_account=connected_account.binding("intervals", "i2", hmac_key=HMAC_KEY)
        )
        self.assertNotEqual(first["proposal_hash"], second["proposal_hash"])

        tampered = {**first, "target_account": second["target_account"]}
        with self.assertRaises(DeliveryError):
            prepare_delivery_set.__globals__["_validate_delivery_set"](tampered)

    def test_a_set_prepared_without_an_account_is_still_a_valid_set(self):
        # The single-user CLI writes the one account its own credentials name; binding is
        # what a gateway serving several athletes adds on top.
        unbound = self.prepared()
        self.assertNotIn("target_account", unbound)
        prepare_delivery_set.__globals__["_validate_delivery_set"](unbound)

    def test_a_malformed_account_is_refused_at_preparation(self):
        for bad in ({"provider": "intervals"}, {"provider": "intervals", "account_ref": ""}):
            with self.assertRaises(DeliveryError):
                self.prepared(target_account=bad)

    def test_a_withdrawal_carries_the_same_binding_in_the_same_hash(self):
        # Deleting from the wrong calendar is the same mistake as writing to it, so the
        # opposite direction is bound the same way.
        first = self.withdrawal(target_account=self.binding)
        second = self.withdrawal(
            target_account=connected_account.binding("intervals", "i2", hmac_key=HMAC_KEY)
        )

        self.assertEqual(self.binding, first["target_account"])
        self.assertNotEqual(first["proposal_hash"], second["proposal_hash"])
        prepare_withdrawal_set.__globals__["_validate_withdrawal_set"](first)
        with self.assertRaises(DeliveryError):
            prepare_withdrawal_set.__globals__["_validate_withdrawal_set"](
                {**first, "target_account": second["target_account"]}
            )


class ConnectedAccountRouteTests(GatewayTestCase):
    """Two accounts on one deployment, told apart by the bearer and nothing else."""

    def setUp(self):
        super().setUp()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.fake.profile_by_token = {
            TOKEN_A: dict(FIRST_LABEL),
            TOKEN_B: dict(SECOND_LABEL),
        }

    def account_for(self, token: str) -> dict[str, Any]:
        status, payload = self.route("permissions", token=token)
        self.assertEqual(200, status, payload)
        return payload["connected_account"]

    def test_each_token_reports_its_own_account_and_never_the_others(self):
        first = self.account_for(TOKEN_A)
        second = self.account_for(TOKEN_B)

        self.assertEqual("resolved", first["resolution"])
        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete", first["label"]
        )
        self.assertEqual("resolved", second["resolution"])
        self.assertEqual(
            "Intervals.icu — " + SECOND_FIXTURE_EMAIL + " — Review Account",
            second["label"],
        )
        self.assertNotIn(SECOND_FIXTURE_EMAIL, json.dumps(first))
        self.assertNotIn(FIXTURE_EMAIL, json.dumps(second))

    def test_the_label_never_carries_an_athlete_id_or_an_owner_id(self):
        rendered = json.dumps(self.account_for(TOKEN_A))
        for forbidden in ("i1", "i2", self.owner_a, self.owner_b, TOKEN_A):
            self.assertNotIn(forbidden, rendered)

    def test_a_denied_profile_read_is_reported_rather_than_named(self):
        """A connection whose Settings permission was refused still answers -- honestly.

        The two live probes are the diagnostic's job and still run; what is lost is only
        the ability to say which account this is, and losing it is what gets reported.
        """
        self.fake.profile_status = 403

        account = self.account_for(TOKEN_A)

        self.assertEqual("unavailable", account["resolution"])
        self.assertEqual("profile_unreadable", account["reason"])
        self.assertNotIn("label", account)

    def test_a_profile_that_names_another_athlete_is_a_mismatch_not_a_label(self):
        self.fake.profile_by_token = {TOKEN_A: dict(SECOND_LABEL)}

        account = self.account_for(TOKEN_A)

        self.assertEqual("mismatch", account["resolution"])
        self.assertNotIn("label", account)
        self.assertNotIn(SECOND_FIXTURE_EMAIL, json.dumps(account))

    def test_no_account_argument_is_accepted_from_the_caller(self):
        """The bearer decides whose account this is; a body cannot suggest another one."""
        status, payload = self.route(
            "permissions",
            body={"athlete_id": "i2", "email": SECOND_FIXTURE_EMAIL},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete",
            payload["connected_account"]["label"],
        )


class DeliveryTargetAccountTests(GatewayTestCase):
    """What a delivery says about its destination, and what that promise is worth."""

    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]
        self.fake.profile_by_token = {
            TOKEN_A: dict(FIRST_LABEL),
            TOKEN_B: dict(SECOND_LABEL),
        }

    def prepare(self, token: str, **extra: Any) -> dict[str, Any]:
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": self.plan["plan_id"],
                "plan_version": self.plan["version"],
                "session_ids": [DELIVERABLE_SESSION],
                **extra,
            },
            token=token,
        )
        self.assertEqual(200, status, prepared)
        return prepared

    def apply(self, token: str, prepared: dict[str, Any]) -> tuple[int, Any]:
        return self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=token,
        )

    def test_the_preview_names_the_account_before_anything_is_written(self):
        prepared = self.prepare(TOKEN_A)

        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete",
            prepared["target_account"]["label"],
        )
        self.assertTrue(prepared["confirmation_required"])
        # Nothing reached the calendar to produce that sentence.
        self.assertEqual([], self.fake.bulk_calls)

    def test_the_account_the_preview_named_is_the_account_that_was_written(self):
        prepared = self.prepare(TOKEN_A)
        status, published = self.apply(TOKEN_A, prepared)

        self.assertEqual(200, status, published)
        self.assertEqual("intervals_accepted", published["delivery_state"])
        self.assertEqual(prepared["target_account"], published["target_account"])
        # And the write itself went out under that account's own bearer.
        self.assertTrue(self.fake.bulk_calls)
        self.assertEqual("Bearer " + TOKEN_A, self.fake.authorizations[-1])

    def test_a_set_previewed_for_one_account_cannot_be_written_to_another(self):
        """Preview A, write B -- the failure this whole change exists to make impossible.

        The conversation still holds A's confirmed set; the bearer has become B's, which
        is what re-authorizing as the other account does. Nothing about the set itself
        changed, so only the account binding can catch this.

        It is tempting to read the existing plan binding as already covering this -- an
        apply carrying another owner's `plan_id` would surely be refused as stale. It is
        not, and this was measured rather than argued: with the account check removed,
        this exact call answers `200`, publishes to Intervals, and steps the *other*
        athlete's stored plan to version 2. The reason is in `plan_init.py`: a plan id is
        `hash(initialization_request, issued_at)` and carries no owner, so one person
        onboarding their normal account and their review account with the same words in
        the same second gets one plan id twice. The plan binding was never an account
        boundary; relying on it would be relying on a hash collision not happening.
        """
        prepared = self.prepare(TOKEN_A)
        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete",
            prepared["target_account"]["label"],
        )
        written_before = len(self.fake.bulk_calls)

        status, refusal = self.apply(TOKEN_B, prepared)

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual(written_before, len(self.fake.bulk_calls))
        # And B's own store is untouched: the refusal lands before any reservation.
        self.assertIsNone(
            json.loads(
                json.dumps(self.route("state", body={}, token=TOKEN_B)[1])
            )["pending_delivery_attempt_id"]
        )

    def test_the_refused_set_still_works_for_the_account_it_was_prepared_for(self):
        # A refusal about accounts must not consume the approval: the athlete who was
        # shown this preview can still confirm it.
        prepared = self.prepare(TOKEN_A)
        self.assertEqual(409, self.apply(TOKEN_B, prepared)[0])

        status, published = self.apply(TOKEN_A, prepared)

        self.assertEqual(200, status, published)
        self.assertEqual("intervals_accepted", published["delivery_state"])

    def test_a_set_carrying_no_account_binding_is_refused_rather_than_trusted(self):
        """What a set prepared by an older build looks like: one field short.

        Its own `proposal_hash` still matches its content, so nothing else on the apply
        path notices. "Which account" is not a field to guess at, so it is re-prepared.
        """
        prepared = self.prepare(TOKEN_A)
        unbound = {
            key: value
            for key, value in prepared["delivery_set"].items()
            if key != "target_account"
        }

        status, refusal = self.route(
            "delivery_apply",
            body={
                "delivery_set": unbound,
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_a_caller_supplied_account_changes_nothing_about_the_target(self):
        """The bearer decides the destination; an argument beside it is not an input.

        `prepareWorkoutDelivery` reads four fields and this is not one of them, so the
        claim is that sending it is inert rather than authoritative -- checked against the
        set that comes back, not against the schema that omits it.
        """
        honest = self.prepare(TOKEN_A)
        suggested = self.prepare(
            TOKEN_A,
            target_account={"provider": "intervals", "account_ref": "whatever-i-say"},
            athlete_id="i2",
        )

        self.assertEqual(
            honest["delivery_set"]["target_account"],
            suggested["delivery_set"]["target_account"],
        )
        self.assertEqual(honest["target_account"], suggested["target_account"])

    def test_an_invented_account_handle_is_refused_at_the_write(self):
        # The handle is keyed to this deployment, so a caller cannot compute one for any
        # account -- including their own. Editing it also breaks the confirmed hash, and
        # this refuses before that is even reached.
        prepared = self.prepare(TOKEN_A)
        forged = {
            **prepared["delivery_set"],
            "target_account": {"provider": "intervals", "account_ref": "f" * 64},
        }

        status, refusal = self.route(
            "delivery_apply",
            body={
                "delivery_set": forged,
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_each_account_delivers_to_its_own_calendar_in_one_deployment(self):
        """Both athletes, interleaved, over the one transport a real deployment shares."""
        a_prepared = self.prepare(TOKEN_A)
        b_prepared = self.prepare(TOKEN_B)

        self.assertNotEqual(
            a_prepared["delivery_set"]["target_account"],
            b_prepared["delivery_set"]["target_account"],
        )
        self.assertEqual(
            "Intervals.icu — " + SECOND_FIXTURE_EMAIL + " — Review Account",
            b_prepared["target_account"]["label"],
        )

        status, b_published = self.apply(TOKEN_B, b_prepared)
        self.assertEqual(200, status, b_published)
        self.assertEqual("Bearer " + TOKEN_B, self.fake.authorizations[-1])
        self.assertEqual(b_prepared["target_account"], b_published["target_account"])

        status, a_published = self.apply(TOKEN_A, a_prepared)
        self.assertEqual(200, status, a_published)
        self.assertEqual("Bearer " + TOKEN_A, self.fake.authorizations[-1])
        self.assertEqual(a_prepared["target_account"], a_published["target_account"])

    def test_the_set_binds_the_account_without_naming_it(self):
        prepared = self.prepare(TOKEN_A)
        binding = prepared["delivery_set"]["target_account"]

        self.assertEqual({"provider", "account_ref"}, set(binding))
        rendered = json.dumps(prepared["delivery_set"])
        for forbidden in ("i1", FIXTURE_EMAIL, "Fixture Athlete", self.owner_a):
            self.assertNotIn(forbidden, rendered)

    def test_the_account_is_checked_before_the_direction_is_even_read(self):
        """One gate covers both directions, because it runs before the dispatch on them.

        A withdrawal removes events from the same calendar a delivery writes to, so it
        must be refused for a stranger's bearer on the same terms. Rather than restate the
        whole withdrawal fixture here, this pins the ordering the shared gate rests on:
        the set below never gets as far as being validated as a withdrawal.
        """
        prepared = self.prepare(TOKEN_A)
        as_withdrawal = {**prepared["delivery_set"], "direction": "withdraw"}

        status, refusal = self.route(
            "delivery_apply",
            body={
                "delivery_set": as_withdrawal,
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_B,
        )

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual([], self.fake.deleted)

    def test_a_delivery_stops_when_the_provider_answers_for_another_athlete(self):
        """The two sources of "whose calendar" disagreeing is the harm, not a label bug.

        Every provider write goes to the token's own account and every store read goes to
        the registered one. A profile naming a different athlete means those are not the
        same account, so a delivery would put a workout on one person's calendar and
        record it against another's. Refusing is the only answer that is not a guess.
        """
        self.fake.profile_by_token = {TOKEN_A: dict(SECOND_LABEL)}

        status, refusal = self.route(
            "delivery_prepare",
            body={
                "plan_id": self.plan["plan_id"],
                "plan_version": self.plan["version"],
                "session_ids": [DELIVERABLE_SESSION],
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_a_disagreement_that_appears_after_the_preview_stops_the_write(self):
        # The preview was honest when it was shown; the provider started answering for
        # somebody else in between. The confirmed set is still the athlete's, and the
        # write still must not happen.
        prepared = self.prepare(TOKEN_A)
        self.fake.profile_by_token = {TOKEN_A: dict(SECOND_LABEL)}

        status, refusal = self.apply(TOKEN_A, prepared)

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_a_delivery_that_cannot_name_its_account_still_delivers(self):
        """The label is a courtesy; losing it must not cost the athlete their workout.

        Which account this writes to was settled by the identity registry before the
        provider was asked anything, so a profile read that fails costs the words and
        nothing else.
        """
        self.fake.profile_status = 500

        prepared = self.prepare(TOKEN_A)
        self.assertEqual("unavailable", prepared["target_account"]["resolution"])

        status, published = self.apply(TOKEN_A, prepared)

        self.assertEqual(200, status, published)
        self.assertEqual("intervals_accepted", published["delivery_state"])
        self.assertEqual("unavailable", published["target_account"]["resolution"])

    def test_a_profile_that_names_no_athlete_costs_the_label_and_not_the_delivery(self):
        """The unreadable-shaped failure the fix in `describe` had to stop refusing.

        A response with no usable `id` cannot say whose account this is -- but it also
        cannot say it is somebody else's, which is the only thing that may stop a write.
        Before the fix this was classified `mismatch` and hard-refused every delivery.
        """
        self.fake.profile_by_token[TOKEN_A] = {
            "name": "Fixture Athlete", "email": FIXTURE_EMAIL
        }

        prepared = self.prepare(TOKEN_A)
        self.assertEqual("unavailable", prepared["target_account"]["resolution"])
        self.assertEqual(
            connected_account.PROFILE_WITHOUT_ATHLETE_ID,
            prepared["target_account"]["reason"],
        )

        status, published = self.apply(TOKEN_A, prepared)

        self.assertEqual(200, status, published)
        self.assertEqual("intervals_accepted", published["delivery_state"])
        # And it carried none of the label it could not verify.
        self.assertNotIn(FIXTURE_EMAIL, json.dumps(published["target_account"]))

    def test_the_email_reaches_the_answer_and_nothing_that_is_kept(self):
        """On demand, for one response. Not the store, not the registry, not a log."""
        self.log_handler.records.clear()
        prepared = self.prepare(TOKEN_A)
        status, published = self.apply(TOKEN_A, prepared)
        self.assertEqual(200, status, published)
        self.assertIn(FIXTURE_EMAIL, json.dumps(prepared["target_account"]))

        logged = "\n".join(self.log_handler.records)
        self.assertNotIn(FIXTURE_EMAIL, logged)
        self.assertNotIn("Fixture Athlete", logged)

        for path in sorted(self.state_root.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            self.assertNotIn(FIXTURE_EMAIL.encode(), data, path)
            self.assertNotIn(b"Fixture Athlete", data, path)

    def test_the_export_an_athlete_asks_for_holds_no_label_either(self):
        self.apply(TOKEN_A, self.prepare(TOKEN_A))
        status, export = self.route("data_export", body={}, token=TOKEN_A)

        self.assertEqual(200, status, export)
        rendered = json.dumps(export)
        self.assertNotIn(FIXTURE_EMAIL, rendered)
        self.assertNotIn("Fixture Athlete", rendered)

    def test_the_usage_counters_the_operator_reads_hold_no_label(self):
        """The fifth surface. A counter is what an operator reads without an athlete.

        `read-usage-stats.md` promises the operator's own report names nobody. Every other
        scan here is about state; this one is about the one place a label could reach an
        operator's terminal by being counted rather than by being stored.
        """
        self.apply(TOKEN_A, self.prepare(TOKEN_A))
        self.route("permissions", body={}, token=TOKEN_A)

        rendered = json.dumps(activity_report(self.identity_db))

        self.assertNotIn(FIXTURE_EMAIL, rendered)
        self.assertNotIn("Fixture Athlete", rendered)


class ResumedDeliveryAccountTests(GatewayTestCase):
    """A reservation outlives the conversation that opened it. The bearer may not have.

    `resume_attempt_id` re-derives the approved set from the plan the reservation is bound
    to, which makes it the one delivery set nobody in the conversation is holding. So it
    is also the set most exposed to "which account is this now": the athlete may have
    reconnected as their review account since, and the reservation itself says nothing
    about accounts.
    """

    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]
        self.fake.profile_by_token = {
            TOKEN_A: dict(FIRST_LABEL),
            TOKEN_B: dict(SECOND_LABEL),
        }

    def session(self, token: str = TOKEN_A) -> dict[str, Any]:
        status, payload = self.route("session", body={"read": "all"}, token=token)
        self.assertEqual(200, status, payload)
        return payload

    def interrupt(self) -> None:
        """Leave account A's store exactly as a delivery killed after one write does."""
        current = self.session()
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01", "run-long-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        self.fake.corrupt_external_ids.add(prepared["preview"][1]["owned_external_id"])
        status, published = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, published)
        self.assertTrue(published["attempt_open"])
        self.fake.corrupt_external_ids.clear()

    def resume(self) -> dict[str, Any]:
        outstanding = self.session()["delivery"]["unresolved_delivery"]
        status, prepared = self.route(
            "delivery_prepare",
            body={"resume_attempt_id": outstanding["attempt_id"]},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        return prepared

    def test_a_resumed_set_carries_the_binding_and_names_the_account(self):
        """Without this the resume route is dead: an unbound set is refused at apply.

        The binding is re-derived from the identity registry rather than read off the
        reservation, which holds no account at all -- so the account a resume writes to is
        the account confirming it, checked, rather than the account that opened it,
        assumed.
        """
        self.interrupt()

        prepared = self.resume()

        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete",
            prepared["target_account"]["label"],
        )
        self.assertEqual(
            connected_account.binding("intervals", "i1", hmac_key=HMAC_KEY),
            prepared["delivery_set"]["target_account"],
        )

        status, applied = self.route(
            "delivery_apply",
            body={"proposal_hash": prepared["proposal_hash"], "confirmed": True},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, applied)
        self.assertFalse(applied["attempt_open"])
        self.assertEqual(prepared["target_account"], applied["target_account"])

    def test_a_resume_confirmed_under_another_bearer_reaches_no_calendar(self):
        """The other account cannot spend this approval, by any of the three routes.

        Named alone it is not held for them, resent it is refused as a resume, and behind
        both stands the account binding -- checked before any reservation would open. What
        matters to the athlete is the same on all three: account B's store gains no
        reservation, and nothing reaches Intervals.
        """
        self.interrupt()
        prepared = self.resume()
        writes_before = len(self.fake.bulk_calls)

        by_name = self.route(
            "delivery_apply",
            body={"proposal_hash": prepared["proposal_hash"], "confirmed": True},
            token=TOKEN_B,
        )
        resent = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_B,
        )

        for status, refusal in (by_name, resent):
            self.assertEqual(409, status, refusal)
            self.assertIn(
                refusal["error"], {"proposal_expired", "account_mismatch"}, refusal
            )
        self.assertEqual(writes_before, len(self.fake.bulk_calls))
        self.assertIsNone(
            self.session(token=TOKEN_B)["delivery"]["unresolved_delivery"]
        )
        # And account A's own reservation is exactly as it was: refusing somebody else
        # must not finish, or abandon, the delivery the athlete is still holding.
        self.assertIsNotNone(self.session()["delivery"]["unresolved_delivery"])

    def test_a_resume_stops_when_the_provider_answers_for_another_athlete(self):
        """The one account disagreement a resume can still meet after its preview.

        A reservation is where the longest time passes between preview and confirmation,
        so it is where the token most plausibly stops reaching the account this connection
        is registered as. Every provider write goes to the token's account and every store
        read goes to the registered one; a resume across that split would finish one
        person's reservation on another person's calendar.
        """
        self.interrupt()
        prepared = self.resume()
        writes_before = len(self.fake.bulk_calls)
        self.fake.profile_by_token = {TOKEN_A: dict(SECOND_LABEL)}

        status, refusal = self.route(
            "delivery_apply",
            body={"proposal_hash": prepared["proposal_hash"], "confirmed": True},
            token=TOKEN_A,
        )

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual(writes_before, len(self.fake.bulk_calls))
        # The reservation is left exactly as it was found, so the athlete can still
        # finish it once the connection is naming the right account again.
        self.assertIsNotNone(self.session()["delivery"]["unresolved_delivery"])


class DecisionCalendarAccountTests(GatewayTestCase):
    """The other way a workout reaches Intervals: one plan change carrying its delivery.

    `applyCoachDecision` writes the same calendar `applyWorkoutDelivery` does, under one
    confirmation covering both. Binding one and not the other would leave the account
    boundary in place on the route that says "deliver" and absent on the route that says
    "change the plan, and deliver what changed".
    """

    def setUp(self):
        super().setUp()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]
        self.fake.profile_by_token = {
            TOKEN_A: dict(FIRST_LABEL),
            TOKEN_B: dict(SECOND_LABEL),
        }

    def prepare(self, token: str = TOKEN_A) -> dict[str, Any]:
        status, session = self.route("session", body={"read": "all"}, token=token)
        self.assertEqual(200, status, session)
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
                "publish_new_workouts": True,
            },
            token=token,
        )
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["preview"]["calendar_delivery"]["workouts"])
        return prepared

    def apply(self, prepared: dict[str, Any], token: str = TOKEN_A):
        return self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=token,
        )

    def held_effects(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """The exact prepared effects this proposal is holding, as the apply reads them."""
        claims = open_proposal(
            prepared["proposal"], key=HMAC_KEY, now=self.now
        )["claims"]
        return self.gateway._calendar_for_confirmation(
            self.owner_a, prepared["proposal"], claims
        )["prepared"]

    def test_the_preview_names_the_account_it_would_write_to(self):
        prepared = self.prepare()

        self.assertEqual(
            "Intervals.icu — " + FIXTURE_EMAIL + " — Fixture Athlete",
            prepared["preview"]["calendar_delivery"]["target_account"]["label"],
        )
        self.assertEqual([], self.fake.bulk_calls)

    def test_every_prepared_effect_carries_the_binding(self):
        prepared = self.prepare()
        expected = connected_account.binding("intervals", "i1", hmac_key=HMAC_KEY)

        effects = self.held_effects(prepared)["effects"]

        self.assertTrue(effects)
        for effect in effects:
            self.assertEqual(expected, effect["set"]["target_account"])

    def test_effects_prepared_for_one_account_are_refused_for_another(self):
        """The gate itself, on the effects a confirmation actually carries.

        End to end the other account never gets this far -- the signed proposal is bound
        to its owner and the held preview is per-owner, so the cross-bearer confirmation
        is refused before this. That is exactly why the gate is worth pinning directly:
        it is the layer that would answer if either of those moved, and it answers before
        the transport is built.
        """
        effects = self.held_effects(self.prepare())

        with self.assertRaises(GatewayError) as raised:
            self.gateway._require_approved_calendar_account(self.owner_b, effects)

        self.assertEqual("account_mismatch", raised.exception.payload()["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_a_confirmation_stops_when_the_provider_answers_for_another_athlete(self):
        prepared = self.prepare()
        self.fake.profile_by_token = {TOKEN_A: dict(SECOND_LABEL)}

        status, refusal = self.apply(prepared)

        self.assertEqual(409, status, refusal)
        self.assertEqual("account_mismatch", refusal["error"])
        self.assertEqual([], self.fake.bulk_calls)
        self.assertIsNone(pending_delivery_attempt(self.owner_dir(self.owner_a)))
        # And the plan did not move either: the refusal lands before the commit.
        self.assertEqual(
            1, read_current_plan(self.owner_dir(self.owner_a))["current_version"]
        )

    def test_a_label_that_stops_resolving_never_costs_the_plan_change(self):
        """Why the label is named after the preview is hashed, and not inside it.

        `preview_hash` is what says the athlete confirmed the words they were shown. A
        live provider read inside it would make one failed profile request at confirmation
        time refuse a plan change the athlete already approved -- an outage deciding
        whether their week saves. The binding is what the confirmation rests on; the label
        is what it says out loud.
        """
        prepared = self.prepare()
        self.fake.profile_status = 500

        status, applied = self.apply(prepared)

        self.assertEqual(200, status, applied)
        self.assertEqual("passed", applied["status"])
        self.assertEqual(
            "unavailable", applied["calendar_delivery"]["target_account"]["resolution"]
        )
        self.assertTrue(applied["calendar_delivery"]["delivered"])

    def test_decision_apply_survives_a_profile_body_timeout(self):
        """The same promise, against the failure shape a provider outage really produces.

        The test above injects an HTTP status, which arrives as `HTTPError` and was always
        caught. A connection that answers, starts the body and then stalls raises a bare
        `TimeoutError` -- not a `URLError`, so it escaped the module's except list, became
        a `500`, and lost the plan change the athlete had already confirmed.
        """
        prepared = self.prepare()
        provider = self.gateway.fetch

        def stalled(request):
            if request.get_method() == "GET" and request.full_url.endswith("/athlete/0"):
                raise TimeoutError("timed out")
            return provider(request)

        self.gateway.fetch = stalled

        status, applied = self.apply(prepared)

        self.assertEqual(200, status, applied)
        # The decision commit and the delivery-state commit that follows it, both kept.
        self.assertEqual(3, read_current_plan(self.owner_dir(self.owner_a))["current_version"])
        self.assertEqual(
            "unavailable", applied["calendar_delivery"]["target_account"]["resolution"]
        )
        self.assertTrue(applied["calendar_delivery"]["delivered"])

    def test_an_unbound_set_and_a_foreign_one_are_not_told_the_same_story(self):
        """Two facts, two sentences -- the way the delivery route already says it.

        "Prepared for nobody" and "prepared for somebody else" send a reader looking for
        different things, and collapsing them into the second sends whoever reads it
        hunting a second Intervals account that does not exist.
        """
        effects = self.held_effects(self.prepare())
        unbound = copy.deepcopy(effects)
        for effect in unbound["effects"]:
            effect["set"].pop("target_account", None)

        with self.assertRaises(GatewayError) as foreign:
            self.gateway._require_approved_calendar_account(self.owner_b, effects)
        with self.assertRaises(GatewayError) as absent:
            self.gateway._require_approved_calendar_account(self.owner_a, unbound)

        self.assertEqual(
            "account_mismatch", foreign.exception.payload()["error"]
        )
        self.assertEqual("account_mismatch", absent.exception.payload()["error"])
        self.assertIn("a different Intervals account", foreign.exception.payload()["detail"])
        self.assertIn("before this gateway bound a set", absent.exception.payload()["detail"])
        self.assertNotEqual(
            foreign.exception.payload()["detail"], absent.exception.payload()["detail"]
        )


class StoredCalendarReplayTests(GatewayTestCase):
    """Retrying a plan change that already committed, when its binding cannot be re-derived.

    `applyCoachDecision` documents one recovery for a delivery Intervals only half
    accepted: retry the same proposal, no second confirmation. That retry reads the
    *stored* effects back out of the store, so it meets whatever binding was written at
    the time -- which may be none (a build older than the binding) or one computed under a
    `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` this deployment has since rotated away from.

    Neither is a question the athlete can answer. The yes was given, the plan moved, and
    re-preparing produces a different change against a plan that has already changed. So
    on this path the binding is not re-derived at all; the live provider check, which asks
    something the athlete's own connection can still get wrong, stays.
    """

    def setUp(self):
        super().setUp()
        self.owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]
        self.fake.profile_by_token = {TOKEN_A: dict(FIRST_LABEL)}

    def prepare(self) -> dict[str, Any]:
        status, session = self.route("session", body={"read": "all"}, token=TOKEN_A)
        self.assertEqual(200, status, session)
        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
                "publish_new_workouts": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["preview"]["calendar_delivery"]["workouts"])
        return prepared

    def apply(self, prepared: dict[str, Any]) -> tuple[int, Any]:
        return self.route(
            "decision_apply",
            body={"proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

    def stored_bindings(self) -> list[Any]:
        """The distinct bindings the committed effects actually carry, if any."""
        found: list[Any] = []
        for path in sorted(self.owner_dir(self.owner_a).rglob("receipt.json")):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            confirmed = receipt.get("confirmed_delivery") or {}
            for effect in (confirmed.get("prepared") or {}).get("effects") or []:
                target = (effect.get("set") or {}).get("target_account")
                if target not in found:
                    found.append(target)
        return found

    def test_a_delivery_committed_before_the_binding_existed_can_still_be_retried(self):
        """The upgrade case: effects written by a build that bound no account at all.

        Nothing about them is wrong. They were confirmed, the plan committed, and the only
        documented way to converge a half-accepted delivery is to send the same proposal
        again. Refusing that permanently is what the strict rule did once it reached this
        path -- and it said the effects were "prepared for a different Intervals account"
        when they were prepared for none.
        """
        with mock.patch.object(gw.CoachGateway, "_require_account_binding", return_value=None):
            prepared = self.prepare()
        with mock.patch.object(
            gw.CoachGateway, "_require_approved_calendar_account", return_value=None
        ):
            status, applied = self.apply(prepared)
        self.assertEqual(200, status, applied)
        self.assertEqual([None], self.stored_bindings())
        delivered = len(self.fake.events)

        status, retried = self.apply(prepared)

        self.assertEqual(200, status, retried)
        self.assertTrue(retried["idempotent_replay"])
        self.assertEqual(
            "resolved", retried["calendar_delivery"]["target_account"]["resolution"]
        )
        # Converged, not duplicated: the retry is the same approved set, once.
        self.assertEqual(delivered, len(self.fake.events))

    def test_a_stored_handle_this_deployment_cannot_recompute_is_not_a_refusal(self):
        """The general shape of the same defect: a handle that no longer matches.

        `account_ref` is keyed by the deployment key, so rotating
        `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` changes every stored handle while the account
        stays the same. Measured end to end, a real rotation stops earlier than this --
        the proposal naming these effects was signed under the old key and no longer
        opens, so the retry answers `plan_state_exists` rather than reaching here. That is
        why this is pinned at the layer that owns it rather than as a rotation scenario:
        the reachable trigger is the upgrade above, and both are one rule -- a stored
        handle is never what refuses a retry of a confirmation already given.
        """
        stale = connected_account.binding(
            "intervals", "i1", hmac_key=b"a-previous-deployment-key-000000"
        )
        with mock.patch.object(gw.CoachGateway, "_account_binding", return_value=stale):
            prepared = self.prepare()
            status, applied = self.apply(prepared)
        self.assertEqual(200, status, applied)
        self.assertEqual([stale], self.stored_bindings())
        self.assertNotEqual(
            stale, connected_account.binding("intervals", "i1", hmac_key=HMAC_KEY)
        )
        delivered = len(self.fake.events)

        status, retried = self.apply(prepared)

        self.assertEqual(200, status, retried)
        self.assertTrue(retried["idempotent_replay"])
        self.assertEqual(delivered, len(self.fake.events))

    def test_the_replay_still_stops_when_the_provider_answers_for_another_athlete(self):
        """What the replay path does *not* drop.

        The binding asks a question the athlete can no longer act on. This one is live and
        still true: if Intervals is answering for a different athlete than this connection
        is registered as, retrying writes one person's calendar against another's record.
        """
        prepared = self.prepare()
        status, applied = self.apply(prepared)
        self.assertEqual(200, status, applied)
        delivered = len(self.fake.events)
        self.fake.profile_by_token = {TOKEN_A: dict(SECOND_LABEL)}

        status, refused = self.apply(prepared)

        self.assertEqual(409, status, refused)
        self.assertEqual("account_mismatch", refused["error"])
        self.assertEqual(delivered, len(self.fake.events))

    def test_the_strict_rule_still_holds_everywhere_the_athlete_can_still_answer(self):
        """The relaxation is the replay path and nothing else.

        Before the commit an unbound set is still refused, because preparing it again is
        a thing the athlete can actually do and the answer they get back names the
        account.
        """
        with mock.patch.object(gw.CoachGateway, "_require_account_binding", return_value=None):
            prepared = self.prepare()

        status, refused = self.apply(prepared)

        self.assertEqual(409, status, refused)
        self.assertEqual("account_mismatch", refused["error"])
        self.assertEqual([], self.fake.bulk_calls)
        self.assertEqual(
            1, read_current_plan(self.owner_dir(self.owner_a))["current_version"]
        )

    def test_the_committed_plan_holds_no_label(self):
        """The effects are persisted with the plan; the label is not part of them."""
        status, applied = self.apply(self.prepare())
        self.assertEqual(200, status, applied)

        for path in sorted(self.owner_dir(self.owner_a).rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            self.assertNotIn(FIXTURE_EMAIL.encode(), data, path)
            self.assertNotIn(b"Fixture Athlete", data, path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
