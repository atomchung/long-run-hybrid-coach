"""The local entry as a client of the hosted gateway, rather than a second coach.

Every hosted entry -- the claude.ai connector, ChatGPT's MCP connector, an agent running
on the athlete's own machine -- reaches one owner-scoped store through ``/mcp``. The local
CLI historically did not: it read and wrote a store on the machine it ran on, and nothing
kept the two in step. On 2026-08-17 they diverged for real, local v7 against hosted v6,
with three sessions on the calendar the hosted side had never heard of (issue #40).

This module is the local side of the answer, and it is deliberately three small things
rather than one large one:

- **Which entry this machine belongs to.** ``GARMIN_COACH_LOOP_GATEWAY_URL`` names the
  hosted coach. Once it is set, a command that would write a *local* store has to say
  ``--offline`` out loud, because on a machine with a hosted entry a silent local write is
  the second plan the product is trying not to have.
- **An MCP client.** The same JSON-RPC-over-HTTP dance a connector does, in about a
  hundred lines, so the athlete can read the canonical plan from a terminal and so the
  end-to-end path -- register, authorize, redeem, call a tool -- is exercised by the test
  suite rather than only by a vendor's client.
- **An authorization that leaves nothing behind.** The token this client ends up holding
  lives in memory for the life of the process. It is never written to disk, never logged,
  and never printed: a credential store is a thing to leak, and this product already has
  a place where connections are recorded -- the gateway's identity registry, which keeps
  a keyed fingerprint and never the token.

What this module is *not* is a second coach. It sends tool calls and renders replies; the
validator, the store, the delivery boundary and identity all stay on the gateway. The
local store keeps working exactly as before for anyone running offline (issue #114's
first-class local execution) -- what changed is that a machine cannot be in both modes
without saying so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable


# Which hosted coach this machine belongs to. Naming it is how an athlete says "the plan
# lives there", and every local write command reads it before touching a store.
GATEWAY_URL_ENV_VAR = "GARMIN_COACH_LOOP_GATEWAY_URL"
# An access token this gateway already issued, for a second command in the same session
# without a second trip through the browser. An environment variable rather than a file on
# purpose: it expires with the shell, and this product never writes a credential down.
HOSTED_TOKEN_ENV_VAR = "GARMIN_COACH_LOOP_HOSTED_TOKEN"
# The explicit override, for a machine that has a hosted entry configured and genuinely
# means to work on a local store this time (a development store, a fixture, a rehearsal).
ENTRY_MODE_ENV_VAR = "GARMIN_COACH_LOOP_MODE"

HOSTED_MODE = "hosted"
OFFLINE_MODE = "offline"
ENTRY_MODES = (HOSTED_MODE, OFFLINE_MODE)

MCP_PATH = "/mcp"
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
CLIENT_NAME = "garmin-coach-loop-cli"
PROTOCOL_VERSION = "2025-06-18"
REQUEST_TIMEOUT_SECONDS = 30
# How long the athlete has to finish consenting in the browser before the local callback
# stops waiting. Long enough for a login and a consent screen, short enough that a
# forgotten terminal does not hold a socket open forever.
CALLBACK_TIMEOUT_SECONDS = 300
CALLBACK_PATH = "/callback"


class HostedEntryError(RuntimeError):
    """A hosted-entry operation was refused, or the gateway could not be reached."""


# --------------------------------------------------------------------------------------
# Which entry this machine belongs to
# --------------------------------------------------------------------------------------


def normalize_gateway_url(raw: str) -> str:
    """Reduce a configured gateway to ``scheme://host[:port]``, or refuse it.

    Plaintext is confined to loopback, exactly as the gateway confines a registrable
    callback: the bearer this client ends up holding carries the athlete's provider
    credential inside it, and ``http://`` to anywhere but this machine puts it on a wire
    in the clear. A path, query or fragment is refused rather than trimmed -- every URL
    this module builds appends its own path, and silently dropping part of a configured
    value is how a client ends up talking to something other than what was configured.
    """
    value = str(raw or "").strip().rstrip("/")
    if not value:
        raise HostedEntryError("hosted gateway URL is empty")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HostedEntryError("hosted gateway URL must be http(s)://host[:port]")
    if parsed.path or parsed.query or parsed.fragment:
        raise HostedEntryError("hosted gateway URL must carry no path, query or fragment")
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme == "http" and not loopback:
        raise HostedEntryError(
            "a hosted gateway URL must be https:// unless it is on this machine"
        )
    return value


def configured_gateway(env: dict[str, str] | None = None) -> str | None:
    """The hosted coach this machine is pointed at, or ``None`` when there is none."""
    source = os.environ if env is None else env
    raw = str(source.get(GATEWAY_URL_ENV_VAR) or "").strip()
    return normalize_gateway_url(raw) if raw else None


def resolve_entry_mode(
    env: dict[str, str] | None = None, *, offline: bool = False
) -> str:
    """Decide whether this invocation acts on the hosted plan or on a local store.

    An explicit ``--offline`` wins, then ``GARMIN_COACH_LOOP_MODE``, then the presence of
    a configured gateway. A machine that has never named a hosted coach is offline and
    stays offline: local execution is a supported way to run this product, not a fallback
    (issue #114), and nothing about it is ambiguous when there is no second store to
    diverge from.
    """
    source = os.environ if env is None else env
    if offline:
        return OFFLINE_MODE
    declared = str(source.get(ENTRY_MODE_ENV_VAR) or "").strip().lower()
    if declared and declared not in ENTRY_MODES:
        raise HostedEntryError(
            f"{ENTRY_MODE_ENV_VAR} must be one of {list(ENTRY_MODES)}"
        )
    if declared == OFFLINE_MODE:
        return OFFLINE_MODE
    gateway = configured_gateway(source)
    if declared == HOSTED_MODE:
        if gateway is None:
            raise HostedEntryError(
                f"{ENTRY_MODE_ENV_VAR}={HOSTED_MODE} needs {GATEWAY_URL_ENV_VAR} set to "
                "the hosted coach's URL"
            )
        return HOSTED_MODE
    return HOSTED_MODE if gateway is not None else OFFLINE_MODE


def require_local_store_write(
    operation: str, env: dict[str, str] | None = None, *, offline: bool = False
) -> None:
    """Refuse a local write on a machine whose plan lives on the hosted coach.

    The refusal is not an opinion about which store is better. It is that two writable
    stores for one Intervals account produce two current plans and one calendar, and the
    only way to be sure which one the athlete is being coached from is that there is one.
    ``--offline`` remains available and remains the honest way to say "a different store,
    on purpose".
    """
    if resolve_entry_mode(env, offline=offline) == OFFLINE_MODE:
        return
    gateway = configured_gateway(env)
    raise HostedEntryError(
        f"{operation} writes a local store, and this machine's plan lives on the hosted "
        f"coach at {gateway}. Do it there, or pass --offline to work on a local store "
        "that is deliberately not the athlete's current plan."
    )


# --------------------------------------------------------------------------------------
# The MCP client
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostedResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostedEntryError("the hosted gateway returned a non-JSON body") from exc
        if not isinstance(value, dict):
            raise HostedEntryError("the hosted gateway returned a non-object body")
        return value


Transport = Callable[[urllib.request.Request], HostedResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the redirect rather than following it.

    Following would send this client to the Intervals consent page, which is the
    browser's hop and not this process's: a terminal cannot consent, and a redirect
    followed here would carry the request somewhere it was never authorized to go.
    """

    def redirect_request(self, *args, **kwargs):  # noqa: D102 - stdlib signature
        return None


def _default_transport(request: urllib.request.Request) -> HostedResponse:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return HostedResponse(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return HostedResponse(exc.code, dict(exc.headers), exc.read())
    except urllib.error.URLError as exc:
        # The reason only: a URLError's string form interpolates the URL, and the URL is
        # the one place in this flow a code or a token can appear.
        raise HostedEntryError(f"cannot reach the hosted gateway: {exc.reason}") from exc


class HostedClient:
    """One athlete's connection to one hosted gateway, over the documented MCP entry.

    ``transport`` is the single injection seam, matching the convention in
    ``source_intervals`` and ``gateway``: one callable taking a prepared request and
    returning the response, so a test can drive the whole flow against a real loopback
    server without a browser, and no code path here has a second way out to the network.
    """

    def __init__(self, base_url: str, *, transport: Transport | None = None):
        self.base_url = normalize_gateway_url(base_url)
        self._transport = transport or _default_transport

    # -- plumbing ---------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HostedResponse:
        data: bytes | None = None
        prepared = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            prepared["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            prepared["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        for name, value in prepared.items():
            request.add_header(name, value)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        return self._transport(request)

    @property
    def mcp_url(self) -> str:
        return self.base_url + MCP_PATH

    # -- discovery and registration ---------------------------------------------------

    def protected_resource(self) -> dict[str, Any]:
        """RFC 9728 discovery, which is how a client learns where to authorize.

        Read rather than assumed even though this client knows the paths: a deployment
        that answered with another authorization server is a deployment this client must
        not silently authorize against.
        """
        response = self._request("GET", PROTECTED_RESOURCE_METADATA_PATH)
        if response.status != 200:
            raise HostedEntryError(
                f"the hosted gateway did not describe its protected resource ({response.status})"
            )
        metadata = response.json()
        if metadata.get("resource") != self.mcp_url:
            raise HostedEntryError("the hosted gateway named a different MCP resource")
        servers = metadata.get("authorization_servers")
        if not isinstance(servers, list) or self.base_url not in servers:
            raise HostedEntryError(
                "the hosted gateway delegates authorization elsewhere; refusing to follow"
            )
        return metadata

    def authorization_server(self) -> dict[str, Any]:
        """RFC 8414 metadata, checked for the three endpoints this flow uses.

        Each returned endpoint has to be on this same gateway. A discovery document is
        fetched before any token exists, so treating one as authoritative about *where* to
        send an authorization is exactly how a client is walked off its own deployment.
        """
        response = self._request("GET", "/.well-known/oauth-authorization-server")
        if response.status != 200:
            raise HostedEntryError(
                f"the hosted gateway did not describe its authorization server ({response.status})"
            )
        metadata = response.json()
        for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            endpoint = str(metadata.get(field) or "")
            if not endpoint.startswith(self.base_url + "/"):
                raise HostedEntryError(
                    f"the hosted gateway's {field} is not on this deployment"
                )
        return metadata

    def register(self, redirect_uri: str) -> str:
        """Take a ``client_id`` for this machine, for this callback.

        No secret is issued and none is asked for: the gateway registers public clients
        only, and a CLI on somebody's laptop could not keep one anyway.
        """
        response = self._request(
            "POST",
            "/oauth/register",
            json_body={
                "redirect_uris": [redirect_uri],
                "client_name": CLIENT_NAME,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
        )
        if response.status not in {200, 201}:
            raise HostedEntryError(
                f"the hosted gateway refused this client's registration ({response.status})"
            )
        client_id = response.json().get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise HostedEntryError("the hosted gateway issued no client id")
        return client_id

    def authorization_url(
        self, *, client_id: str, redirect_uri: str, state: str, verifier: str
    ) -> str:
        """The URL the athlete opens. Everything secret about this hop stays here.

        The verifier never leaves this process -- only its S256 challenge travels -- so an
        authorization code observed anywhere between the browser and this callback cannot
        be redeemed by whoever observed it.
        """
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "resource": self.mcp_url,
        }
        return f"{self.base_url}/oauth/authorize?" + urllib.parse.urlencode(query)

    def redeem(
        self, *, code: str, client_id: str, redirect_uri: str, verifier: str
    ) -> str:
        """Exchange the code for this gateway's own access token, and return it.

        Returned rather than stored: the caller holds it for as long as the command runs.
        """
        response = self._request(
            "POST",
            "/oauth/token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "resource": self.mcp_url,
            },
        )
        payload = response.json()
        if response.status != 200:
            # The gateway's own OAuth error code, which is the actionable half; its
            # description is not echoed, because a description is where an upstream
            # message could carry something this client should not be repeating.
            raise HostedEntryError(
                "the hosted gateway refused the authorization: "
                + str(payload.get("error") or response.status)
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise HostedEntryError("the hosted gateway issued no access token")
        return token

    # -- MCP --------------------------------------------------------------------------

    def _rpc(self, token: str, message: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            MCP_PATH,
            json_body=message,
            token=token,
            headers={
                "Accept": "application/json",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )
        if response.status == 401:
            raise HostedEntryError(
                "the hosted gateway no longer accepts this token; authorize again"
            )
        if response.status >= 400:
            raise HostedEntryError(
                f"the hosted gateway refused the request ({response.status})"
            )
        payload = response.json()
        if "error" in payload:
            error = payload["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise HostedEntryError(f"the hosted gateway returned a protocol error: {detail}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HostedEntryError("the hosted gateway returned no result")
        return result

    def initialize(self, token: str) -> dict[str, Any]:
        return self._rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": "1"},
                },
            },
        )

    def list_tools(self, token: str) -> list[dict[str, Any]]:
        result = self._rpc(
            token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(
        self, token: str, name: str, arguments: dict[str, Any] | None = None
    ) -> tuple[bool, dict[str, Any]]:
        """One tool call, as ``(refused, payload)``.

        A refusal is a result rather than an exception because that is what it is on the
        wire: the gateway answers a blocked coaching action with its own error body inside
        an ``isError`` result, and the caller has to be able to print it.
        """
        result = self._rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        content = result.get("content")
        if not isinstance(content, list) or not content:
            raise HostedEntryError(f"{name} returned no content")
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if not isinstance(text, str):
            raise HostedEntryError(f"{name} returned no readable content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HostedEntryError(f"{name} returned unreadable content") from exc
        if not isinstance(payload, dict):
            raise HostedEntryError(f"{name} returned a non-object payload")
        return bool(result.get("isError")), payload


# --------------------------------------------------------------------------------------
# The browser hop
# --------------------------------------------------------------------------------------


def code_verifier() -> str:
    """A fresh PKCE verifier. 64 bytes of urandom, which is well past RFC 7636's floor."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catch exactly one redirect back from the gateway, and say nothing else."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        query = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(parsed.query).items()
            if values
        }
        self.server.captured = query  # type: ignore[attr-defined]
        body = (
            b"<!doctype html><meta charset=utf-8><title>Coach connected</title>"
            b"<p>Connected. You can close this tab and return to the terminal.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        """Silence. The request line carries the authorization code."""


class LoopbackCallback:
    """A one-shot ``http://127.0.0.1:<port>/callback`` for one authorization.

    Bound before the client registers, so the callback registered is the exact one the
    gateway will redirect to -- port included -- rather than a guess that RFC 8252's
    port-agnostic matching would have to rescue.
    """

    def __init__(self) -> None:
        self._server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._server.captured = None  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{CALLBACK_PATH}"

    def __enter__(self) -> "LoopbackCallback":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def wait(self, *, state: str, timeout: float = CALLBACK_TIMEOUT_SECONDS) -> str:
        """Block until the browser comes back, and return the authorization code.

        The ``state`` is compared before the code is read: it is what proves this callback
        belongs to the authorization this process started, rather than one a page the
        athlete happened to have open sent to the same loopback port.
        """
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout:
            captured = getattr(self._server, "captured", None)
            if captured is not None:
                if captured.get("error"):
                    raise HostedEntryError(
                        "the authorization was refused: " + str(captured["error"])
                    )
                if not secrets.compare_digest(str(captured.get("state") or ""), state):
                    raise HostedEntryError("the authorization callback did not match this request")
                code = captured.get("code")
                if not isinstance(code, str) or not code:
                    raise HostedEntryError("the authorization callback carried no code")
                return code
            deadline.wait(0.05)
            waited += 0.05
        raise HostedEntryError("timed out waiting for the browser to complete authorization")


@dataclass(frozen=True)
class HostedConnection:
    """One live connection. ``access_token`` exists for this process and no longer."""

    client: HostedClient
    access_token: str

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> tuple[bool, dict[str, Any]]:
        return self.client.call_tool(self.access_token, name, arguments)


def connect(
    base_url: str,
    *,
    transport: Transport | None = None,
    open_url: Callable[[str], Any] | None = None,
    announce: Callable[[str], None] | None = None,
    timeout: float = CALLBACK_TIMEOUT_SECONDS,
) -> HostedConnection:
    """Run the whole authorization once and hand back a connection.

    Discovery, registration, the browser hop, and the redemption -- in that order, because
    each one's output is the next one's input, and because a client that skipped discovery
    would be asserting endpoints rather than being told them.

    ``open_url`` is the browser. It is injectable so the test suite can play the athlete's
    part against a real server; the default opens the athlete's actual browser.
    """
    client = HostedClient(base_url, transport=transport)
    client.protected_resource()
    client.authorization_server()
    with LoopbackCallback() as callback:
        client_id = client.register(callback.redirect_uri)
        verifier = code_verifier()
        state = secrets.token_urlsafe(32)
        url = client.authorization_url(
            client_id=client_id,
            redirect_uri=callback.redirect_uri,
            state=state,
            verifier=verifier,
        )
        if announce is not None:
            announce(url)
        opener = open_url or _open_browser
        opener(url)
        code = callback.wait(state=state, timeout=timeout)
    token = client.redeem(
        code=code,
        client_id=client_id,
        redirect_uri=callback.redirect_uri,
        verifier=verifier,
    )
    return HostedConnection(client, token)


def _open_browser(url: str) -> None:
    # Imported here rather than at module scope: `webbrowser` reads the environment and
    # probes for browsers on import, which is work no non-interactive use of this module
    # should be doing.
    import webbrowser

    webbrowser.open(url)


def hosted_connection(
    base_url: str,
    *,
    env: dict[str, str] | None = None,
    transport: Transport | None = None,
    open_url: Callable[[str], Any] | None = None,
    announce: Callable[[str], None] | None = None,
) -> HostedConnection:
    """A connection from an already-held token when there is one, else a fresh authorization.

    The variable is read here and nowhere else, and its value never reaches a report, a
    log line or a file. What a command may print about a connection is what the gateway
    tells it -- the plan, the owner's own state -- never the credential that reached it.
    """
    source = os.environ if env is None else env
    held = str(source.get(HOSTED_TOKEN_ENV_VAR) or "").strip()
    if held:
        return HostedConnection(HostedClient(base_url, transport=transport), held)
    return connect(base_url, transport=transport, open_url=open_url, announce=announce)
