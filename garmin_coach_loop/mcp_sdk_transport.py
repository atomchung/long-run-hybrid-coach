"""The MCP protocol and wire layer, owned by the official MCP Python SDK.

``mcp_transport`` keeps what this product decided: the tool catalogue, the schemas, the
annotations, the model-facing projection, and the two prompts. This module keeps nothing
of its own about the protocol. It hands the SDK (``mcp`` on PyPI, v2) the four questions
a client can ask -- list the tools, call one, list the prompts, get one -- and the SDK
answers every protocol question around them: which revision this connection speaks, what
an ``initialize`` handshake returns, what ``server/discover`` returns, how a JSON-RPC
fault maps to an HTTP status, and which of those two eras a given request belongs to.

**Why the SDK owns it.** MCP support was hand-written here in August 2026 against
2025-06-18, and every revision since has been this repository's problem to implement.
2026-07-28 is the one that made that unsustainable: it is not another value in a version
list but a different wire shape -- no ``initialize`` handshake, no session, a
self-contained POST whose ``params._meta`` carries the protocol version and the client's
capabilities, and ``server/discover`` in place of the handshake's answer. A client that
speaks it was answered ``400`` here and fell back to 2025. Implementing a second era by
hand would have doubled the surface this repository maintains for a protocol it does not
own. The SDK already serves both eras from one endpoint, so the transport is the part
that moves.

**What did not move.** Everything a client or a reviewer can see stays where it was:

- The tool catalogue is still ``mcp_transport.TOOLS``, still hashed by
  ``mcp_transport.tool_catalogue_sha256``, and the descriptors below are that module's
  own ``Tool.descriptor()`` output handed to the SDK unmodified.
- ``instructions`` is still ``orchestration.instructions()``.
- Authentication, owner isolation, the ``Origin`` check and the OAuth routes all run in
  ``gateway.py`` *before* anything here is reached, exactly as they did.
- A tool call still ends in ``CoachGateway.route`` through the same authenticated
  closure, for both eras, so the coaching semantics have one implementation and cannot
  drift per protocol revision.

**Where it runs.** The gateway is a ``ThreadingHTTPServer`` and the SDK's server is
asyncio. Rewriting the gateway as an ASGI application would have put OAuth, the browser
pages, ``/healthz``, ``/readyz`` and the release identity through a rewrite to change the
MCP wire format, so instead one background event loop is started beside the HTTP server
and each ``/mcp`` request is marshalled into it as a synthesized ASGI call. The stdlib
server keeps every other route byte for byte.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from typing import Any

import anyio.to_thread
import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from . import mcp_transport, orchestration
from .source_intervals import (
    ProviderQuotaScope,
    adopt_provider_quota_scope,
    name_provider_quota_tool,
    note_tool_outcome,
)


LOGGER = logging.getLogger("garmin_coach_loop.gateway")

# The SDK release this process actually resolved, for `/readyz`. Read from the installed
# distribution rather than from `requirements.txt`, because the pin is what the build
# asked for and this is what is answering.
SDK_VERSION = importlib.metadata.version("mcp")

# Every revision the served protocol layer accepts in the `MCP-Protocol-Version` header,
# read from the SDK's own registry rather than restated here. That is the point of the
# migration: the next revision arrives with an SDK upgrade, not with an edit to a tuple
# in this repository. The two sets are the two eras -- the handshake revisions reachable
# through `initialize`, and the per-request-envelope revisions reachable through
# `server/discover` -- and one `/mcp` serves both.
HTTP_PROTOCOL_VERSIONS: tuple[str, ...] = (*HANDSHAKE_PROTOCOL_VERSIONS, *MODERN_PROTOCOL_VERSIONS)

# The key this module reads its per-request state out of. ASGI's `scope["state"]` is the
# one channel that reaches a handler on both eras (verified for the legacy stateless
# transport and the modern single-exchange entry alike), which matters because the
# alternative -- a context variable -- would be set on the request thread and read on the
# event loop, where it is not visible.
_STATE_KEY = "garmin_coach_loop"


class _RequestState:
    """What one authenticated `/mcp` request carries into the SDK's handlers.

    ``call_tool`` is ``CoachGatewayHandler._mcp_tool_call``'s closure: already bound to
    one owner and one provider credential, and raising ``ToolCallBlocked`` for a refusal.
    Nothing in this module knows which athlete it is serving, which is the property the
    hand-written transport had and this one keeps.

    ``quota`` is the ``ProviderQuotaScope`` the request thread opened. The tool call runs
    on a worker thread, so the scope has to travel with the request rather than be
    inherited: without it the access-log line would report no tool, no outcome and no
    provider spend for every MCP call.

    ``escalated`` is how a failure that is *not* a coaching refusal gets back out. The
    hand-written transport called the closure inline, so an exception raised there landed
    in the gateway's own ``except`` clauses -- which is what turns a provider credential
    the athlete revoked into an HTTP ``401`` carrying the challenge that starts
    re-authorization, rather than into a tool result the model would narrate. The SDK
    deliberately does the opposite with a handler exception: it becomes a JSON-RPC
    ``INTERNAL_ERROR`` and the cause never reaches the caller. So anything other than
    ``ToolCallBlocked`` is caught, parked here, and re-raised on the request thread by
    ``dispatch`` before any response is written.
    """

    __slots__ = ("call_tool", "quota", "escalated")

    def __init__(
        self,
        call_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
        quota: ProviderQuotaScope | None,
    ) -> None:
        self.call_tool = call_tool
        self.quota = quota
        self.escalated: BaseException | None = None


def _request_state(ctx: Any) -> _RequestState:
    """This request's binding, or a programming error.

    A handler reached without one would be a request that skipped the gateway's own
    dispatch, which cannot happen: ``dispatch`` below is the only caller and it always
    sets it. Raising beats defaulting to an unauthenticated call.
    """
    request = getattr(ctx, "request", None)
    scope = getattr(request, "scope", None) or {}
    state = (scope.get("state") or {}).get(_STATE_KEY)
    if not isinstance(state, _RequestState):  # pragma: no cover - defensive
        raise MCPError(types.INTERNAL_ERROR, "Internal server error")
    return state


# --------------------------------------------------------------------------------------
# The four questions a client can ask
# --------------------------------------------------------------------------------------


def _tool_descriptors() -> list[types.Tool]:
    """``mcp_transport``'s own catalogue, validated into the SDK's wire model.

    ``model_validate`` rather than a hand-built model: the descriptor dictionary is the
    reviewed surface, and letting the SDK parse it is what proves the two agree. The
    schemas ride through as declared -- ``InputSchema``/``OutputSchema`` allow every
    JSON Schema keyword -- so no field is dropped, renamed or defaulted on the way out.
    ``tests/test_mcp_sdk_transport.py`` holds the round trip to equality, tool by tool.
    """
    return [types.Tool.model_validate(tool.descriptor()) for tool in mcp_transport.TOOLS]


async def _on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    del ctx, params
    return types.ListToolsResult(tools=_tool_descriptors())


async def _on_list_prompts(ctx: Any, params: Any) -> types.ListPromptsResult:
    del ctx, params
    return types.ListPromptsResult(
        prompts=[
            types.Prompt.model_validate(descriptor())
            for descriptor, _ in orchestration.PROMPTS.values()
        ]
    )


async def _on_get_prompt(ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
    """Serve one of the prompts this server has, or say plainly that a name is not one."""
    del ctx
    served = orchestration.PROMPTS.get(params.name)
    if served is None:
        raise MCPError(types.INVALID_PARAMS, f"unknown prompt: {params.name!r}")
    return types.GetPromptResult.model_validate(served[1]())


def _retired_tool_result(name: str) -> types.CallToolResult:
    """A name this server used to serve, refused in the product's own shape.

    A cached catalogue, not a protocol mistake: answered the way the gateway answers any
    refusal, so the model reads ``status: blocked`` and cannot report an erasure that
    never happened. ``mcp_transport`` owns the sentence; this only carries it.
    """
    name_provider_quota_tool(name)
    note_tool_outcome("blocked:account_deletion_moved")
    payload = {
        "status": "blocked",
        "error": "account_deletion_moved",
        "detail": mcp_transport._account_deletion_moved(mcp_transport.RETIRED_TOOLS[name]),
        "support_url": mcp_transport.SUPPORT_DATA_REQUEST_URL,
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=mcp_transport.client_result_json(payload))],
        is_error=True,
    )


async def _on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    """One tool call, from either era, into ``CoachGateway.route``.

    The blocking work -- provider reads, store writes, file locks -- runs on a worker
    thread rather than on the event loop, because the loop serves every other in-flight
    ``/mcp`` request at the same time and a synchronous delivery would stall all of them.
    The request's provider-quota scope is re-opened on that worker thread so the tool
    name, the outcome and the Intervals spend land on the object the request thread is
    holding for its access-log line.
    """
    name = params.name
    if name in mcp_transport.RETIRED_TOOLS:
        return _retired_tool_result(name)
    tool = mcp_transport.TOOLS_BY_NAME.get(name)
    if tool is None:
        # A name this server does not serve is a protocol-level mistake by the client,
        # not a coaching refusal: there is no tool whose result it could be.
        raise MCPError(types.INVALID_PARAMS, f"unknown tool: {name!r}")
    arguments = dict(params.arguments or {})
    state = _request_state(ctx)

    def run() -> types.CallToolResult:
        with adopt_provider_quota_scope(state.quota):
            # Which tool this HTTP request's provider spend belongs to, for the
            # access-log line (issue #260). The public name, which the client already sent.
            name_provider_quota_tool(name)
            try:
                payload = state.call_tool(tool.kind, arguments)
            except mcp_transport.ToolCallBlocked as blocked:
                # The gateway's own refusal body, through the same projection, so the
                # model reads the same `status: blocked` and error code the gateway's own
                # body carries. No `structuredContent` here: `outputSchema` describes this
                # tool's result, a refusal is not one, and a validating client must not be
                # handed a refusal shaped as if it were.
                projected = mcp_transport._redact(blocked.payload, tool.redactions)
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=mcp_transport.client_result_json(projected)
                        )
                    ],
                    is_error=True,
                )
            except BaseException as escalated:  # noqa: BLE001 - parked, then re-raised
                # Not a refusal: the gateway raised something only the request thread can
                # answer correctly -- a provider credential the athlete revoked, which
                # must become a `401` carrying the challenge, or an internal failure,
                # which must become a `500` with a logged traceback. `dispatch` re-raises
                # it there. The result built here is never written: the raise comes first.
                state.escalated = escalated
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="")], is_error=True
                )
        projected = mcp_transport._redact(payload, tool.redactions)
        # Both members carry the same projected object: `structuredContent` is what the
        # protocol obliges a tool with an `outputSchema` to return, and the text block is
        # the same JSON serialized for clients that read only `content`. One projection,
        # serialized once -- never a fuller copy on one member than the other.
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=mcp_transport.client_result_json(projected))
            ],
            structured_content=projected,
        )

    return await anyio.to_thread.run_sync(run)


def build_server(server_version: str) -> Server[Any]:
    """The SDK server this deployment serves, built from this product's own surface.

    ``name``, ``title`` and ``instructions`` are ``mcp_transport``'s and
    ``orchestration``'s, unchanged: they are reviewed bytes, and the SDK is the thing
    that carries them onto whichever wire the client is speaking. No resources, sampling
    or logging are registered -- each would be a second way to reach the same state -- so
    the capabilities the SDK advertises are derived from exactly the four handlers here.
    """
    return Server(
        mcp_transport.SERVER_NAME,
        version=server_version,
        title=mcp_transport.SERVER_TITLE,
        instructions=orchestration.instructions(),
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_prompts=_on_list_prompts,
        on_get_prompt=_on_get_prompt,
    )


# --------------------------------------------------------------------------------------
# One event loop beside the stdlib HTTP server
# --------------------------------------------------------------------------------------


class SdkTransportError(RuntimeError):
    """The transport was asked to serve a request while it was not running."""


class SdkTransport:
    """The SDK's session manager, hosted on a loop of its own.

    One instance per gateway process. ``start`` brings up the loop and the manager;
    ``dispatch`` is called from a request thread and returns the finished HTTP response;
    ``stop`` tears both down after the HTTP server has already drained, so a request
    thread can never be waiting on a loop that was closed underneath it.

    ``stateless`` and ``json_response`` are what this product already was: it keeps no
    per-connection state -- every request re-resolves its owner from the bearer token --
    and it answers one JSON body per POST rather than opening an SSE stream.
    """

    def __init__(self, server_version: str) -> None:
        self._server_version = server_version
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._manager: StreamableHTTPSessionManager | None = None
        self._ready = threading.Event()
        self._stopping: asyncio.Event | None = None
        self._failure: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        """Bring the loop up and wait until the manager is actually serving.

        Blocking until ready rather than starting lazily on the first request: a
        deployment whose MCP layer cannot start should fail at startup, beside the state
        root and identity registry checks, not on whichever athlete's request arrives
        first.
        """
        if self._thread is not None:  # pragma: no cover - defensive
            raise SdkTransportError("transport already started")
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-sdk-transport", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        if self._failure is not None:
            raise SdkTransportError(f"MCP transport failed to start: {self._failure}")

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # pragma: no cover - defensive
            self._failure = exc
            LOGGER.exception("MCP transport loop stopped")
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        server = build_server(self._server_version)
        manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
        self._stopping = asyncio.Event()
        async with manager.run():
            self._manager = manager
            self._loop = asyncio.get_running_loop()
            self._ready.set()
            await self._stopping.wait()
        self._manager = None

    def stop(self) -> None:
        """Close the loop once nothing can still be dispatched into it."""
        loop, stopping, thread = self._loop, self._stopping, self._thread
        if loop is None or stopping is None or thread is None:
            return
        self._loop = None
        loop.call_soon_threadsafe(stopping.set)
        thread.join(timeout=10)
        self._thread = None

    # -- serving ------------------------------------------------------------------

    def dispatch(
        self,
        *,
        method: str,
        path: str,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        call_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
        quota: ProviderQuotaScope | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """Serve one already-authenticated ``/mcp`` request; return its HTTP response.

        ``headers`` is the caller's raw header lines rather than a folded mapping: the
        modern era refuses a routing header supplied twice, and a mapping has already
        lost that.

        There is no timeout here on purpose. A coaching call can take as long as the
        provider does, which is already bounded per call by
        ``source_intervals.REQUEST_TIMEOUT_SECONDS``, and a ceiling invented here would
        cut off a delivery that was still converging. What makes the wait safe is the
        shutdown order: ``stop`` runs only after the HTTP server has joined every request
        thread, so this future always belongs to a loop that is still running.
        """
        loop, manager = self._loop, self._manager
        if loop is None or manager is None:
            raise SdkTransportError("MCP transport is not running")
        state = _RequestState(call_tool, quota)
        future: Future[tuple[int, list[tuple[str, str]], bytes]] = asyncio.run_coroutine_threadsafe(
            _asgi_exchange(
                manager,
                method=method,
                path=path,
                headers=headers,
                body=body,
                state=state,
            ),
            loop,
        )
        answered = future.result()
        if state.escalated is not None:
            raise state.escalated
        return answered


# What the streamable-HTTP specification requires a client to send, and what the SDK
# enforces before it will answer.
_STREAMABLE_HTTP_ACCEPT = "application/json, text/event-stream"


def _asgi_headers(headers: Sequence[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """The request's header lines, with one absent header supplied.

    The hand-written transport never read ``Accept`` at all, so a client that sent none
    was answered. The SDK enforces the specification's requirement and answers ``406``.
    That difference is only reachable by a client that is already out of spec, but a
    rollout is not the place to discover which ones those are: an athlete cannot fix a
    ``406`` from their side, and there is no version of this migration worth a working
    connection going dark. So an *absent* ``Accept`` is read as the pair the
    specification asks for, which is exactly what this server has been answering as.

    A *present* ``Accept`` is left alone, including one this server cannot satisfy. That
    client is asking for something else, and has said so; answering it anyway would be
    the dishonest half of leniency.
    """
    lines = [
        (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers
    ]
    if not any(name == b"accept" for name, _ in lines):
        lines.append((b"accept", _STREAMABLE_HTTP_ACCEPT.encode("latin-1")))
    return lines


async def _asgi_exchange(
    manager: StreamableHTTPSessionManager,
    *,
    method: str,
    path: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    state: _RequestState,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """One stdlib request, expressed as the ASGI call the SDK expects.

    The scope carries only what the SDK reads: the method, the path, the headers and a
    ``state`` mapping this module's handlers pick the caller's binding out of. No client
    address and no query string are forwarded -- neither reaches a decision here, and the
    gateway's own rule is that nothing in the transport learns who the athlete is.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": _asgi_headers(headers),
        "client": None,
        "server": None,
        "state": {_STATE_KEY: state},
    }
    status = 500
    response_headers: list[tuple[str, str]] = []
    chunks: list[bytes] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal status, response_headers, delivered
        if message["type"] == "http.response.start":
            status = int(message["status"])
            response_headers = [
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in message.get("headers", ())
            ]
            delivered = True
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))

    await manager.handle_request(scope, receive, send)
    if not delivered:  # pragma: no cover - defensive; the SDK always starts a response
        raise SdkTransportError("MCP transport produced no response")
    return status, response_headers, b"".join(chunks)
