"""Keep the recorded protocol envelope equal to what the installed SDK actually serves.

The pin in `requirements.lock` decides the wire behind `/mcp`: which revisions the
handshake agrees to, what an `initialize` result carries, what `server/discover` answers,
and which JSON-RPC error a batched or malformed request gets. None of that is in this
repository's diff when the pin moves, which is exactly why it is recorded in
`docs/mcp-protocol-envelope.json` and compared here on every run.

A moved pin therefore fails the suite until somebody has looked at the delta and recorded
it -- `python3 scripts/mcp_protocol_envelope.py --update`, step 3 of
`docs/ops/upgrade-the-mcp-sdk.md`. That is the point: the upgrade's behavioural change gets
read in a pull request rather than discovered by a client.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.mcp_protocol_envelope import (
    BASELINE,
    CAPTURE_KIND,
    capture,
    differences,
)


ROOT = Path(__file__).resolve().parents[1]


class RecordedEnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.recorded = json.loads(BASELINE.read_text(encoding="utf-8"))
        # One capture for the class: it starts the SDK's event loop and drives about
        # twenty requests through the real transport.
        cls.served = capture()

    def test_the_record_is_what_the_installed_sdk_serves(self):
        found = differences(self.recorded, self.served)
        self.assertEqual(
            [],
            found,
            "the installed MCP SDK answers differently from "
            f"{BASELINE.relative_to(ROOT)}. Read the delta, run the dual-era acceptance "
            "(docs/ops/accept-both-protocol-eras.md), then record it with "
            "`python3 scripts/mcp_protocol_envelope.py --update`:\n  "
            + "\n  ".join(found),
        )

    def test_the_record_names_the_sdk_version_it_was_taken_from(self):
        import importlib.metadata

        self.assertEqual(CAPTURE_KIND, self.recorded["kind"])
        self.assertEqual(
            importlib.metadata.version("mcp"), self.recorded["mcp_sdk_version"]
        )

    def test_the_accepted_set_is_the_sdks_registry_rather_than_a_local_list(self):
        """There is no list of revisions here, and a capture must not become one.

        The accepted set is read from `mcp_types.version` through
        `mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS`. A revision arriving in a later release
        shows up as a new row in the record instead of as an edit somebody has to make.
        """
        from garmin_coach_loop import mcp_sdk_transport

        accepted = self.served["accepted_protocol_versions"]
        self.assertEqual(
            sorted(mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS),
            sorted([*accepted["handshake"], *accepted["modern"]]),
        )
        # Every accepted revision is probed, and so are the deliberately invalid controls.
        for version in mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS:
            self.assertIn(version, self.served["negotiation"])
        self.assertIn("not-a-revision", self.served["negotiation"])

    def test_the_capture_carries_no_catalogue_and_no_instruction_text(self):
        """The product half of the surface belongs to the contract gate, not to this one.

        A capture that carried the tool catalogue or the served instructions would move on
        every ordinary release and stop reading as a protocol fact -- and it would be a
        second, unreviewed copy of bytes `scripts/mcp_contract_equivalence.py` already
        compares field by field.
        """
        from garmin_coach_loop import mcp_transport, orchestration

        serialized = json.dumps(self.served, ensure_ascii=False)
        for tool in mcp_transport.TOOLS:
            self.assertNotIn(tool.descriptor()["name"], serialized)
        instructions = orchestration.instructions()
        self.assertNotIn(instructions[:80], serialized)
        self.assertIn('"instructions": "<present>"', json.dumps(self.served, indent=2))

    def test_the_wire_facts_the_record_exists_for_are_all_in_it(self):
        # Named individually rather than by counting keys: each row is one of the things
        # issue #489 measured moving between two SDK releases.
        self.assertEqual(
            "2025-06-18",
            self.served["negotiation"]["2025-06-18"]["initialize"]["protocolVersion"],
        )
        self.assertIn("capabilities", self.served["handshake_result"]["result"])
        self.assertIn("serverInfo", self.served["handshake_result"]["result"])
        self.assertIn("resultType", self.served["discover_result"]["result"])
        self.assertIn("jsonrpc_batch", self.served["errors"])
        self.assertIn("error_code", self.served["errors"]["jsonrpc_batch"])


class EnvelopeDiffTests(unittest.TestCase):
    """The diff has to name the delta, not merely notice one."""

    BASE = {
        "mcp_sdk_version": "2.2.0",
        "negotiation": {"2025-03-26": {"initialize": {"protocolVersion": "2025-03-26"}}},
        "handshake_result": {"result": {"capabilities": {"experimental": {}}}},
        "errors": {"jsonrpc_batch": {"http_status": 400, "error_code": -32602}},
    }

    def _candidate(self, **changes: object) -> dict[str, object]:
        return json.loads(json.dumps({**self.BASE, **changes}))

    def test_two_identical_captures_differ_nowhere(self):
        self.assertEqual([], differences(self.BASE, self._candidate()))

    def test_a_counter_offered_revision_is_named_with_both_values(self):
        # The exact 2.2.0 change issue #489 recorded: the old transport counter-offered
        # 2025-06-18 where the SDK agrees to the revision as asked.
        candidate = self._candidate(
            negotiation={"2025-03-26": {"initialize": {"protocolVersion": "2025-06-18"}}}
        )
        found = differences(self.BASE, candidate)
        self.assertEqual(1, len(found), found)
        self.assertIn("negotiation.2025-03-26.initialize.protocolVersion", found[0])
        self.assertIn('"2025-03-26" -> "2025-06-18"', found[0])

    def test_a_changed_error_code_is_named(self):
        candidate = self._candidate(
            errors={"jsonrpc_batch": {"http_status": 400, "error_code": -32600}}
        )
        found = differences(self.BASE, candidate)
        self.assertEqual(["errors.jsonrpc_batch.error_code: -32602 -> -32600"], found)

    def test_an_added_and_a_removed_member_are_both_named(self):
        candidate = self._candidate(
            handshake_result={"result": {"capabilities": {"logging": {}}}}
        )
        found = sorted(differences(self.BASE, candidate))
        self.assertEqual(2, len(found), found)
        self.assertIn(
            "handshake_result.result.capabilities.experimental: removed", found[0]
        )
        self.assertIn("handshake_result.result.capabilities.logging: added", found[1])

    def test_a_changed_accepted_set_is_named_as_a_list(self):
        base = {"accepted_protocol_versions": {"handshake": ["2025-06-18"]}}
        candidate = {"accepted_protocol_versions": {"handshake": ["2025-06-18", "2026-11-01"]}}
        found = differences(base, candidate)
        self.assertEqual(1, len(found))
        self.assertIn("accepted_protocol_versions.handshake", found[0])
        self.assertIn("2026-11-01", found[0])


if __name__ == "__main__":
    unittest.main()
