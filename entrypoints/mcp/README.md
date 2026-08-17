# MCP entry

The same Coach Gateway that serves the Custom GPT entry also speaks the Model Context
Protocol on `POST /mcp` — JSON-RPC 2.0 over streamable HTTP, tools only. There is no
separate server, package, or state: an MCP tool call and a Custom GPT action land in the
same dispatch, the same validator, and the same per-athlete PlanState. An athlete who
connects both entries with the same Intervals account is the same owner in both, and
connecting one does not disconnect the other.

## Connecting a client

Any MCP client that speaks streamable HTTP can use the hosted endpoint directly:

- **claude.ai / Claude Desktop** — Settings → Connectors → *Add custom connector*, with
  the gateway's `/mcp` URL. Every plan (including Free) can add one; no directory
  listing is required.
- **Anything else** (ChatGPT developer-mode connectors, Codex, other agents) — point the
  client at the same URL.

## Authorization

Discovered, not configured. An unauthenticated request is answered with `401` plus a
`WWW-Authenticate` challenge naming `/.well-known/oauth-protected-resource`, and the
metadata there points at the gateway's own authorization server. Dynamic client
registration (`POST /oauth/register`) mints the client a `client_id` of its own, carrying
the redirect URIs it registered sealed inside it: the id *is* the registration, so there
is no client table, and it never expires. No client secret is issued — the Intervals one
stays in this process, and the Intervals client id is this gateway's credential upstream
rather than anything an MCP client may present.

A registered redirect URI is `http://` on `127.0.0.1`, `[::1]` or `localhost` — a local
client cannot hold a certificate for its own loopback callback, which is the only reason
plaintext is allowed at all — or `https://` on an origin this deployment trusts. Anything
else (a custom scheme, a plaintext public host, a URI with a fragment, an untrusted
remote origin) refuses the whole registration rather than being quietly dropped from it.
At authorize time the requested URI must be one of the registered ones, matched exactly;
a loopback URI matches on scheme, host, path and query with the port compared out,
because a local client binds its port after it registers (RFC 8252 §7.3).

**Remote registration is not open.** A client that names a callback on an origin this
deployment does not trust is refused at `/oauth/register`, before an authorization can
start — and so before the athlete could be shown an Intervals consent screen that does
not name the client receiving the result. Intervals can tell an athlete which upstream
application is asking; nothing in that flow tells them which downstream MCP client the
Coach authorization goes to, and PKCE does not help, because whoever starts the flow
holds the verifier. The trusted set is `https://claude.ai` and `https://claude.com`, plus
whatever `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` adds; loopback needs no entry.
Supporting a new hosted agent normally means validating its flow once and adding its
origin, not changing code. This is a separate list from the `Origin` check below, which
answers a different question about a different caller.

The gateway runs the flow rather than forwarding it:

1. `GET /oauth/authorize` — the client arrives with its `client_id`, its redirect URI, its
   `state`, and a PKCE `S256` challenge. The id has to open as a registration this gateway
   issued and the URI has to be one that registration holds, or nothing reaches Intervals;
   all of it is then sealed into the `state` sent on to the Intervals consent page. A
   request without a challenge is refused here.
2. The athlete consents at Intervals, which returns to `<gateway>/oauth/callback`.
3. The gateway exchanges the provider code server-side (the client secret never leaves
   the process), registers the athlete's identity, and redirects the client back to its
   own URI with an authorization code of the gateway's own — good for 60 seconds.
4. `POST /oauth/token` — the client presents that code with its `client_id` and its
   `code_verifier`. The gateway checks the client id, the verifier, the redirect URI, and
   the requested resource, then issues **its own** access token.

That token is what `/mcp` accepts, and the only thing it accepts: a bare Intervals token
presented there is refused. The token names the audience it was issued for, so it is
useless against another deployment, and the Intervals credential inside it is encrypted
under a key only the gateway holds — a client that leaks its token does not leak the
athlete's Intervals account. Nothing is stored server-side: the token *is* the storage.

There is no refresh token and no stated expiry, because Intervals issues neither. When a
provider credential does stop working, the next tool call comes back as `401` with the
challenge above, and a conforming client re-runs this flow on its own.

One deployment-side prerequisite, once per deployment rather than once per client: add
`https://<gateway-domain>/oauth/callback` to the Intervals application's registered
redirect URIs (intervals.icu → Settings → Developer). MCP clients no longer need anything
registered at Intervals — their callback URLs are the gateway's business, not the
provider's.

The Custom GPT entry keeps its own `/oauth/intervals/*` endpoints unchanged, and an
athlete connected through both entries is one owner with two live connections.

## Two headers `/mcp` checks before the token

- `Origin` — absent passes, which is the normal case: a server-side MCP client sends none,
  and every client this product is reached from is one. A header that *is* present is a
  browser, and must name this deployment's own origin, `https://claude.ai`, or an origin
  listed in `GARMIN_COACH_LOOP_MCP_ALLOWED_ORIGINS` (comma-separated); anything else is
  `403`. The protocol requires this against DNS rebinding, and the comparison is the whole
  scheme-host-port triple — `https://claude.ai.evil.example` is a different origin.
- `MCP-Protocol-Version` — absent means `2025-03-26`, which is what the specification says
  it means. `2025-06-18` and `2025-03-26` are accepted; anything else is `400`. This is the
  HTTP-level statement of an already-negotiated revision, separate from the
  `protocolVersion` settled during `initialize`.

## What a client gets

`tools/list` returns the fifteen coach operations, named identically to the OpenAPI
`operationId`s the Custom GPT entry uses (`startCoachSession`,
`prepareWorkoutDelivery`, …). Contract tests hold the two surfaces to each other, so a
capability present in one entry and missing from the other fails the build, not the
athlete.

A refused coaching action — a stale plan version, a missing confirmation, an open
delivery reservation — comes back as a tool *result* with `isError: true` and the
gateway's own error payload, so the model can read the reason and act on it. Only a
message that cannot be read as JSON-RPC at all becomes a protocol error.

The server keeps nothing between requests: no session id, no SSE stream, no server-side
proposal store. A new conversation reconstructs continuity the same way every entry
does — by reading the current PlanState back through `startCoachSession`.
