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

import contextlib
import datetime as dt
import io
import json
import os
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop import hosted, token_envelope
from garmin_coach_loop.cli import main
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

        # `connect` itself already ran the MCP lifecycle handshake -- initialize, then
        # notifications/initialized -- as its last step before handing back a connection
        # (see McpLifecycleTests, which watches the wire for that sequence). Calling
        # `initialize` a second time here would be testing a step the production path
        # does not take a second time either.
        connection = self.connect_as(provider_token=TOKEN_A)
        self.assertEqual("2025-06-18", connection.protocol_version)
        tools = connection.client.list_tools(
            connection.access_token, protocol_version=connection.protocol_version
        )
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


class McpLifecycleTests(HostedFlowTestCase):
    """Issue #131: the production path built a connection and went straight to
    ``tools/call``, never sending ``initialize`` at all. These watch the wire for the
    sequence MCP 2025-06-18's lifecycle requires --
    https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle -- via a
    transport spy that still drives the real ``CoachGatewayServer`` underneath, so what
    is asserted is what the real ``hosted-session`` command actually sends, in order,
    with the headers Streamable HTTP requires
    (https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

    A few cases -- a server negotiating a version this client does not speak, one
    answering with an SSE stream -- cannot be produced by this repository's own gateway,
    which always negotiates its one supported version and never opens a stream
    (``mcp_transport.py``'s own docstring). Those run against a fake transport instead,
    and say so.
    """

    @staticmethod
    def _request_header(request: urllib.request.Request, name: str) -> str | None:
        """``Request.get_header`` looks up its argument verbatim rather than
        case-insensitively -- only ``add_header`` normalizes case, and only on write --
        so a header added as ``MCP-Protocol-Version`` has to be read back as
        ``request.headers['Mcp-protocol-version']`` or not found at all. HTTP header
        names carry no case of their own; this reads them that way instead of relying on
        the exact internal spelling ``str.capitalize()`` happens to produce.
        """
        lowered = name.lower()
        for key, value in request.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def _mcp_call_spy(self) -> tuple[list[dict[str, Any]], "hosted.Transport"]:
        """Wrap the real transport to record each ``/mcp`` POST, in order, while still
        handing every request to the real loopback server underneath."""
        original = hosted._default_transport
        calls: list[dict[str, Any]] = []

        def spy(request: urllib.request.Request) -> hosted.HostedResponse:
            if (
                urllib.parse.urlsplit(request.full_url).path == hosted.MCP_PATH
                and request.data
            ):
                body = json.loads(request.data.decode("utf-8"))
                calls.append(
                    {
                        "rpc_method": body.get("method"),
                        "has_id": "id" in body,
                        "accept": self._request_header(request, "Accept"),
                        "protocol_header": self._request_header(
                            request, "MCP-Protocol-Version"
                        ),
                    }
                )
            return original(request)

        return calls, spy

    def _run_hosted_session_cli(self, *, token: str) -> tuple[int, dict[str, Any]]:
        """The real CLI entry point, over the real loopback server, with a held token
        supplied the way a second command in the same session supplies one."""
        out, err = io.StringIO(), io.StringIO()
        environment = {hosted.HOSTED_TOKEN_ENV_VAR: token}
        with mock.patch.dict(os.environ, environment, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["hosted-session", "--gateway", self.base_url])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_the_ordered_lifecycle_runs_before_the_tool_call(self):
        """The real ``hosted-session`` path, driven by a held token from the
        environment -- one of the two authentication paths issue #131 named -- in the
        order 2025-06-18 requires: ``initialize``, then ``notifications/initialized``,
        then (and only then) ``tools/call``."""
        plan = publishable_plan()
        self.seed_owner(TOKEN_A, plan=plan)
        token = self.mcp_bearer(TOKEN_A)
        calls, spy = self._mcp_call_spy()
        with mock.patch.object(hosted, "_default_transport", spy):
            code, report = self._run_hosted_session_cli(token=token)

        self.assertEqual(0, code, report)
        self.assertEqual(plan["plan_id"], report["plan_id"])

        self.assertEqual(
            ["initialize", "notifications/initialized", "tools/call"],
            [call["rpc_method"] for call in calls],
        )
        initialize_call, initialized_call, tool_call = calls

        # Streamable HTTP requires both media types on every POST, regardless of phase.
        for call in calls:
            self.assertEqual("application/json, text/event-stream", call["accept"])

        # Nothing is negotiated yet when `initialize` is sent, so it names no version;
        # everything after it states the version the server just agreed to.
        self.assertIsNone(initialize_call["protocol_header"])
        self.assertEqual("2025-06-18", initialized_call["protocol_header"])
        self.assertEqual("2025-06-18", tool_call["protocol_header"])

        # A notification carries no id; a request, including `initialize` itself, does.
        self.assertTrue(initialize_call["has_id"])
        self.assertFalse(initialized_call["has_id"])
        self.assertTrue(tool_call["has_id"])

    def test_a_failed_initialize_never_reaches_a_tool_call(self):
        """A token the gateway does not recognize fails on the very first MCP
        interaction. Nothing named `initialized` or `tools/call` may follow it."""
        calls, spy = self._mcp_call_spy()
        with mock.patch.object(hosted, "_default_transport", spy):
            code, report = self._run_hosted_session_cli(
                token="not-a-token-this-gateway-issued"
            )

        self.assertEqual(2, code)
        self.assertEqual("blocked", report["status"])
        self.assertIn("authorize again", report["error"])
        self.assertEqual(["initialize"], [call["rpc_method"] for call in calls])

    def test_the_oauth_path_completes_the_same_lifecycle(self):
        """``connect()`` -- the OAuth path, issue #131's other named case -- completes
        the same handshake before it ever hands back a connection a caller could call a
        tool on."""
        init_store(
            self.owner_dir(lookup_or_create_owner(self.identity_db, "intervals", "i1")),
            publishable_plan(),
        )
        calls, spy = self._mcp_call_spy()
        with mock.patch.object(hosted, "_default_transport", spy):
            connection = self.connect_as(provider_token=TOKEN_A)
        self.assertEqual(
            ["initialize", "notifications/initialized"],
            [call["rpc_method"] for call in calls],
        )
        self.assertEqual("2025-06-18", connection.protocol_version)

        # The handshake already happened inside `connect_as`; a tool call is the
        # caller's first one, not a second attempt at the handshake. `connection.client`
        # captured the spy as its transport when it was constructed, so it keeps
        # recording into the same list without needing the patch reapplied.
        calls.clear()
        refused, payload = connection.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        self.assertEqual(["tools/call"], [call["rpc_method"] for call in calls])

    def test_a_negotiated_version_this_client_does_not_speak_is_refused_before_any_tool_call(
        self,
    ):
        """This repository's own gateway always negotiates its one supported version
        (``mcp_transport._negotiated_version``), so a disagreeing server is simulated
        with a fake transport rather than asked of the real one."""
        calls: list[str | None] = []

        def bad_version_transport(request: urllib.request.Request) -> hosted.HostedResponse:
            body = json.loads(request.data.decode("utf-8"))
            calls.append(body.get("method"))
            if body.get("method") != "initialize":
                raise AssertionError(
                    f"no call beyond a failed initialize should happen; got {body!r}"
                )
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "protocolVersion": "1999-01-01",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "garmin-coach-loop", "version": "test"},
                },
            }
            return hosted.HostedResponse(
                200,
                {"Content-Type": "application/json"},
                json.dumps(payload).encode("utf-8"),
            )

        client = HostedClient(self.base_url, transport=bad_version_transport)
        with self.assertRaises(HostedEntryError) as refusal:
            client.complete_initialization("irrelevant-token")
        self.assertIn("1999-01-01", str(refusal.exception))
        self.assertEqual(["initialize"], calls)

    def test_a_result_naming_no_tools_capability_is_refused(self):
        """The other half of "verify what was negotiated": a version this client
        speaks is not enough if the server never says it can serve tool calls."""

        def transport(request: urllib.request.Request) -> hosted.HostedResponse:
            body = json.loads(request.data.decode("utf-8"))
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "protocolVersion": hosted.PROTOCOL_VERSION,
                    "capabilities": {},
                    "serverInfo": {"name": "garmin-coach-loop", "version": "test"},
                },
            }
            return hosted.HostedResponse(
                200,
                {"Content-Type": "application/json"},
                json.dumps(payload).encode("utf-8"),
            )

        client = HostedClient(self.base_url, transport=transport)
        with self.assertRaises(HostedEntryError) as refusal:
            client.complete_initialization("irrelevant-token")
        self.assertIn("tools capability", str(refusal.exception))

    def test_the_notification_path_accepts_202_with_no_body(self):
        """Streamable HTTP's success answer to a notification is 202 with no body --
        nothing here should try to read a JSON-RPC result out of it."""
        client = HostedClient(
            self.base_url, transport=lambda request: hosted.HostedResponse(202, {}, b"")
        )
        client._notify(
            "irrelevant-token",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            protocol_version=hosted.PROTOCOL_VERSION,
        )

    def test_a_non_202_notification_response_is_refused(self):
        client = HostedClient(
            self.base_url, transport=lambda request: hosted.HostedResponse(200, {}, b"")
        )
        with self.assertRaises(HostedEntryError):
            client._notify(
                "irrelevant-token",
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                protocol_version=hosted.PROTOCOL_VERSION,
            )

    def test_an_sse_response_is_refused_rather_than_misparsed(self):
        """Streamable HTTP lets a server answer a POST with an SSE stream instead of one
        JSON body. This client declares it accepts either (Accept carries both media
        types on every call, proven in test_the_ordered_lifecycle_runs_before_the_tool_call)
        but implements only the JSON one -- this repository's own gateway is stateless
        and never opens a stream (see the module docstring in ``mcp_transport.py``), so a
        stream response is simulated here rather than asked of the real one. The point is
        that it fails loudly and names SSE, rather than handing SSE framing to
        ``json.loads`` and failing on an unrelated parse error.
        """

        def transport(request: urllib.request.Request) -> hosted.HostedResponse:
            body = json.loads(request.data.decode("utf-8"))
            if body.get("method") == "initialize":
                payload = {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": hosted.PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "garmin-coach-loop", "version": "test"},
                    },
                }
                return hosted.HostedResponse(
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(payload).encode("utf-8"),
                )
            if body.get("method") == "notifications/initialized":
                return hosted.HostedResponse(202, {}, b"")
            # tools/list, the call under test.
            sse_body = (
                b'event: message\ndata: {"jsonrpc": "2.0", "id": 2, '
                b'"result": {"tools": []}}\n\n'
            )
            return hosted.HostedResponse(
                200, {"Content-Type": "text/event-stream"}, sse_body
            )

        client = HostedClient(self.base_url, transport=transport)
        protocol_version = client.complete_initialization("irrelevant-token")
        with self.assertRaises(HostedEntryError) as refusal:
            client.list_tools("irrelevant-token", protocol_version=protocol_version)
        self.assertIn("SSE", str(refusal.exception))


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

    def test_the_custom_gpt_entry_narrows_scope_the_same_way(self):
        """The other authorize route forwards the athlete's consent request too.

        Which entry a request arrived through is not a reason for a wider grant to reach
        the consent screen through one and be refused through the other.
        """
        status, headers, _ = self.fetch(
            self.base_url
            + "/oauth/intervals/authorize?"
            + urllib.parse.urlencode(
                {
                    "client_id": "chatgpt-configured",
                    "redirect_uri": "https://chatgpt.com/connector/oauth/1",
                    "response_type": "code",
                    "state": "gpt-state",
                    "scope": "ACTIVITY:READ ATHLETE:DELETE",
                }
            )
        )
        self.assertEqual(307, status)
        sent = self.query_of(headers["Location"])
        self.assertEqual("ACTIVITY:READ", sent["scope"])
        # Everything else it was configured with still travels untouched.
        self.assertEqual("chatgpt-configured", sent["client_id"])
        self.assertEqual("gpt-state", sent["state"])

    def test_a_token_never_travels_in_a_connection_repr(self):
        connection = self.connect_as(provider_token=TOKEN_A)
        self.assertNotIn(connection.access_token, repr(connection))

    def test_revoking_connections_ends_every_client_and_leaves_the_plan(self):
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        plan = publishable_plan()
        init_store(self.owner_dir(owner_id), plan)
        first = self.connect_as(provider_token=TOKEN_A)
        second = self.connect_as(provider_token=TOKEN_B)
        for connection in (first, second):
            self.assertFalse(connection.call_tool("startCoachSession", {"all_clear": True})[0])

        removed = revoke_owner_connections(
            self.identity_db, owner_id, now=int(self.now.timestamp())
        )
        self.assertEqual(2, removed["token_fingerprints"])

        # Tokens this gateway already issued stop working too: the envelope is opened, and
        # then the registry -- which is the only answer to "whose store is this" -- has
        # nothing to resolve it to.
        for connection in (first, second):
            with self.assertRaises(HostedEntryError):
                connection.call_tool("startCoachSession", {"all_clear": True})

        # The plan is untouched, and signing in again lands on the same owner and store.
        self.assertEqual(1, read_current_plan(self.owner_dir(owner_id))["current_version"])
        self.now += dt.timedelta(seconds=1)
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

    def test_a_revoked_token_stays_dead_even_after_the_same_credential_returns(self):
        """The case that makes deleting fingerprints alone insufficient.

        The access token carries the provider credential, not a fingerprint. If the same
        credential is ever recorded again -- the athlete reconnecting on a token Intervals
        still accepts -- a token issued before the revocation would resolve once more
        unless the revocation itself is remembered.
        """
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        init_store(self.owner_dir(owner_id), publishable_plan())
        leaked = self.connect_as(provider_token=TOKEN_A)

        revoke_owner_connections(self.identity_db, owner_id, now=int(self.now.timestamp()))
        self.now += dt.timedelta(seconds=1)

        # The very same provider credential is registered again by a fresh authorization.
        fresh = self.connect_as(provider_token=TOKEN_A)
        self.assertIsNotNone(
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            )
        )

        refused, payload = fresh.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        with self.assertRaises(HostedEntryError):
            leaked.call_tool("startCoachSession", {"all_clear": True})


# --------------------------------------------------------------------------------------
# The same-second collision `iat` alone cannot resolve
# --------------------------------------------------------------------------------------


class RevocationEpochTests(HostedFlowTestCase):
    """A revocation and the reconnect it explicitly permits, timestamped to the same second.

    `revoked_after` and an access token's `iat` are both whole unix seconds. A revoke
    followed immediately by a reconnect -- the case the two tests above already prove this
    product must support -- can mint a token whose `iat` equals the revocation second, and
    the plain `issued_at <= revoked` check could not tell that token apart from one issued
    a moment *before* the revoke. `issue_access_token` now stamps a fresh token with the
    registry's `revoked_after` as read at that instant (0 when the owner has never been
    revoked), and verification prefers that epoch over `iat` whenever it is present.
    """

    def _bearer(self, provider_token: str, **claims: Any) -> str:
        """`McpTestCase.mcp_bearer`, with room for the claim shapes issuance never sends.

        A real access token either carries no ``revocation_epoch`` at all (every envelope
        minted before this claim existed) or carries one this gateway computed itself. The
        tests below need to hand-seal the shapes in between -- present but malformed -- so
        they build the envelope here rather than through the real issuance path.
        """
        payload: dict[str, Any] = {
            "intervals_token": provider_token,
            "aud": self.base_url + "/mcp",
            "scope": ",".join(INTERVALS_OAUTH_SCOPES),
            "iat": int(self.now.timestamp()),
        }
        payload.update(claims)
        return token_envelope.seal(payload, kind=token_envelope.ACCESS_TOKEN, key=HMAC_KEY)

    def test_a_reconnect_the_same_second_as_a_revocation_is_accepted(self):
        """The report itself, run through the real issuance and verification code.

        Nothing here advances ``self.now``: the revoke and the reconnect that follows it
        happen at the identical instant, which is what made the bug reproducible in the
        first place.
        """
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        init_store(self.owner_dir(owner_id), publishable_plan())
        before = self.connect_as(provider_token=TOKEN_A)

        revoke_owner_connections(self.identity_db, owner_id, now=int(self.now.timestamp()))
        # No `self.now` advance -- everything below still lands in that same second.
        after = self.connect_as(provider_token=TOKEN_A)

        with self.assertRaises(HostedEntryError):
            before.call_tool("startCoachSession", {"all_clear": True})
        refused, payload = after.call_tool("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)

    def test_an_envelope_without_the_claim_keeps_the_iat_only_behavior(self):
        """A token minted before this claim existed carries no ``revocation_epoch`` at all.

        ``mcp_bearer`` never sets it, so it stands in for exactly that envelope shape.
        Verification has to fall back to the ``iat`` comparison this repository already
        shipped, in both directions -- refusing what it always refused, and accepting what
        it always accepted -- not just whichever direction happens to keep working.
        """
        owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        stale = self.mcp_bearer(TOKEN_A)

        revoke_owner_connections(self.identity_db, owner_id, now=int(self.now.timestamp()))
        # The same credential is registered again -- the reconnect the revocation is meant
        # to survive -- so what refuses `stale` below is its timestamp, not a fingerprint
        # that no longer resolves to anyone.
        self.seed_owner(TOKEN_A)

        status, _, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=stale
        )
        self.assertEqual(401, status)

        self.now += dt.timedelta(seconds=1)
        fresh = self.mcp_bearer(TOKEN_A)
        status, _, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=fresh
        )
        self.assertEqual(200, status)

    def test_a_non_integer_or_boolean_epoch_claim_is_refused(self):
        owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        revoke_owner_connections(self.identity_db, owner_id, now=int(self.now.timestamp()))
        self.seed_owner(TOKEN_A)
        # Well after the revocation, so a plain `iat` check would accept every one of
        # these -- what refuses them below is the claim's type, not its age.
        self.now += dt.timedelta(seconds=5)

        for garbage in (True, False, "0", 1.5, None, [0]):
            with self.subTest(revocation_epoch=garbage):
                bearer = self._bearer(TOKEN_A, revocation_epoch=garbage)
                status, _, _ = self.post_mcp(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=bearer
                )
                self.assertEqual(401, status)

        # The control: a well-typed epoch at this same instant is accepted.
        valid = self._bearer(TOKEN_A, revocation_epoch=int(self.now.timestamp()))
        status, _, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=valid
        )
        self.assertEqual(200, status)


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
