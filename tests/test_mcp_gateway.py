"""The MCP entry, proven against the same loopback server the REST entry is proven on.

Every test here goes through a real socket and the real handler, so what is asserted is
what an MCP client would actually receive -- including the response headers, which is
where the OAuth discovery contract lives.

The OpenAPI contract test at the bottom is the anti-drift half: it reads
entrypoints/custom-gpt/openapi.yaml with the same line-level, fixed-indentation scan
tests/test_openapi_contract.py uses (stdlib only, no YAML parser) and holds every tool to
the name and the required fields of the Action operation it shares a name with. Two
entries, one command surface.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from garmin_coach_loop import mcp_transport, token_envelope
from garmin_coach_loop.gateway import (
    AUTHORIZATION_CODE_TTL_SECONDS,
    INTERVALS_AUTHORIZE_URL,
    INTERVALS_OAUTH_SCOPES,
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
from garmin_coach_loop.store import init_store, read_current_plan

# The REST entry's own harness -- a real loopback server over one injected fetcher --
# reused rather than rebuilt: a second fake provider would be a second answer to what
# Intervals does. Resolved by the documented `unittest discover -s tests` run, which puts
# this directory on the path.
from test_gateway import (
    CLIENT_ID_VALUE,
    CLIENT_SECRET_VALUE,
    HMAC_KEY,
    TOKEN_A,
    TOKEN_B,
    UNKNOWN_TOKEN,
    WEEKLY_CHANGE,
    GatewayTestCase,
    load,
    publishable_plan,
)


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"

# The two health operations are platform checks, not coaching capability, so they are the
# only documented operations that must never become a tool.
HEALTH_OPERATION_IDS = {"healthCheck", "readinessCheck"}


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

    def test_the_three_envelopes_are_not_interchangeable(self):
        # One key, three uses. A state or a code presented as a bearer opens nothing,
        # because the kind is part of what the tag covers.
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        payload = {
            "intervals_token": TOKEN_A,
            "aud": self.base_url + "/mcp",
            "scope": "ACTIVITY:READ",
            "client_redirect_uri": "https://client.example/callback",
            "code_challenge": "x",
            "iat": int(self.now.timestamp()),
        }
        for kind in (token_envelope.AUTHORIZE_STATE, token_envelope.AUTHORIZATION_CODE):
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
        # The Custom GPT contract is settled; only /mcp gained the header.
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

    def test_initialize_answers_with_the_supported_version_and_tools_only(self):
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
        self.assertEqual({"tools": {}}, result["capabilities"])
        self.assertEqual("garmin-coach-loop", result["serverInfo"]["name"])
        self.assertTrue(result["serverInfo"]["version"])

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


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


class McpToolTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def test_the_catalogue_is_the_whole_coaching_surface_and_nothing_else(self):
        tools = self.rpc("tools/list")["result"]["tools"]

        self.assertEqual(15, len(tools))
        self.assertEqual(
            {
                "startCoachSession",
                "inspectIntervalsPermissions",
                "recordAthleteProfile",
                "recordAthleteAvailability",
                "recordStrengthExecution",
                "confirmPrescribedStrength",
                "prepareCoachInitialization",
                "initializeCoachPlan",
                "prepareCoachDecision",
                "applyCoachDecision",
                "prepareWorkoutDelivery",
                "publishWorkoutDelivery",
                "prepareDeliveryWithdrawal",
                "applyDeliveryWithdrawal",
                "clearDeliveryAttempt",
            },
            {tool["name"] for tool in tools},
        )
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"].strip())
                self.assertEqual("object", tool["inputSchema"]["type"])

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

    def test_a_tool_with_no_arguments_still_reaches_its_route(self):
        self.fake.sport_settings = []
        payload = self.tool_payload(self.tool_result("inspectIntervalsPermissions"))
        self.assertEqual("passed", payload["status"])
        self.assertEqual("readable", payload["settings_read"])

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
                    "SETTINGS:READ",
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

    def test_registration_hands_back_the_public_client_and_never_a_secret(self):
        status, payload = self.call(
            "POST",
            "/oauth/register",
            body={
                "client_name": "Test MCP Client",
                "redirect_uris": ["https://client.example/callback"],
            },
        )

        self.assertEqual(201, status)
        self.assertEqual(CLIENT_ID_VALUE, payload["client_id"])
        self.assertEqual(["https://client.example/callback"], payload["redirect_uris"])
        self.assertEqual("none", payload["token_endpoint_auth_method"])
        self.assertNotIn("client_secret", payload)
        self.assertNotIn(CLIENT_SECRET_VALUE, json.dumps(payload))

    def test_registration_without_usable_redirect_uris_is_refused(self):
        for body in ({}, {"redirect_uris": []}, {"redirect_uris": [""]}, {"redirect_uris": "x"}):
            with self.subTest(body=body):
                status, payload = self.call("POST", "/oauth/register", body=body)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_redirect_uri"}, payload)


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

    def test_another_key_opens_nothing(self):
        with self.assertRaises(token_envelope.EnvelopeError):
            self.open(self.sealed(), key=b"another-deployment-key-0000000000")

    def test_a_kind_is_not_a_label_that_can_be_swapped(self):
        for kind in (token_envelope.AUTHORIZE_STATE, token_envelope.AUTHORIZATION_CODE):
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
            "client_id": CLIENT_ID_VALUE,
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
        for uri in ("", "javascript:alert(1)", "/relative", "https://client.example/#x"):
            with self.subTest(uri=uri):
                status, _, body = self.authorize(redirect_uri=uri)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid_request"}, json.loads(body))

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

    def test_the_other_two_envelope_kinds_cannot_be_redeemed_as_a_code(self):
        for kind in (token_envelope.AUTHORIZE_STATE, token_envelope.ACCESS_TOKEN):
            with self.subTest(kind=kind):
                forged = token_envelope.seal(
                    {
                        "intervals_token": TOKEN_A,
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
            "publishWorkoutDelivery",
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


# --------------------------------------------------------------------------------------
# The OpenAPI contract
# --------------------------------------------------------------------------------------


_PATH_LINE = re.compile(r"^  (/\S+):\s*$")
_OPERATION_ID_LINE = re.compile(r"^      operationId:\s*(\S+)\s*$")
_REQUEST_SCHEMA_LINE = re.compile(r'^              \$ref: "#/components/schemas/(\w+)"\s*$')
_SCHEMA_LINE = re.compile(r"^    (\w+):\s*$")
_REQUIRED_ITEM_LINE = re.compile(r"^        - (\S+)\s*$")
_SCHEMA_PROPERTY_LINE = re.compile(r"^        (\w+):\s*$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _schema_block(lines: list[str], name: str) -> list[str]:
    """The lines of one ``components.schemas`` entry, by the file's own indentation."""
    start = next(
        index
        for index, line in enumerate(lines)
        if (match := _SCHEMA_LINE.match(line)) and match.group(1) == name
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and _indent(lines[index]) <= 4:
            end = index
            break
    return lines[start + 1 : end]


def _operation_request_schemas(lines: list[str]) -> dict[str, str | None]:
    """operationId -> the components schema its requestBody uses, or None for a GET.

    The requestBody ``$ref`` sits at 14 spaces and a response's at 16, but the scan is
    bounded by ``requestBody:``/``responses:`` anyway so the two can never be confused.
    """
    starts = [i for i, line in enumerate(lines) if _PATH_LINE.match(line)]
    result: dict[str, str | None] = {}
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block = lines[start:end]
        operation_ids = [
            match.group(1) for line in block if (match := _OPERATION_ID_LINE.match(line))
        ]
        assert len(operation_ids) == 1, block[0]
        schema: str | None = None
        inside_request_body = False
        for line in block:
            if line == "      requestBody:":
                inside_request_body = True
            elif line == "      responses:":
                inside_request_body = False
            elif inside_request_body and (match := _REQUEST_SCHEMA_LINE.match(line)):
                schema = match.group(1)
        result[operation_ids[0]] = schema
    return result


def _schema_required(lines: list[str], name: str) -> set[str]:
    """The named schema's own top-level ``required`` list; nested ones keep their own."""
    block = _schema_block(lines, name)
    try:
        required_at = block.index("      required:")
    except ValueError:
        return set()
    fields = set()
    for line in block[required_at + 1 :]:
        match = _REQUIRED_ITEM_LINE.match(line)
        if match is None:
            break
        fields.add(match.group(1))
    return fields


def _schema_properties(lines: list[str], name: str) -> set[str]:
    """The named schema's own top-level property names, by the same indentation rule."""
    return {
        match.group(1)
        for line in _schema_block(lines, name)
        if (match := _SCHEMA_PROPERTY_LINE.match(line))
    }




class McpOpenApiContractTests(unittest.TestCase):
    def setUp(self):
        self.lines = OPENAPI_PATH.read_text(encoding="utf-8").splitlines()
        self.request_schemas = _operation_request_schemas(self.lines)
        self.coach_operations = {
            operation: schema
            for operation, schema in self.request_schemas.items()
            if operation not in HEALTH_OPERATION_IDS
        }

    def test_every_coach_operation_has_a_tool_of_the_same_name(self):
        self.assertEqual(set(self.coach_operations), set(TOOLS_BY_NAME))
        self.assertEqual(len(self.coach_operations), len(TOOLS))

    def test_the_health_operations_are_not_coaching_capability(self):
        for operation in HEALTH_OPERATION_IDS:
            self.assertIn(operation, self.request_schemas)
            self.assertNotIn(operation, TOOLS_BY_NAME)

    def test_each_tool_requires_exactly_what_its_operation_requires(self):
        for operation, schema in self.coach_operations.items():
            with self.subTest(operation=operation):
                expected = set() if schema is None else _schema_required(self.lines, schema)
                actual = set(TOOLS_BY_NAME[operation].input_schema.get("required", []))
                self.assertEqual(expected, actual)

    def test_each_tool_schema_names_the_same_top_level_fields(self):
        # The nested shapes are deliberately not compared field for field -- an MCP schema
        # is not a copy of an OpenAPI document -- but a top-level field present in one and
        # missing from the other is a capability the two entries do not share.
        for operation, schema in self.coach_operations.items():
            if schema is None:
                continue
            with self.subTest(operation=operation):
                self.assertEqual(
                    _schema_properties(self.lines, schema),
                    set(TOOLS_BY_NAME[operation].input_schema.get("properties", {})),
                )

    def test_the_advertised_scopes_are_the_openapi_security_scopes(self):
        # The authorize request an MCP client builds from `scopes_supported` must ask
        # Intervals for exactly what the Custom GPT entry asks for -- one product, one
        # grant shape, whichever entry the athlete connects through.
        declared = {
            match.group(1)
            for line in self.lines
            if (match := re.match(r'\s+"([A-Z]+:[A-Z]+)":', line))
        }
        self.assertEqual(declared, set(INTERVALS_OAUTH_SCOPES))
        self.assertTrue(declared)

    def test_a_tool_schema_is_self_contained(self):
        # No $ref anywhere: an MCP client resolves nothing, so a reference would reach it
        # as a field it cannot fill.
        rendered = json.dumps([tool.descriptor() for tool in TOOLS])
        self.assertNotIn("$ref", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
