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

import json
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from garmin_coach_loop import mcp_transport
from garmin_coach_loop.gateway import (
    INTERVALS_OAUTH_SCOPES,
    ROUTES,
    authorization_server_metadata,
    protected_resource_metadata,
    public_base_url,
)
from garmin_coach_loop.mcp_transport import PROTOCOL_VERSION, TOOLS, TOOLS_BY_NAME

# The REST entry's own harness -- a real loopback server over one injected fetcher --
# reused rather than rebuilt: a second fake provider would be a second answer to what
# Intervals does. Resolved by the documented `unittest discover -s tests` run, which puts
# this directory on the path.
from test_gateway import (
    CLIENT_ID_VALUE,
    CLIENT_SECRET_VALUE,
    HMAC_KEY,
    TOKEN_A,
    UNKNOWN_TOKEN,
    GatewayTestCase,
    publishable_plan,
)


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"

# The two health operations are platform checks, not coaching capability, so they are the
# only documented operations that must never become a tool.
HEALTH_OPERATION_IDS = {"healthCheck", "readinessCheck"}


class McpTestCase(GatewayTestCase):
    """One MCP request over the real server, with the headers kept."""

    def post_mcp(
        self,
        message: Any = None,
        *,
        token: str | None = TOKEN_A,
        raw: bytes | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        data = raw if raw is not None else json.dumps(message).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/mcp", data=data, method="POST")
        request.add_header("Content-Type", content_type)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
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

    def tool_result(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]

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

        self.assertEqual(14, len(tools))
        self.assertEqual(
            {
                "startCoachSession",
                "inspectIntervalsPermissions",
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

    def test_the_authorization_server_metadata_points_at_the_existing_passthrough(self):
        status, payload = self.get("/.well-known/oauth-authorization-server")

        self.assertEqual(200, status)
        self.assertEqual(
            {
                "issuer": self.base_url,
                "authorization_endpoint": f"{self.base_url}/oauth/intervals/authorize",
                "token_endpoint": f"{self.base_url}/oauth/intervals/token",
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
            "https://coach.example/oauth/intervals/authorize",
            server["authorization_endpoint"],
        )

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
