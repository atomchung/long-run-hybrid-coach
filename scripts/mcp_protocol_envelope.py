#!/usr/bin/env python3
"""Print what the installed MCP SDK puts on the wire, and diff it against the record.

`scripts/mcp_contract_equivalence.py` holds this product's own bytes equal across a
change: the tool catalogue, the schemas, the annotations, `instructions`, the prompts.
None of those belong to the SDK. What the SDK decides is the envelope around them --
which protocol revisions the handshake agrees to, what an `initialize` result carries
beside the instructions, what `server/discover` answers with, and which JSON-RPC error a
malformed or batched request returns -- and *that* moves when the pin moves, with no diff
anywhere in this repository to review.

So this script records the envelope and nothing else. No tool, no schema, no result and
no instruction text enters a capture: `instructions` is recorded as present or absent and
`serverInfo` as its key set, because the wire question is whether the member is there and
what shape it has, and the product question is already answered by the contract gate.
Splitting them this way is deliberate -- a capture that carried the catalogue would move
on every ordinary release and stop being readable as a protocol fact.

    # what this environment serves, against the recorded baseline
    python3 scripts/mcp_protocol_envelope.py

    # after installing a candidate SDK: the upgrade's wire delta, before rolling
    python3 scripts/mcp_protocol_envelope.py --against docs/mcp-protocol-envelope.json

    # accept the delta as the new record (step 3 of docs/ops/upgrade-the-mcp-sdk.md)
    python3 scripts/mcp_protocol_envelope.py --update

Exit status is 0 when the envelope matches and 1 when it does not. A difference is not
automatically wrong: it is the thing an upgrade is supposed to show somebody before a
client meets it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "mcp-protocol-envelope.json"

CAPTURE_KIND = "mcp-protocol-envelope"

# The version a capture's own shape is stamped with. It moves when *this script* changes
# what it records, which is the one way two captures can differ without the wire having.
CAPTURE_VERSION = 1

_MODERN = "2026-07-28"
_LEGACY = "2025-06-18"
_ENVELOPE_VERSION = "io.modelcontextprotocol/protocolVersion"
_ENVELOPE_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

# Deliberately invalid controls, probed beside every revision the SDK's own registry
# names. They are not a list of revisions this product accepts -- there is no such list
# here, and adding one is exactly what the SDK migration removed (CLAUDE.md, "One `/mcp`,
# two protocol eras"). What they pin down is the *refusal*: a server that quietly starts
# accepting anything shaped like a date has changed its mind about something.
UNSUPPORTED_PROBES = ("2020-01-01", "2099-12-31", "not-a-revision")

# Members a result carries that belong to this product rather than to the protocol.
# Recorded as presence, never as content.
PRODUCT_TEXT_MEMBERS = ("instructions",)

# Members whose *shape* is the protocol fact and whose values are the deployment's.
# `serverInfo.version` is the running release, so a capture that kept it would differ
# between any two deployments and say nothing about the wire.
SHAPE_ONLY_MEMBERS = ("serverInfo", "clientInfo")


def _headers(extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    return [
        ("Content-Type", "application/json"),
        ("Accept", "application/json, text/event-stream"),
        *(extra or []),
    ]


def _refuse_tool_call(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("the protocol envelope capture never calls a tool")


def _envelope(value: Any) -> Any:
    """``value`` with product content replaced by what the wire question needs.

    Structure-preserving on purpose: a member the SDK adds in a later revision is
    recorded as it arrives, so the diff names it rather than silently dropping it.
    """
    if isinstance(value, dict):
        recorded: dict[str, Any] = {}
        for key in sorted(value):
            inner = value[key]
            leaf = key.rsplit("/", 1)[-1]
            if leaf in PRODUCT_TEXT_MEMBERS:
                recorded[key] = "<present>" if inner else "<empty>"
            elif leaf in SHAPE_ONLY_MEMBERS:
                recorded[key] = (
                    {"<keys>": sorted(inner)} if isinstance(inner, dict) else "<present>"
                )
            else:
                recorded[key] = _envelope(inner)
        return recorded
    if isinstance(value, list):
        return [_envelope(item) for item in value]
    return value


class _Wire:
    """One `/mcp` conversation, driven through the real transport."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def post(
        self, body: bytes, *, headers: list[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        status, _response_headers, raw = self._runtime.dispatch(
            method="POST",
            path="/mcp",
            headers=headers if headers is not None else _headers(),
            body=body,
            call_tool=_refuse_tool_call,
        )
        answered: dict[str, Any] = {"http_status": int(status)}
        if not raw:
            answered["body"] = "empty"
            return answered
        try:
            parsed = json.loads(raw)
        except ValueError:
            answered["body"] = "not-json"
            return answered
        if isinstance(parsed, dict) and "error" in parsed:
            answered["body"] = "jsonrpc-error"
            error = parsed["error"]
            # The code, never the message: an error string carries pydantic's wording and
            # the offending input, and comparing those would report a library's release
            # notes as a protocol change.
            answered["error_code"] = error.get("code") if isinstance(error, dict) else None
            return answered
        if isinstance(parsed, dict) and "result" in parsed:
            answered["body"] = "jsonrpc-result"
            answered["result"] = parsed["result"]
            return answered
        answered["body"] = "other"
        return answered

    def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        headers: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return self.post(json.dumps(message).encode("utf-8"), headers=headers)


def _initialize(wire: _Wire, version: str) -> dict[str, Any]:
    return wire.rpc(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "envelope", "version": "0"},
        },
    )


def _discover(wire: _Wire, version: str) -> dict[str, Any]:
    return wire.rpc(
        "server/discover",
        {"_meta": {_ENVELOPE_VERSION: version, _ENVELOPE_CAPABILITIES: {}}},
        headers=_headers(
            [("MCP-Protocol-Version", version), ("Mcp-Method", "server/discover")]
        ),
    )


def _outcome(answered: dict[str, Any], *, keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """An answer reduced to the facts a protocol comparison is about."""
    recorded = {"http_status": answered["http_status"], "body": answered["body"]}
    if "error_code" in answered:
        recorded["error_code"] = answered["error_code"]
    result = answered.get("result")
    if isinstance(result, dict):
        for name in keep:
            if name in result:
                recorded[name] = _envelope(result[name])
    return recorded


def capture() -> dict[str, Any]:
    """Everything this environment's SDK decides about the wire, in one document."""
    sys.path.insert(0, str(ROOT))
    from garmin_coach_loop import mcp_sdk_transport
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

    runtime = mcp_sdk_transport.SdkTransport("envelope")
    runtime.start()
    try:
        wire = _Wire(runtime)
        negotiation: dict[str, Any] = {}
        for version in sorted({*mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS, *UNSUPPORTED_PROBES}):
            negotiation[version] = {
                "initialize": _outcome(_initialize(wire, version), keep=("protocolVersion",)),
                "discover": _outcome(_discover(wire, version), keep=("supportedVersions",)),
            }

        handshake = _initialize(wire, _LEGACY)
        discover = _discover(wire, _MODERN)
        recorded = {
            "kind": CAPTURE_KIND,
            "capture_version": CAPTURE_VERSION,
            "mcp_sdk_version": mcp_sdk_transport.SDK_VERSION,
            # Read from the SDK's registry, which is where the accepted set lives. A
            # revision arriving in a later release shows up here as a new row.
            "accepted_protocol_versions": {
                "handshake": list(HANDSHAKE_PROTOCOL_VERSIONS),
                "modern": list(MODERN_PROTOCOL_VERSIONS),
            },
            "negotiation": negotiation,
            "handshake_result": {
                "http_status": handshake["http_status"],
                "body": handshake["body"],
                "result": _envelope(handshake.get("result")),
            },
            "discover_result": {
                "http_status": discover["http_status"],
                "body": discover["body"],
                "result": _envelope(discover.get("result")),
            },
            "errors": _error_semantics(wire),
        }
    finally:
        runtime.stop()
    return recorded


def _error_semantics(wire: _Wire) -> dict[str, Any]:
    """How the wire refuses, by HTTP status and JSON-RPC code.

    The batch row is the one with history: the hand-written transport refused to agree to
    `2025-03-26` at all, because that revision permits JSON-RPC batching and this server
    does not implement it. The SDK agrees to the revision and refuses the batch itself, at
    a different error code. Both designs refuse the same request, so the migration
    accepted the change (issue #489) -- and this is the row that would show it moving
    again.
    """
    legacy = _headers([("MCP-Protocol-Version", _LEGACY)])
    probes: dict[str, dict[str, Any]] = {
        "jsonrpc_batch": wire.post(
            json.dumps(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                ]
            ).encode("utf-8"),
            headers=legacy,
        ),
        "malformed_json": wire.post(b"{", headers=legacy),
        "unknown_method": wire.rpc("no/such/method", headers=legacy),
        "unknown_protocol_version_header": wire.rpc(
            "ping", headers=_headers([("MCP-Protocol-Version", "2020-01-01")])
        ),
        "absent_protocol_version_header": wire.rpc("ping"),
        "modern_request_without_envelope": wire.rpc(
            "ping",
            headers=_headers(
                [("MCP-Protocol-Version", _MODERN), ("Mcp-Method", "ping")]
            ),
        ),
        "modern_routing_header_mismatch": wire.rpc(
            "ping",
            {"_meta": {_ENVELOPE_VERSION: _MODERN, _ENVELOPE_CAPABILITIES: {}}},
            headers=_headers(
                [("MCP-Protocol-Version", _MODERN), ("Mcp-Method", "tools/list")]
            ),
        ),
        "unacceptable_accept_header": wire.post(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8"),
            headers=[
                ("Content-Type", "application/json"),
                ("Accept", "text/plain"),
                ("MCP-Protocol-Version", _LEGACY),
            ],
        ),
        "notification_without_id": wire.post(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(
                "utf-8"
            ),
            headers=legacy,
        ),
    }
    return {name: _outcome(answered) for name, answered in probes.items()}


def differences(base: Any, candidate: Any, *, path: str = "") -> list[str]:
    """Every place two captures disagree, named by where it is in the document."""
    where = path or "(root)"
    if isinstance(base, dict) and isinstance(candidate, dict):
        found: list[str] = []
        for key in sorted(set(base) | set(candidate)):
            inner = f"{path}.{key}" if path else key
            if key not in candidate:
                found.append(f"{inner}: removed (was {_short(base[key])})")
            elif key not in base:
                found.append(f"{inner}: added ({_short(candidate[key])})")
            else:
                found += differences(base[key], candidate[key], path=inner)
        return found
    if isinstance(base, list) and isinstance(candidate, list) and base != candidate:
        return [f"{where}: {_short(base)} -> {_short(candidate)}"]
    if base != candidate:
        return [f"{where}: {_short(base)} -> {_short(candidate)}"]
    return []


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 300 else text[:297] + "..."


def _serialize(recorded: dict[str, Any]) -> str:
    return json.dumps(recorded, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"no capture at {path}")
    except ValueError as invalid:
        raise SystemExit(f"{path} is not a capture: {invalid}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--against",
        metavar="CAPTURE",
        help="compare this environment against a stored capture (default: the record)",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASE", "CANDIDATE"),
        help="compare two stored captures without starting a transport",
    )
    parser.add_argument("--write", metavar="PATH", help="write this capture ('-' for stdout)")
    parser.add_argument(
        "--update", action="store_true", help=f"rewrite {BASELINE.relative_to(ROOT)}"
    )
    arguments = parser.parse_args(argv)

    if arguments.compare:
        base, candidate = (_read(Path(name)) for name in arguments.compare)
        return _report(differences(base, candidate), base, candidate)

    recorded = capture()

    if arguments.update:
        BASELINE.write_text(_serialize(recorded), encoding="utf-8")
        print(f"recorded {BASELINE.relative_to(ROOT)} for mcp {recorded['mcp_sdk_version']}")
        return 0
    if arguments.write:
        if arguments.write == "-":
            sys.stdout.write(_serialize(recorded))
        else:
            Path(arguments.write).write_text(_serialize(recorded), encoding="utf-8")
            print(f"wrote {arguments.write} for mcp {recorded['mcp_sdk_version']}")
        return 0

    base = _read(Path(arguments.against) if arguments.against else BASELINE)
    return _report(differences(base, recorded), base, recorded)


def _report(found: list[str], base: dict[str, Any], candidate: dict[str, Any]) -> int:
    was = base.get("mcp_sdk_version", "unknown")
    now = candidate.get("mcp_sdk_version", "unknown")
    if not found:
        print(f"MCP protocol envelope unchanged: mcp {was} and mcp {now} answer identically.")
        return 0
    print(f"MCP protocol envelope differs (mcp {was} -> mcp {now}):\n")
    for difference in found:
        print(f"  - {difference}")
    print(
        "\nRun the dual-era acceptance before deploying this "
        "(docs/ops/accept-both-protocol-eras.md), then record it with --update."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
