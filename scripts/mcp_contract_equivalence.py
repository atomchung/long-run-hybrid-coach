#!/usr/bin/env python3
"""Hold the model-facing MCP surface equal across a transport change.

OpenAI's plugin review snapshots a *contract*, not a build: the tool list and their
names, titles, descriptions, input and output schemas, annotations and `_meta`, plus the
MCP server's `instructions`. The published app keeps calling the live server, so a
server-only change may deploy while a metadata version is in review **as long as that
contract still answers the same way**. This script is what turns that sentence into a
gate: it builds the surface a base ref serves, builds the surface this checkout serves
over both protocol eras, and reports any difference.

It is deliberately not a tool count and not a digest alone. A digest says two catalogues
differ; it cannot say a `readOnlyHint` flipped, and it says nothing at all about the
served instructions or about whether the 2026-07-28 era answers with the same tools the
2025 era does. Every field is compared, and the difference is printed.

    python3 scripts/mcp_contract_equivalence.py --base origin/main

Exit status is 0 when the contract is unchanged and 1 when it is not. A difference is not
automatically wrong -- a deliberate catalogue change moves it, and that change goes
through Scan Tools and a new reviewed version (AGENTS.md, "Development and release
gates"). What this refuses is moving it *by accident*, which is exactly what a transport
migration is in a position to do.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

# What travels to the base ref. The catalogue is assembled from constants several modules
# own, so the whole package goes; `contracts/` rides along as the schema surface a tool
# definition may read.
EXPORT_PATHS = ("garmin_coach_loop", "contracts")

# Run inside the exported base ref, with that ref's own package on `sys.path`. It emits
# the surface that ref serves, taking the framing from whichever transport it has: a ref
# from before the SDK migration answers through its own `mcp_transport.handle`, and one
# from after has no hand-written framing left to drive, so its catalogue and prompts are
# read directly -- they are the same objects the SDK is handed there.
BASE_PROGRAM = r"""
import json
from garmin_coach_loop import mcp_transport, orchestration

handle = getattr(mcp_transport, "handle", None)


def rpc(method, params=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    _status, body = handle(
        json.dumps(message).encode("utf-8"),
        call_tool=lambda kind, arguments: {},
        server_version="base",
    )
    return body["result"]


if handle is not None:
    initialize = rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "gate", "version": "0"}},
    )
    surface = {
        "framing": "handwritten",
        "tools": rpc("tools/list")["tools"],
        "instructions": initialize["instructions"],
        "server_name": initialize["serverInfo"]["name"],
        "server_title": initialize["serverInfo"].get("title"),
        "prompts": rpc("prompts/list")["prompts"],
        "prompt_bodies": {
            name: rpc("prompts/get", {"name": name}) for name in sorted(orchestration.PROMPTS)
        },
    }
else:
    surface = {
        "framing": "sdk",
        "tools": [tool.descriptor() for tool in mcp_transport.TOOLS],
        "instructions": orchestration.instructions(),
        "server_name": mcp_transport.SERVER_NAME,
        "server_title": mcp_transport.SERVER_TITLE,
        "prompts": [descriptor() for descriptor, _ in orchestration.PROMPTS.values()],
        "prompt_bodies": {
            name: orchestration.PROMPTS[name][1]() for name in sorted(orchestration.PROMPTS)
        },
    }

surface["catalogue_sha256"] = mcp_transport.tool_catalogue_sha256()
print(json.dumps(surface, ensure_ascii=False, sort_keys=True))
"""


def base_surface(ref: str) -> dict[str, Any]:
    """The served surface at ``ref``, built from that ref's own bytes.

    Out of Git rather than out of this working tree: the whole point is to compare
    against what was reviewed, and a file this checkout has edited is not that.
    """
    with tempfile.TemporaryDirectory() as directory:
        archive = subprocess.run(
            ["git", "archive", ref, *EXPORT_PATHS], cwd=ROOT, capture_output=True
        )
        if archive.returncode != 0:
            raise SystemExit(
                f"cannot read {ref}: {archive.stderr.decode('utf-8', 'replace').strip()}"
            )
        extracted = subprocess.run(
            ["tar", "-x", "-C", directory], input=archive.stdout, capture_output=True
        )
        if extracted.returncode != 0:
            raise SystemExit(f"cannot unpack {ref}: {extracted.stderr.decode('utf-8', 'replace')}")
        built = subprocess.run(
            [sys.executable, "-c", BASE_PROGRAM],
            cwd=directory,
            capture_output=True,
            env={"PYTHONPATH": directory, "PATH": "/usr/bin:/bin"},
        )
        if built.returncode != 0:
            raise SystemExit(
                f"cannot build the surface at {ref}:\n"
                + built.stderr.decode("utf-8", "replace")
            )
        return json.loads(built.stdout.decode("utf-8"))


def served_surface() -> dict[str, dict[str, Any]]:
    """What this checkout actually puts on the wire, one reading per protocol era.

    Through the real transport rather than through the catalogue constants: a migration
    that changed how descriptors are serialized would pass a constants-only comparison
    and still hand a client something different. Both eras are read because one endpoint
    now serves both, and "the same tools on both" is part of the contract.
    """
    sys.path.insert(0, str(ROOT))
    from garmin_coach_loop import mcp_sdk_transport, mcp_transport, orchestration

    def refuse(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("the contract gate never calls a tool")

    runtime = mcp_sdk_transport.SdkTransport("gate")
    runtime.start()
    try:
        readings = {
            "legacy": _read_era(runtime, refuse, modern=False),
            "modern": _read_era(runtime, refuse, modern=True),
        }
    finally:
        runtime.stop()
    for reading in readings.values():
        reading["catalogue_sha256"] = mcp_transport.tool_catalogue_sha256()
        reading["prompt_names"] = sorted(orchestration.PROMPTS)
    return readings


_MODERN = "2026-07-28"
_LEGACY = "2025-06-18"
_ENVELOPE_VERSION = "io.modelcontextprotocol/protocolVersion"
_ENVELOPE_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"


def _read_era(runtime: Any, call_tool: Any, *, modern: bool) -> dict[str, Any]:
    from garmin_coach_loop import orchestration

    def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        headers = [
            ("Content-Type", "application/json"),
            ("Accept", "application/json, text/event-stream"),
        ]
        if modern:
            envelope = {_ENVELOPE_VERSION: _MODERN, _ENVELOPE_CAPABILITIES: {}}
            body["params"] = {**(params or {}), "_meta": envelope}
            headers += [("MCP-Protocol-Version", _MODERN), ("Mcp-Method", method)]
            if method == "prompts/get":
                headers += [("Mcp-Name", str((params or {})["name"]))]
        else:
            if params is not None:
                body["params"] = params
            if method != "initialize":
                headers += [("MCP-Protocol-Version", _LEGACY)]
        status, _response_headers, raw = runtime.dispatch(
            method="POST",
            path="/mcp",
            headers=headers,
            body=json.dumps(body).encode("utf-8"),
            call_tool=call_tool,
        )
        answered = json.loads(raw)
        if "result" not in answered:
            raise SystemExit(f"{method} failed on the {'modern' if modern else 'legacy'} era: {answered}")
        if int(status) != 200:
            raise SystemExit(f"{method} answered HTTP {int(status)}")
        return answered["result"]

    if modern:
        opening = rpc("server/discover")
        server_info = opening["_meta"]["io.modelcontextprotocol/serverInfo"]
    else:
        opening = rpc(
            "initialize",
            {
                "protocolVersion": _LEGACY,
                "capabilities": {},
                "clientInfo": {"name": "gate", "version": "0"},
            },
        )
        server_info = opening["serverInfo"]
    return {
        "framing": "sdk",
        "tools": rpc("tools/list")["tools"],
        "instructions": opening["instructions"],
        "server_name": server_info["name"],
        "server_title": server_info.get("title"),
        "prompts": rpc("prompts/list")["prompts"],
        "prompt_bodies": {
            name: rpc("prompts/get", {"name": name}) for name in sorted(orchestration.PROMPTS)
        },
    }


# The two members every 2026-07-28 *result envelope* carries and no 2025 one does:
# `resultType`, which that revision makes mandatory, and the reserved `_meta` key the SDK
# stamps the server's identity into. They are the era's own mechanics, not this product's
# contract, so comparing a 2025 reading against a 2026 one has to set them aside or every
# era comparison reports the era.
#
# Narrow on purpose, and only at the envelope: a tool descriptor's own `_meta` **is**
# reviewed contract (it is one of the members Scan Tools snapshots), so nothing strips it
# from a tool. This applies to the result object a `prompts/get` returns and nowhere else.
_ERA_ENVELOPE_MEMBERS = ("resultType", "_meta")


def _without_era_envelope(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    return {key: value for key, value in result.items() if key not in _ERA_ENVELOPE_MEMBERS}


def _by_name(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in tools}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def compare(base: dict[str, Any], served: dict[str, Any], *, label: str) -> list[str]:
    """Every difference between two surfaces, named field by field."""
    differences: list[str] = []
    base_tools, served_tools = _by_name(base["tools"]), _by_name(served["tools"])

    for name in sorted(set(base_tools) - set(served_tools)):
        differences.append(f"{label}: tool removed: {name}")
    for name in sorted(set(served_tools) - set(base_tools)):
        differences.append(f"{label}: tool added: {name}")
    for name in sorted(set(base_tools) & set(served_tools)):
        was, now = base_tools[name], served_tools[name]
        for field in sorted(set(was) | set(now)):
            if _canonical(was.get(field)) != _canonical(now.get(field)):
                differences.append(
                    f"{label}: {name}.{field} changed:\n"
                    f"    base:   {_canonical(was.get(field))[:400]}\n"
                    f"    served: {_canonical(now.get(field))[:400]}"
                )

    for field in ("instructions", "server_name", "server_title", "catalogue_sha256"):
        if base.get(field) != served.get(field):
            differences.append(
                f"{label}: {field} changed:\n"
                f"    base:   {str(base.get(field))[:400]}\n"
                f"    served: {str(served.get(field))[:400]}"
            )

    if _canonical(base["prompts"]) != _canonical(served["prompts"]):
        differences.append(
            f"{label}: prompts/list changed:\n"
            f"    base:   {_canonical(base['prompts'])[:400]}\n"
            f"    served: {_canonical(served['prompts'])[:400]}"
        )
    for name in sorted(set(base["prompt_bodies"]) | set(served["prompt_bodies"])):
        was = _without_era_envelope(base["prompt_bodies"].get(name))
        now = _without_era_envelope(served["prompt_bodies"].get(name))
        if _canonical(was) != _canonical(now):
            differences.append(
                f"{label}: prompt {name} changed:\n"
                f"    base:   {_canonical(was)[:400]}\n"
                f"    served: {_canonical(now)[:400]}"
            )
    return differences


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/main",
        help="the ref whose reviewed surface this checkout must still answer with",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    arguments = parser.parse_args()

    base = base_surface(arguments.base)
    readings = served_surface()

    differences: list[str] = []
    for era in ("legacy", "modern"):
        differences += compare(base, readings[era], label=era)

    # The two eras are one endpoint, so the catalogue a client is handed must not depend
    # on which one it speaks. Compared directly rather than inferred from both matching
    # the base: a shared deviation would cancel out above and show up only here.
    if _canonical(readings["legacy"]["tools"]) != _canonical(readings["modern"]["tools"]):
        differences.append("the 2025 and 2026 eras serve different tool catalogues")
    if readings["legacy"]["instructions"] != readings["modern"]["instructions"]:
        differences.append("the 2025 and 2026 eras serve different instructions")

    if arguments.json:
        print(
            json.dumps(
                {
                    "base": arguments.base,
                    "base_framing": base["framing"],
                    "tools": len(base["tools"]),
                    "catalogue_sha256": base["catalogue_sha256"],
                    "equivalent": not differences,
                    "differences": differences,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif differences:
        print(f"MCP contract differs from {arguments.base}:\n")
        for difference in differences:
            print(f"  - {difference}")
    else:
        print(
            f"MCP contract unchanged from {arguments.base}: "
            f"{len(base['tools'])} tools, identical on the 2025 and 2026-07-28 eras.\n"
            f"  catalogue_sha256 {base['catalogue_sha256']}\n"
            f"  instructions     {len(base['instructions'])} characters\n"
            f"  prompts          {len(base['prompts'])}"
        )
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
