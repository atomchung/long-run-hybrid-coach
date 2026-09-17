"""One `/mcp`, two protocol eras, one coach underneath.

Issue #352: a client speaking 2026-07-28 was answered `400` and fell back to 2025, and a
second hand-written protocol era was the alternative to this. What the SDK migration has
to prove is not that the new code runs but that nothing a client or a reviewer can see
moved, so these tests are written as the two receipts:

- **Each era works end to end on its own terms.** The 2025 lifecycle is `initialize`,
  `notifications/initialized`, `tools/list`, `tools/call`. The 2026-07-28 one is
  `server/discover`, `tools/list`, `tools/call` -- no handshake, no session, a
  self-contained POST every time -- and it must never need the legacy path to answer.
- **They are the same coach.** The same owner calling the same tool through either era
  reaches `CoachGateway.route`, gets the same product result, and leaves the same state
  behind. A protocol revision is a way in; it is not a second product.

The harness is `test_mcp_gateway`'s -- a real loopback server, a real bearer token, one
injected provider fake -- because a second one would be a second answer to what this
gateway does.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from garmin_coach_loop import athlete_evidence, mcp_sdk_transport
from garmin_coach_loop.gateway import PRODUCT_VERSION
from garmin_coach_loop.mcp_transport import (
    SERVER_NAME,
    SERVER_TITLE,
    TOOLS,
    TOOLS_BY_NAME,
)
from mcp_types.jsonrpc import INVALID_PARAMS, METHOD_NOT_FOUND
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from test_gateway import TOKEN_A, publishable_plan
from test_mcp_gateway import PROTOCOL_VERSION, McpTestCase


MODERN = MODERN_PROTOCOL_VERSIONS[-1]

# The two envelope keys 2026-07-28 requires inside `params._meta` on every request, and
# the third it recommends. Written out rather than imported from the SDK's private
# constants so a client reading this file sees the wire, not an alias for it.
ENVELOPE_VERSION = "io.modelcontextprotocol/protocolVersion"
ENVELOPE_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
ENVELOPE_CLIENT = "io.modelcontextprotocol/clientInfo"


class EraTestCase(McpTestCase):
    """One request in either era's own shape, over the same endpoint."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def legacy(
        self, method: str, params: Any = None, *, message_id: Any = 1, **kwargs
    ) -> tuple[int, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        headers = dict(kwargs.pop("headers", None) or {})
        if method != "initialize":
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        status, _, body = self.post_mcp(message, headers=headers, **kwargs)
        return status, (json.loads(body) if body else None)

    def modern(
        self, method: str, params: Any = None, *, message_id: Any = 1, **kwargs
    ) -> tuple[int, Any]:
        """A 2026-07-28 request: the envelope in the body, mirrored in the headers.

        `Mcp-Method` and, for the name-bearing methods, `Mcp-Name` are what let an
        intermediary route without reading the body, and the server refuses a request
        whose headers disagree with it. Sent here because a real client sends them.
        """
        envelope = {
            ENVELOPE_VERSION: MODERN,
            ENVELOPE_CAPABILITIES: {},
            ENVELOPE_CLIENT: {"name": "era-test", "version": "0"},
        }
        message = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
            "params": {**(params or {}), "_meta": envelope},
        }
        headers = {
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": method,
            **(kwargs.pop("headers", None) or {}),
        }
        name = (params or {}).get("name") if method in {"tools/call", "prompts/get"} else None
        if isinstance(name, str):
            headers.setdefault("Mcp-Name", name)
        status, _, body = self.post_mcp(message, headers=headers, **kwargs)
        return status, (json.loads(body) if body else None)

    def call(self, era: str, name: str, arguments: dict[str, Any] | None = None) -> Any:
        send = self.modern if era == "modern" else self.legacy
        status, response = send("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertEqual(200, status, response)
        return response["result"]


class LegacyLifecycleTests(EraTestCase):
    """2025: initialize, notifications/initialized, tools/list, tools/call."""

    def test_the_whole_2025_lifecycle_completes_over_one_endpoint(self):
        status, initialized = self.legacy(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "era-test", "version": "0"},
            },
        )
        self.assertEqual(200, status)
        result = initialized["result"]
        self.assertEqual(PROTOCOL_VERSION, result["protocolVersion"])
        self.assertEqual(SERVER_NAME, result["serverInfo"]["name"])
        self.assertEqual(SERVER_TITLE, result["serverInfo"]["title"])
        self.assertEqual(PRODUCT_VERSION, result["serverInfo"]["version"])

        # The notification has no response at all, which is the protocol's own shape and
        # not an empty result object.
        status, _, body = self.post_mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
        )
        self.assertEqual(202, status)
        self.assertEqual(b"", body)

        status, listed = self.legacy("tools/list")
        self.assertEqual(200, status)
        self.assertEqual(
            sorted(tool.name for tool in TOOLS),
            sorted(entry["name"] for entry in listed["result"]["tools"]),
        )

        answered = self.call("legacy", "getCoachState")
        self.assertEqual("passed", self.tool_payload(answered)["status"])
        self.assertNotEqual(True, answered.get("isError"))

    def test_no_session_id_is_issued_so_a_restart_loses_nothing(self):
        _, headers, _ = self.post_mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "era-test", "version": "0"},
                },
            }
        )
        self.assertNotIn("Mcp-Session-Id", headers)


class ModernLifecycleTests(EraTestCase):
    """2026-07-28: server/discover, tools/list, tools/call, and no fallback."""

    def test_discover_answers_without_a_handshake_ever_happening(self):
        """The whole point of the migration, stated as one assertion.

        This connection sends no `initialize` and no `notifications/initialized`, and
        nothing before this request told the server anything about it. Before the SDK
        took the wire, the `MCP-Protocol-Version: 2026-07-28` on it was a `400` and the
        client's only way forward was to start again at 2025.
        """
        status, discovered = self.modern("server/discover")
        self.assertEqual(200, status, discovered)
        result = discovered["result"]
        self.assertEqual([MODERN], result["supportedVersions"])
        self.assertEqual({"listChanged": False}, result["capabilities"]["tools"])
        self.assertEqual({"listChanged": False}, result["capabilities"]["prompts"])
        # The sequencing layer, on the era that has no `initialize` to carry it.
        self.assertIn("startCoachSession", result["instructions"])
        self.assertEqual(
            {"name": SERVER_NAME, "title": SERVER_TITLE, "version": PRODUCT_VERSION},
            result["_meta"]["io.modelcontextprotocol/serverInfo"],
        )

    def test_the_modern_era_lists_and_calls_without_touching_the_legacy_path(self):
        status, listed = self.modern("tools/list")
        self.assertEqual(200, status, listed)
        self.assertEqual(
            sorted(tool.name for tool in TOOLS),
            sorted(entry["name"] for entry in listed["result"]["tools"]),
        )
        answered = self.call("modern", "getCoachState")
        self.assertEqual("passed", self.tool_payload(answered)["status"])
        self.assertEqual("complete", answered["resultType"])

    def test_a_header_that_disagrees_with_the_body_is_refused(self):
        """The routing headers are a promise about the body, so they are checked.

        This is the SDK's rung, not this product's, and it is here because the product
        depends on it: an intermediary that routed on `Mcp-Name` while the body named a
        different tool would be routing one athlete's call by another tool's rules.
        """
        status, refused = self.modern(
            "tools/call",
            {"name": "getCoachState", "arguments": {}},
            headers={"Mcp-Name": "exportOwnerData"},
        )
        self.assertEqual(400, status)
        self.assertIn("error", refused)
        self.assertEqual([], self.fake.calls)

    def test_a_request_without_the_envelope_is_refused_rather_than_read_as_legacy(self):
        """A 2026 header with a 2025 body is a broken client, not a negotiation."""
        status, _, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
        )
        self.assertEqual(400, status)
        self.assertEqual(INVALID_PARAMS, json.loads(body)["error"]["code"])

    def test_an_unserved_modern_revision_names_what_this_server_does_serve(self):
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    ENVELOPE_VERSION: "2099-01-01",
                    ENVELOPE_CAPABILITIES: {},
                }
            },
        }
        # The gateway's own header check runs first and refuses the revision outright;
        # what matters is that the answer names the served set either way, so a client
        # has something to retry with.
        status, _, body = self.post_mcp(
            message,
            headers={"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"},
        )
        self.assertEqual(400, status)
        self.assertEqual(
            list(mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS), json.loads(body)["supported"]
        )


class EraEquivalenceTests(EraTestCase):
    """The same owner, the same tool, either era: one coach, one result, one state."""

    def test_both_eras_serve_the_identical_tool_catalogue(self):
        legacy = self.legacy("tools/list")[1]["result"]["tools"]
        modern = self.modern("tools/list")[1]["result"]["tools"]
        self.assertEqual(legacy, modern)
        # And it is the catalogue this repository declares, field for field -- not a
        # re-derivation the SDK made from it.
        self.assertEqual([tool.descriptor() for tool in TOOLS], legacy)

    def test_both_eras_serve_the_identical_prompts(self):
        self.assertEqual(
            self.legacy("prompts/list")[1]["result"]["prompts"],
            self.modern("prompts/list")[1]["result"]["prompts"],
        )
        name = self.legacy("prompts/list")[1]["result"]["prompts"][0]["name"]
        legacy = self.legacy("prompts/get", {"name": name})[1]["result"]
        modern = self.modern("prompts/get", {"name": name})[1]["result"]
        self.assertEqual(legacy["messages"], modern["messages"])
        self.assertEqual(legacy["description"], modern["description"])

    def test_a_read_answers_the_same_through_either_era(self):
        legacy = self.tool_payload(self.call("legacy", "getCoachState"))
        modern = self.tool_payload(self.call("modern", "getCoachState"))
        self.assertEqual("passed", legacy["status"])
        self.assertEqual(legacy, modern)

    def test_a_write_lands_in_the_store_the_same_way_through_either_era(self):
        """One evidence row per era, written by the same route into the same store.

        `recordBodyMeasurement` holds one row per date and overwrites a restatement, so
        two calls a day apart are two rows and the second era's call is provably the one
        that wrote the second. What this rules out is a modern-era call that answered
        without reaching `CoachGateway.route` at all.
        """
        first = self.tool_payload(
            self.call("legacy", "recordBodyMeasurement", {"weight_kg": 72.5, "date": "2026-03-01"})
        )
        second = self.tool_payload(
            self.call("modern", "recordBodyMeasurement", {"weight_kg": 71.8, "date": "2026-03-02"})
        )
        self.assertEqual("passed", first["status"])
        self.assertEqual("passed", second["status"])

        stored = athlete_evidence.load_evidence(self.state_dir)
        measurements = {
            record["date"]: record["weight_kg"] for record in stored["body_measurements"]
        }
        self.assertEqual({"2026-03-01": 72.5, "2026-03-02": 71.8}, measurements)

    def test_a_refusal_is_a_tool_result_on_both_eras_rather_than_a_protocol_error(self):
        """A block is something the model has to read and act on.

        Folding it into a JSON-RPC error would hand it to the client's transport layer
        instead, which is where the model cannot see it -- and that has to hold on the
        era that maps error codes onto HTTP statuses just as it does on the one that
        does not.
        """
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                answered = self.call(
                    era, "clearDeliveryAttempt", {"attempt_id": "attempt-1", "confirmed": False}
                )
                self.assertTrue(answered["isError"])
                payload = self.tool_payload(answered)
                self.assertEqual("blocked", payload["status"])
                # No `structuredContent`: `outputSchema` describes a result, and a
                # refusal is not one.
                self.assertNotIn("structuredContent", answered)

    def test_an_unknown_tool_is_a_protocol_error_on_both_eras(self):
        for era, send in (("legacy", self.legacy), ("modern", self.modern)):
            with self.subTest(era=era):
                _, response = send("tools/call", {"name": "deleteEverything", "arguments": {}})
                self.assertEqual(INVALID_PARAMS, response["error"]["code"])
                self.assertNotIn("result", response)

    def test_a_retired_tool_name_is_refused_in_the_products_own_shape_on_both_eras(self):
        """A cached catalogue must never read as an erasure that happened."""
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                answered = self.call(era, "applyOwnerDeletion", {})
                self.assertTrue(answered["isError"])
                payload = self.tool_payload(answered)
                self.assertEqual("account_deletion_moved", payload["error"])
                self.assertIn("nothing has been deleted", payload["detail"])
                self.assertIn("support", payload["support_url"])

    def test_an_unknown_method_is_answered_on_both_eras_without_a_result(self):
        for era, send in (("legacy", self.legacy), ("modern", self.modern)):
            with self.subTest(era=era):
                _, response = send("resources/list")
                self.assertEqual(METHOD_NOT_FOUND, response["error"]["code"])
                self.assertNotIn("result", response)


class AuthorizationAcrossErasTests(EraTestCase):
    """Identity is settled before the protocol, so both eras answer it identically."""

    def test_an_unauthenticated_call_is_challenged_before_any_era_is_decided(self):
        """Identity runs above the protocol layer, so the era never enters into it.

        The same request with each era's headers on it: neither reaches a protocol
        handler, and both are told where to authenticate.
        """
        for era, headers in (
            ("legacy", {"MCP-Protocol-Version": PROTOCOL_VERSION}),
            ("modern", {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}),
        ):
            with self.subTest(era=era):
                status, response_headers, body = self.post_mcp(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    token=None,
                    headers=headers,
                )
                self.assertEqual(401, status)
                self.assertIn(
                    "resource_metadata=", response_headers.get("WWW-Authenticate", "")
                )
                self.assertEqual("unauthorized", json.loads(body)["error"])

    def test_a_revoked_provider_credential_becomes_a_challenge_not_a_tool_result(self):
        """The one failure a tool result cannot carry, on the era-crossing path.

        A provider credential the athlete revoked leaves the model nothing to do: the
        client has to authorize again, and it starts that on a transport-level `401`
        with the challenge in it. The SDK turns a handler exception into a JSON-RPC
        internal error instead, so the transport parks anything that is not a refusal
        and re-raises it on the request thread -- and this is what holds that it does.
        """
        # What a credential the athlete revoked at intervals.icu looks like from here.
        self.fake.read_status = 401
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
                message: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "startCoachSession", "arguments": {"all_clear": True}},
                }
                if era == "modern":
                    headers = {
                        "MCP-Protocol-Version": MODERN,
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": "startCoachSession",
                    }
                    message["params"]["_meta"] = {
                        ENVELOPE_VERSION: MODERN,
                        ENVELOPE_CAPABILITIES: {},
                    }
                status, response_headers, body = self.post_mcp(message, headers=headers)
                self.assertEqual(401, status, body)
                self.assertIn("resource_metadata=", response_headers.get("WWW-Authenticate", ""))
                self.assertEqual("unauthorized", json.loads(body)["error"])


class AccessLogAcrossErasTests(EraTestCase):
    """The operator's one line per request survives the hop onto the SDK's loop."""

    def test_the_access_line_still_names_the_tool_and_how_it_ended(self):
        """The quota scope is opened on the request thread and the call runs elsewhere.

        Without the scope travelling with the request, every MCP line would report no
        tool, no outcome and no provider spend -- which is the only record of what a
        refused call did (issue #417) and of what one athlete's run of them cost.
        """
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                self.log_handler.records.clear()
                self.call(era, "getCoachState")
                lines = [line for line in self.log_handler.records if "POST /mcp" in line]
                self.assertTrue(lines, self.log_handler.records)
                self.assertIn("tool=getCoachState", lines[-1])
                self.assertIn("outcome=passed", lines[-1])

    def test_a_refusal_is_told_apart_from_a_success_on_the_same_line(self):
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                self.log_handler.records.clear()
                self.call(era, "clearDeliveryAttempt", {"attempt_id": "a", "confirmed": False})
                lines = [line for line in self.log_handler.records if "POST /mcp" in line]
                self.assertTrue(lines, self.log_handler.records)
                self.assertIn("tool=clearDeliveryAttempt", lines[-1])
                self.assertIn("outcome=blocked:", lines[-1])


class CatalogueRoundTripTests(unittest.TestCase):
    """The reviewed descriptors, through the SDK's own wire model and back."""

    def test_every_descriptor_survives_the_sdk_model_unchanged(self):
        """`model_validate` is what proves the SDK agrees with the reviewed surface.

        A field the SDK dropped, renamed or defaulted would reach a client altered while
        `tool_catalogue_sha256` -- which hashes the descriptor, not the wire -- stayed
        still. Compared tool by tool so a failure names the one that moved.
        """
        import mcp_types

        for tool in TOOLS:
            with self.subTest(tool=tool.name):
                descriptor = tool.descriptor()
                round_tripped = mcp_types.Tool.model_validate(descriptor).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                self.assertEqual(descriptor, round_tripped)

    def test_the_accepted_revisions_span_both_eras(self):
        self.assertIn(PROTOCOL_VERSION, mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS)
        self.assertIn(MODERN, mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS)

    def test_every_served_tool_is_one_the_catalogue_names(self):
        self.assertEqual(
            sorted(TOOLS_BY_NAME), sorted(tool.name for tool in TOOLS)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
