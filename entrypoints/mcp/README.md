# MCP entry

The same Coach Gateway that serves the Custom GPT entry also speaks the Model Context
Protocol on `POST /mcp` — JSON-RPC 2.0 over streamable HTTP, tools only. There is no
separate server, package, or state: an MCP tool call and a Custom GPT action land in the
same dispatch, the same validator, and the same per-athlete PlanState. An athlete who
connects both entries with the same Intervals authorization is the same owner in both.

## Connecting a client

Any MCP client that speaks streamable HTTP can use the hosted endpoint directly:

- **claude.ai / Claude Desktop** — Settings → Connectors → *Add custom connector*, with
  the gateway's `/mcp` URL. Every plan (including Free) can add one; no directory
  listing is required.
- **Anything else** (ChatGPT developer-mode connectors, Codex, other agents) — point the
  client at the same URL.

Authorization is discovered, not configured: an unauthenticated request is answered with
`401` plus a `WWW-Authenticate` challenge naming
`/.well-known/oauth-protected-resource`, and the metadata there walks the client through
the gateway's existing Intervals OAuth passthrough — authorize at Intervals, exchange
the code at the gateway (the client secret never leaves the server), then present the
resulting token as a bearer on every request. Dynamic client registration
(`POST /oauth/register`) hands the client the one registered public client id.

One deployment-side prerequisite: the MCP client's OAuth callback URL must be added to
the Intervals application's registered redirect URIs (intervals.icu → Settings →
Developer). Intervals is the party that validates redirects; the gateway deliberately
does not second-guess it. claude.ai's callback is
`https://claude.ai/api/mcp/auth_callback` — if the consent page refuses with a redirect
mismatch, the URI shown in that error is the one to register.

## What a client gets

`tools/list` returns the fourteen coach operations, named identically to the OpenAPI
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
