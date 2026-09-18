# Demo entry

The public playground behind the Product Hunt launch: one synthetic athlete, one endpoint,
no account. It is a **second Railway service built from this same repository**, not a route
added to the Coach Gateway — separate process, separate secret, separate rate limits, no
volume, no Intervals credentials. A visitor hammering the demo cannot slow, break, or reach
a connected athlete's plan, and a demo deploy cannot take the gateway down with it.

Three names, three jobs, and they do not overlap:

| | |
| --- | --- |
| `mcp.paceandstaystrong.com` | the production Coach Gateway — connected athletes, OAuth, real plans |
| `demo-api.paceandstaystrong.com` | this service |
| `paceandstaystrong.com/demo.html`, `/zh/demo.html` | the page a visitor opens, which calls the middle one |

The demo route is never added to the gateway and the gateway's host never answers demo
traffic. `PUBLIC_HOST` in [`config.py`](config.py) is where that decision is written down,
and `tests/test_demo_deployment.py` fails if the documentation drifts off it.

What it does share is the part worth sharing. The context contract, the evidence
projection, the PlanState schema and the plan-change projector are imported from
`garmin_coach_loop` and used exactly as the gateway uses them. There is no second coaching
engine here, no second prompt-only coach, no second PlanState schema, and no second set of
decision rules.

## The boundary

Read, reason, explore, preview. That is all of it.

| | |
| --- | --- |
| Allowed | `read_demo_evidence` — load evidence groups the first read left out |
| Allowed | `preview_demo_plan_change` — project one change request and show the preview |
| Refused | every provider write, calendar delivery, OAuth step, account mutation, deletion, and any change to a PlanState |

The refusal is a named decision in [`boundary.py`](boundary.py), not an omission. A model
that asks for `applyCoachDecision` is answered with `demo_write_forbidden` and told, in
words it can pass on, that this is a playground — which is a better answer than "no such
tool", because it is the true one. There is no code path in this process that reaches a
store, a provider or an owner, so nothing here can be talked into a write.

A preview is computed against **this conversation's own copy** of the fixture plan and is
never applied. No DecisionEvent is stored, no version is spent, and no confirmation is
offered, because there is nothing to confirm into.

## The athlete

[`fixtures/coach-context.json`](fixtures/coach-context.json) and
[`fixtures/plan-state.json`](fixtures/plan-state.json), held to the product's own
validators by `tests/test_demo_fixture.py`. Anonymous and synthetic: no name, no account,
no provider id, no contact detail. What it contains is not restated here --
[`fixtures/manifest.json`](fixtures/manifest.json) says it once, and
`tests/test_demo_fixture.py` asserts it, so a page that summarised it would be a third
copy to keep in step.

The half worth saying out loud is that it is **deliberately incomplete**. A fixture where
every field is populated would teach the demo that evidence is always there, which is the
one habit this product spends the most code refusing (AGENTS.md 3), so several of its
records are missing on purpose. The manifest lists which. Each is named in the context's
own `unknowns`, and a reply that reads one as a zero is wrong, not terse.

## The endpoint

```
POST /demo/v1/respond
Content-Type: application/json

{"session_id": "opaque-random-id", "message": "..."}
```

```json
{"reply": "..."}
```

In production that is `https://demo-api.paceandstaystrong.com/demo/v1/respond`.

No streaming in this version. `GET /healthz` reports whether this deployment can answer,
and is what the platform health check reads.

Errors are machine-readable and carry a stable code:

| Status | `error.code` | |
| --- | --- | --- |
| 400 | `invalid_request`, `session_id_invalid`, `message_too_long` | the request cannot be read as a turn |
| 403 | `origin_not_allowed` | the browser origin is not on the allowlist |
| 404 | `not_found` | there is one route and this is not it |
| 409 | `turn_limit_reached`, `session_busy` | this conversation has spent its turns, or is already answering one |
| 413 | `payload_too_large` | the body is past the byte limit |
| 415 | `unsupported_media_type` | JSON only — there is no upload path |
| 429 | `rate_limited`, `model_rate_limited` | with `Retry-After` |
| 502 / 503 / 504 | `model_unavailable`, `model_output_truncated`, `demo_model_unconfigured`, `model_timeout` | the turn could not be answered |

A turn that failed for any of the last row does **not** count against the conversation's
turn budget: the visitor got no answer, and losing part of a short demo to a provider
timeout is not their doing.

## Sessions

A `session_id` is an opaque random token the page generates. It is not a login and grants
nothing; two ids reach two conversations about the same synthetic athlete. What it must
never do is let one visitor's exploration appear in another's, so a session holds its own
deep copy of the plan and its own history, and nothing is shared between them.

Bounded in three directions, because the endpoint is anonymous: sessions expire (15 minutes
by default), a conversation is capped at 12 turns, and the store holds 500 sessions before
the least recently touched is dropped. Memory only — no volume, no database, and a deploy
starts every conversation over, which is the correct lifetime for a playground.

## The model

The OpenAI Responses API, on `gpt-5.6-luna`, pinned as a constant in [`model.py`](model.py).
There is no environment override and no fallback to another model when a call fails: a demo
that quietly answers from something else is a demo whose answers mean nothing, so a failure
is reported as a failure.

Every provider-specific shape is in that one file — the request body, which response items
carry into the next round, how a tool result is spelled, and how a failure maps onto this
service's codes. `service.py` drives a conversation without naming a provider field, and
`garmin_coach_loop` does not know the file exists.

Requests carry `reasoning: {"effort": "medium"}` and `max_output_tokens: 8000`, which bounds
reasoning and visible output together rather than just the answer. `max` was tried against
the deployed service and timed out: it opens with three evidence reads, and the rounds that
carry those results back exceed the 60-second provider timeout.

`store` is false on every call, so nothing is retained at the provider. That makes the
conversation stateless, which is exactly why the next round has to carry the previous one:
each response's output items are echoed back verbatim and in order, including the
`reasoning` item and its `encrypted_content`. Drop that item and every round after a tool
call starts over from the words alone.

The credential is read from the server's `OPENAI_API_KEY` and goes nowhere else — not into
a log, not into an error body, not into any response. Without it the service **refuses to
start**, and a request to a deployment that somehow has none is answered `503
demo_model_unconfigured` rather than served from a substitute.

## What it logs

Method, path, status, error code, a session **fingerprint** (a hash prefix, never the id
itself), the turn number, which acts were asked for, how long it took, and how long the
reply was. Never the visitor's message, never the athlete payload, never a header, never a
credential.

## Configuration

`OPENAI_API_KEY` is the only required variable. Everything else has a default that is safe
for an anonymous public endpoint, and none of them can be changed by a request.

| | default | |
| --- | --- | --- |
| `PORT` | — | Railway injects it and routes to it. Read **first**; a service that binds its own number instead fails the health check while running perfectly. |
| `COACH_DEMO_PORT` | `8433` | only consulted when `PORT` is unset |
| `COACH_DEMO_ALLOWED_ORIGINS` | — | comma-separated preview origins, **added to** `https://paceandstaystrong.com`, which is compiled in. https only, apart from a loopback origin. No wildcard, in any position. |
| `COACH_DEMO_SESSION_TTL_SECONDS` | `900` | |
| `COACH_DEMO_MAX_SESSIONS` | `500` | |
| `COACH_DEMO_MAX_TURNS` | `12` | per session |
| `COACH_DEMO_MAX_MESSAGE_CHARS` | `1200` | |
| `COACH_DEMO_MAX_BODY_BYTES` | `8192` | |
| `COACH_DEMO_RATE_PER_MINUTE` | `12` | per client |
| `COACH_DEMO_GLOBAL_RATE_PER_MINUTE` | `120` | the ceiling a rotated address cannot pass |
| `COACH_DEMO_CLIENT_IP_SOURCE` | `forwarded` | `forwarded` reads the left-most `X-Forwarded-For`, which is only trustworthy behind a proxy that overwrites it. Set `peer` when nothing fronts the service. |
| `COACH_DEMO_MODEL_TIMEOUT_SECONDS` | `60` | |

## Deploying it

The full runbook, including the steps only the owner can click, is
[`docs/ops/deploy-demo-service.md`](../../docs/ops/deploy-demo-service.md). In short: a
second Railway service in the same project, pointed at this repository, with
`railway.demo.toml` as its config-as-code path, `OPENAI_API_KEY` as its one required
variable, no volume and no Intervals OAuth secrets.

The production MCP service keeps its own `railway.toml`, `Dockerfile` and volume, and none
of them are touched by any of this.

Locally:

```bash
OPENAI_API_KEY=... python3 -m entrypoints.demo
```

## Proving it works, once there is a credential

```bash
OPENAI_API_KEY=... python3 -m entrypoints.demo.acceptance
```

One command. It runs the three committed turns below in three separate conversations
against the real model, and reports for each: whether it answered, the latency, how many
model rounds it used, which demo acts ran, and whether the reply claimed a write or named a
score this product does not have. Transcripts are written out beside a JSON report.

`--base-url https://demo-api.paceandstaystrong.com` runs the same three turns over HTTP
against the deployed service, which also exercises the platform port, CORS and the deploy
itself.

It grades nothing. Two of its checks are real refusals; the rest is a prompt to read the
transcript. No test in this repository judges a coaching answer, and this command does not
become the first one that does.

## The turns it is built for

```
I only have three 45-minute sessions next week. I want a faster 10K without losing strength. What are my realistic options?
```

Two or three materially different allocations — running-biased, balanced,
strength-preserving — and for each one what it **preserves**, what it **sacrifices**, the
**evidence** in this athlete's own record that makes it valid, and what stays **uncertain**.
No running score, strength score, recovery score or trade-off curve: those quantities are
not defined, and inventing one would be manufacturing precision the evidence does not
carry (issue #472).

```
What changed across the comparable quality runs, and what is still unproven?
```

The comparable execution trend and the cycle's declared outcome measurement are two
different claims and are answered separately. The three threshold sessions are the same
prescription repeated, so their paces compare; the declared 10K measurement has not been
run, so nothing yet reads the adaptation against its reference. And two of those three
sessions carry no per-segment heart rate, so what the repetitions cost stays unknown rather
than assumed unchanged.

```
Thursday is no longer available. What would you preserve and what would you give up?
```

Revises this conversation's own exploration. No other session sees it, and the fixture
underneath every session is unchanged.

All three are committed in [`fixtures/acceptance-prompts.json`](fixtures/acceptance-prompts.json)
so this page, the tests and whoever runs the demo read one copy.

## Tests

```bash
python3 -m unittest tests.test_demo_fixture tests.test_demo_boundary tests.test_demo_service
```

They cover the missing credential, the three acceptance turns, session isolation, TTL
expiry, the turn and input limits, both rate limits, the forbidden write path, the fixture
carrying no real identity, and the log carrying no secret. Nothing in them reaches a
network. The production MCP suite is untouched and still runs as it did.
