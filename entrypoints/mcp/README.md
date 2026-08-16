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
registration (`POST /oauth/register`) hands the client the one public client id.

The gateway runs the flow rather than forwarding it:

1. `GET /oauth/authorize` — the client arrives with its redirect URI, its `state`, and a
   PKCE `S256` challenge. All three are sealed into the `state` the gateway sends on to
   the Intervals consent page. A request without a challenge is refused here.
2. The athlete consents at Intervals, which returns to `<gateway>/oauth/callback`.
3. The gateway exchanges the provider code server-side (the client secret never leaves
   the process), registers the athlete's identity, and redirects the client back to its
   own URI with an authorization code of the gateway's own — good for 60 seconds.
4. `POST /oauth/token` — the client presents that code with its `code_verifier`. The
   gateway checks the verifier, the redirect URI, and the requested resource, then issues
   **its own** access token.

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
