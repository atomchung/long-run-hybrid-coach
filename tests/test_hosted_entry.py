"""Hosted-first: one plan per athlete across clients, and a local client that reaches it.

Three questions, all answered against the real loopback server rather than against the
gateway object, because what has to hold is what a client would actually receive:

- Two independently authorized clients for one Intervals athlete read and write **one**
  PlanState, and authorizing the second does not log the first out (issue #40's
  acceptance criteria).
- Two different athletes cannot see, name, or change each other's state.
- The client in this repository -- the one `hosted-session` uses, and the one a local MCP
  client's flow looks like -- completes registration, authorization, redemption and a tool
  call over HTTP, and ends the run with the token nowhere on disk.

The harness is `test_gateway`'s: a real server over one injected fetcher, so nothing here
reaches a network.
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import hosted
from garmin_coach_loop.gateway import INTERVALS_AUTHORIZE_URL, INTERVALS_OAUTH_SCOPES
from garmin_coach_loop.hosted import HostedClient, HostedEntryError
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_for_fingerprint,
    revoke_owner_connections,
    token_fingerprint,
)
from garmin_coach_loop.store import init_store, read_current_plan

from test_gateway import HMAC_KEY, TOKEN_A, TOKEN_B, WEEKLY_CHANGE, load, publishable_plan
from test_mcp_gateway import CLIENT_REDIRECT_URI, CODE_CHALLENGE, McpTestCase


class HostedFlowTestCase(McpTestCase):
    """One athlete, one client, one whole OAuth flow -- driven the way a client drives it."""

    def client(self) -> HostedClient:
        return HostedClient(self.base_url)

    def open_browser(self, url: str, *, provider_token: str, athlete_id: str) -> None:
        """Play the athlete's browser: consent at Intervals, come back to the client.

        Every hop is a real HTTP request against the real server. Only the Intervals
        consent page is missing, and it is the one hop that is a human at a vendor's
        website rather than anything this product serves.
        """
        self.fake.token_payload = {
            "access_token": provider_token,
            "scope": ",".join(INTERVALS_OAUTH_SCOPES),
            "athlete": {"id": athlete_id},
        }
        status, headers, _ = self.fetch(url)
        self.assertEqual(302, status)
        intervals = headers["Location"]
        self.assertTrue(intervals.startswith(INTERVALS_AUTHORIZE_URL), intervals)
        state = self.query_of(intervals)["state"]
        status, headers, _ = self.fetch(
            self.base_url
            + "/oauth/callback?"
            + urllib.parse.urlencode({"code": "provider-code-1", "state": state})
        )
        self.assertEqual(302, status)
        # Back to the client's own loopback callback, which is listening in this process.
        status, _, _ = self.fetch(headers["Location"])
        self.assertEqual(200, status)

    def fetch(self, url: str) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, method="GET")
        return (
            lambda response: (response.status, response.headers, response.body)
        )(hosted._default_transport(request))

    @staticmethod
    def query_of(location: str) -> dict[str, str]:
        return {
            key: values[0]
            for key, values in urllib.parse.parse_qs(
                urllib.parse.urlsplit(location).query
            ).items()
        }

    def connect_as(
        self, *, provider_token: str = TOKEN_A, athlete_id: str = "i1"
    ) -> hosted.HostedConnection:
        """One complete authorization through this repository's own client."""
        return hosted.connect(
            self.base_url,
            open_url=lambda url: self.open_browser(
                url, provider_token=provider_token, athlete_id=athlete_id
            ),
            timeout=10,
        )

    def owner_directories(self) -> list[str]:
        owners = self.state_root / "owners"
        return sorted(path.name for path in owners.iterdir()) if owners.is_dir() else []


class TwoClientsOneAthleteTests(HostedFlowTestCase):
    """One Intervals athlete, two clients, one plan -- which is the whole point of hosted."""

    def test_two_authorized_clients_resolve_to_one_owner_and_one_plan(self):
        plan = publishable_plan()
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            plan,
        )

        first = self.connect_as(provider_token=TOKEN_A)
        # A second client, registering its own callback and authorizing on its own. The
        # provider mints a second access token, exactly as Intervals does for a second
        # connection rather than replacing the first.
        second = self.connect_as(provider_token=TOKEN_B)

        self.assertNotEqual(first.access_token, second.access_token)
        owners = {
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(token, hmac_key=HMAC_KEY)
            )
            for token in (TOKEN_A, TOKEN_B)
        }
        self.assertEqual(1, len(owners), owners)
        self.assertEqual(1, len(self.owner_directories()))

        for connection in (first, second):
            refused, payload = connection.call_tool("startCoachSession", {"all_clear": True})
            self.assertFalse(refused, payload)
            self.assertEqual(plan["plan_id"], payload["plan_state"]["plan_id"])
            self.assertEqual(1, payload["plan_state"]["plan_version"])

    def test_a_decision_through_one_client_is_what_the_other_reads(self):
        plan = load("plan-state-v1.json")
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            plan,
        )
        first = self.connect_as(provider_token=TOKEN_A)
        second = self.connect_as(provider_token=TOKEN_B)

        shared = {
            "plan_id": plan["plan_id"],
            "plan_version": plan["version"],
            "context": load("coach-context-day-4.json"),
            "change_request": WEEKLY_CHANGE,
        }
        refused, prepared = first.call_tool("prepareCoachDecision", shared)
        self.assertFalse(refused, prepared)
        refused, applied = first.call_tool(
            "applyCoachDecision",
            {**shared, "proposal": prepared["proposal"], "confirmed": True},
        )
        self.assertFalse(refused, applied)
        self.assertEqual(2, applied["plan_version"])

        # The other client, in what is a brand-new conversation as far as it is concerned.
        refused, payload = second.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        self.assertEqual(2, payload["plan_state"]["plan_version"])
        self.assertEqual(plan["plan_id"], payload["plan_state"]["plan_id"])

    def test_reauthorizing_keeps_the_first_client_working_and_creates_no_second_store(self):
        plan = publishable_plan()
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            plan,
        )
        first = self.connect_as(provider_token=TOKEN_A)
        before = self.owner_directories()

        # The same athlete comes back tomorrow, on a new token, through a new client.
        second = self.connect_as(provider_token=TOKEN_B)

        self.assertEqual(before, self.owner_directories())
        for connection in (first, second):
            refused, payload = connection.call_tool("startCoachSession", {"all_clear": True})
            self.assertFalse(refused, payload)
            self.assertEqual(1, payload["plan_state"]["plan_version"])

    def test_reauthorization_leaks_nothing_between_the_two_connections(self):
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            publishable_plan(),
        )
        first = self.connect_as(provider_token=TOKEN_A)
        second = self.connect_as(provider_token=TOKEN_B)

        # Neither bearer contains the other's, nor either provider token; each seals its
        # own credential and nothing else.
        for token in (TOKEN_A, TOKEN_B):
            self.assertNotIn(token, first.access_token)
            self.assertNotIn(token, second.access_token)
        self.assertNotIn(first.access_token, second.access_token)
        logged = "\n".join(self.log_handler.records)
        for secret in (TOKEN_A, TOKEN_B, first.access_token, second.access_token):
            self.assertNotIn(secret, logged)


class TwoAthletesStayIsolatedTests(HostedFlowTestCase):
    """Two provider athletes, two owners, and no route between them."""

    def test_each_athlete_reaches_only_their_own_plan(self):
        plan = publishable_plan()
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            plan,
        )
        mine = self.connect_as(provider_token=TOKEN_A, athlete_id="i1")
        theirs = self.connect_as(provider_token=TOKEN_B, athlete_id="i2")

        refused, payload = mine.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        self.assertEqual(plan["plan_id"], payload["plan_state"]["plan_id"])

        # The second athlete has never planned anything. What they must not get is the
        # first athlete's plan, and what they must not be told is that one exists.
        refused, other = theirs.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, other)
        self.assertEqual("no_plan_state", other["status"])
        self.assertFalse(other["plan_state"]["present"])
        self.assertNotIn(plan["plan_id"], json.dumps(other, ensure_ascii=False))
        # Two owners, two directories -- and only the one with a plan exists on disk,
        # because reading an empty account is not allowed to create a store for it.
        owners = {
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(token, hmac_key=HMAC_KEY)
            )
            for token in (TOKEN_A, TOKEN_B)
        }
        self.assertEqual(2, len(owners))
        self.assertEqual(2, len({self.owner_dir(owner) for owner in owners}))
        self.assertEqual(1, len(self.owner_directories()))

    def test_naming_another_athletes_plan_changes_nothing(self):
        plan = load("plan-state-v1.json")
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        init_store(self.owner_dir(owner_id), plan)
        self.connect_as(provider_token=TOKEN_A, athlete_id="i1")
        theirs = self.connect_as(provider_token=TOKEN_B, athlete_id="i2")

        refused, payload = theirs.call_tool(
            "prepareCoachDecision",
            {
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "context": load("coach-context-day-4.json"),
                "change_request": WEEKLY_CHANGE,
            },
        )
        self.assertTrue(refused, payload)
        # And the first athlete's store is untouched: same version, same commit chain.
        self.assertEqual(1, read_current_plan(self.owner_dir(owner_id))["current_version"])


class LocalClientReachesTheGatewayTests(HostedFlowTestCase):
    """The client in this repository, end to end, over HTTP, with nothing left behind."""

    def test_discovery_registration_authorization_and_a_tool_call(self):
        plan = publishable_plan()
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            plan,
        )
        client = self.client()

        resource = client.protected_resource()
        self.assertEqual(f"{self.base_url}/mcp", resource["resource"])
        metadata = client.authorization_server()
        self.assertEqual(f"{self.base_url}/oauth/authorize", metadata["authorization_endpoint"])

        connection = self.connect_as(provider_token=TOKEN_A)
        handshake = connection.client.initialize(connection.access_token)
        self.assertEqual("garmin-coach-loop", handshake["serverInfo"]["name"])
        tools = connection.client.list_tools(connection.access_token)
        self.assertIn("startCoachSession", {tool["name"] for tool in tools})

        refused, payload = connection.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        self.assertEqual(plan["plan_id"], payload["plan_state"]["plan_id"])
        # What Intervals saw is the athlete's own token; what this client holds is not it.
        self.assertNotIn(TOKEN_A, connection.access_token)
        self.assertEqual({f"Bearer {TOKEN_A}"}, set(self.fake.authorizations))

    def test_the_token_is_never_written_down(self):
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            publishable_plan(),
        )
        connection = self.connect_as(provider_token=TOKEN_A)
        connection.call_tool("startCoachSession", {"all_clear": True})

        written = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(Path(self.state_root).rglob("*"))
            if path.is_file() and path.suffix in {".json", ".db"}
        )
        for secret in (connection.access_token, TOKEN_A):
            self.assertNotIn(secret, written)
            self.assertNotIn(secret, "\n".join(self.log_handler.records))

    def test_a_refused_tool_call_comes_back_as_a_readable_refusal(self):
        # No plan, then a change request against one: the gateway refuses, and the client
        # has to hand that refusal back rather than raise something unreadable.
        connection = self.connect_as(provider_token=TOKEN_A)
        refused, payload = connection.call_tool(
            "prepareCoachDecision",
            {
                "plan_id": "plan-5k-2026-08",
                "plan_version": 1,
                "context": load("coach-context-day-4.json"),
                "change_request": WEEKLY_CHANGE,
            },
        )
        self.assertTrue(refused, payload)
        self.assertEqual("blocked", payload["status"])

    def test_an_unknown_bearer_is_refused_as_an_authorization_problem(self):
        client = self.client()
        with self.assertRaises(HostedEntryError) as refusal:
            client.initialize("not-a-token-this-gateway-issued")
        self.assertIn("authorize again", str(refusal.exception))


class AuthorizationBoundaryTests(HostedFlowTestCase):
    """What a client may ask the athlete for, and what taking it back removes."""

    def authorize_query(self, **overrides: str) -> dict[str, str]:
        """Start one authorization and return what the gateway asked Intervals for."""
        query = {
            "response_type": "code",
            "client_id": self.registered_client_id(CLIENT_REDIRECT_URI),
            "redirect_uri": CLIENT_REDIRECT_URI,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state-1",
            "resource": f"{self.base_url}/mcp",
            **overrides,
        }
        status, headers, _ = self.fetch(
            self.base_url + "/oauth/authorize?" + urllib.parse.urlencode(query)
        )
        self.assertEqual(302, status)
        return self.query_of(headers["Location"])

    def test_a_client_cannot_ask_the_athlete_for_more_than_the_coach_uses(self):
        sent = self.authorize_query(scope="ACTIVITY:READ ACTIVITY:WRITE ATHLETE:DELETE")
        self.assertEqual("ACTIVITY:READ", sent["scope"])

    def test_a_request_naming_nothing_this_product_uses_falls_back_to_what_it_declares(self):
        for requested in ("ATHLETE:DELETE", "", "not a scope"):
            with self.subTest(requested=requested):
                sent = self.authorize_query(scope=requested)
                self.assertEqual(",".join(INTERVALS_OAUTH_SCOPES), sent["scope"])

    def test_revoking_connections_ends_every_client_and_leaves_the_plan(self):
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        plan = publishable_plan()
        init_store(self.owner_dir(owner_id), plan)
        first = self.connect_as(provider_token=TOKEN_A)
        second = self.connect_as(provider_token=TOKEN_B)
        for connection in (first, second):
            self.assertFalse(connection.call_tool("startCoachSession", {"all_clear": True})[0])

        removed = revoke_owner_connections(self.identity_db, owner_id)
        self.assertEqual(2, removed["token_fingerprints"])

        # Tokens this gateway already issued stop working too: the envelope is opened, and
        # then the registry -- which is the only answer to "whose store is this" -- has
        # nothing to resolve it to.
        for connection in (first, second):
            with self.assertRaises(HostedEntryError):
                connection.call_tool("startCoachSession", {"all_clear": True})

        # The plan is untouched, and signing in again lands on the same owner and store.
        self.assertEqual(1, read_current_plan(self.owner_dir(owner_id))["current_version"])
        again = self.connect_as(provider_token=TOKEN_A)
        refused, payload = again.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        self.assertEqual(plan["plan_id"], payload["plan_state"]["plan_id"])
        self.assertEqual(
            owner_id,
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            ),
        )
        self.assertEqual(1, len(self.owner_directories()))


class HostedNeverFallsBackToThisMachineTests(HostedFlowTestCase):
    """Serving somebody else must not pick up whatever this machine happens to hold.

    The two machine-wide settings a local run reads are a provider API key and a health
    database. Both name one person. A hosted request carries its own credential and its
    own owner, so consulting either would attach a stranger's account or a stranger's
    strength log to their context.
    """

    def test_a_machine_wide_api_key_and_health_db_are_both_ignored(self):
        plan = publishable_plan()
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            plan,
        )
        machine = {
            "INTERVALS_ICU_API_KEY": "machine-wide-key-not-real",
            "INTERVALS_ICU_ATHLETE_ID": "i-someone-else",
            # A path that does not exist: a *configured* health database that cannot be
            # read is a blocked build, so a successful call is proof this was never read.
            "GARMIN_COACH_LOOP_HEALTH_DB": str(Path(self.state_root) / "nobody.db"),
            "HEALTH_DB_PATH": str(Path(self.state_root) / "nobody.db"),
        }
        with mock.patch.dict(os.environ, machine, clear=False):
            connection = self.connect_as(provider_token=TOKEN_A)
            refused, payload = connection.call_tool(
                "startCoachSession", {"all_clear": True}
            )

        self.assertFalse(refused, payload)
        self.assertEqual(plan["plan_id"], payload["plan_state"]["plan_id"])
        # Only the athlete's own OAuth token ever reached the provider.
        self.assertEqual({f"Bearer {TOKEN_A}"}, set(self.fake.authorizations))
        self.assertNotIn("machine-wide-key-not-real", json.dumps(payload, ensure_ascii=False))
        # And the optional local evidence group reads as unconfigured, not as somebody's.
        self.assertIsNone(payload["context"]["strength_execution"])


class GatewayUrlPolicyTests(unittest.TestCase):
    """What a machine may be pointed at, and what it must say out loud to write locally."""

    def test_plaintext_is_confined_to_this_machine(self):
        self.assertEqual(
            "http://127.0.0.1:8422", hosted.normalize_gateway_url("http://127.0.0.1:8422/")
        )
        with self.assertRaises(HostedEntryError):
            hosted.normalize_gateway_url("http://coach.example")
        with self.assertRaises(HostedEntryError):
            hosted.normalize_gateway_url("https://coach.example/mcp")
        with self.assertRaises(HostedEntryError):
            hosted.normalize_gateway_url("ftp://coach.example")

    def test_a_machine_with_no_hosted_coach_stays_a_local_one(self):
        self.assertEqual(hosted.OFFLINE_MODE, hosted.resolve_entry_mode({}))
        hosted.require_local_store_write("apply-decision", {})

    def test_a_configured_gateway_makes_a_local_write_say_so(self):
        env = {hosted.GATEWAY_URL_ENV_VAR: "https://coach.example"}
        self.assertEqual(hosted.HOSTED_MODE, hosted.resolve_entry_mode(env))
        with self.assertRaises(HostedEntryError) as refusal:
            hosted.require_local_store_write("apply-decision", env)
        self.assertIn("--offline", str(refusal.exception))
        self.assertIn("https://coach.example", str(refusal.exception))
        # Said out loud, it is allowed -- both ways of saying it.
        hosted.require_local_store_write("apply-decision", env, offline=True)
        hosted.require_local_store_write(
            "apply-decision", {**env, hosted.ENTRY_MODE_ENV_VAR: "offline"}
        )

    def test_declaring_hosted_without_a_gateway_is_a_refusal_not_a_guess(self):
        with self.assertRaises(HostedEntryError):
            hosted.resolve_entry_mode({hosted.ENTRY_MODE_ENV_VAR: "hosted"})
        with self.assertRaises(HostedEntryError):
            hosted.resolve_entry_mode({hosted.ENTRY_MODE_ENV_VAR: "whenever"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
