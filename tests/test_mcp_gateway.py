"""The MCP entry, proven against the same loopback server the REST entry is proven on.

Every test here goes through a real socket and the real handler, so what is asserted is
what an MCP client would actually receive -- including the response headers, which is
where the OAuth discovery contract lives.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import re
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from garmin_coach_loop import (
    athlete_evidence,
    mcp_transport,
    orchestration,
    security_log,
    token_envelope,
)
from garmin_coach_loop.gateway import (
    AUTHORIZATION_CODE_TTL_SECONDS,
    CoachGateway,
    INTERVALS_AUTHORIZE_URL,
    INTERVALS_OAUTH_SCOPES,
    PRODUCT_VERSION,
    ROUTES,
    authorization_server_metadata,
    protected_resource_metadata,
    public_base_url,
)
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_for_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.mcp_transport import PROTOCOL_VERSION, TOOLS, TOOLS_BY_NAME
from garmin_coach_loop.store import canonical_hash, init_store, read_current_plan

# The REST entry's own harness -- a real loopback server over one injected fetcher --
# reused rather than rebuilt: a second fake provider would be a second answer to what
# Intervals does. Resolved by the documented `unittest discover -s tests` run, which puts
# this directory on the path.
from test_gateway import (
    CLIENT_ID_VALUE,
    CLIENT_SECRET_VALUE,
    HMAC_KEY,
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    TOKEN_B,
    UNKNOWN_TOKEN,
    WEEKLY_CHANGE,
    GatewayTestCase,
    load,
    publishable_plan,
    recovery_signals_upload,
)


ROOT = Path(__file__).resolve().parents[1]


class McpTestCase(GatewayTestCase):
    """One MCP request over the real server, with the headers kept."""

    def mcp_bearer(self, provider_token: str, *, base_url: str | None = None) -> str:
        """The access token this gateway's own token endpoint would have issued.

        Sealed here rather than danced for, so that a test about the protocol is not also
        a test about OAuth. ``McpAuthorizationServerTests`` runs the real flow and proves
        this shortcut mints the same thing the endpoint does.
        """
        return token_envelope.seal(
            {
                "intervals_token": provider_token,
                "aud": (base_url or self.base_url) + "/mcp",
                "scope": ",".join(INTERVALS_OAUTH_SCOPES),
                "iat": int(self.now.timestamp()),
            },
            kind=token_envelope.ACCESS_TOKEN,
            key=HMAC_KEY,
        )

    def post_mcp(
        self,
        message: Any = None,
        *,
        token: str | None = TOKEN_A,
        bearer: str | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """``token`` is the athlete's Intervals token, sent as the envelope around it.

        ``bearer`` overrides that with a verbatim value, for the cases where what the
        client presents is exactly the point: a bare provider token, an envelope minted
        for another origin, a value this gateway never issued.
        """
        data = raw if raw is not None else json.dumps(message).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/mcp", data=data, method="POST")
        request.add_header("Content-Type", content_type)
        if bearer is not None:
            request.add_header("Authorization", "Bearer " + bearer)
        elif token is not None:
            request.add_header("Authorization", "Bearer " + self.mcp_bearer(token))
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers), exc.read()

    def rpc(self, method: str, params: Any = None, *, message_id: Any = 1, **kwargs) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        status, _, body = self.post_mcp(message, **kwargs)
        self.assertEqual(200, status)
        return json.loads(body)

    def tool_result(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs
    ) -> Any:
        return self.rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs
        )["result"]

    @staticmethod
    def tool_payload(result: dict[str, Any]) -> dict[str, Any]:
        content = result["content"]
        assert len(content) == 1, content
        assert content[0]["type"] == "text", content
        return json.loads(content[0]["text"])

    def register(self, *redirect_uris: Any, **body: Any) -> tuple[int, Any]:
        """RFC 7591 registration, as an MCP client with no configured id sends it."""
        return self.call(
            "POST", "/oauth/register", body={"redirect_uris": list(redirect_uris), **body}
        )

    def registered_client_id(self, *redirect_uris: str) -> str:
        status, payload = self.register(*redirect_uris)
        self.assertEqual(201, status, payload)
        return payload["client_id"]

    # The two shapes a refused registration takes. RFC 7591 has one error code for both,
    # so what separates them is the description -- and they have opposite fixes, which is
    # the whole reason the description is there.
    MALFORMED = "must be an https URL"
    UNTRUSTED = "origins it trusts"

    def assert_registration_refused(self, redirect_uris: Any, *, because: str) -> None:
        status, payload = (
            self.register(*redirect_uris)
            if isinstance(redirect_uris, (list, tuple))
            else self.call("POST", "/oauth/register", body=redirect_uris)
        )
        self.assertEqual(400, status, payload)
        self.assertEqual({"error", "error_description"}, set(payload))
        self.assertEqual("invalid_redirect_uri", payload["error"])
        self.assertIn(because, payload["error_description"])


# --------------------------------------------------------------------------------------
# Identity and the OAuth challenge
# --------------------------------------------------------------------------------------


class McpAuthenticationTests(McpTestCase):
    def _challenge(self, headers: dict[str, str]) -> str:
        value = headers.get("WWW-Authenticate")
        self.assertIsNotNone(value, "a 401 on /mcp must say where to authorize")
        return str(value)

    def test_a_request_without_a_token_is_refused_and_points_at_the_metadata(self):
        status, headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None
        )

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, json.loads(body))
        self.assertEqual(
            'Bearer resource_metadata="%s/.well-known/oauth-protected-resource"'
            % self.base_url,
            self._challenge(headers),
        )
        # Identity precedes the protocol: nothing was parsed, so nothing was dispatched.
        self.assertEqual([], self.fake.calls)

    def test_an_unknown_token_is_refused_the_same_way_and_creates_no_state(self):
        status, headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=UNKNOWN_TOKEN
        )

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, json.loads(body))
        self.assertIn("resource_metadata=", self._challenge(headers))
        self.assertFalse((self.state_root / "owners").exists())

    def test_a_bare_intervals_token_is_not_an_identity_this_entry_accepts(self):
        # The athlete's own provider credential, presented directly. It resolves to a
        # real owner on the REST entry and to nothing here: an MCP bearer is only ever a
        # token this gateway issued, which is what stops a client from holding one it
        # could leak as the athlete's whole Intervals account.
        self.seed_owner(TOKEN_A, plan=publishable_plan())

        status, headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=TOKEN_A
        )

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, json.loads(body))
        self.assertIn("resource_metadata=", self._challenge(headers))
        self.assertEqual([], self.fake.calls)

    def test_an_envelope_is_not_an_identity_the_rest_entry_accepts(self):
        # The other direction, and the reason the two entries stay separable: the Custom
        # GPT contract takes the provider token and nothing else.
        self.seed_owner(TOKEN_A, plan=publishable_plan())

        status, payload = self.call(
            "POST", "/v1/coach/session", body={}, token=self.mcp_bearer(TOKEN_A)
        )

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)
        self.assertEqual([], self.fake.calls)

    def test_a_token_minted_for_another_origin_is_refused(self):
        # Audience, the check a passthrough bearer could not support: this same code
        # deployed twice must not accept the other deployment's tokens, and neither must
        # one host accept a token an athlete obtained for another.
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        elsewhere = self.mcp_bearer(TOKEN_A, base_url="https://coach.example")

        status, headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=elsewhere
        )

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, json.loads(body))
        self.assertIn("resource_metadata=", self._challenge(headers))

        # And it is accepted on the host it was actually issued for.
        status, _, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            bearer=elsewhere,
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "coach.example"},
        )
        self.assertEqual(200, status)

    def test_the_envelope_kinds_are_not_interchangeable(self):
        # One key, four uses. A state, a code or a client registration presented as a
        # bearer opens nothing, because the kind is part of what the tag covers.
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        payload = {
            "intervals_token": TOKEN_A,
            "aud": self.base_url + "/mcp",
            "scope": "ACTIVITY:READ",
            "client_redirect_uri": "https://client.example/callback",
            "redirect_uris": ["https://client.example/callback"],
            "code_challenge": "x",
            "iat": int(self.now.timestamp()),
        }
        for kind in (
            token_envelope.AUTHORIZE_STATE,
            token_envelope.AUTHORIZATION_CODE,
            token_envelope.CLIENT_REGISTRATION,
        ):
            with self.subTest(kind=kind):
                wrong = token_envelope.seal(payload, kind=kind, key=HMAC_KEY)
                status, _, body = self.post_mcp(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer=wrong
                )
                self.assertEqual(401, status)
                self.assertEqual(
                    {"status": "blocked", "error": "unauthorized"}, json.loads(body)
                )

    def test_the_challenge_names_the_origin_the_client_reached_through_a_proxy(self):
        _, headers, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            token=None,
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "coach.example"},
        )

        self.assertEqual(
            'Bearer resource_metadata="https://coach.example'
            '/.well-known/oauth-protected-resource"',
            self._challenge(headers),
        )

    def test_an_authenticated_response_carries_no_challenge(self):
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        _, headers, _ = self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertNotIn("WWW-Authenticate", headers)

    def test_the_rest_entry_keeps_its_bare_401(self):
        # Only /mcp is a protected resource in RFC 9728's sense, so only it gains the header.
        request = urllib.request.Request(
            self.base_url + "/v1/coach/session", data=b"{}", method="POST"
        )
        request.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        with caught.exception as exc:
            self.assertEqual(401, exc.code)
            self.assertNotIn("WWW-Authenticate", dict(exc.headers))

    def test_a_get_is_refused_because_this_server_opens_no_stream(self):
        status, payload = self.call("GET", "/mcp", token=TOKEN_A)
        self.assertEqual(405, status)
        self.assertEqual("method_not_allowed", payload["error"])


# --------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------


class McpProtocolTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def test_initialize_carries_the_sequencing_without_being_asked(self):
        """The one layer a host can put in front of its model with nobody choosing it.

        Prompts are user-controlled by specification, so serving one is not delivering
        it. A client driving these operations without knowing that exactly one
        confirmation stands before a write can reach an athlete's calendar the product
        never meant it to, which is why the sequencing rides the field the specification
        defines for text a host may add to a system prompt rather than only the field a
        user has to pick.
        """
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        )["result"]

        # The file itself, not a summary of it: two copies of this text would drift, and
        # the release binds the digest of exactly one of them.
        self.assertEqual(orchestration.instructions(), result["instructions"])
        # The coaching layer stays out. `instructions` is defined as how to use the
        # server, and a host that appends it to a system prompt has not agreed to carry
        # a training reference there.
        self.assertNotIn("Hybrid running and strength judgment", result["instructions"])

    def test_initialize_answers_with_the_supported_version_and_what_it_serves(self):
        response = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        )

        self.assertEqual("2.0", response["jsonrpc"])
        self.assertEqual(1, response["id"])
        result = response["result"]
        self.assertEqual(PROTOCOL_VERSION, result["protocolVersion"])
        # Tools, and the one prompt that says how to sequence them (issue #125). Nothing
        # else: resources, sampling and logging would each be a second way to the same
        # state, and a capability advertised is a capability a client will call.
        self.assertEqual({"tools": {}, "prompts": {}}, result["capabilities"])
        self.assertEqual("garmin-coach-loop", result["serverInfo"]["name"])
        # Pinned, not merely truthy: this is the version a person quotes when saying
        # what is live, so a client and /readyz must state the same one.
        self.assertEqual(PRODUCT_VERSION, result["serverInfo"]["version"])

    def test_an_older_protocol_version_is_negotiated_to_the_one_this_server_speaks(self):
        for requested in ("2025-03-26", "2024-11-05", "not-a-version"):
            with self.subTest(requested=requested):
                result = self.rpc("initialize", {"protocolVersion": requested})["result"]
                self.assertEqual(PROTOCOL_VERSION, result["protocolVersion"])

    def test_a_notification_is_accepted_with_no_body(self):
        status, _, body = self.post_mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(202, status)
        self.assertEqual(b"", body)

    def test_no_session_id_is_issued(self):
        _, headers, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertNotIn("Mcp-Session-Id", headers)

    def test_ping_is_answered_so_the_connection_does_not_read_as_dead(self):
        self.assertEqual({}, self.rpc("ping")["result"])

    def test_an_unknown_method_is_a_json_rpc_error(self):
        response = self.rpc("resources/list")
        self.assertEqual(mcp_transport.METHOD_NOT_FOUND, response["error"]["code"])
        self.assertNotIn("result", response)

    def test_a_body_that_is_not_json_is_a_parse_error(self):
        status, _, body = self.post_mcp(raw=b"{not json")
        self.assertEqual(400, status)
        self.assertEqual(mcp_transport.PARSE_ERROR, json.loads(body)["error"]["code"])

    def test_an_empty_body_is_a_parse_error_rather_than_a_silent_success(self):
        status, _, body = self.post_mcp(raw=b"")
        self.assertEqual(400, status)
        self.assertEqual(mcp_transport.PARSE_ERROR, json.loads(body)["error"]["code"])

    def test_a_batch_is_refused_rather_than_partly_answered(self):
        status, _, body = self.post_mcp(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )
        self.assertEqual(400, status)
        self.assertEqual(mcp_transport.INVALID_REQUEST, json.loads(body)["error"]["code"])

    def test_a_message_that_is_not_json_rpc_is_refused(self):
        status, _, body = self.post_mcp({"method": "tools/list", "id": 1})
        self.assertEqual(400, status)
        self.assertEqual(mcp_transport.INVALID_REQUEST, json.loads(body)["error"]["code"])


class McpTransportHeaderTests(McpTestCase):
    """The two headers MCP asks a streamable-HTTP server to check before anything else."""

    def setUp(self):
        super().setUp()
        self.seed_owner(TOKEN_A, plan=publishable_plan())

    def list_tools(self, **kwargs) -> tuple[int, dict[str, str], bytes]:
        return self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, **kwargs)

    def start_session(self, **kwargs) -> tuple[int, dict[str, str], bytes]:
        """A message that does reach the provider when it is served, so a refusal shows."""
        return self.post_mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "startCoachSession", "arguments": {"all_clear": True}},
            },
            **kwargs,
        )

    # -- Origin -----------------------------------------------------------------------

    def test_a_client_that_sends_no_origin_is_the_normal_case_and_passes(self):
        # Every server-side MCP client -- which is every client this product is actually
        # reached from -- sends none, so refusing an absent header would refuse them all.
        self.assertEqual(200, self.list_tools()[0])

    def test_the_origin_this_request_arrived_on_passes(self):
        self.assertEqual(200, self.list_tools(headers={"Origin": self.base_url})[0])

    def test_the_forwarded_origin_passes_where_that_is_what_the_browser_reached(self):
        status, _, _ = self.list_tools(
            headers={
                "Origin": "https://coach.example",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "coach.example",
            },
            bearer=self.mcp_bearer(TOKEN_A, base_url="https://coach.example"),
        )
        self.assertEqual(200, status)

    def test_the_first_party_connector_host_passes(self):
        for origin in ("https://claude.ai", "HTTPS://Claude.AI"):
            with self.subTest(origin=origin):
                self.assertEqual(200, self.list_tools(headers={"Origin": origin})[0])

    def test_a_foreign_origin_is_refused_before_any_owner_or_provider_is_reached(self):
        status, headers, body = self.start_session(headers={"Origin": "https://evil.example"})

        self.assertEqual(403, status)
        self.assertEqual({"status": "blocked", "error": "forbidden_origin"}, json.loads(body))
        # Not a 401: this is not a client that needs to authorize, and pointing it at the
        # metadata would be inviting it to try.
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertEqual([], self.fake.calls)

    def test_an_origin_that_merely_looks_like_an_allowed_one_is_refused(self):
        # DNS rebinding is the whole reason this check exists, and a suffix or substring
        # test would hand it exactly the domain it needs.
        for origin in (
            "https://claude.ai.evil.example",
            "https://evil.example?x=https://claude.ai",
            "http://claude.ai",
            "https://claude.ai:8443",
            "null",
            "",
        ):
            with self.subTest(origin=origin):
                status, _, body = self.list_tools(headers={"Origin": origin})
                self.assertEqual(403, status)
                self.assertEqual(
                    {"status": "blocked", "error": "forbidden_origin"}, json.loads(body)
                )

    def test_an_operator_may_name_further_origins_and_they_are_the_only_extras(self):
        self.gateway.config = replace(
            self.config, allowed_mcp_origins=("https://studio.example",)
        )
        self.assertEqual(200, self.list_tools(headers={"Origin": "https://studio.example"})[0])
        self.assertEqual(403, self.list_tools(headers={"Origin": "https://other.example"})[0])

    def test_the_rest_entry_is_not_a_browser_surface_and_checks_no_origin(self):
        # The REST entry is server-to-server, never a browser surface, so the header
        # that is a 403 on /mcp is nothing here.
        request = urllib.request.Request(
            self.base_url + "/v1/coach/session",
            data=b'{"all_clear": true}',
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + TOKEN_A)
        request.add_header("Origin", "https://evil.example")

        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(200, response.status)

    # -- MCP-Protocol-Version ---------------------------------------------------------

    def test_a_request_without_the_version_header_is_accepted(self):
        # 2025-06-18 says an absent header means 2025-03-26 rather than a refusal.
        self.assertEqual(200, self.list_tools()[0])

    def test_the_revisions_this_server_speaks_over_http_are_accepted(self):
        for version in mcp_transport.HTTP_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                status, _, _ = self.list_tools(headers={"MCP-Protocol-Version": version})
                self.assertEqual(200, status)
        self.assertIn("2025-03-26", mcp_transport.HTTP_PROTOCOL_VERSIONS)
        self.assertIn(PROTOCOL_VERSION, mcp_transport.HTTP_PROTOCOL_VERSIONS)

    def test_a_revision_this_server_does_not_implement_is_refused(self):
        for version in ("2024-11-05", "not-a-version", "", "2025-06-18, 2025-03-26"):
            with self.subTest(version=version):
                status, _, body = self.start_session(
                    headers={"MCP-Protocol-Version": version}
                )
                self.assertEqual(400, status)
                self.assertEqual(
                    {"status": "blocked", "error": "unsupported_protocol_version"},
                    json.loads(body),
                )
                self.assertEqual([], self.fake.calls)

    def test_the_header_is_checked_separately_from_the_initialize_handshake(self):
        # The handshake still answers 2025-03-26 with the one revision this server
        # implements; the header still accepts it, because that is what the spec assumes
        # of a client that sends no header at all.
        result = self.rpc(
            "initialize",
            {"protocolVersion": "2025-03-26"},
            headers={"MCP-Protocol-Version": "2025-03-26"},
        )["result"]
        self.assertEqual(PROTOCOL_VERSION, result["protocolVersion"])


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


class McpToolTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def test_the_catalogue_is_the_whole_coaching_surface_and_nothing_else(self):
        tools = self.rpc("tools/list")["result"]["tools"]

        self.assertEqual(22, len(tools))
        self.assertEqual(
            {
                "startCoachSession",
                "getCoachState",
                "inspectIntervalsPermissions",
                "recordAthleteProfile",
                "recordAthleteAvailability",
                "recordLongTermGoal",
                "recordTrainingPreference",
                "recordStrengthExecution",
                "recordBodyMeasurement",
                "recordActivitySummary",
                "recordSubjectiveState",
                "retractAthleteRecord",
                "importAthleteHistory",
                "confirmPrescribedStrength",
                "prepareCoachDecision",
                "applyCoachDecision",
                "prepareWorkoutDelivery",
                "applyWorkoutDelivery",
                "clearDeliveryAttempt",
                "exportOwnerData",
                "prepareOwnerDeletion",
                "applyOwnerDeletion",
            },
            {tool["name"] for tool in tools},
        )
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"].strip())
                self.assertEqual("object", tool["inputSchema"]["type"])

    def test_the_retired_delivery_and_withdrawal_tool_names_are_gone(self):
        """The converged pair replaces three names outright; none is a live alias."""
        names = {tool.name for tool in TOOLS}
        for retired in (
            "publishWorkoutDelivery",
            "prepareDeliveryWithdrawal",
            "applyDeliveryWithdrawal",
        ):
            with self.subTest(tool=retired):
                self.assertNotIn(retired, names)

    def test_every_coach_route_the_rest_entry_serves_has_a_tool(self):
        rest_kinds = {
            kind
            for _, kind in ROUTES.values()
            if kind
            not in {
                "health",
                "readiness",
                "token",
                "authorize",
                "gateway_authorize",
                "gateway_callback",
                "gateway_token",
                "client_registration",
                "protected_resource_metadata",
                "authorization_server_metadata",
                # A directory's domain-verification path: it proves who controls this
                # host, answers no coaching question, and is never something a model
                # should be able to call.
                "openai_apps_challenge",
                "mcp",
            }
        }
        self.assertEqual(rest_kinds, {tool.kind for tool in TOOLS})

    def test_starting_a_session_returns_the_same_payload_the_rest_route_returns(self):
        result = self.tool_result("startCoachSession", {"all_clear": True})

        self.assertNotEqual(True, result.get("isError"))
        payload = self.tool_payload(result)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("fixture-plan-001", payload["plan_state"]["plan_id"])
        self.assertEqual(1, payload["plan_state"]["plan_version"])
        self.assertEqual("intervals_accepted", payload["delivery"]["max_delivery_state"])
        # The provider was read with this request's own bearer token, against athlete 0.
        self.assertTrue(self.fake.calls)
        self.assertTrue(all("/athlete/0/" in url for _, url in self.fake.calls))

    def test_starting_a_session_carries_client_uploaded_recovery_evidence(self):
        result = self.tool_result(
            "startCoachSession", {"recovery_signals": recovery_signals_upload()}
        )

        self.assertNotEqual(True, result.get("isError"), result)
        group = self.tool_payload(result)["context"]["recovery_signals"]
        self.assertEqual(
            "client-uploaded:personal-os:recovery_daily+daily_metrics", group["source"]
        )
        self.assertEqual("2026-08-13", group["days"][0]["date"])

    def test_the_training_judgment_arrives_through_the_tool_call_itself(self):
        """The delivery half of issue #82's boundary, checked where a client reads it.

        `prompts/get` serves this same text, but serving is not delivery: a client
        decides whether to fetch a prompt, and the ones in use either drop it or hide it
        behind a command nobody types. A tool result is the one thing every coaching turn
        already reads, and the schema says so in the tool's own description.
        """
        payload = self.tool_payload(self.tool_result("startCoachSession"))

        self.assertEqual(orchestration.training_judgment(), payload["coaching_guidance"])
        self.assertIn(
            "coaching_guidance", TOOLS_BY_NAME["startCoachSession"].description
        )

    def test_a_tool_with_no_arguments_still_reaches_its_route(self):
        self.fake.sport_settings = []
        payload = self.tool_payload(self.tool_result("inspectIntervalsPermissions"))
        self.assertEqual("passed", payload["status"])
        self.assertEqual("readable", payload["settings_read"])

    def test_get_coach_state_answers_from_the_store_alone(self):
        payload = self.tool_payload(self.tool_result("getCoachState"))

        self.assertEqual("passed", payload["status"])
        self.assertEqual("fixture-plan-001", payload["plan_id"])
        self.assertEqual(1, payload["plan_version"])
        self.assertIsNone(payload["pending_delivery_attempt_id"])
        # Through this transport too: no provider request, whatever the REST test proves
        # directly against the store's bytes.
        self.assertEqual([], self.fake.calls)

    def test_the_two_conversational_writers_echo_what_they_stored(self):
        """The direct-write contract over this transport: written, then read back.

        The echo is the whole correction mechanism -- no confirmation is asked for, so
        what comes back has to be the stored record rather than a restatement of the
        arguments, which would agree with a call that stored something else.
        """
        measurement = self.tool_payload(
            self.tool_result("recordBodyMeasurement", {"weight_kg": 72.5})
        )
        summary = self.tool_payload(
            self.tool_result(
                "recordActivitySummary", {"sport": "running", "duration_minutes": 40}
            )
        )

        self.assertEqual("passed", measurement["status"])
        self.assertEqual("passed", summary["status"])
        stored = athlete_evidence.load_evidence(self.state_dir)
        # The echo is the stored record through the model-facing projection: identical
        # except the store's own content hashes, which the model never sends back.
        self.assertEqual(
            {k: v for k, v in stored["body_measurements"][0].items() if k != "measurement_id"},
            measurement["measurement"],
        )
        self.assertEqual(
            {
                k: v
                for k, v in stored["reported_activities"][0].items()
                if k not in ("summary_id", "dedup_keys")
            },
            summary["activity"],
        )
        # Neither reached the provider: an athlete's own account of a session is not a
        # row on their Intervals calendar.
        self.assertEqual([], self.fake.calls)

    def test_restating_either_one_corrects_it_rather_than_adding_a_second(self):
        self.tool_payload(self.tool_result("recordBodyMeasurement", {"weight_kg": 72.5}))
        corrected = self.tool_payload(
            self.tool_result("recordBodyMeasurement", {"weight_kg": 72.3})
        )
        self.tool_payload(
            self.tool_result(
                "recordActivitySummary", {"sport": "running", "duration_minutes": 40}
            )
        )
        restated = self.tool_payload(
            self.tool_result(
                "recordActivitySummary", {"sport": "running", "duration_minutes": 45}
            )
        )

        self.assertEqual(1, corrected["measurement_count"])
        self.assertEqual(72.5, corrected["replaced"]["weight_kg"])
        self.assertEqual(1, restated["activity_count"])
        self.assertIn("combined summary", restated["replaced_note"])

    def test_a_retraction_over_mcp_removes_what_a_record_call_stored(self):
        """The other kind of statement, over one shared tool: removes rather than replaces."""
        self.tool_payload(
            self.tool_result(
                "recordStrengthExecution",
                {"exercise": "bench press", "sets": [{"weight_kg": 65, "reps": 4}]},
            )
        )
        self.tool_payload(self.tool_result("recordBodyMeasurement", {"weight_kg": 72.5}))
        self.tool_payload(
            self.tool_result(
                "recordActivitySummary", {"sport": "running", "duration_minutes": 40}
            )
        )

        strength = self.tool_payload(
            self.tool_result(
                "retractAthleteRecord",
                {"kind": "strength_execution", "exercise": "bench press"},
            )
        )
        measurement = self.tool_payload(
            self.tool_result("retractAthleteRecord", {"kind": "body_measurement"})
        )
        summary = self.tool_payload(
            self.tool_result(
                "retractAthleteRecord", {"kind": "activity_summary", "sport": "running"}
            )
        )

        for payload in (strength, measurement, summary):
            self.assertEqual("passed", payload["status"])
            self.assertTrue(payload["retracted"])
            self.assertIsNotNone(payload["removed"])
            self.assertEqual(0, payload["record_count"])
        self.assertIsNone(measurement["on_record_that_day"])
        stored = athlete_evidence.load_evidence(self.state_dir)
        self.assertEqual([], stored["strength_reports"])
        self.assertEqual([], stored["body_measurements"])
        self.assertEqual([], stored["reported_activities"])

    def test_a_subjective_state_is_stored_and_echoed_verbatim(self):
        """Issue #188: the athlete's sentence, over the transport, unread by anything."""
        payload = self.tool_payload(
            self.tool_result("recordSubjectiveState", {"note": "這幾天覺得很累"})
        )

        self.assertEqual("passed", payload["status"])
        stored = athlete_evidence.load_evidence(self.state_dir)
        # Verbatim through the projection: the sentence, the day, the provenance -- only
        # the store's own content hash stays behind.
        self.assertEqual(
            {k: v for k, v in stored["subjective_states"][0].items() if k != "state_id"},
            payload["state"],
        )
        self.assertEqual("這幾天覺得很累", payload["state"]["note"])
        # No calendar row: this is a fact about the athlete, not about their week.
        self.assertEqual([], self.fake.calls)

    def test_a_subjective_state_retracts_through_the_one_retraction_tool(self):
        """The mechanism issue #188 asked to be covered, not a second retraction route."""
        self.tool_payload(self.tool_result("recordSubjectiveState", {"note": "很累"}))

        payload = self.tool_payload(
            self.tool_result("retractAthleteRecord", {"kind": "subjective_state"})
        )

        self.assertEqual("passed", payload["status"])
        self.assertTrue(payload["retracted"])
        self.assertEqual("很累", payload["removed"]["note"])
        self.assertEqual(0, payload["record_count"])
        # Keyed by the day alone, so there is no second name for the response to report.
        self.assertIsNone(payload["on_record_that_day"])
        self.assertEqual([], payload["candidates"])
        self.assertEqual(
            [], athlete_evidence.load_evidence(self.state_dir)["subjective_states"]
        )

    def test_the_three_record_tools_no_longer_carry_a_retract_property(self):
        """Retraction moved wholly to retractAthleteRecord; the record tools stay additive."""
        for name in ("recordStrengthExecution", "recordBodyMeasurement", "recordActivitySummary"):
            with self.subTest(tool=name):
                self.assertNotIn(
                    "retract", TOOLS_BY_NAME[name].input_schema.get("properties", {})
                )

    def test_omitted_arguments_are_read_as_an_empty_object(self):
        response = self.rpc("tools/call", {"name": "startCoachSession"})
        self.assertEqual("passed", self.tool_payload(response["result"])["status"])

    def test_a_refused_call_is_a_tool_error_the_model_can_read(self):
        result = self.tool_result(
            "startCoachSession", {"timezone": "Nowhere/Nothing"}
        )

        self.assertTrue(result["isError"])
        payload = self.tool_payload(result)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("invalid_request", payload["error"])
        self.assertEqual([], self.fake.calls)

    def test_a_state_conflict_is_reported_as_the_gateway_reports_it(self):
        result = self.tool_result(
            "prepareWorkoutDelivery",
            {"plan_id": "some-other-plan", "plan_version": 1, "session_ids": ["run-long-01"]},
        )

        self.assertTrue(result["isError"])
        payload = self.tool_payload(result)
        self.assertEqual("plan_mismatch", payload["error"])
        self.assertEqual("fixture-plan-001", payload["current_plan_id"])

    def test_a_tool_error_body_carries_no_credential_or_path_material(self):
        texts = [
            json.dumps(self.tool_result("startCoachSession", {"timezone": "Nope/Nope"})),
            json.dumps(self.tool_result("clearDeliveryAttempt", {"attempt_id": "x"})),
            json.dumps(self.tool_result("prepareCoachDecision", {})),
        ]
        joined = "\n".join(texts)
        for secret in (TOKEN_A, CLIENT_SECRET_VALUE, HMAC_KEY.decode("ascii")):
            self.assertNotIn(secret, joined)
        self.assertNotIn(str(self.state_root), joined)
        self.assertNotIn(self.owner_id, joined)

    def test_an_unknown_tool_name_is_a_protocol_error_not_a_tool_result(self):
        response = self.rpc("tools/call", {"name": "deleteEverything", "arguments": {}})
        self.assertEqual(mcp_transport.INVALID_PARAMS, response["error"]["code"])
        self.assertNotIn("result", response)

    def test_arguments_that_are_not_an_object_are_refused_before_any_route(self):
        response = self.rpc("tools/call", {"name": "startCoachSession", "arguments": []})
        self.assertEqual(mcp_transport.INVALID_PARAMS, response["error"]["code"])
        self.assertEqual([], self.fake.calls)

    def test_a_write_still_needs_its_confirmation_through_this_entry(self):
        result = self.tool_result(
            "clearDeliveryAttempt", {"attempt_id": "attempt-1", "confirmed": False}
        )
        self.assertTrue(result["isError"])
        self.assertEqual("confirmation_required", self.tool_payload(result)["error"])


# --------------------------------------------------------------------------------------
# What the catalogue claims about itself
# --------------------------------------------------------------------------------------


# Every tool, and what it tells a client it does: read-only, destructive, idempotent,
# open-world. Written out here rather than derived from the catalogue, because a test
# that recomputed the answer would agree with any answer. Changing a hint means changing
# this table, which is the point: the protocol's defaults are the cautious ones, so a
# hint is a claim about the athlete's plan and their calendar, not a formality.
#
# `destructiveHint` is the one worth restating, because this repository read it wrong
# once and the wrong reading is the intuitive one. The specification's words are: "If
# true, the tool may perform destructive updates to its environment. If false, the tool
# performs only additive updates." Additive is the test, not deletion -- so a tool that
# overwrites a value the athlete already stated is destructive even though it removes no
# record and the athlete asked for the correction. The nine record/confirm tools below
# are all of that shape: `athlete-evidence.json` holds one row per key, the row a
# restatement displaces "is returned, never kept", and that file sits outside the
# append-only commit chain, so there is no earlier copy the way there is for a plan
# version. `False` here is therefore the strong claim and `True` is the protocol's own
# default; the table used to say `False` for all nine while its own comment described
# them replacing, which is the drift this pins shut.
EXPECTED_HINTS: dict[str, tuple[bool, bool, bool, bool]] = {
    # This one is the whole reason the table exists. `startCoachSession` reads like a
    # read: it is what a conversation calls first, and its name says session, not write.
    # It also applies reconciliation, which commits.
    "startCoachSession": (False, False, False, True),
    # The read-only counterpart to startCoachSession: it never reaches Intervals at all,
    # which is why its openWorldHint is false where startCoachSession's and
    # inspectIntervalsPermissions' are both true.
    "getCoachState": (True, False, True, False),
    "inspectIntervalsPermissions": (True, False, True, True),
    # Destructive: every field is latest-wins, so a second timezone overwrites the first.
    "recordAthleteProfile": (False, True, True, False),
    # Destructive because `recurring` is a single latest-wins value: an athlete who moves
    # their training days leaves no readable trace of the week they moved off. Idempotent
    # on both halves, which each reach it their own way -- `recurring` leaves the record
    # standing when the days match rather than re-stamping it, and the append-only `week`
    # half compares against the statement standing for that week and answers from it, so
    # an identical replay cannot reach the coach as a doubled note.
    "recordAthleteAvailability": (False, True, True, False),
    # The two standing statements: what the athlete is training for past this cycle, and
    # how they say they like to train. Both go through `_upsert_standing`, which pops the
    # record for that metric or topic and appends the new one, so restating replaces and
    # what it displaced is gone. Idempotent in the sense that matters -- an identical
    # replay leaves one record holding the same content, not two. Neither reaches
    # Intervals, because neither is a row on anybody's calendar.
    "recordLongTermGoal": (False, True, True, False),
    "recordTrainingPreference": (False, True, True, False),
    # The four conversational evidence writers, and the correction this table needed.
    # Each holds one row per key -- (date, exercise), (date), (date, sport), (date) --
    # so a restatement overwrites the row rather than joining it, and the row it
    # displaces is returned to the caller once and never stored. That is a destructive
    # update, not an additive one, however ordinary the athlete's "65, sorry, 70" is.
    # Still idempotent, and for a good reason rather than by assumption: each hashes its
    # own content and short-circuits an identical replay before writing. None reaches
    # Intervals: the whole point of this evidence is that it is the athlete's own
    # account, not a row on their calendar. The fourth stores how the athlete says they
    # feel (issue #188) and has the same shape for the same reasons -- and, deliberately,
    # no separate shape for being about a person rather than a session: it fires no rule.
    "recordStrengthExecution": (False, True, True, False),
    "recordBodyMeasurement": (False, True, True, False),
    "recordActivitySummary": (False, True, True, False),
    "recordSubjectiveState": (False, True, True, False),
    # Destructive like the writers above, and no longer distinguished by that: removing
    # a record and overwriting one both leave the athlete's earlier statement
    # unreachable. What still singles this one out is that leaving nothing behind is its
    # purpose rather than a side effect. Idempotent because a repeat -- or a retraction
    # that finds nothing left -- converges rather than erroring. Reaches no Intervals,
    # for the same reason the record tools above do not.
    "retractAthleteRecord": (False, True, True, False),
    # The one writer of this evidence that really is additive, and now the only tool in
    # the group claiming it: a session already on record is left standing and only gains
    # the upload's reference, and a body measurement for a day the athlete already stated
    # is skipped rather than overwritten. Idempotent for a stronger reason than the
    # others: the payload's own digest recognises a re-send, so dropping the same export
    # in twice writes nothing the second time.
    "importAthleteHistory": (False, False, True, False),
    # Destructive for a reason its name hides: it writes through the same
    # `_upsert_strength_reports` as recordStrengthExecution, so confirming a session the
    # athlete had already reported movement by movement overwrites what they said with
    # what the plan prescribed.
    "confirmPrescribedStrength": (False, True, True, False),
    "prepareCoachDecision": (True, False, True, False),
    # Not destructive, and this is the contrast that makes the record tools above
    # destructive: a plan change appends a version to the commit chain and the version it
    # supersedes stays readable, so nothing the athlete had becomes unreachable. Not
    # idempotent, because the proposal is bound to the plan version it was previewed
    # against -- a second send is refused rather than repeated.
    "applyCoachDecision": (False, False, False, False),
    # One preview tool for both directions -- annotations unchanged from when this
    # covered only delivery, since withdraw: true still only ever reads (it may still
    # read Intervals for a Run threshold HR the delivery direction needs).
    "prepareWorkoutDelivery": (True, False, True, True),
    # Replaces publishWorkoutDelivery and applyDeliveryWithdrawal: destructive because a
    # session already on the calendar is replaced in place, or a superseded one is
    # removed outright; idempotent because retrying the identical set -- either
    # direction -- is how a partial delivery or withdrawal converges.
    "applyWorkoutDelivery": (False, True, True, True),
    "clearDeliveryAttempt": (False, True, True, False),
    "exportOwnerData": (True, False, True, False),
    "prepareOwnerDeletion": (True, False, True, False),
    # The one destructive tool with nothing conversational about it: this erases the
    # whole account rather than one record, and there is no restating an account back.
    # Idempotent because a repeat finds nothing left -- which is also how a
    # half-finished erasure finishes.
    "applyOwnerDeletion": (False, True, True, False),
}


class ToolSchemaSelfContainmentTests(unittest.TestCase):
    def test_a_tool_schema_is_self_contained(self):
        # No $ref anywhere: an MCP client resolves nothing, so a reference would reach the
        # model as a field it cannot fill. This is why a grammar two tools both accept is
        # written out in both -- the duplication is the protocol's, not this file's.
        rendered = json.dumps([tool.descriptor() for tool in TOOLS])
        self.assertNotIn("$ref", rendered)


class McpToolAnnotationTests(McpTestCase):
    """Issue #117: a client decides what to show the athlete from these, so they are facts."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def test_every_tool_names_itself_and_states_all_four_hints(self):
        tools = self.rpc("tools/list")["result"]["tools"]

        for tool in tools:
            with self.subTest(tool=tool["name"]):
                annotations = tool["annotations"]
                # The title is in both places the specification has put one, and it is the
                # same string: a client reading either sees the same name.
                self.assertTrue(annotations["title"].strip())
                self.assertEqual(annotations["title"], tool["title"])
                self.assertNotEqual(annotations["title"], tool["name"])
                self.assertEqual(
                    {
                        "title",
                        "readOnlyHint",
                        "destructiveHint",
                        "idempotentHint",
                        "openWorldHint",
                    },
                    set(annotations),
                )
                for hint in (
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint",
                ):
                    self.assertIsInstance(annotations[hint], bool, hint)

    def test_each_hint_is_the_one_this_repository_decided_on(self):
        actual = {
            tool.name: (
                tool.annotations["readOnlyHint"],
                tool.annotations["destructiveHint"],
                tool.annotations["idempotentHint"],
                tool.annotations["openWorldHint"],
            )
            for tool in TOOLS
        }
        self.assertEqual(EXPECTED_HINTS, actual)

    def test_nothing_annotated_read_only_writes_anything(self):
        """The claim, checked against the store rather than against the docstring.

        Each of these is called for real and the whole owner directory is hashed on both
        sides of it. A refusal still counts: a tool that cannot write is a tool that
        cannot write when the request is wrong either.
        """
        arguments: dict[str, dict[str, Any]] = {
            "getCoachState": {},
            "inspectIntervalsPermissions": {},
            "prepareCoachDecision": {},
            "prepareWorkoutDelivery": {
                "plan_id": "fixture-plan-001",
                "plan_version": 1,
                "session_ids": ["run-long-01"],
            },
            "exportOwnerData": {},
            "prepareOwnerDeletion": {},
        }
        read_only = [
            tool.name for tool in TOOLS if tool.annotations["readOnlyHint"] is True
        ]
        self.assertEqual(sorted(arguments), sorted(read_only))

        for name in read_only:
            with self.subTest(tool=name):
                before = self.snapshot(self.state_dir)
                self.tool_result(name, arguments[name])
                self.assertEqual(before, self.snapshot(self.state_dir))

    def test_every_destructive_record_tool_really_does_displace_what_it_replaces(self):
        """The `destructiveHint` claim, checked against the store rather than the table.

        The specification's test is "performs only additive updates", so the thing to
        show is not deletion but displacement: the athlete states one value, states
        another for the same key, and the first is no longer anywhere in the file. Each
        pair below is one athlete correcting themselves -- the ordinary case, which is
        exactly why annotating it additive was easy to do and wrong.

        Two assertions per tool, because either alone would pass for the wrong reason: a
        collection that did not grow could just have dropped the write, and a changed
        value could have been appended beside the old one.
        """
        evidence_file = athlete_evidence.evidence_path(self.state_dir)
        # tool -> (first statement, its correction, where the stored rows live)
        corrections: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
            "recordAthleteProfile": (
                {"timezone": "Europe/Berlin"},
                {"timezone": "Asia/Taipei"},
                "profile",
            ),
            "recordAthleteAvailability": (
                {"recurring": {"available_days": ["mon", "wed"]}},
                {"recurring": {"available_days": ["tue"]}},
                "availability",
            ),
            "recordLongTermGoal": (
                {"metric": "體重", "target": "70 kg"},
                {"metric": "體重", "target": "68 kg"},
                "long_term_goals",
            ),
            "recordTrainingPreference": (
                {"topic": "長跑日", "statement": "習慣週五"},
                {"topic": "長跑日", "statement": "改成週六"},
                "training_preferences",
            ),
            "recordStrengthExecution": (
                {
                    "exercise": "bench press",
                    "date": "2026-08-12",
                    "sets": [{"reps": 5, "weight_kg": 65}],
                },
                {
                    "exercise": "bench press",
                    "date": "2026-08-12",
                    "sets": [{"reps": 5, "weight_kg": 70}],
                },
                "strength_reports",
            ),
            "recordBodyMeasurement": (
                {"date": "2026-08-12", "weight_kg": 72.5},
                {"date": "2026-08-12", "weight_kg": 71.8},
                "body_measurements",
            ),
            "recordActivitySummary": (
                {"date": "2026-08-12", "sport": "swimming", "duration_minutes": 40},
                {"date": "2026-08-12", "sport": "swimming", "duration_minutes": 55},
                "reported_activities",
            ),
            "recordSubjectiveState": (
                {"date": "2026-08-12", "note": "很累"},
                {"date": "2026-08-12", "note": "其實還好"},
                "subjective_states",
            ),
        }

        for name, (stated, corrected, container) in corrections.items():
            with self.subTest(tool=name):
                self.assertIs(
                    True,
                    TOOLS_BY_NAME[name].annotations["destructiveHint"],
                    f"{name} is in this table, so it claims to be destructive",
                )
                self.tool_result(name, stated)
                first = json.loads(evidence_file.read_text(encoding="utf-8"))[container]
                self.tool_result(name, corrected)
                second = json.loads(evidence_file.read_text(encoding="utf-8"))[container]

                self.assertNotEqual(
                    first, second, f"{name} did not record the correction at all"
                )
                if isinstance(first, list):
                    self.assertEqual(
                        len(first),
                        len(second),
                        f"{name} appended the correction instead of displacing it, "
                        "which would make it additive after all",
                    )
                self.assertNotIn(
                    json.dumps(first, ensure_ascii=False, sort_keys=True),
                    json.dumps(second, ensure_ascii=False, sort_keys=True),
                    f"{name} kept what it replaced, so it is not destructive",
                )

    def test_the_one_evidence_tool_annotated_additive_really_leaves_a_record_standing(self):
        """The control for the table above, and the reason it is not vacuous.

        `importAthleteHistory` is the only writer of this evidence still claiming
        `destructiveHint: false`, so that claim is the one worth an inverse test: a day
        the athlete has already stated survives an upload naming the same day, where any
        of the record tools above would have overwritten it.
        """
        self.assertIs(
            False, TOOLS_BY_NAME["importAthleteHistory"].annotations["destructiveHint"]
        )
        self.tool_result(
            "recordBodyMeasurement", {"date": "2026-08-12", "weight_kg": 72.5}
        )
        evidence_file = athlete_evidence.evidence_path(self.state_dir)
        before = json.loads(evidence_file.read_text(encoding="utf-8"))["body_measurements"]

        self.tool_result(
            "importAthleteHistory",
            {
                "source": "csv-export",
                "body_measurements": [{"date": "2026-08-12", "weight_kg": 99.9}],
            },
        )

        after = json.loads(evidence_file.read_text(encoding="utf-8"))["body_measurements"]
        self.assertEqual(before, after, "the upload overwrote what the athlete stated")

    def test_starting_a_session_really_does_write_which_is_why_it_is_not_read_only(self):
        """The inverse control, and the reason #117 singles this tool out.

        Reconciliation is made of store commits. A client told this were read-only would
        run it unannounced, retry it freely, and read the higher plan version that comes
        back as somebody else's edit.
        """
        self.fake.sport_settings = RUN_SPORT_SETTINGS
        current = self.tool_payload(self.tool_result("startCoachSession"))
        prepared = self.tool_payload(
            self.tool_result(
                "prepareWorkoutDelivery",
                {
                    "plan_id": current["plan_state"]["plan_id"],
                    "plan_version": current["plan_state"]["plan_version"],
                    "session_ids": ["run-quality-01"],
                },
            )
        )
        published = self.tool_payload(
            self.tool_result(
                "applyWorkoutDelivery",
                {
                    "delivery_set": prepared["delivery_set"],
                    "proposal_hash": prepared["proposal_hash"],
                    "confirmed": True,
                },
            )
        )
        self.assertEqual("passed", published["status"], published)
        # The provider event id no longer rides the apply response -- it is the store's
        # record. The session view still serves it, which is also where a model would
        # read delivery evidence back.
        state = self.tool_payload(self.tool_result("getCoachState"))
        delivered_id = next(
            item["external_id"]
            for item in state["delivery"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.fake.activities = [
            {
                "id": "i4001",
                "type": "Run",
                "start_date_local": "2026-08-13T07:00:00",
                "moving_time": 2400,
                "distance": 8000.0,
                "average_speed": 3.33,
                "average_heartrate": 158,
                "paired_event_id": delivered_id,
            }
        ]

        before = self.snapshot(self.state_dir)
        reconciled = self.tool_payload(self.tool_result("startCoachSession"))

        self.assertEqual(
            ["run-quality-01"],
            [item["session_id"] for item in reconciled["reconciliation"]["applied"]],
        )
        self.assertNotEqual(before, self.snapshot(self.state_dir))
        self.assertGreater(
            reconciled["plan_state"]["plan_version"],
            current["plan_state"]["plan_version"],
        )
        self.assertIs(False, TOOLS_BY_NAME["startCoachSession"].annotations["readOnlyHint"])


# --------------------------------------------------------------------------------------
# The orchestration prompt
# --------------------------------------------------------------------------------------


class McpPromptTests(McpTestCase):
    """Issue #125: what a connecting client receives above the tool schemas."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def test_the_server_advertises_prompts_alongside_tools(self):
        capabilities = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION})[
            "result"
        ]["capabilities"]
        self.assertEqual({"tools": {}, "prompts": {}}, capabilities)

    def test_both_layers_are_listed_and_neither_is_the_other(self):
        """Sequencing and coaching, served side by side and never merged.

        A client that received only the first would drive this product correctly and
        coach worse than the Skill does, which is what the hosted entry used to be.
        """
        prompts = self.rpc("prompts/list")["result"]["prompts"]

        self.assertEqual(
            [orchestration.PROMPT_NAME, orchestration.TRAINING_PROMPT_NAME],
            [prompt["name"] for prompt in prompts],
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt["name"]):
                self.assertTrue(prompt["title"].strip())
                self.assertTrue(prompt["description"].strip())
                # No arguments: there is nothing to parameterise, and a client that had
                # to supply one could get either layer wrong before the first turn.
                self.assertNotIn("arguments", prompt)

    def test_getting_the_training_layer_returns_the_training_file_itself(self):
        result = self.rpc(
            "prompts/get", {"name": orchestration.TRAINING_PROMPT_NAME}
        )["result"]

        self.assertEqual(1, len(result["messages"]))
        self.assertEqual(
            (ROOT / "garmin_coach_loop" / "hybrid_training.md")
            .read_text(encoding="utf-8")
            .rstrip("\r\n"),
            result["messages"][0]["content"]["text"],
        )

    def test_getting_it_returns_the_orchestration_file_itself(self):
        result = self.rpc(
            "prompts/get", {"name": orchestration.PROMPT_NAME}
        )["result"]

        self.assertEqual(1, len(result["messages"]))
        message = result["messages"][0]
        self.assertEqual("user", message["role"])
        self.assertEqual("text", message["content"]["type"])
        # One file, two readers -- not two copies held in step by a comparison.
        self.assertEqual(
            (ROOT / "garmin_coach_loop" / "orchestration.md")
            .read_text(encoding="utf-8")
            .rstrip("\r\n"),
            message["content"]["text"],
        )

    def test_it_carries_the_orchestration_a_tool_schema_cannot(self):
        text = self.rpc("prompts/get", {"name": orchestration.PROMPT_NAME})["result"][
            "messages"
        ][0]["content"]["text"]

        for phrase in (
            # Which call answers a question, and what the answer is authoritative about.
            "`startCoachSession`",
            "only source of truth",
            # Where exactly one confirmation stands.
            "ONE confirmation",
            # What a delivery result may be claimed to prove.
            "Garmin Connect or the watch",
            # How to read a refusal.
            "`stale_plan_version`",
        ):
            self.assertIn(phrase, text, phrase)

    def test_a_prompt_name_this_server_does_not_serve_is_a_protocol_error(self):
        response = self.rpc("prompts/get", {"name": "coach_nutrition"})
        self.assertEqual(mcp_transport.INVALID_PARAMS, response["error"]["code"])
        self.assertNotIn("result", response)

    def test_prompts_are_still_behind_the_same_identity_check_as_tools(self):
        status, headers, _ = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}, token=None
        )
        self.assertEqual(401, status)
        self.assertIn("WWW-Authenticate", headers)


# --------------------------------------------------------------------------------------
# OAuth discovery and dynamic client registration
# --------------------------------------------------------------------------------------


class McpDiscoveryTests(McpTestCase):
    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        request = urllib.request.Request(self.base_url + path, method="GET")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def test_the_protected_resource_metadata_is_readable_without_a_token(self):
        status, payload = self.get("/.well-known/oauth-protected-resource")

        self.assertEqual(200, status)
        self.assertEqual(
            {
                "resource": f"{self.base_url}/mcp",
                "authorization_servers": [self.base_url],
                "bearer_methods_supported": ["header"],
                # The same four names the authorization server advertises, so a client
                # that reads only the document the `401` challenge names can still say
                # what it is about to ask Intervals for.
                "scopes_supported": [
                    "ACTIVITY:READ",
                    "WELLNESS:READ",
                    "CALENDAR:WRITE",
                    "SETTINGS:WRITE",
                ],
            },
            payload,
        )
        self.assertFalse((self.state_root / "owners").exists())

    def test_the_authorization_server_metadata_points_at_this_gateways_own_endpoints(self):
        status, payload = self.get("/.well-known/oauth-authorization-server")

        self.assertEqual(200, status)
        self.assertEqual(
            {
                "issuer": self.base_url,
                "authorization_endpoint": f"{self.base_url}/oauth/authorize",
                "token_endpoint": f"{self.base_url}/oauth/token",
                "registration_endpoint": f"{self.base_url}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [
                    "ACTIVITY:READ",
                    "WELLNESS:READ",
                    "CALENDAR:WRITE",
                    "SETTINGS:WRITE",
                ],
            },
            payload,
        )

    def test_both_documents_name_the_forwarded_origin_behind_a_proxy(self):
        forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "coach.example"}

        _, resource = self.get("/.well-known/oauth-protected-resource", forwarded)
        _, server = self.get("/.well-known/oauth-authorization-server", forwarded)

        self.assertEqual("https://coach.example/mcp", resource["resource"])
        self.assertEqual(["https://coach.example"], resource["authorization_servers"])
        self.assertEqual("https://coach.example", server["issuer"])
        self.assertEqual(
            "https://coach.example/oauth/authorize", server["authorization_endpoint"]
        )

    def test_the_path_aware_well_known_variants_serve_the_same_documents(self):
        # RFC 9728 and RFC 8414 both define a form where the resource's own path is
        # appended to the well-known prefix, and a client that looks only there and finds
        # nothing cannot begin an authorization at all.
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            with self.subTest(path=path):
                plain_status, plain = self.get(path)
                variant_status, variant = self.get(path + "/mcp")
                self.assertEqual(200, plain_status)
                self.assertEqual(200, variant_status)
                self.assertEqual(plain, variant)

    def test_registration_mints_a_client_id_of_its_own_and_never_a_secret(self):
        status, payload = self.call(
            "POST",
            "/oauth/register",
            body={
                "client_name": "Test MCP Client",
                "redirect_uris": ["https://client.example/callback"],
            },
        )

        self.assertEqual(201, status)
        self.assertEqual(["https://client.example/callback"], payload["redirect_uris"])
        self.assertEqual("none", payload["token_endpoint_auth_method"])
        self.assertEqual(["authorization_code"], payload["grant_types"])
        self.assertEqual(["code"], payload["response_types"])
        self.assertEqual("Test MCP Client", payload["client_name"])
        self.assertIsInstance(payload["client_id_issued_at"], int)
        self.assertNotIn("client_secret", payload)
        self.assertNotIn(CLIENT_SECRET_VALUE, json.dumps(payload))
        # The Intervals application's own id is this gateway's credential upstream, not
        # an identity an MCP client is handed. What the client gets is its registration.
        self.assertNotEqual(CLIENT_ID_VALUE, payload["client_id"])
        self.assertEqual(
            ["https://client.example/callback"],
            token_envelope.open_envelope(
                payload["client_id"],
                kind=token_envelope.CLIENT_REGISTRATION,
                key=HMAC_KEY,
                now=self.now,
                max_age_seconds=None,
            )["redirect_uris"],
        )

    def test_two_registrations_are_two_clients(self):
        first = self.registered_client_id("https://client.example/callback")
        second = self.registered_client_id("https://client.example/callback")
        self.assertNotEqual(first, second)

    def test_registration_without_usable_redirect_uris_is_refused(self):
        for body in ({}, {"redirect_uris": []}, {"redirect_uris": [""]}, {"redirect_uris": "x"}):
            with self.subTest(body=body):
                self.assert_registration_refused(body, because=self.MALFORMED)

    def test_a_plaintext_callback_is_registrable_only_on_loopback(self):
        for uri in (
            "http://127.0.0.1:1234/callback",
            "http://[::1]:1234/callback",
            "http://localhost:1234/callback",
        ):
            with self.subTest(uri=uri):
                status, payload = self.register(uri)
                self.assertEqual(201, status)
                self.assertEqual([uri], payload["redirect_uris"])

        for uri in ("http://client.example/callback", "http://127.0.0.1.evil.example/cb"):
            with self.subTest(uri=uri):
                self.assert_registration_refused([uri], because=self.MALFORMED)

    def test_a_callback_that_is_not_a_web_callback_is_refused_at_registration(self):
        for uri in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "myapp://callback",
            "/relative",
            "https://client.example/callback#fragment",
            7,
        ):
            with self.subTest(uri=uri):
                self.assert_registration_refused([uri], because=self.MALFORMED)

    def test_one_unusable_uri_refuses_the_whole_registration(self):
        # Not the usable ones minus the bad one: a client told it is registered, whose
        # callback silently is not, finds out at authorize time with nothing to connect
        # the two refusals.
        self.assert_registration_refused(
            ["https://client.example/callback", "javascript:alert(1)"],
            because=self.MALFORMED,
        )


class McpRegistrationTrustTests(McpTestCase):
    """Who may be registered at all -- the question PKCE and redirect binding cannot ask.

    Every other OAuth test here runs against a gateway that has been *configured* to trust
    the example client (see ``TEST_CLIENT_ORIGINS``), because those tests are about what a
    registered client may then do. This one takes that configuration away, so what is left
    is the shipped default: loopback, and the connector origins whose flow has actually
    been validated against this gateway.
    """

    def setUp(self):
        super().setUp()
        self.gateway.config = replace(self.config, trusted_client_origins=())

    def trust(self, *origins: str) -> None:
        self.gateway.config = replace(self.config, trusted_client_origins=origins)

    def assert_refused(self, *redirect_uris: str) -> None:
        self.assert_registration_refused(redirect_uris, because=self.UNTRUSTED)

    def test_an_arbitrary_https_callback_can_no_longer_register(self):
        # The whole point: before this, anyone could take a client id for a callback they
        # controlled, and the athlete's consent at Intervals named the Coach application
        # without naming who would receive the authorization it produced.
        self.assert_refused("https://evil.example/callback")

    def test_the_refusal_says_what_a_person_can_do_about_it(self):
        # A client that cannot connect, with no stated reason, is a support ticket. RFC
        # 7591 gives one error code to both kinds of bad callback, so the description is
        # the only thing separating "fix your URI" from "ask the operator to trust you".
        _, untrusted = self.register("https://new-agent.example/callback")
        _, malformed = self.register("myapp://callback")

        self.assertIn("loopback", untrusted["error_description"])
        self.assertIn("operator", untrusted["error_description"])
        self.assertNotIn("operator", malformed["error_description"])
        # And neither one echoes the URI that was rejected.
        self.assertNotIn("new-agent.example", json.dumps(untrusted))
        self.assertNotIn("myapp", json.dumps(malformed))

    def test_a_validated_connector_host_registers_without_operator_action(self):
        # The supported hosted distribution paths have to work on a fresh deployment that
        # configured nothing, or trust would be a manual step on every new install.
        for uri in (
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            # ChatGPT mints a callback id per connector instance, and published apps from
            # before that change still use the older fixed path. Trusting the origin
            # covers both without this list knowing either path.
            "https://chatgpt.com/connector/oauth/01JQZ9X4EXAMPLE",
            "https://chatgpt.com/connector/oauth/a-completely-different-id",
            "https://chatgpt.com/connector_platform_oauth_redirect",
        ):
            with self.subTest(uri=uri):
                status, payload = self.register(uri)
                self.assertEqual(201, status, payload)
                self.assertEqual([uri], payload["redirect_uris"])

    def test_a_platform_is_admitted_by_configuration_and_not_by_a_code_change(self):
        self.assert_refused("https://new-agent.example/oauth/callback")

        self.trust("https://new-agent.example")

        status, payload = self.register("https://new-agent.example/oauth/callback")
        self.assertEqual(201, status, payload)

    def test_trust_is_the_exact_origin_and_a_lookalike_is_not_it(self):
        # Same reduction the `/mcp` Origin check uses, for the same reason: a suffix test
        # would make every attacker-owned subdomain of a trusted name trusted.
        for uri in (
            "https://claude.ai.evil.example/callback",
            "https://evil.example/claude.ai/callback",
            "https://claude.ai:8443/callback",
            "https://chatgpt.com.evil.example/connector/oauth/x",
            "https://chatgpt.com:8443/connector/oauth/x",
            # Userinfo, which a reader skims as the host and a browser does not: the
            # request goes to 1.2.3.4. An origin carrying any is not an origin.
            "https://claude.ai@1.2.3.4/callback",
        ):
            with self.subTest(uri=uri):
                self.assert_refused(uri)

    def test_a_local_client_still_registers_on_any_loopback_port(self):
        # RFC 8252: the port is bound when the client starts listening, so a local client
        # cannot be asked to have its address trusted in advance -- and does not need to
        # be, since the code never leaves the athlete's own machine.
        for uri in (
            "http://127.0.0.1:0/callback",
            "http://127.0.0.1:52341/callback",
            "http://[::1]:9999/callback",
            "http://localhost:1234/callback",
            # A local client that does hold a certificate for its own loopback is no less
            # local for using it, and pinning its origin would pin the port it cannot
            # promise. The host is what makes this local, not the scheme.
            "https://127.0.0.1:8443/callback",
            "https://localhost:52341/callback",
        ):
            with self.subTest(uri=uri):
                status, payload = self.register(uri)
                self.assertEqual(201, status, payload)
                self.assertEqual([uri], payload["redirect_uris"])

    def test_one_untrusted_uri_refuses_the_whole_registration(self):
        # Same rule as an unusable URI: a registration that silently kept only its
        # acceptable half would hand back an id whose other callback fails later.
        self.assert_refused(
            "http://127.0.0.1:1234/callback", "https://evil.example/callback"
        )

    def test_the_hostile_flow_stops_before_the_athlete_can_consent(self):
        """The complete hostile flow, carried to the point where it dies.

        An attacker registers their own callback, gets a client id, starts PKCE with a
        verifier they hold, and induces the athlete to approve the real Coach application
        at Intervals. Every later check passes for them -- they *are* the initiating
        client -- so the only place this can be stopped is the first one.
        """
        self.assert_refused("https://evil.example/callback")
        _, registration = self.register("https://evil.example/callback")
        self.assertNotIn("client_id", registration)

        # With no id to present, the rest of the flow has nothing to start from. The two
        # things an attacker might try instead -- an invented id, and this gateway's own
        # Intervals credential -- are refused as unregistered clients.
        verifier = "attacker-verifier-0123456789abcdefghijklmnopqrstuv"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        for client_id in ("invented-client-id", CLIENT_ID_VALUE):
            with self.subTest(client_id=client_id):
                query = urllib.parse.urlencode(
                    {
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": "https://evil.example/callback",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                )
                status, payload = self.call(
                    "GET", "/oauth/authorize?" + query
                )
                self.assertEqual(400, status)
                self.assertEqual({"error": "unauthorized_client"}, payload)

        # Nothing reached Intervals at any point, so the athlete was never shown a consent
        # screen for an authorization that would have been delivered to the attacker.
        self.assertEqual([], self.fake.calls)


class TokenEnvelopeTests(unittest.TestCase):
    """The envelope's own properties, below the HTTP layer that carries it."""

    PAYLOAD = {"intervals_token": TOKEN_A, "aud": "https://coach.example/mcp", "iat": 1_700_000_000}
    NOW = dt.datetime.fromtimestamp(1_700_000_000, dt.timezone.utc)

    def open(self, sealed: str, **overrides: Any) -> dict[str, Any]:
        return token_envelope.open_envelope(
            sealed,
            **{
                "kind": token_envelope.ACCESS_TOKEN,
                "key": HMAC_KEY,
                "now": self.NOW,
                "max_age_seconds": None,
                **overrides,
            },
        )

    def sealed(self) -> str:
        return token_envelope.seal(
            self.PAYLOAD, kind=token_envelope.ACCESS_TOKEN, key=HMAC_KEY
        )

    def test_what_goes_in_comes_back_out_and_never_appears_on_the_wire(self):
        sealed = self.sealed()
        self.assertEqual(self.PAYLOAD, self.open(sealed))
        self.assertNotIn(TOKEN_A, sealed)
        self.assertNotIn("intervals_token", sealed)
        # A fresh nonce every time: the same payload sealed twice is two different
        # strings, which is what keeps one keystream from being reused.
        self.assertNotEqual(sealed, self.sealed())

    def test_a_single_changed_byte_anywhere_refuses(self):
        sealed = self.sealed()
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        # Front, middle and end: the nonce, the ciphertext, and the tag.
        for index in (0, len(sealed) // 2, len(sealed) - 1):
            with self.subTest(index=index):
                replacement = alphabet[(alphabet.index(sealed[index]) + 1) % len(alphabet)]
                tampered = sealed[:index] + replacement + sealed[index + 1 :]
                with self.assertRaises(token_envelope.EnvelopeError):
                    self.open(tampered)

    def test_an_envelope_has_one_spelling_and_the_others_are_not_it(self):
        """The last character of unpadded base64 carries bits no byte needs.

        Every decoder ignores them, so a value differing only there would decode to the
        same envelope and open exactly as the original does -- which would make the test
        above pass or fail on whether a random tag happened to end in a character whose
        successor changed a real bit. It also has to be false of a ``client_id``, which is
        an identity compared as a string at the token endpoint: two spellings of one
        registration is one of them being an id nothing issued.
        """
        for _ in range(64):
            sealed = self.sealed()
            for last in ("A", "B"):
                if sealed[-1] == last:
                    continue
                with self.assertRaises(token_envelope.EnvelopeError):
                    self.open(sealed[:-1] + last)

    def test_another_key_opens_nothing(self):
        with self.assertRaises(token_envelope.EnvelopeError):
            self.open(self.sealed(), key=b"another-deployment-key-0000000000")

    def test_a_kind_is_not_a_label_that_can_be_swapped(self):
        for kind in (
            token_envelope.AUTHORIZE_STATE,
            token_envelope.AUTHORIZATION_CODE,
            token_envelope.CLIENT_REGISTRATION,
        ):
            with self.subTest(kind=kind):
                other = token_envelope.seal(self.PAYLOAD, kind=kind, key=HMAC_KEY)
                with self.assertRaises(token_envelope.EnvelopeError):
                    self.open(other)
                self.assertEqual(self.PAYLOAD, self.open(other, kind=kind))

    def test_an_age_limit_applies_only_where_one_is_asked_for(self):
        sealed = self.sealed()
        later = self.NOW + dt.timedelta(seconds=60)
        self.assertEqual(self.PAYLOAD, self.open(sealed, now=later))
        self.assertEqual(self.PAYLOAD, self.open(sealed, now=later, max_age_seconds=61))
        with self.assertRaises(token_envelope.EnvelopeError):
            self.open(sealed, now=later, max_age_seconds=60)

    def test_a_payload_with_no_issue_time_is_not_sealable(self):
        with self.assertRaises(token_envelope.EnvelopeError):
            token_envelope.seal(
                {"intervals_token": TOKEN_A}, kind=token_envelope.ACCESS_TOKEN, key=HMAC_KEY
            )

    def test_anything_that_is_not_an_envelope_refuses_the_same_way(self):
        for value in ("", "not-base64!!", TOKEN_A, "a" * 64, None, 7):
            with self.subTest(value=value):
                with self.assertRaises(token_envelope.EnvelopeError):
                    self.open(value)


# --------------------------------------------------------------------------------------
# The authorization server this gateway runs in front of Intervals
# --------------------------------------------------------------------------------------


CLIENT_REDIRECT_URI = "https://client.example/callback"
# One PKCE pair, computed the way RFC 7636 says and hardcoded so the test states what it
# is proving rather than recomputing the implementation.
CODE_VERIFIER = "test-verifier-0123456789abcdefghijklmnopqrstuvwxyz"
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode("ascii")).digest())
    .decode("ascii")
    .rstrip("=")
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Read the redirect instead of following it -- Intervals is not reachable here."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class McpAuthorizationServerTests(McpTestCase):
    """The whole OAuth dance, over the real server, with Intervals faked at both hops."""

    def setUp(self):
        super().setUp()
        # Every flow below starts where a real client starts: at registration. The id it
        # is given there is the only thing `/oauth/authorize` accepts as a client.
        self.client_id = self.registered_client_id(CLIENT_REDIRECT_URI)

    def request(
        self, method: str, url: str, *, form: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], Any]:
        data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers), exc.read()

    def authorize(self, **overrides: str) -> tuple[int, dict[str, str], Any]:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": CLIENT_REDIRECT_URI,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state-1",
            "resource": f"{self.base_url}/mcp",
            **overrides,
        }
        query = {key: value for key, value in query.items() if value is not None}
        return self.request(
            "GET", self.base_url + "/oauth/authorize?" + urllib.parse.urlencode(query)
        )

    @staticmethod
    def query_of(location: str) -> dict[str, str]:
        return {
            key: values[0]
            for key, values in urllib.parse.parse_qs(
                urllib.parse.urlsplit(location).query
            ).items()
        }

    def consent(self, location: str, *, provider_code: str = "provider-code-1") -> str:
        """Play Intervals: take the redirect it was sent, hand the state back with a code.

        The gateway's redirect is checked here rather than in a separate test, because a
        redirect that did not carry these exact parameters would never have reached a
        consent page at all.
        """
        self.assertTrue(location.startswith(INTERVALS_AUTHORIZE_URL), location)
        sent = self.query_of(location)
        # The Intervals application, which is what a client id means on this hop -- the
        # MCP client's own registration never leaves this gateway.
        self.assertEqual(CLIENT_ID_VALUE, sent["client_id"])
        self.assertEqual(f"{self.base_url}/oauth/callback", sent["redirect_uri"])
        self.assertNotIn("code_challenge", sent)
        callback = self.base_url + "/oauth/callback?" + urllib.parse.urlencode(
            {"code": provider_code, "state": sent["state"]}
        )
        status, headers, _ = self.request("GET", callback)
        self.assertEqual(302, status)
        return headers["Location"]

    def token(self, code: str, **overrides: str) -> tuple[int, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "code_verifier": CODE_VERIFIER,
            "redirect_uri": CLIENT_REDIRECT_URI,
            **overrides,
        }
        form = {key: value for key, value in form.items() if value is not None}
        status, _, body = self.request("POST", self.base_url + "/oauth/token", form=form)
        return status, json.loads(body or b"{}")

    def connect(self, provider_token: str = TOKEN_A, *, athlete_id: str = "i1") -> str:
        """Run the whole flow once and return the bearer the client ends up holding."""
        self.fake.token_payload = {
            "access_token": provider_token,
            "scope": ",".join(INTERVALS_OAUTH_SCOPES),
            "athlete": {"id": athlete_id},
        }
        status, headers, _ = self.authorize()
        self.assertEqual(302, status)
        client_location = self.consent(headers["Location"])
        returned = self.query_of(client_location)
        self.assertTrue(client_location.startswith(CLIENT_REDIRECT_URI), client_location)
        self.assertEqual("client-state-1", returned["state"])
        status, payload = self.token(returned["code"])
        self.assertEqual(200, status)
        return payload["access_token"]

    # -- the whole dance --------------------------------------------------------------

    def test_an_authorized_client_reaches_a_tool_with_the_provider_token_inside(self):
        init_store(self.owner_dir(
            lookup_or_create_owner(self.identity_db, "intervals", "i1")
        ), publishable_plan())

        bearer = self.connect()

        # What the client holds is this gateway's token, not the athlete's.
        self.assertNotEqual(TOKEN_A, bearer)
        self.assertNotIn(TOKEN_A, bearer)

        result = self.tool_result("startCoachSession", {"all_clear": True}, bearer=bearer)
        self.assertNotEqual(True, result.get("isError"), result)
        self.assertEqual("passed", self.tool_payload(result)["status"])
        # And what Intervals saw is the athlete's own token, unwrapped.
        provider_reads = [
            header for header in self.fake.authorizations if header.startswith("Bearer ")
        ]
        self.assertTrue(provider_reads)
        self.assertEqual({f"Bearer {TOKEN_A}"}, set(provider_reads))
        self.assertNotIn(bearer, " ".join(self.fake.authorizations))

    def test_the_client_secret_and_the_provider_token_stay_on_the_server(self):
        bearer = self.connect()
        # The exchange with Intervals carried the secret; nothing that reached the client
        # did. The code that travelled through the browser is an envelope, not a token.
        self.assertEqual([CLIENT_SECRET_VALUE], self.fake.token_forms[0]["client_secret"])
        self.assertNotIn(CLIENT_SECRET_VALUE, bearer)
        logged = "\n".join(self.log_handler.records)
        for secret in (TOKEN_A, CLIENT_SECRET_VALUE, bearer):
            self.assertNotIn(secret, logged)

    # -- PKCE -------------------------------------------------------------------------

    def test_a_wrong_verifier_redeems_nothing(self):
        self.fake.token_payload = {"access_token": TOKEN_A, "athlete": {"id": "i1"}}
        _, headers, _ = self.authorize()
        code = self.query_of(self.consent(headers["Location"]))["code"]

        status, payload = self.token(code, code_verifier=CODE_VERIFIER + "-not")

        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_grant"}, payload)

    def test_an_authorization_without_a_challenge_never_starts(self):
        for missing in ({"code_challenge": ""}, {"code_challenge_method": "plain"}):
            with self.subTest(**missing):
                status, _, body = self.authorize(**missing)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_request"}, json.loads(body))
                self.assertEqual([], self.fake.calls)

    def test_an_authorization_for_another_client_or_response_type_never_starts(self):
        status, _, body = self.authorize(client_id="somebody-elses-app")
        self.assertEqual(400, status)
        self.assertEqual({"error": "unauthorized_client"}, json.loads(body))

        status, _, body = self.authorize(response_type="token")
        self.assertEqual(400, status)
        self.assertEqual({"error": "unsupported_response_type"}, json.loads(body))

    def test_a_redirect_uri_that_is_not_a_web_callback_is_refused(self):
        for uri in (
            "",
            "javascript:alert(1)",
            "/relative",
            "https://client.example/#x",
            "http://client.example/callback",
        ):
            with self.subTest(uri=uri):
                status, _, body = self.authorize(redirect_uri=uri)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_request"}, json.loads(body))
                self.assertEqual([], self.fake.calls)

    # -- which client, and which of its callbacks -------------------------------------

    def test_the_intervals_client_id_no_longer_authorizes_anything(self):
        # The attack this registration exists to close. The Intervals client id is a
        # public value -- it is in every authorize URL the athlete's browser has ever
        # followed -- so anyone could present it and, while it was accepted here, name
        # any callback they liked. The athlete consented at Intervals, and this gateway
        # sealed their credential into a code it redirected to the attacker, who held
        # the PKCE verifier because they had started the flow. Nothing about PKCE
        # defends that; only refusing the client id does.
        status, _, body = self.authorize(
            client_id=CLIENT_ID_VALUE, redirect_uri="https://attacker.example/callback"
        )

        self.assertEqual(400, status)
        self.assertEqual({"error": "unauthorized_client"}, json.loads(body))
        # Refused here, so the athlete was never shown a consent page to approve.
        self.assertEqual([], self.fake.calls)

    def test_a_client_id_this_gateway_did_not_seal_is_not_a_registered_client(self):
        tampered = self.client_id[:-1] + ("A" if self.client_id[-1] != "A" else "B")
        other_kind = token_envelope.seal(
            {"redirect_uris": [CLIENT_REDIRECT_URI], "iat": int(self.now.timestamp())},
            kind=token_envelope.AUTHORIZE_STATE,
            key=HMAC_KEY,
        )
        elsewhere = token_envelope.seal(
            {"redirect_uris": [CLIENT_REDIRECT_URI], "iat": int(self.now.timestamp())},
            kind=token_envelope.CLIENT_REGISTRATION,
            key=b"another-deployment-key-0000000000",
        )

        for client_id in ("", "not-an-id", tampered, other_kind, elsewhere):
            with self.subTest(client_id=client_id):
                status, _, body = self.authorize(client_id=client_id)
                self.assertEqual(400, status)
                self.assertEqual({"error": "unauthorized_client"}, json.loads(body))
                self.assertEqual([], self.fake.calls)

    def test_a_client_can_only_authorize_the_callbacks_it_registered(self):
        other = self.registered_client_id("https://other.example/callback")

        status, _, body = self.authorize(redirect_uri="https://other.example/callback")
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_request"}, json.loads(body))
        self.assertEqual([], self.fake.calls)

        # And the same URI under the registration that does hold it starts normally, so
        # the refusal above is about the pairing and not about the URI.
        status, _, _ = self.authorize(
            client_id=other, redirect_uri="https://other.example/callback"
        )
        self.assertEqual(302, status)

    def test_a_loopback_client_may_come_back_on_whatever_port_it_ended_up_with(self):
        # RFC 8252 7.3: the port is chosen when the client starts listening, which is
        # after it registered, so the port is the one part of a loopback callback that
        # cannot be matched exactly.
        client_id = self.registered_client_id("http://127.0.0.1:1234/callback")

        status, _, _ = self.authorize(
            client_id=client_id, redirect_uri="http://127.0.0.1:59999/callback"
        )
        self.assertEqual(302, status)

        for uri in (
            "http://127.0.0.1:59999/elsewhere",
            "http://localhost:1234/callback",
            "http://127.0.0.1:1234/callback?extra=1",
        ):
            with self.subTest(uri=uri):
                status, _, body = self.authorize(client_id=client_id, redirect_uri=uri)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_request"}, json.loads(body))

    def test_a_public_callback_matches_its_port_like_everything_else(self):
        client_id = self.registered_client_id("https://client.example:8443/callback")

        status, _, _ = self.authorize(
            client_id=client_id, redirect_uri="https://client.example:8443/callback"
        )
        self.assertEqual(302, status)

        status, _, body = self.authorize(
            client_id=client_id, redirect_uri="https://client.example:9443/callback"
        )
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_request"}, json.loads(body))

    def test_a_code_only_redeems_under_the_client_id_it_was_issued_to(self):
        self.fake.token_payload = {"access_token": TOKEN_A, "athlete": {"id": "i1"}}
        other = self.registered_client_id(CLIENT_REDIRECT_URI)
        _, headers, _ = self.authorize()
        code = self.query_of(self.consent(headers["Location"]))["code"]

        status, payload = self.token(code, client_id=other)
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_client"}, payload)

        status, payload = self.token(code, client_id=None)
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_client"}, payload)

        # The same code under its own registration still redeems, so the two refusals
        # are about the client id and not about the code being spent.
        self.assertEqual(200, self.token(code)[0])

    def test_a_registration_does_not_expire_and_take_a_working_connector_with_it(self):
        # A client id that stopped opening after N days would take a connector down with
        # a failure the athlete could neither predict nor act on.
        self.now += dt.timedelta(days=400)
        self.assertEqual(302, self.authorize()[0])

    # -- what the code is bound to ----------------------------------------------------

    def test_a_code_is_refused_once_its_minute_has_passed(self):
        self.fake.token_payload = {"access_token": TOKEN_A, "athlete": {"id": "i1"}}
        _, headers, _ = self.authorize()
        code = self.query_of(self.consent(headers["Location"]))["code"]

        self.now += dt.timedelta(seconds=AUTHORIZATION_CODE_TTL_SECONDS + 1)
        status, payload = self.token(code)

        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_grant"}, payload)

    def test_a_code_only_redeems_at_the_redirect_uri_and_resource_it_was_issued_for(self):
        self.fake.token_payload = {"access_token": TOKEN_A, "athlete": {"id": "i1"}}
        _, headers, _ = self.authorize()
        code = self.query_of(self.consent(headers["Location"]))["code"]

        status, payload = self.token(code, redirect_uri="https://attacker.example/callback")
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_grant"}, payload)

        status, payload = self.token(code, resource="https://elsewhere.example/mcp")
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_target"}, payload)

        # The unaltered request still works, so the two refusals above are about the
        # fields they changed and not about the code being spent.
        self.assertEqual(200, self.token(code)[0])

    def test_the_other_envelope_kinds_cannot_be_redeemed_as_a_code(self):
        for kind in (
            token_envelope.AUTHORIZE_STATE,
            token_envelope.ACCESS_TOKEN,
            token_envelope.CLIENT_REGISTRATION,
        ):
            with self.subTest(kind=kind):
                forged = token_envelope.seal(
                    {
                        "intervals_token": TOKEN_A,
                        "client_id": self.client_id,
                        "client_redirect_uri": CLIENT_REDIRECT_URI,
                        "code_challenge": CODE_CHALLENGE,
                        "aud": self.base_url + "/mcp",
                        "iat": int(self.now.timestamp()),
                    },
                    kind=kind,
                    key=HMAC_KEY,
                )
                status, payload = self.token(forged)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_grant"}, payload)

    def test_a_state_this_gateway_did_not_issue_reaches_no_client(self):
        status, _, body = self.request(
            "GET", self.base_url + "/oauth/callback?code=c1&state=not-ours"
        )
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_request"}, json.loads(body))
        self.assertEqual([], self.fake.calls)

    def test_a_refusal_at_intervals_comes_back_as_access_denied_and_nothing_more(self):
        _, headers, _ = self.authorize()
        state = self.query_of(headers["Location"])["state"]

        status, callback_headers, _ = self.request(
            "GET",
            self.base_url
            + "/oauth/callback?"
            + urllib.parse.urlencode({"error": "user_denied_everything", "state": state}),
        )

        self.assertEqual(302, status)
        returned = self.query_of(callback_headers["Location"])
        self.assertTrue(callback_headers["Location"].startswith(CLIENT_REDIRECT_URI))
        self.assertEqual("access_denied", returned["error"])
        self.assertEqual("client-state-1", returned["state"])
        self.assertNotIn("user_denied_everything", callback_headers["Location"])

    def test_an_exchange_intervals_refuses_comes_back_as_access_denied(self):
        self.fake.token_status = 400
        _, headers, _ = self.authorize()
        state = self.query_of(headers["Location"])["state"]

        status, callback_headers, _ = self.request(
            "GET",
            self.base_url + "/oauth/callback?" + urllib.parse.urlencode(
                {"code": "provider-code-1", "state": state}
            ),
        )

        self.assertEqual(302, status)
        self.assertEqual("access_denied", self.query_of(callback_headers["Location"])["error"])

    def test_the_token_endpoint_answers_the_refresh_grant_the_way_the_other_one_does(self):
        status, _, body = self.request(
            "POST",
            self.base_url + "/oauth/token",
            form={"grant_type": "refresh_token", "refresh_token": "r1"},
        )
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_grant"}, json.loads(body))

        status, _, body = self.request(
            "POST", self.base_url + "/oauth/token", form={"grant_type": "client_credentials"}
        )
        self.assertEqual(400, status)
        self.assertEqual({"error": "unsupported_grant_type"}, json.loads(body))

    def test_the_issued_token_states_no_lifetime_it_cannot_honour(self):
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": "ACTIVITY:READ,WELLNESS:READ",
            "athlete": {"id": "i1"},
        }
        _, headers, _ = self.authorize()
        code = self.query_of(self.consent(headers["Location"]))["code"]

        _, payload = self.token(code)

        self.assertEqual({"token_type", "access_token", "scope"}, set(payload))
        self.assertEqual("Bearer", payload["token_type"])
        self.assertEqual("ACTIVITY:READ,WELLNESS:READ", payload["scope"])

    def test_a_client_that_asks_for_scopes_the_rfc_way_still_asks_intervals_its_way(self):
        # `scopes_supported` is a JSON list, so a conforming client joins with spaces;
        # Intervals reads commas. A verbatim forward would authorize nothing.
        _, headers, _ = self.authorize(scope="ACTIVITY:READ WELLNESS:READ")
        self.assertEqual(
            "ACTIVITY:READ,WELLNESS:READ", self.query_of(headers["Location"])["scope"]
        )

        _, headers, _ = self.authorize(scope="")
        self.assertEqual(
            ",".join(INTERVALS_OAUTH_SCOPES), self.query_of(headers["Location"])["scope"]
        )

    # -- two clients, one athlete -----------------------------------------------------

    def test_a_second_client_connecting_does_not_disconnect_the_first(self):
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        init_store(self.owner_dir(owner_id), publishable_plan())

        first = self.connect(TOKEN_A)
        second = self.connect(TOKEN_B)

        self.assertNotEqual(first, second)
        for bearer in (first, second):
            with self.subTest(bearer=bearer):
                payload = self.tool_payload(
                    self.tool_result("startCoachSession", {"all_clear": True}, bearer=bearer)
                )
                self.assertEqual("passed", payload["status"])
                self.assertEqual("fixture-plan-001", payload["plan_state"]["plan_id"])
        for token in (TOKEN_A, TOKEN_B):
            self.assertEqual(
                owner_id,
                owner_for_fingerprint(
                    self.identity_db, token_fingerprint(token, hmac_key=HMAC_KEY)
                ),
            )

    # -- a connection that stopped working --------------------------------------------

    def test_a_revoked_provider_token_is_a_401_the_client_can_recover_from(self):
        init_store(self.owner_dir(
            lookup_or_create_owner(self.identity_db, "intervals", "i1")
        ), publishable_plan())
        bearer = self.connect()
        self.fake.read_status = 401

        status, headers, body = self.post_mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "startCoachSession", "arguments": {"all_clear": True}},
            },
            bearer=bearer,
        )

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, json.loads(body))
        self.assertIn("resource_metadata=", headers["WWW-Authenticate"])

    def test_a_provider_outage_stays_a_tool_result_the_model_can_read(self):
        # The distinction that makes the 401 above meaningful: only a refused credential
        # restarts the authorization. A bad minute upstream is still coaching state the
        # model should report, not a reason to disconnect the athlete.
        init_store(self.owner_dir(
            lookup_or_create_owner(self.identity_db, "intervals", "i1")
        ), publishable_plan())
        bearer = self.connect()
        self.fake.read_status = 500

        result = self.tool_result("startCoachSession", {"all_clear": True}, bearer=bearer)

        self.assertTrue(result["isError"])
        self.assertEqual("provider_error", self.tool_payload(result)["error"])

    def test_the_rest_entry_still_reports_a_revoked_token_as_a_provider_error(self):
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.fake.read_status = 401

        status, payload = self.call(
            "POST", "/v1/coach/session", body={"all_clear": True}, token=TOKEN_A
        )

        self.assertEqual(502, status)
        self.assertEqual("provider_error", payload["error"])


class TrustedOriginRevocationTests(McpAuthorizationServerTests):
    """Issue #121: removing an origin has to stop the clients it already issued.

    A registration is sealed into the ``client_id`` and never expires, which is what
    keeps a working connector alive across restarts -- and is also why, when the trust
    list was consulted at registration only, removing an origin refused new clients and
    left every existing one bringing athletes through consent. There is no client table
    to delete from, so the authorize hop is the lever.
    """

    def untrust(self, *origins: str) -> None:
        """Rebuild the deployment with a different trusted set, as a redeploy would.

        The list is read once at startup into the config rather than per request, so a
        test that reached into the frozen dataclass would be testing something no
        operator can do.
        """
        self.gateway = CoachGateway(
            replace(self.config, trusted_client_origins=origins),
            fetch=self.fake,
            now=lambda: self.now,
        )
        self.server.gateway = self.gateway

    def test_a_client_on_a_removed_origin_can_no_longer_start_an_authorization(self):
        # It could a moment ago: same client id, same callback, same request.
        status, headers, _ = self.authorize()
        self.assertEqual(302, status)
        self.assertTrue(headers["Location"].startswith(INTERVALS_AUTHORIZE_URL))

        self.untrust()

        status, _, body = self.authorize()
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", json.loads(body)["error"])
        self.assertEqual(
            security_log.UNTRUSTED_REDIRECT_ORIGIN,
            self.security_events()[-1]["reason"],
        )

    def test_the_refusal_lands_before_the_athlete_could_consent(self):
        """Which is the only place it is worth landing.

        Past the consent screen the athlete has already approved the real application at
        Intervals, and the code comes back to whoever started the flow.
        """
        self.untrust()
        self.fake.calls.clear()

        status, headers, _ = self.authorize()

        self.assertEqual(400, status)
        self.assertNotIn("Location", headers)
        self.assertEqual([], self.fake.calls)

    def test_an_origin_that_is_still_trusted_is_untouched(self):
        """The cost of option 1, stated: removal is immediate and it is not selective.

        An operator tightening the list carelessly takes down the connectors on the
        origin they removed. Every other origin keeps working, which is what makes that
        a decision rather than an outage.
        """
        self.untrust("https://client.example", "https://client.example:8443")

        status, headers, _ = self.authorize()

        self.assertEqual(302, status)
        self.assertTrue(headers["Location"].startswith(INTERVALS_AUTHORIZE_URL))

    def test_a_local_client_never_needed_the_list_and_still_does_not(self):
        loopback = "http://127.0.0.1:52341/callback"
        self.client_id = self.registered_client_id(loopback)
        self.untrust()

        status, headers, _ = self.authorize(redirect_uri=loopback)

        self.assertEqual(302, status)
        self.assertTrue(headers["Location"].startswith(INTERVALS_AUTHORIZE_URL))

    def test_a_built_in_host_is_not_removable_by_configuration(self):
        """Honest limitation: the environment variable adds origins, it does not subtract.

        Un-trusting claude.ai or chatgpt.com is a code change, and the blunt instrument
        that reaches every client at once remains rotating the token HMAC key
        (docs/deploy-gateway.md).
        """
        self.client_id = self.registered_client_id("https://claude.ai/api/mcp/auth_callback")
        self.untrust()

        status, headers, _ = self.authorize(
            redirect_uri="https://claude.ai/api/mcp/auth_callback"
        )

        self.assertEqual(302, status)
        self.assertTrue(headers["Location"].startswith(INTERVALS_AUTHORIZE_URL))


class SecurityEventTests(McpAuthorizationServerTests):
    """What the deployment can reconstruct afterwards, and what it must never have kept.

    Prevention above; evidence here. These run on the same real flow, and read the events
    back out of the log the process actually writes -- so what is asserted is what an
    operator would have to work with, not an internal call record.
    """

    def chain(self) -> list[tuple[str, str, str | None]]:
        return [
            (event["event"], event["result"], event["reason"])
            for event in self.security_events()
        ]

    def test_one_normal_flow_is_one_correlated_chain(self):
        # setUp already registered this client, so the chain starts there and runs to an
        # authenticated tool call: the five boundary crossings, in order.
        init_store(self.owner_dir(
            lookup_or_create_owner(self.identity_db, "intervals", "i1")
        ), publishable_plan())

        bearer = self.connect()
        self.tool_result("startCoachSession", {"all_clear": True}, bearer=bearer)

        self.assertEqual(
            [
                ("client_registration", "accepted", None),
                ("authorization", "accepted", None),
                ("provider_callback", "accepted", None),
                ("token_issuance", "accepted", None),
                ("mcp_authentication", "accepted", None),
            ],
            self.chain(),
        )
        # One flow, one handle: the registration and the authenticated call it eventually
        # produced can be joined without either of them naming the client.
        handles = {event["client"] for event in self.security_events()}
        self.assertEqual(1, len(handles), handles)
        handle = handles.pop()
        self.assertIsInstance(handle, str)
        self.assertNotIn(handle, self.client_id)
        # And the callback appears as its origin alone, never as the URI that was used.
        self.assertEqual(
            {"https://client.example", None},
            {event["origin"] for event in self.security_events()},
        )

    def test_a_blocked_registration_records_the_origin_it_named(self):
        status, _ = self.register("https://evil.example/callback")

        self.assertEqual(400, status)
        self.assertEqual(
            {
                "event": "client_registration",
                "result": "refused",
                "reason": "untrusted_redirect_origin",
                "origin": "https://evil.example",
                # Nothing was issued, so there is no client to correlate -- which is
                # itself the finding: an attempt that never became a client.
                "client": None,
            },
            self.security_events()[-1],
        )

    def test_each_hop_records_which_check_refused_it(self):
        self.fake.token_payload = {"access_token": TOKEN_A, "athlete": {"id": "i1"}}
        self.authorize(client_id="somebody-elses-app")
        self.authorize(response_type="token")
        self.authorize(code_challenge="")
        _, headers, _ = self.authorize()
        code = self.query_of(self.consent(headers["Location"]))["code"]
        self.token(code, code_verifier=CODE_VERIFIER + "-not")
        self.token(code, grant_type="refresh_token")
        self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)
        self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer="not-a-token"
        )

        self.assertEqual(
            [
                ("client_registration", "accepted", None),
                ("authorization", "refused", "unknown_client"),
                ("authorization", "refused", "unsupported_response_type"),
                ("authorization", "refused", "missing_pkce_challenge"),
                ("authorization", "accepted", None),
                ("provider_callback", "accepted", None),
                ("token_issuance", "refused", "pkce_verification_failed"),
                ("token_issuance", "refused", "no_refresh_grant"),
                ("mcp_authentication", "refused", "missing_bearer"),
                ("mcp_authentication", "refused", "unrecognized_token"),
            ],
            self.chain(),
        )

    def test_the_token_carries_the_handle_and_not_the_registration(self):
        # A `client_id` is a sealed registration of its own -- inlining it would put most
        # of a kilobyte into a header sent on every request of the connection's life, to
        # say a 16-character thing. The correlation is identical either way, which the
        # chain test above proves; this holds the size and the exposure down.
        bearer = self.connect()

        opened = token_envelope.open_envelope(
            bearer,
            kind=token_envelope.ACCESS_TOKEN,
            key=HMAC_KEY,
            now=self.now,
            max_age_seconds=None,
        )
        self.assertNotIn("client_id", opened)
        self.assertNotIn(self.client_id, bearer)
        self.assertEqual(16, len(opened["client"]))
        self.assertEqual(opened["client"], self.security_events()[-1]["client"])

    def test_a_token_issued_before_this_existed_still_authenticates(self):
        # The live connectors are holding tokens minted without a `client_id` inside, and
        # an event stream is not a reason to disconnect them. Such a token authenticates
        # exactly as it did; what is lost is only the handle joining it to its own flow.
        self.seed_owner(TOKEN_A, plan=publishable_plan())

        result = self.tool_result("startCoachSession", {"all_clear": True})

        self.assertNotEqual(True, result.get("isError"), result)
        self.assertEqual(
            {"event": "mcp_authentication", "result": "accepted", "reason": None,
             "origin": None, "client": None},
            self.security_events()[-1],
        )

    def test_every_event_stays_inside_the_declared_shape(self):
        self.connect()
        self.register("https://evil.example/callback")
        self.authorize(client_id="somebody-elses-app")
        self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)

        events = self.security_events()
        self.assertTrue(events)
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(set(security_log.FIELDS), set(event))
                self.assertIn(event["event"], security_log.EVENTS)
                self.assertIn(event["result"], security_log.RESULTS)
                self.assertIn(event["reason"], security_log.REASONS | {None})
                self.assertNotEqual(security_log.UNCLASSIFIED, event["event"])
                self.assertNotEqual(security_log.UNCLASSIFIED, event["reason"])

    def test_no_credential_identity_or_url_detail_reaches_the_log(self):
        """The property that makes this evidence safe to retain at all.

        Asserted over the whole log rather than over the events alone, because the point
        is what the deployment keeps -- and it is asserted after a flow that carried every
        one of these values through the process.
        """
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", "i1")
        init_store(self.owner_dir(owner_id), publishable_plan())
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": ",".join(INTERVALS_OAUTH_SCOPES),
            "athlete": {"id": "i1"},
        }
        _, headers, _ = self.authorize()
        location = headers["Location"]
        provider_state = self.query_of(location)["state"]
        client_location = self.consent(location)
        code = self.query_of(client_location)["code"]
        status, payload = self.token(code)
        self.assertEqual(200, status)
        bearer = payload["access_token"]
        self.tool_result("startCoachSession", {"all_clear": True}, bearer=bearer)
        self.register("https://evil.example/cb-for-athlete-i1?athlete=i1")

        logged = "\n".join(self.log_handler.records)
        self.assertTrue(self.security_events())
        for secret in (
            TOKEN_A,  # the athlete's provider credential
            CLIENT_SECRET_VALUE,  # this gateway's own upstream secret
            HMAC_KEY.decode("ascii"),  # the key every envelope and handle derives from
            bearer,  # the token the client now holds
            code,  # the authorization code it redeemed
            provider_state,  # the sealed authorize state
            CODE_VERIFIER,  # the PKCE secret
            CODE_CHALLENGE,  # and its public half, which pairs with it
            self.client_id,  # the registration itself, as opposed to its handle
            "client-state-1",  # the client's own OAuth state
            owner_id,  # whose store this is
            CLIENT_REDIRECT_URI,  # a callback as a URI rather than as an origin
            "cb-for-athlete-i1",  # a path, and so anything a client chose to put in one
            "athlete=i1",  # a query parameter, for the same reason
        ):
            with self.subTest(secret=secret[:16]):
                self.assertNotIn(secret, logged)


class SecurityLogTests(unittest.TestCase):
    """The event shape's own properties, below the endpoints that emit it."""

    KEY = b"unit-test-security-log-key-000000"

    def test_a_callback_is_reduced_to_its_origin_and_nothing_else(self):
        for uri, expected in (
            ("https://client.example/callback", "https://client.example"),
            ("https://Client.Example/cb?a=b#c", "https://client.example"),
            ("https://client.example:8443/cb", "https://client.example:8443"),
            # The port stays: it is part of the origin, and on loopback it is the only
            # thing separating one local client from another.
            ("http://127.0.0.1:52341/callback", "http://127.0.0.1:52341"),
        ):
            with self.subTest(uri=uri):
                self.assertEqual(expected, security_log.redirect_origin(uri))

    def test_anything_that_is_not_a_callback_is_written_as_absent(self):
        for value in ("", None, 7, "javascript:alert(1)", "/relative", "not a url",
                      "https://client.example:secret@1.2.3.4/cb", "https://a b/cb"):
            with self.subTest(value=value):
                self.assertIsNone(security_log.redirect_origin(value))

    def test_a_client_handle_is_stable_opaque_and_keyed_to_the_deployment(self):
        client_id = "sealed-client-registration-value"
        handle = security_log.client_fingerprint(client_id, key=self.KEY)

        self.assertEqual(handle, security_log.client_fingerprint(client_id, key=self.KEY))
        self.assertNotIn(handle, client_id)
        self.assertNotIn(client_id, handle)
        # Another deployment's key gives another handle, so events cannot be correlated
        # across deployments by anyone holding neither key.
        self.assertNotEqual(
            handle, security_log.client_fingerprint(client_id, key=b"another-key-00000000000000000000")
        )
        self.assertNotEqual(
            handle, security_log.client_fingerprint(client_id + "x", key=self.KEY)
        )
        self.assertIsNone(security_log.client_fingerprint("", key=self.KEY))
        self.assertIsNone(security_log.client_fingerprint(None, key=self.KEY))

    def test_a_reason_outside_the_vocabulary_is_never_passed_through(self):
        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
        logger = logging.getLogger(security_log.LOGGER_NAME)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            security_log.emit(
                security_log.AUTHORIZATION,
                security_log.REFUSED,
                key=self.KEY,
                reason="provider said: token tok-alpha-1 is invalid",
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        event = json.loads(records[-1].split(" ", 1)[1])
        self.assertEqual(security_log.UNCLASSIFIED, event["reason"])
        self.assertNotIn("tok-alpha-1", records[-1])


class PublicBaseUrlTests(unittest.TestCase):
    """The one place a wrong answer would send a client somewhere else to authorize."""

    def test_the_forwarded_pair_wins_over_the_host_header(self):
        headers = {
            "Host": "internal:8422",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "coach.example",
        }
        self.assertEqual("https://coach.example", public_base_url(headers))

    def test_only_the_first_hop_of_a_proxy_chain_is_read(self):
        headers = {
            "X-Forwarded-Proto": "https, http",
            "X-Forwarded-Host": "coach.example, internal",
        }
        self.assertEqual("https://coach.example", public_base_url(headers))

    def test_a_direct_request_falls_back_to_the_host_header(self):
        self.assertEqual("http://127.0.0.1:8422", public_base_url({"Host": "127.0.0.1:8422"}))

    def test_an_unusable_host_yields_nothing_rather_than_a_guess(self):
        for host in ("", "coach.example\"\r\nX-Evil: 1", "coach example", "a/b"):
            with self.subTest(host=host):
                self.assertIsNone(public_base_url({"Host": host}))

    def test_an_unknown_forwarded_scheme_falls_back_rather_than_being_echoed(self):
        headers = {"Host": "coach.example", "X-Forwarded-Proto": "javascript"}
        self.assertEqual("http://coach.example", public_base_url(headers))

    def test_the_documents_are_absolute_urls_under_the_given_origin(self):
        base = "https://coach.example"
        self.assertEqual(f"{base}/mcp", protected_resource_metadata(base)["resource"])
        self.assertEqual(base, authorization_server_metadata(base)["issuer"])


# --------------------------------------------------------------------------------------
# The whole loop, over this entry alone
# --------------------------------------------------------------------------------------


class McpJourneyTests(McpTestCase):
    """One athlete's loop, end to end, with nothing carried between requests.

    The property under test is continuity. The server keeps no session, so everything a
    later step depends on -- and everything a *next conversation* depends on -- must
    come back out of the store. Each test therefore finishes by handshaking again, as a
    fresh client would, and asserting that what it reads is what the confirmed write
    left behind, not what the previous conversation remembered.
    """

    def setUp(self):
        super().setUp()
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]

    def handshake(self) -> None:
        response = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "journey-client", "version": "0"},
            },
        )
        self.assertEqual(PROTOCOL_VERSION, response["result"]["protocolVersion"])
        status, _, body = self.post_mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(202, status)
        self.assertEqual(b"", body)

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.tool_result(name, arguments)
        self.assertNotEqual(True, result.get("isError"), result)
        return self.tool_payload(result)

    def test_a_change_previewed_and_applied_is_what_a_new_conversation_reads(self):
        before = load("plan-state-v1.json")
        context = load("coach-context-day-4.json")
        self.seed_owner(TOKEN_A, plan=before)
        self.handshake()

        session = self.tool("startCoachSession", {"all_clear": True})
        self.assertEqual(1, session["plan_state"]["plan_version"])

        shared = {
            "plan_id": before["plan_id"],
            "plan_version": before["version"],
            "context": context,
            "change_request": WEEKLY_CHANGE,
        }
        prepared = self.tool("prepareCoachDecision", shared)
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual(2, prepared["resulting_version"])

        applied = self.tool(
            "applyCoachDecision",
            {**shared, "proposal": prepared["proposal"], "confirmed": True},
        )
        self.assertEqual(2, applied["plan_version"])

        self.handshake()
        again = self.tool("startCoachSession", {"all_clear": True})
        self.assertEqual(2, again["plan_state"]["plan_version"])
        self.assertEqual(before["plan_id"], again["plan_state"]["plan_id"])

    def test_a_confirmed_delivery_survives_to_the_next_conversation_and_the_calendar(self):
        plan = publishable_plan()
        owner_id = self.seed_owner(TOKEN_A, plan=plan)
        self.handshake()

        session = self.tool("startCoachSession", {"all_clear": True})
        self.assertEqual(1, session["plan_state"]["plan_version"])

        prepared = self.tool(
            "prepareWorkoutDelivery",
            {
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "session_ids": ["run-quality-01", "run-long-01"],
            },
        )
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual([], self.fake.bulk_calls)

        published = self.tool(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        self.assertEqual("intervals_accepted", published["delivery_state"])
        self.assertEqual(2, len(published["delivered"]))
        self.assertEqual(2, len(self.fake.bulk_calls))

        self.handshake()
        again = self.tool("startCoachSession", {"all_clear": True})
        self.assertEqual(2, again["plan_state"]["plan_version"])

        current = read_current_plan(self.owner_dir(owner_id))["current_plan"]
        delivered = {
            session["session_id"]: session["execution"]
            for session in current["week"]["sessions"]
            if session["session_id"] in {"run-quality-01", "run-long-01"}
        }
        self.assertEqual(2, len(delivered))
        for execution in delivered.values():
            self.assertEqual("intervals_accepted", execution["delivery_state"])
            self.assertTrue(execution["external_id"])

    def test_the_withdraw_direction_removes_a_superseded_event_through_the_same_pair(self):
        """withdraw: true on prepareWorkoutDelivery, applied by the same applyWorkoutDelivery.

        The test above is the "vice versa": the plain, withdraw-absent call delivers.
        This one supersedes what it delivered and removes it, through the identical
        prepare/apply pair -- proving the two directions really do converge on one set
        of tool names rather than one covering the other's cases only by coincidence.
        """
        plan = publishable_plan()
        self.seed_owner(TOKEN_A, plan=plan)
        self.handshake()

        session = self.tool("startCoachSession", {"all_clear": True})
        prepared = self.tool(
            "prepareWorkoutDelivery",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
        )
        self.assertEqual("deliver", prepared["delivery_set"]["direction"])
        delivered = self.tool(
            "applyWorkoutDelivery",
            {
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        self.assertEqual("intervals_accepted", delivered["delivery_state"])

        # A confirmed change that replaces the delivered session leaves the event it
        # published superseded rather than deleting it -- the same fixture change
        # tests/test_gateway.py's GatewayWithdrawalTests._supersede uses.
        current = self.tool("startCoachSession", {"all_clear": True})
        # The provider event id comes off the session's delivery view, where a model
        # would read it too -- the apply response no longer carries it.
        delivered_id = next(
            item["external_id"]
            for item in current["delivery"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        shared = {
            "plan_id": current["plan_state"]["plan_id"],
            "plan_version": current["plan_state"]["plan_version"],
            "context": current["context"],
            "change_request": {
                "summary": "改成完全休息",
                "reason_codes": ["multi_signal_recovery_down"],
                "evidence": [
                    {"field": "recovery_trends.hrv", "observation": "HRV 連三天偏低"}
                ],
                "goal_effect": {"week": "本週少一次刺激", "cycle": "28 天方向不變"},
                "next_review_condition": "休息後重新評估",
                "sessions": [
                    {
                        "operation": "replace",
                        "session_id": "run-quality-01",
                        "sport": "rest",
                        "purpose": "完全休息",
                        "adaptation": "recovery",
                        "cost": "easy",
                        "planned_minutes": 0,
                        "plan": {"kind": "unstructured"},
                    }
                ],
            },
        }
        decision_prepared = self.tool("prepareCoachDecision", shared)
        self.tool(
            "applyCoachDecision",
            {**shared, "proposal": decision_prepared["proposal"], "confirmed": True},
        )

        withdrawing = self.tool("startCoachSession", {"all_clear": True})
        withdrawal_prepared = self.tool(
            "prepareWorkoutDelivery",
            {
                "plan_id": withdrawing["plan_state"]["plan_id"],
                "plan_version": withdrawing["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
        )
        self.assertEqual("withdraw", withdrawal_prepared["delivery_set"]["direction"])
        self.assertEqual(
            [delivered_id],
            [item["superseded_external_id"] for item in withdrawal_prepared["preview"]],
        )
        withdrawn = self.tool(
            "applyWorkoutDelivery",
            {
                "delivery_set": withdrawal_prepared["delivery_set"],
                "proposal_hash": withdrawal_prepared["proposal_hash"],
                "confirmed": True,
            },
        )
        self.assertEqual("passed", withdrawn["status"], withdrawn)
        # The apply response names the withdrawn session; the provider event id stayed in
        # the preview above (superseded_external_id) and in the store's receipt.
        self.assertEqual(
            ["run-quality-01"], [item["session_id"] for item in withdrawn["withdrawn"]]
        )
        self.assertEqual([], withdrawn["unresolved"])

    def test_a_set_prepared_for_one_direction_is_refused_applied_as_the_other(self):
        """The direction is one of the fields the athlete's confirmation binds.

        Flipping it alone (without re-preparing) is exactly what a confused or
        adversarial client might send. It is refused because ``proposal_hash`` covers
        ``direction``, so the flipped set is no longer the set that was confirmed --
        not because a delivery item and a withdrawal item happen to have disjoint
        shapes today. The check therefore keeps working if those shapes ever converge.
        """
        plan = publishable_plan()
        self.seed_owner(TOKEN_A, plan=plan)
        self.handshake()

        session = self.tool("startCoachSession", {"all_clear": True})
        prepared = self.tool(
            "prepareWorkoutDelivery",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
        )
        relabelled = {**prepared["delivery_set"], "direction": "withdraw"}

        result = self.tool_result(
            "applyWorkoutDelivery",
            {
                "delivery_set": relabelled,
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
        )

        self.assertTrue(result["isError"], result)
        payload = self.tool_payload(result)
        self.assertEqual("delivery_blocked", payload["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_the_confirmed_direction_is_covered_by_the_proposal_hash_itself(self):
        """Not just refused at apply -- the hash the athlete confirmed changes with it.

        The regression this guards is subtle: while the direction rode *beside* the
        signed set, a relabelled set still hashed to the confirmed value, and only the
        item-shape mismatch downstream stopped it. Re-hashing the flipped set here shows
        the binding itself moved, which is what AGENTS.md 7 asks of an approval.
        """
        plan = publishable_plan()
        self.seed_owner(TOKEN_A, plan=plan)
        self.handshake()

        session = self.tool("startCoachSession", {"all_clear": True})
        prepared = self.tool(
            "prepareWorkoutDelivery",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
        )
        delivery_set = prepared["delivery_set"]
        self.assertEqual("deliver", delivery_set["direction"])

        def set_hash(value):
            return canonical_hash(
                {key: item for key, item in value.items() if key != "proposal_hash"}
            )

        self.assertEqual(delivery_set["proposal_hash"], set_hash(delivery_set))
        self.assertNotEqual(
            delivery_set["proposal_hash"],
            set_hash({**delivery_set, "direction": "withdraw"}),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
