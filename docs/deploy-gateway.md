# Deploying the Coach Gateway

A runbook for putting `garmin_coach_loop.gateway` on a persistent host, so every entry --
the hosted MCP endpoint, the canonical Skill, any MCP client -- reaches it at a
stable HTTPS domain instead of an ephemeral tunnel. Railway is the current production target and ships
`railway.toml`; Fly.io remains the detailed manual example and ships `fly.toml`. The last
section maps the same invariants onto other single-instance hosts.

This is operational documentation for standing the service up. It assumes you have
already read [`entrypoints/mcp/README.md`](../entrypoints/mcp/README.md) for what the
gateway is and how a client talks to it -- this file does not repeat that.
If the service is already deployed and the question is just "is it healthy right now,"
see [`ops/verify-production-status.md`](ops/verify-production-status.md) instead of
redoing this file's reasoning from scratch. If the question is what happened at the
OAuth/MCP trust boundary — who registered, what was refused, how far one authorization
got — see [`ops/security-events.md`](ops/security-events.md).

## Deployment shape

One long-running Python process, no application server or process manager in front of
it: `garmin_coach_loop.gateway.CoachGatewayServer` is itself an HTTP server
(`http.server.ThreadingHTTPServer`) that binds `0.0.0.0` on a platform-provided port. TLS
terminates at the platform, never in this process. One persistent volume holds the
identity registry (`identity.db`) and every owner's PlanState store; nothing else in this
deployment is stateful. **Exactly one replica** -- see the "single replica" note in
`fly.toml` and the "Crash-safe locking" section below for why that is a hard constraint
and not a cost-saving default.

## Prerequisites

- A Fly.io account and the `flyctl` CLI, authenticated (`fly auth login`).
- An Intervals.icu OAuth app already registered. Its authorize requests ask for exactly
  `ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:WRITE` (the registration page has
  no scope field; scopes are chosen in each authorize query).
  Every entry authorizes through the same application, and the list is not advisory:
  asking for one scope Intervals does not grant costs the whole authorization rather than
  the one capability (issue #97). Deployment does not change this step.
- This repository, at the commit you intend to deploy.

## First deploy

1. Create the app and pick a region. `fly.toml` leaves `app` and `primary_region`
   commented out on purpose -- they are specific to your Fly organization, not something
   this repository should hardcode:

   ```bash
   fly apps create YOUR-APP-NAME
   ```

   Then set `app = "YOUR-APP-NAME"` and `primary_region = "..."` in `fly.toml` (see
   `fly platform regions` for the list), or pass `--app YOUR-APP-NAME` on every `fly`
   command below instead of editing the file.

2. Create the volume that `[mounts]` in `fly.toml` names. Size it for the number of
   athletes you actually expect on the owner-only dogfood path -- each store is a handful
   of small JSON files per plan revision, not media:

   ```bash
   fly volumes create garmin_coach_loop_state --region YOUR-REGION --size 1
   ```

3. Set secrets. None of these belong in `fly.toml`, in this repository, or in any shell
   history you keep -- `fly secrets set` stores them outside both:

   ```bash
   fly secrets set \
     GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)" \
     GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID="..." \
     GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET="..."
   ```

   `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` must be at least 32 characters (`load_config` in
   `gateway.py` refuses a shorter one at startup); the `openssl` command above produces a
   44-character value, comfortably over that floor. The client id/secret are the same
   pair from Intervals `/settings` -> Manage App for the application registered in the
   Prerequisites above.

   `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` is **not** a secret -- it is already set as a
   plain value in `fly.toml`'s `[env]` (`/data`, the `[mounts]` destination) and does not
   need to be set again here.

   `GARMIN_COACH_LOOP_MCP_ALLOWED_ORIGINS` is optional and also not a secret: a
   comma-separated list of extra browser origins `/mcp` answers to, on top of the
   deployment's own origin and `https://claude.ai`. A server-side MCP client sends no
   `Origin` at all and needs nothing here. An entry that is not a bare
   `scheme://host[:port]` refuses startup rather than being skipped, so a typo is a failed
   deploy instead of a `403` for the client it was added for.

   `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` is optional, not a secret, and parsed the
   same way — but it answers a different question: which **remote callback origins this
   deployment has verified**. The connector hosts of the platforms this product is
   distributed through (`https://claude.ai`, `https://claude.com`, `https://chatgpt.com`)
   are verified without configuration, and loopback callbacks are unless explicitly blocked, so a deployment
   serving only those sets nothing. Since 1.4.1 this list decides whether an athlete is
   *warned* about a client, not whether that client may connect at all — see "Admitting a
   new hosted client" below. Keep it separate from the `/mcp` browser list even where
   their hosts coincide: one decides which browser page may call `/mcp`, the other decides
   who connects without being asked about.

   `GARMIN_COACH_LOOP_BLOCKED_CLIENT_ORIGINS` is optional, not a secret, parsed the same
   way, and empty on every deployment that has had no reason to fill it. It is the
   emergency lever: an origin named here is refused at registration and at every
   authorization, including for the client ids already issued on it. Reserve it for
   concrete abuse or compromise evidence. A malformed entry refuses startup like every
   other origin list — a block that was silently dropped would be a revocation an operator
   believes they performed.

   `GARMIN_COACH_LOOP_OPENAI_APPS_CHALLENGE` is optional, not a secret, and set only
   while a plugin directory is verifying that whoever submitted a listing controls this
   domain: the token it generates is served verbatim at
   `/.well-known/openai-apps-challenge`, and the path answers `404` like any unknown one
   while the variable is unset. See
   [`distribution/openai-plugin.md`](distribution/openai-plugin.md).

   Release identity is optional at this step and covered in "Post-deploy verification"
   below: six more variables, all six or none (`load_config` refuses a partial set).

4. Deploy:

   ```bash
   fly deploy
   ```

5. Confirm exactly one machine is running, since nothing in `fly.toml` hard-caps this by
   itself (see "Crash-safe locking" below):

   ```bash
   fly status
   fly scale count 1   # only if status shows more than one
   ```

## Post-deploy verification

```bash
curl -s https://YOUR-APP-NAME.fly.dev/healthz | python3 -m json.tool
```

`/healthz` is liveness-only by product design (`CoachGateway.health()` in `gateway.py`)
and always answers HTTP 200 -- that status code alone only proves the process is up, not
that it is the release meant to serve real traffic. Read the response body's own `status`
field:

Railway points its platform health check at `/readyz`, which returns HTTP 503 for the
same blocked body. It also compares Railway's injected `RAILWAY_GIT_COMMIT_SHA` with the
release identity's `git_commit`, so a green but unpromoted `main` build cannot replace
the current production release while claiming an older release bundle.

- `"status": "blocked"`, `"error": "missing_or_mismatched_runtime_release_deployment_or_source_identity"` --
  expected immediately after a first deploy. No `GARMIN_COACH_LOOP_RELEASE_*` variable is
  set yet, so nothing has told this gateway what "the current release" means. The process
  is healthy; it is just not yet certified as the release this domain is meant to serve.
- `"status": "ok"` -- the seven `GARMIN_COACH_LOOP_RELEASE_*` secrets are set and match
  this exact deployed commit, orchestration prompt, MCP tool catalogue, canonical Agent
  Skill, package artifact, and domain. Two of those are not taken on trust: the artifact
  digest and the tool catalogue digest are recomputed from the running process and
  compared, so a variable naming a catalogue this build does not serve reads `blocked`.

To move from the first to the second, build this commit's bundle and hold the live
gateway to it:

```bash
python3 scripts/release_bundle.py build \
  --gateway-domain https://YOUR-APP-NAME.fly.dev \
  --output /secure/release/bundle.json
python3 scripts/release_bundle.py verify \
  --bundle /secure/release/bundle.json \
  --receipt /secure/release/receipt.json \
  --expected-deployment-identity /secure/release/deployment-identity.json
```

Then set the seven `GARMIN_COACH_LOOP_RELEASE_*` values the bundle names
(`fly secrets set ...`) and redeploy. `/healthz` flips to `"status": "ok"` once they match;
`--gateway-domain` must be this deployment's real domain, not the
`YOUR-GATEWAY-DOMAIN` placeholder that fails `normalise_gateway_domain` in
`release_identity.py`.

## Admitting a new hosted client

**Since 1.4.1, admitting one is not an operator task.** A hosted MCP client on any
structurally valid `https` callback registers and connects on its own; before the flow
reaches Intervals, this gateway shows the athlete a page of its own naming the exact
origin the authorization would be sent to, and nothing proceeds until they choose
Continue. Cancel reaches no provider and issues no token. So a new platform appearing is
a page one person reads, not a log read, a variable edit and a redeploy.

What an operator still decides is narrower and worth doing deliberately:

- **Verify an origin** — add it to `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` — once you
  have validated that the origin is under the control of the platform you mean to
  support, and that the platform's whole OAuth flow actually works against this gateway.
  Athletes on a verified origin are not shown the page. This is a statement that somebody
  checked an identity fact, never a promise the service is well-behaved.
- **Block an origin** — add it to `GARMIN_COACH_LOOP_BLOCKED_CLIENT_ORIGINS` — when there
  is concrete abuse or compromise evidence. See "Revoking an origin" below.

Neither is a code change. Neither is required for a legitimate client to work.

### What the athlete sees, and what it does not claim

> An app is asking to connect to your Long Run Hybrid Coach. Its authorization would be
> sent to: `https://<the-origin>`
>
> This app has not been verified. If you continue, it can read your training data and
> write workouts to your Intervals.icu calendar for as long as it stays connected.

Nothing the client wrote appears on that page — not its `client_name`, not its callback
path, not a description. A client that could put its own text next to the address it is
being warned about would be arguing both sides. The origin is normalized before it is
shown, and a callback whose authority cannot be reduced to a plain ASCII origin is refused
rather than displayed, which is what keeps a homograph host off the page.

**The client cannot answer its own page.** Drawing it sets a random value as a cookie on
this gateway's own origin and seals a keyed digest of that value into the request; the
digest travels through to Intervals' callback and is checked again there — on the leg only
the athlete's own browser walks. A client that fetched the page and posted its own
Continue holds that cookie in its own process, so when the athlete returns from Intervals
the binding does not match, the provider code is never exchanged, and no authorization
code reaches the client. This is why the page is a security boundary and not a
notification.

**What it does not stop, stated plainly:** an athlete who is sent this page directly and
presses Continue on a hostile origin has authorized that origin. The page makes the
decision informed and puts it in front of the one person who knows whether they just
started a connection from that app; it does not make it for them. That is the trade this
release accepts, because whether an unknown service is benevolent is not something a
deployment can decide in advance — and deciding it in advance was what made every
legitimate new client an operator ticket.

### Reading who is connecting

The security log distinguishes the outcomes without recording who the athlete is:

```bash
railway logs --lines 200 --filter "client_consent"
```

```
security {"client": "<handle>", "event": "client_consent", "origin": "https://<the-origin>", "reason": "unverified_client_origin", "result": "prompted"}
security {"client": "<handle>", "event": "client_consent", "origin": "https://<the-origin>", "reason": "unverified_client_origin", "result": "accepted"}
```

`prompted` is a page that was shown, `accepted` a Continue, `refused` with
`consent_declined` a Cancel, and `refused` with `consent_binding_missing` a decision or a
provider return that did not come from the browser that was warned. This is also how you
find the origin of a platform worth verifying: it is the one athletes keep accepting.

The entry-origin column an operator reads for usage (`/coach-usage`) records `local`, a
verified origin, or the single fixed word `unverified` — never a host an anonymous
registration chose. Which unverified origin it was is in the security log above.

### Revoking an origin

**Blocking is the revocation; un-verifying is not** (issue #121, revisited in #403). Both
lists are consulted again at `/oauth/authorize`, so both reach the client ids already
issued — but they now do different things:

- Taking an origin **off** `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` no longer refuses
  anything. It demotes that origin to the consent page: the connector keeps working and
  every athlete on it is warned first.
- Adding an origin to `GARMIN_COACH_LOOP_BLOCKED_CLIENT_ORIGINS` refuses it outright, at
  registration and at authorization, for existing clients as well as new ones. This is
  also the only configuration that stops a built-in verified host, which the trusted
  variable cannot subtract from.

Both are exact-origin comparisons: `https://evil.example` does not cover
`https://evil.example:8443` or `https://evil.example.co`.

Without either, the only lever remains rotating `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY`, which
invalidates every registration, token and code for everyone. It also changes the account
handle inside every stored approved calendar effect, which is derived from the same key —
but that is not what a rotation costs, because a retry of an already-confirmed delivery
does not re-derive the handle (`_replayed_calendar_account` in `gateway.py`). What it does
cost is the signed proposal that names those effects: it was signed under the old key, so
after a rotation the retry-by-proposal path does not authenticate at all and the athlete
starts a fresh coaching turn against the plan as it now stands. Persisted delivery state
itself is not made unreplayable, and nothing has to be cleared before rotating.

It is also not selective. Removing an origin takes down every connector on it, working
ones included, and athletes on that platform have to reconnect through a platform you
trust. Loopback clients and the built-in hosts are unaffected either way — and the
built-in hosts cannot be removed by configuration at all, since the variable adds origins
rather than replacing them. Un-trusting one of those is a code change; the
everyone-at-once instrument remains the key rotation.

## Changing domains

If the gateway's public domain ever changes (a custom domain replacing `fly.dev`,
migrating to a different host), two things stop matching it and must be updated
together, not left for the next confusing failure to surface:

1. **The release identity** -- `gateway_domain` is part of what `release_identity.py`
   binds into `release_id` (see `make_release_id`). A changed domain makes every existing
   `GARMIN_COACH_LOOP_RELEASE_*` secret stale; `/healthz` will report `"blocked"` again
   until you rebuild and reset them via the release gate above with the new
   `--gateway-domain`.
2. **The Intervals OAuth app's redirect URIs** -- every client authorizes through this
   gateway's own `/oauth/callback`, so `https://<domain>/oauth/callback` must be
   registered at intervals.icu (Settings -> Developer) and re-registered when the domain
   changes.

## Crash-safe locking

Each owner's store is guarded by a POSIX exclusive-create lock file
(`garmin_coach_loop/store.py`'s `_exclusive_lock`, a `.lock` marker inside that owner's
directory). A process that exits through `run_gateway`'s normal path always removes its
own lock; one that does not -- `SIGKILL`, an out-of-memory kill, a host crash -- leaves it
behind. Left in place, a stale `.lock` refuses every read and write for that one owner,
`restore-store` included, until someone removes it.

Recovery is automatic and requires no volume access: a hosted `run_preflight`
(`gateway.py`) first waits 35 seconds, longer than the committed 30-second platform
drain/kill window, then scans
`$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT/owners/*/.lock` and removes every marker still
present. It logs only the count reclaimed -- never an owner id or a path. The wait is
necessary because a rolling deploy can briefly overlap the replacement process with its
draining predecessor even when the service is configured for one replica; a predecessor
that finishes cleanly removes its own lock during that interval. Local development has no
deployment identity and skips the wait.

This remains safe only because deployment is single-replica after that bounded rollout
overlap. Running more than one permanent replica against the same volume would make the
reasoning false and is exactly what `fly.toml`'s
`auto_stop_machines = false` / `auto_start_machines = false` and a checked
`fly scale count 1` exist to prevent.

**Manual check, if you want to confirm this yourself rather than trust the log line:**
after any ungraceful stop, redeploy or restart, then read the startup log:

```bash
fly logs | grep "reclaimed"
```

A count greater than zero means exactly that many owners had a leftover lock from before
this start, and all of them are now clear. This is the only volume-level detail this log
line ever reveals; it does not name which owners.

`delivery-attempt.json` is a different file with a different purpose -- a deliberately
durable journal of a delivery that may have partially reached Intervals, not a process
marker -- and `run_preflight` never touches it under any circumstance. Recovering it is
the athlete-facing `clear-delivery-attempt --confirm` path (see the working rules in this
repository's `CLAUDE.md`), which requires confirming against the live Intervals calendar
first. That is a judgment call about what a provider actually holds; a process restart
cannot make it safely, and does not try to.

## Graceful shutdown

`run_gateway` (`gateway.py`) registers one handler for both `SIGTERM` and `SIGINT`: stop
accepting new connections, let every in-flight request finish and send its response, then
close. A hosting platform's redeploy is a routine `SIGTERM`, not an operator error, and
this is what keeps a routine redeploy from ever being the reason a `.lock` is left behind
in the first place -- the startup reap above is the backstop for the cases (a host crash,
an out-of-memory kill) that skip this entirely.

What a redeploy carries across depends on which confirmation it is. Every proposal this
gateway issues names the release that issued it (`release_id`), and on the two writes no
later call can undo -- an erasure, and an account's first plan -- the release it is handed
back to has to be the same one. An athlete who previewed on the outgoing release and
confirms one of those on the incoming one is refused with `proposal_mismatch` and told to
prepare it again. That is the intended answer, not an incident, and re-preparing writes
nothing.

A plan change is not refused on identity. It is re-derived: the incoming release projects
the candidate plan, the decision event and the preview again, and commits only when all
three are the ones the athlete approved (issue #358). Two builds that agree on every one
of them have nothing left to disagree about, and refusing them anyway meant a rollout
landing mid-conversation cost an athlete the whole authoring turn. A build that genuinely
renders or projects the change differently is refused as `proposal_superseded`, with the
current version of the same change prepared and attached.

So after a rollout, expect a burst of `proposal_mismatch` on erasures and first plans only.
A steady rate of either code afterwards is not the expected shape, and means two processes
with different release variables are answering the same domain.

Nor does a redeploy carry across the CoachContexts the outgoing process was holding.
`startCoachSession` keeps each CoachContext it returns in process memory for
`CONTEXT_RETENTION_SECONDS` (60 minutes), so a client may send `{"context_id": ...}` on
`prepareCoachDecision` and
`applyCoachDecision` instead of echoing the whole object -- the one route a result-capped
client could not reach otherwise (issue #355). A preview or confirmation naming a context
the new process never held is refused with `context_expired` and told to start the session
again; a confirmation that resends the whole context is unaffected, and so is the replay
of a decision already at the head. The same hold covers the `change_request` a preview
was given (keyed by the proposal it issued, so `applyCoachDecision` may carry the proposal
alone) and the `delivery_set` a delivery prepared (keyed by its own `proposal_hash`, so
`applyWorkoutDelivery` may carry the hash alone); after a redeploy each is refused the
same way and told what to send or prepare again. Same shape as the proposal refusal
above: a burst right after a rollout, none afterwards.

## When the volume runs low

One volume holds every owner's store, so the athlete who fills it is not the athlete
whose next write meets the bottom of it. A write is refused once free space falls below
`store.py`'s `VOLUME_LOW_WATER_BYTES` (16 MiB), with

```text
the state volume is nearly full, so nothing may be written to it right now
```

as the `detail` of a `409`. **Reads keep working, and so does deleting an account** --
the reserve exists so an operator still has room to export, snapshot or erase once it
trips. A volume whose free space cannot be measured is treated as unknown rather than
full, so an unanswerable `statvfs` does not take the deployment down.

What to do: grow the volume, or export and remove stores that are no longer wanted. There
is no per-owner quota and no accounting of who consumed the space -- this guard only
stops any one account from reaching the bottom of a shared volume.

## Public-edge protections already in place

Unchanged by this deployment, listed here because they matter more once the gateway is
reachable from the public internet: the 1 MiB request body limit and strict
`Content-Type` checking (`gateway.py` `MAX_REQUEST_BYTES`), the `/oauth/register` payload
bounds (`MAX_REGISTERED_REDIRECT_URIS` and its two neighbours, which stop an
unauthenticated body from becoming an unbounded `client_id`), no server-side proposal
storage (a proposal is a signed, expiring token the client holds, not gateway state -- the
one thing the gateway does hold per athlete is what its own last few previews handed out --
the CoachContext, the change request previewed, the delivery set prepared -- in memory for
60 minutes, so that the client can name each rather than resend it), and
`Cache-Control: no-store` on every response so an intermediate cache never serves one
athlete's answer to another's request.

## Platform-neutral: Railway, Render, or elsewhere

### Railway production promotion lane

Railway production follows the `production` branch, not `main`, with **Wait for CI**
enabled. Product development continues to merge into `main`; a release is an explicit
fast-forward of `production` to one already-green `main` commit, only after the seven
release variables have been prepared for that exact commit.
`.github/workflows/ci.yml` runs on `production` pushes so Railway has a branch check to
wait for. Later merges to `main` therefore remain deployable candidates, not silent
production changes.

The safe order is: build the bundle for the chosen commit, set the seven
`GARMIN_COACH_LOOP_RELEASE_*` values, fast-forward `production`, wait for CI and Railway
`/readyz`, then run a read-only smoke through one real client. Rollback moves
`production` back to the preceding certified commit and restores that commit's release
variables with it; moving only the Git ref is deliberately blocked by `/readyz`.

A **code-only roll** — a `main` commit that touches `garmin_coach_loop/` but changes
neither `orchestration.md`, nor the MCP tool catalogue, nor
`.agents/skills/garmin-coach-loop/` — needs no new content hashes. Build the bundle the
same way (`scripts/release_bundle.py build`) for the new commit; `instructions_sha256`,
`tool_catalogue_sha256` and `skill_sha256` will come out unchanged, so only `RELEASE_ID`,
`RELEASE_COMMIT`, and `RELEASE_GATEWAY_ARTIFACT_SHA256` need updating before the
fast-forward. The order
still matters: variables first, then the Git ref, or the new deployment answers
`/readyz` with a commit the release identity does not name and refuses to go ready.
The CLI path for this lane is recorded in
[`ops/roll-with-railway-cli.md`](ops/roll-with-railway-cli.md).

`Dockerfile` has no platform-specific instruction in it; `fly.toml` and `railway.toml`
carry their respective host policies. Moving to another single-instance host means
recreating what `fly.toml` expresses, in that platform's own terms:

| Setting | Fly (`fly.toml`) | Generic equivalent |
| --- | --- | --- |
| Bind port | `GARMIN_COACH_LOOP_GATEWAY_PORT` set to match `internal_port` (Fly does not inject a `PORT` variable itself) | Most platforms inject `PORT` at runtime; set `GARMIN_COACH_LOOP_GATEWAY_PORT` from it (Railway/Render both support referencing `$PORT` in the start command or an env-var expression) |
| Bind host | `GARMIN_COACH_LOOP_GATEWAY_HOST=0.0.0.0` | Same value, same variable, everywhere |
| Persistent volume | `[mounts]`, destination `/data` | Railway volumes / Render disks -- mount anywhere outside the image's app directory (see the `/app` note in `Dockerfile` and `fly.toml`) and point `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` at it |
| Single instance | `auto_stop_machines`/`auto_start_machines = false`, `fly scale count 1` | Railway: one replica in the service's scaling settings. Render: a single instance web service (not autoscaling) |
| Health check | `[[http_service.checks]]` on `/healthz`, status code only | Railway uses `GET /readyz` and refuses a release/source mismatch; platforms without an explicit promotion lane may keep `GET /healthz` for liveness and must inspect its body separately |
| Secrets | `fly secrets set` | Railway/Render's own secret/environment variable store -- never the platform's plain build-time env vars if those end up logged or exposed in a dashboard readable by more people than the operator |
| Graceful shutdown signal | `kill_signal = "SIGTERM"` (Fly's own default is `SIGINT`, escalating to `SIGTERM` after `kill_timeout`) | `run_gateway` treats `SIGTERM` and `SIGINT` identically, so no platform-specific change is needed here |
| Rolling-deploy drain | `kill_timeout = "30s"`; health-check grace is 45s | Keep the platform drain/kill window at or below 30s and its startup health timeout above the gateway's conditional 35s wait when a predecessor owner lock is present |

The one thing every platform must still provide on its own: **single-replica
enforcement**. Nothing in this product's code can detect a second concurrent replica from
inside the process -- it is a deployment-time guarantee, not a runtime check, on every
platform equally.

## 1.4.1 callback and publication boundaries

Consent displays the browser destination origin. Non-canonical IPv4 forms, trailing DNS
dots, invalid IDNA, authority escapes and ambiguous ports fail closed; valid IPv6 is
compressed before comparisons. This blocks address-alias revocation bypasses, where a
warning alone is insufficient because it would name a different destination. Canonical
IPv4/IPv6, ASCII HTTPS names, valid punycode and loopback remain usable. The compatibility
cost is that clients using rejected spellings must register a canonical callback.

Explicit blocked origins take precedence over verification, including loopback. This
stops existing client IDs starting another authorization; it does not retroactively
revoke previously issued bearers. Continue grants the downstream app Coach capabilities,
including Coach-held state/records and permitted Intervals effects. Prepare/apply is not
a security boundary against an authorized malicious client.

After production identity is verified, dispatch the hardened Registry workflow as described
in [the Registry runbook](distribution/mcp-registry.md). Merging source does not publish.
1.4.1 admits structurally safe hosted callbacks for the currently supported legacy MCP
revisions; issue #352 owns the separate 2026-07-28 dual-era migration.
