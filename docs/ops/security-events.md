# Reading the OAuth and MCP security events

The gateway writes a small structured event at each boundary crossing of its authorization
chain: a client registering, an authorization starting, the provider callback landing, a
token being issued, an MCP request authenticating. Each is written once, for the success
and for the refusal, and carries only what an investigation needs.

This file is how an operator finds them. `deploy-gateway.md` covers standing the service
up; `verify-production-status.md` covers whether it is healthy. This one covers the
question that comes after an incident report: *what actually happened at the boundary.*

## The shape of one event

One line per event, on the process's normal log stream, under the logger name
`garmin_coach_loop.security`:

```
2026-08-17 09:14:02,113 INFO garmin_coach_loop.security security {"client": "31657b3e62cdad2b", "event": "client_registration", "origin": "https://claude.ai", "reason": null, "result": "accepted"}
```

Five fields, and never a sixth:

| field | what it is |
| --- | --- |
| `event` | `client_registration`, `authorization`, `provider_callback`, `token_issuance`, `mcp_authentication` |
| `result` | `accepted` or `refused` |
| `reason` | why it was refused, from a closed vocabulary (`untrusted_redirect_origin`, `unknown_client`, `pkce_verification_failed`, …); `null` when accepted |
| `origin` | the callback's `scheme://host[:port]` and nothing else — never the path, query, or fragment; `null` where no callback is involved |
| `client` | an opaque, deterministic handle for the `client_id` — the same value across every event of one flow, and across restarts |

The timestamp is the logging system's, not the application's.

## Finding them

With the Railway CLI linked to the production environment (see
`roll-with-railway-cli.md` for the one-time `railway link`):

```bash
railway logs --lines 500 --filter "garmin_coach_loop.security"
```

Add `--json` when the output is going into a script rather than onto a screen. The Railway
dashboard's log view takes the same filter string.

### One flow, end to end

Every event of a single client flow carries the same `client` handle, so a normal
connection reads back as a chain:

```bash
railway logs --lines 1000 --filter "31657b3e62cdad2b"
```

```
client_registration accepted → authorization accepted → provider_callback accepted
  → token_issuance accepted → mcp_authentication accepted
```

A gap in that chain is the finding. An `authorization accepted` with no
`provider_callback` is an athlete who never finished consenting; a `token_issuance
refused` with `pkce_verification_failed` is a code presented by something that did not
start the flow.

### Refusals only

```bash
railway logs --lines 1000 --filter "garmin_coach_loop.security AND refused"
```

A blocked registration is the one event with no `client` handle — nothing was issued, so
there is no client to correlate. It is identified by its `origin` instead:

```
security {"client": null, "event": "client_registration", "origin": "https://evil.example", "reason": "untrusted_redirect_origin", "result": "refused"}
```

That single line is the answer to "did somebody try to register a callback of their own",
which is the question this stream exists for.

### Refusals that are not incidents

Two of them are ordinary traffic, and reading them as attacks will waste an afternoon:

- `mcp_authentication refused / missing_bearer` — how every MCP connection *starts*. The
  client calls `/mcp` with no token precisely to receive the `401` and the
  `WWW-Authenticate` challenge that tells it where to authorize. Expect one per new
  connection.
- `mcp_authentication refused / unrecognized_token` — usually a client still holding a
  token from a deployment whose key has since been rotated, or from before an athlete
  revoked their Intervals access. It re-authorizes on its own.

`client_registration refused / untrusted_redirect_origin`, on the other hand, has no
routine cause. Nothing legitimate registers a callback on an origin this deployment does
not trust.

## What is deliberately not in them

No authorization code, access token, provider token, PKCE verifier or challenge, OAuth
`state`, full callback URL, request or response body, owner id, provider athlete id,
PlanState, or health and training content. A test holds this property against a complete
live flow, so it fails the build rather than the athlete if a field is added carelessly.

Source IP and edge metadata are Railway's own HTTP logs (`railway logs --http`), not
duplicated here.

The `client` handle is keyed to the deployment's `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY`.
Rotating that key ends correlation across the rotation — old events stay readable, but a
client's handle before and after will not match.

## Retention

These events live exactly as long as Railway keeps the service's logs, and no longer.
There is no second log store, no export, and no archive: this repository does not promise
a retention period it does not implement.

**Check the actual window in the Railway dashboard for the plan this project is on**
before relying on it — log retention is a plan property that can change under the service
without anything here changing. If an investigation needs a longer window than the plan
gives, that is the evidence for choosing a longer-lived sink, which is a decision to make
then rather than a framework to add now.

## What this stream is not

Not alerting, not rate limiting, not a security dashboard, and not an automatic blocker.
Nothing reads these events back at runtime. They exist so that a question asked later has
an answer; adding machinery on top of them is a separate decision, to be made against real
traffic rather than in advance of it.
