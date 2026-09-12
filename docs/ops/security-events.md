# Reading the OAuth and MCP security events

The gateway writes a small structured event at each boundary crossing of its authorization
chain: a client registering, an
authorization starting, the provider callback landing, a token being issued, an MCP
request authenticating. Each is written once, for the success and for the refusal. An
authenticated MCP request refused on protocol revision writes one additional refusal
event. Each carries only what an investigation needs.

This file is how an operator finds them. `deploy-gateway.md` covers standing the service
up; `verify-production-status.md` covers whether it is healthy. This one covers the
question that comes after an incident report: *what actually happened at the boundary.*

## The shape of one event

One line per event, on the process's normal log stream, under the logger name
`garmin_coach_loop.security`:

```
2026-09-05 09:14:02,113 INFO garmin_coach_loop.security security {"client": "31657b3e62cdad2b", "event": "client_registration", "origin": "https://claude.ai", "protocol_version": null, "reason": null, "result": "accepted"}
```

Six fixed fields (older deployments wrote the first five):

| field | what it is |
| --- | --- |
| `event` | `client_registration`, `authorization`, `provider_callback`, `token_issuance`, `mcp_authentication`, `mcp_protocol` |
| `result` | `accepted` or `refused` |
| `reason` | why it was refused, from a closed vocabulary (`blocked_redirect_origin`, `unknown_client`, `pkce_verification_failed`, …); `null` otherwise |
| `origin` | the callback's `scheme://host[:port]` and nothing else — never the path, query, or fragment; `null` where no callback is involved |
| `client` | an opaque, deterministic handle for the `client_id` — the same value across authorization events of one flow, and across restarts; `null` where no handle is supplied, including the protocol diagnostic |
| `protocol_version` | on a protocol refusal, the exact canonical ASCII `YYYY-MM-DD` date if it is a real calendar date, otherwise `invalid`; `duplicate` for repeated headers or a comma-joined value, without retaining any member; `null` on other events |

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

Events carrying a `client` handle share it across that client's authorization flow,
so a normal connection reads back as a chain:

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

Unknown and verified origins emit the same OAuth event chain.

### Refusals only

```bash
railway logs --lines 1000 --filter "garmin_coach_loop.security AND refused"
```

A blocked registration has no `client` handle — nothing was issued, so there is no
client to correlate. It is identified by its `origin` instead:

```
security {"client": null, "event": "client_registration", "origin": "https://evil.example", "protocol_version": null, "reason": "blocked_redirect_origin", "result": "refused"}
```

### Refusals that are not incidents

Two of them are ordinary traffic, and reading them as attacks will waste an afternoon:

- `mcp_authentication refused / missing_bearer` — how every MCP connection *starts*. The
  client calls `/mcp` with no token precisely to receive the `401` and the
  `WWW-Authenticate` challenge that tells it where to authorize. Expect one per new
  connection.
- `mcp_authentication refused / unrecognized_token` — usually a client still holding a
  token from a deployment whose key has since been rotated, or from before an athlete
  revoked their Intervals access. It re-authorizes on its own.

One more has a routine cause worth knowing before it is investigated:

- `token_issuance refused / code_already_redeemed` — an authorization code is spent on
  its first successful redemption, so a client retrying a token request after a network
  timeout meets this on the retry. One of these beside a `token_issuance accepted` with
  the same `client` handle is that retry. A run of them with no acceptance is not.

`client_registration refused / blocked_redirect_origin`, on the other hand, has no routine
cause: an origin is on that list because an operator put it there against real evidence,
so every line is that client still trying. Neither does `client_registration refused /
registration_too_large`: the bounds are far above what any real connector registers, so
something is sending a body rather than a registration.

### A client refused on protocol revision

Filter for `mcp_protocol` and `unsupported_protocol_version`. `protocol_version` names
the refused date, or says `invalid` or `duplicate`; the response still carries the same
400 and supported-revision list. Missing and supported headers emit no protocol event,
and an unauthenticated caller still receives its 401 before any revision diagnostic.

Duplicate classification describes only a request already refused by the existing
header check. It does not change which duplicate headers are accepted. This observation
is the first step of issue #369: after a future deployment, read the real refused dates
before deciding what compatibility work is needed. The diagnostic alone does not resolve
the client's failed call.

### The access line

Beside the security stream, every request writes exactly one line under the logger
`garmin_coach_loop.gateway`, whatever the answer was:

```
POST /mcp -> 200 access=authenticated owner=31657b3e62cdad2b tool=applyOwnerDeletion outcome=blocked:account_deletion_moved
```

| field | what it is |
| --- | --- |
| `method path -> status` | the HTTP request and the status this deployment answered with |
| `access=` | `authenticated` or `anonymous` — whether an owner was resolved |
| `owner=` | which account, on an authenticated request only: the first 16 characters of the same keyed handle the athlete's own export returns as `owner_reference`. Never the owner id, the bearer, or a provider athlete id |
| `error=` | on a 4xx or 5xx, the machine-readable code already in the response body |
| `tool=` | the MCP tool the call named, whenever one was named |
| `outcome=` | how that tool call ended: its result `status` (`passed`, `partial`, `no_plan_state`), or `blocked:` and the refusal code |
| `intervals_calls=`, `intervals_remaining=`, `intervals_limit=` | what the request spent against the shared Intervals pool, and only when it spent something |

The example is a real one: `applyOwnerDeletion` is served by nothing since 1.4.5, and a
client still holding the old catalogue is answered with a refusal naming the support page
rather than with an unknown method. This line is the only record that such a client is
still calling it, and the filter below is how to count them.

`tool=` and `outcome=` are what answer *did a tool call reach this gateway, and was it
refused here*. A tool call a client blocks before dispatch leaves no line at all; a call
this gateway refused leaves an HTTP `200` — a refusal travels as a JSON-RPC `isError`
result, not as an HTTP status — so the status alone cannot tell the two apart, and
`outcome=` is the field that does.

`owner=` is what makes a count of refusals mean something. Without it a day's log says
six calls were refused and cannot say whether that was one athlete refused six times or
six athletes refused once — and those are opposite findings. The registry counters used
to answer it and were removed in 1.4.3, because they wrote to the registry and a platform
review counts a write as a state change, which cost every read and preview an approval
the athlete did not need to give. A log line is not a write, so this marker costs no
approval, no annotation and no tool-catalogue change.

An athlete quoting their `owner_reference` in a support mail and an operator filtering
these lines land on one account:

```bash
railway logs --lines 1000 --filter "owner=31657b3e62cdad2b"
```

The handle is keyed to this deployment's `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY`, so rotating
that key ends correlation across the rotation exactly as it does for the `client` handle,
and a handle read here cannot be turned back into an account without the registry.

```bash
railway logs --lines 1000 --filter "outcome=blocked:account_deletion_moved"
```

Both fields carry the gateway's own closed vocabulary and nothing else: never the detail
sentence beside a refusal, never an owner id, a token, or any athlete value.

## What is deliberately not in them

No authorization code, access token, provider token, PKCE verifier or challenge, OAuth
`state`, full callback URL, request or
response body, owner id, provider athlete id, PlanState, or health and training content. A test holds this property against a complete
live flow, so it fails the build rather than the athlete if a field is added carelessly.

The protocol field cannot retain an arbitrary header: tokens, URLs, whitespace,
controls, oversized values, Unicode lookalikes and impossible dates reduce to a fixed
classification. This is enforced by the security logger itself, not by its callers.

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
without anything here changing.

### The window was too short, once, and this is what was done about it

The condition this section used to defer on has happened. Asked in September 2026 where
new athletes were dropping out, the answer needed days that the service's log window —
about half a day in practice — no longer held, and the registry counters that had held
them permanently were removed in 1.4.3. Nothing was lost that a longer window would not
have kept.

So the lines are pulled down and appended to a file on the operator's own machine:

```bash
python3 scripts/archive_access_log.py --out ~/.local/share/garmin-coach-loop-ops/access.log
```

It runs `railway logs`, keeps only lines it has not already stored, and appends. Running
it twice in a row adds nothing the second time; missing a run costs only the lines that
fell out of the window in between, so it wants to run more often than the window is long.
It changes nothing in production, reads nothing but the log stream, and needs no
deployment.

The durable version of the same idea is a Railway **log drain**, configured in the
project dashboard, which pushes every line to a sink as it is written rather than waiting
to be asked. The CLI cannot configure one. Prefer it when the pulled file starts having
gaps; until then, a pull that runs often enough is the same evidence without a second
service to run.

Neither is a product feature. Both are an operator keeping their own copy of a log the
platform is about to discard, and both hold the same fields — including `owner=`, which
is a keyed handle and not an identity.

## What this stream is not

Not alerting, not rate limiting, not a security dashboard, and not an automatic blocker.
Nothing reads these events back at runtime. They exist so that a question asked later has
an answer; adding machinery on top of them is a separate decision, to be made against real
traffic rather than in advance of it.
