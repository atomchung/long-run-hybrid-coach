# MCP entry

The same Coach Gateway that serves the Custom GPT entry also speaks the Model Context
Protocol on `POST /mcp` — JSON-RPC 2.0 over streamable HTTP, serving the coach tools
and one orchestration prompt. There is no
separate server, package, or state: an MCP tool call and a Custom GPT action land in the
same dispatch, the same validator, and the same per-athlete PlanState. An athlete who
connects both entries with the same Intervals account is the same owner in both, and
connecting one does not disconnect the other.

## One store, whichever client asks

The hosted owner store is the athlete's canonical PlanState, and every client here reads
and writes that one (issue #40). `(provider, provider_athlete_id) -> owner -> one
append-only PlanState` is the whole of the mapping: re-authorizing resolves to the same
owner, a second client resolves to the same owner, and neither creates a second plan. Two
different athletes resolve to different owners and cannot name, read or change each
other's state.

The local CLI is a client of the same endpoint rather than an exception to it. On a
machine that names a hosted coach — `GARMIN_COACH_LOOP_GATEWAY_URL` — every command that
would write a *local* store refuses unless it is told `--offline`, and a store that has
been migrated is sealed outright. Local execution stays a supported way to run the whole
product (issue #114); what is not supported is running both at once without saying so.
See [`../../docs/ops/migrate-local-store-to-hosted.md`](../../docs/ops/migrate-local-store-to-hosted.md)
for moving an existing local store here once.

## Connecting a client

Any MCP client that speaks streamable HTTP can use the hosted endpoint directly:

- **claude.ai / Claude Desktop** — Settings → Connectors → *Add custom connector*, with
  the gateway's `/mcp` URL. Every plan (including Free) can add one; no directory
  listing is required. Trusted out of the box.
- **A client running on the athlete's own machine** (Codex, a local agent, anything using
  a loopback callback) — point it at the same URL. Loopback registration needs no
  deployment change.
- **Another hosted agent** (ChatGPT's MCP connector, OpenClaw, a Gemini remote surface) —
  the same URL, but its callback origin has to be trusted by the deployment first, or
  registration is refused with an `error_description` saying so. See "Admitting a new
  hosted client" in [`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md); it is
  a one-line configuration change, not a code change.

## Authorization

Discovered, not configured. An unauthenticated request is answered with `401` plus a
`WWW-Authenticate` challenge naming `/.well-known/oauth-protected-resource`, and the
metadata there points at the gateway's own authorization server. Dynamic client
registration (`POST /oauth/register`) mints the client a `client_id` of its own, carrying
the redirect URIs it registered sealed inside it: the id *is* the registration, so there
is no client table, and it never expires. No client secret is issued — the Intervals one
stays in this process, and the Intervals client id is this gateway's credential upstream
rather than anything an MCP client may present.

A registered redirect URI is either **on loopback** — `127.0.0.1`, `[::1]` or `localhost`,
under `http` or `https`, on any port — or **`https://` on an origin this deployment
trusts**. Plaintext is confined to loopback, because a local client cannot hold a
certificate for its own callback and a code on that address never crosses a network.
Anything else (a custom scheme, a plaintext public host, a URI with a fragment, an
untrusted remote origin) refuses the whole registration rather than being quietly
dropped from it.
At authorize time the requested URI must be one of the registered ones, matched exactly;
a loopback URI matches on scheme, host, path and query with the port compared out,
because a local client binds its port after it registers (RFC 8252 §7.3).

**Remote registration is not open.** A client that names a callback on an origin this
deployment does not trust is refused at `/oauth/register`, before an authorization can
start — and so before the athlete could be shown an Intervals consent screen that does
not name the client receiving the result. Intervals can tell an athlete which upstream
application is asking; nothing in that flow tells them which downstream MCP client the
Coach authorization goes to, and PKCE does not help, because whoever starts the flow
holds the verifier.

The trusted set is `https://claude.ai`, `https://claude.com` and `https://chatgpt.com`,
plus whatever `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` adds; loopback needs no entry.
**Origins, not callback URLs** — ChatGPT mints a callback id per connector instance and a
local client binds its port at startup, so a list of whole URLs would refuse both while a
list of origins refuses neither. Supporting a new hosted agent normally means validating
its flow once and adding its origin, not changing code. Removing one is a revocation
rather than a closed door: the list is checked again at `/oauth/authorize`, so an origin
taken off it stops every client already registered there from starting an authorization,
not only new registrations. This is a separate list from the `Origin` check below, which
answers a different question about a different caller.

Two shapes are known not to work yet, both from the Codex/OpenAI side: a Codex client
pointed at a **custom non-loopback callback** (`mcp_oauth_callback_url`) needs that origin
configured like any other remote client, and **CIMD**, where the client identifies itself
with a URL-shaped `client_id` it hosts rather than one this gateway issued, is not
implemented — `/oauth/authorize` accepts only ids it sealed itself.

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

The scope a client asks for is narrowed to what this product declares, never widened: a
client may request less than the four scopes above and then discover the call it cannot
make, but it cannot put a wider grant in front of the athlete on the Intervals consent
screen.

Taking access back is `revoke-connections`, an operator command against the identity
registry. It removes the owner's recorded connections and nothing else — every entry
resolves a request by looking a fingerprint up there, so tokens this gateway already
issued stop working at the same moment, with no revocation list to keep. PlanState is
untouched and signing in again resolves to the same owner and the same plan. What it does
not do is reach Intervals: the provider's own tokens stay valid until the athlete revokes
them at intervals.icu (see [`../../docs/account-lifecycle.md`](../../docs/account-lifecycle.md)).

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

`tools/list` returns the twenty-three coach operations, named identically to the OpenAPI
`operationId`s the Custom GPT entry uses (`startCoachSession`,
`prepareWorkoutDelivery`, …). Contract tests hold the two surfaces to each other, so a
capability present in one entry and missing from the other fails the build, not the
athlete.

Each carries a `title` and the four behavioural annotations, stated rather than left to
the protocol's defaults. Three are worth reading before wiring up a client:

- **`startCoachSession` is not read-only.** It applies the deterministic reconciliation
  that pairs completed work with what was prescribed, and that is written to the store —
  a plan can come back at a higher version than it went in at.
- **`getCoachState` is the read-only alternative.** It answers "what plan/version is
  current" from the store alone — no provider call, no reconciliation, no write — for
  the client that wants a status check without the side effect `startCoachSession` can
  have.
- **`applyWorkoutDelivery` is both destructive and idempotent.** It replaces a session
  already on the athlete's calendar, or removes a superseded one outright — whichever
  direction `prepareWorkoutDelivery` was called for — and retrying the *identical* set is
  how a partial delivery or withdrawal converges. A client that builds a second set
  instead writes twice.

`prompts/list` returns one prompt, `coach_orchestration`, and a conforming client should
fetch it and put it in front of its model before the first coaching turn. It carries the
sequencing the tool schemas cannot: which call answers a question, where exactly one
explicit confirmation stands before a write, how to read each error code, and what a
delivery result may be said to prove. Without it a model is working from field
descriptions alone, which is how a confirmation gets skipped or an Intervals acceptance
gets reported as a workout on the watch.

That prompt is [`garmin_coach_loop/orchestration.md`](../../garmin_coach_loop/orchestration.md)
served verbatim — the same file the Custom GPT entry is configured with, not a second copy
of it. It is orchestration only: training judgment stays in the Skill's
`references/hybrid-training.md` and is not something this server pushes at connect time.

A refused coaching action — a stale plan version, a missing confirmation, an open
delivery reservation — comes back as a tool *result* with `isError: true` and the
gateway's own error payload, so the model can read the reason and act on it. Only a
message that cannot be read as JSON-RPC at all becomes a protocol error.

The server keeps nothing between requests: no session id, no SSE stream, no server-side
proposal store. A new conversation reconstructs continuity the same way every entry
does — by calling `startCoachSession` again, which returns the current PlanState (and
may also reconcile it); `getCoachState` answers the same "what is current" question
without that side effect, when nothing else about the turn needs fresh evidence.
