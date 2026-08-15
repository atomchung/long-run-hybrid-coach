# Deploying the Coach Gateway

A runbook for putting `garmin_coach_loop.gateway` on a persistent host, so the Custom GPT
entry point (or any other agent-neutral client) reaches it at a stable HTTPS domain
instead of an ephemeral tunnel. Fly.io is the concrete example throughout, and is the
only platform this repository ships a config file for (`fly.toml`); the last section maps
the same settings onto Railway and Render for operators who land somewhere else.

This is operational documentation for standing the service up. It assumes you have
already read [`entrypoints/custom-gpt/README.md`](../entrypoints/custom-gpt/README.md) for
what the gateway is and how a Custom GPT talks to it -- this file does not repeat that.

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
- An Intervals.icu OAuth app already registered (Step B of the Custom GPT README) --
  deployment does not change that step.
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
   pair from Intervals `/settings` -> Manage App that the Custom GPT README's Step B
   registers.

   `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` is **not** a secret -- it is already set as a
   plain value in `fly.toml`'s `[env]` (`/data`, the `[mounts]` destination) and does not
   need to be set again here.

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

- `"status": "blocked"`, `"error": "missing_or_mismatched_runtime_release_identity"` --
  expected immediately after a first deploy. No `GARMIN_COACH_LOOP_RELEASE_*` variable is
  set yet, so nothing has told this gateway what "the current release" means. The process
  is healthy; it is just not yet certified as the one the Custom GPT should trust.
- `"status": "ok"` -- the six `GARMIN_COACH_LOOP_RELEASE_*` secrets are set and match this
  exact deployed commit, OpenAPI, instructions, artifact, and domain.

To move from the first to the second, run the same release gate the Custom GPT README's
"Release gate" section documents in full -- this is the same step, not a separate one, so
this file only summarizes the two commands and the domain it needs:

```bash
python3 scripts/custom_gpt_release.py build \
  --gateway-domain https://YOUR-APP-NAME.fly.dev \
  --output /secure/release/builder-bundle.json
python3 scripts/custom_gpt_release.py verify \
  --bundle /secure/release/builder-bundle.json \
  --builder-instructions /secure/release/builder-instructions.md \
  --builder-openapi /secure/release/builder-openapi.yaml \
  --receipt /secure/release/receipt.json
```

Then set the six `GARMIN_COACH_LOOP_RELEASE_*` values the bundle names
(`fly secrets set ...`) and redeploy. `/healthz` flips to `"status": "ok"` once they match;
`--gateway-domain` must be this deployment's real domain, not the
`YOUR-GATEWAY-DOMAIN` placeholder that fails `normalise_gateway_domain` in
`release_identity.py`.

## Changing domains

If the gateway's public domain ever changes (a custom domain replacing `fly.dev`,
migrating to a different host), two things elsewhere in this repository stop matching it
and must be updated together, not left for the next confusing failure to surface:

1. **The Custom GPT Action schema** -- `entrypoints/custom-gpt/openapi.yaml`'s `servers`
   URL, and the OAuth Token URL in the GPT editor's Authentication config
   (`https://<domain>/oauth/intervals/token`). The Custom GPT README's Step C and its
   "Tunnel URL changed" troubleshooting entry are the existing instructions for exactly
   this edit; a domain change is that same edit, just planned instead of reactive.
2. **The release identity** -- `gateway_domain` is part of what `release_identity.py`
   binds into `release_id` (see `make_release_id`). A changed domain makes every existing
   `GARMIN_COACH_LOOP_RELEASE_*` secret stale; `/healthz` will report `"blocked"` again
   until you rebuild and reset them via the release gate above with the new
   `--gateway-domain`.

The Intervals OAuth app registration itself is unaffected -- its redirect URI is
ChatGPT's own callback domain, never the gateway's, so a gateway domain change never
requires re-registering with Intervals.

## Crash-safe locking

Each owner's store is guarded by a POSIX exclusive-create lock file
(`garmin_coach_loop/store.py`'s `_exclusive_lock`, a `.lock` marker inside that owner's
directory). A process that exits through `run_gateway`'s normal path always removes its
own lock; one that does not -- `SIGKILL`, an out-of-memory kill, a host crash -- leaves it
behind. Left in place, a stale `.lock` refuses every read and write for that one owner,
`restore-store` included, until someone removes it.

Recovery is automatic and requires no volume access: `run_preflight` (`gateway.py`) scans
`$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT/owners/*/.lock` once at every startup and removes
every one it finds, logging only the count reclaimed -- never an owner id or a path. This
is safe only because deployment is single-replica: at the instant a fresh process reaches
that scan, no *other* process can legitimately hold one of those locks, so every marker
found is necessarily a previous process's leftover. Running more than one replica against
the same volume would make that reasoning false and is exactly what `fly.toml`'s
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

## Public-edge protections already in place

Unchanged by this deployment, listed here because they matter more once the gateway is
reachable from the public internet: the 1 MiB request body limit and strict
`Content-Type` checking (`gateway.py` `MAX_REQUEST_BYTES`), no server-side proposal
storage (a proposal is a signed, expiring token the client holds, not gateway state), and
`Cache-Control: no-store` on every response so an intermediate cache never serves one
athlete's answer to another's request.

## Platform-neutral: Railway, Render, or elsewhere

`Dockerfile` has no Fly-specific instruction in it -- `fly.toml` is the only
Fly-specific piece of this artifact. Moving to a different single-instance host means
recreating what `fly.toml` expresses, in that platform's own terms:

| Setting | Fly (`fly.toml`) | Generic equivalent |
| --- | --- | --- |
| Bind port | `GARMIN_COACH_LOOP_GATEWAY_PORT` set to match `internal_port` (Fly does not inject a `PORT` variable itself) | Most platforms inject `PORT` at runtime; set `GARMIN_COACH_LOOP_GATEWAY_PORT` from it (Railway/Render both support referencing `$PORT` in the start command or an env-var expression) |
| Bind host | `GARMIN_COACH_LOOP_GATEWAY_HOST=0.0.0.0` | Same value, same variable, everywhere |
| Persistent volume | `[mounts]`, destination `/data` | Railway volumes / Render disks -- mount anywhere outside the image's app directory (see the `/app` note in `Dockerfile` and `fly.toml`) and point `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` at it |
| Single instance | `auto_stop_machines`/`auto_start_machines = false`, `fly scale count 1` | Railway: one replica in the service's scaling settings. Render: a single instance web service (not autoscaling) |
| Health check | `[[http_service.checks]]` on `/healthz`, status code only | Any platform health check pointed at `GET /healthz`, status-code-only -- the body's own `status` field is a separate, human-read signal (see "Post-deploy verification"), not something a platform health check should parse |
| Secrets | `fly secrets set` | Railway/Render's own secret/environment variable store -- never the platform's plain build-time env vars if those end up logged or exposed in a dashboard readable by more people than the operator |
| Graceful shutdown signal | `kill_signal = "SIGTERM"` (Fly's own default is `SIGINT`, escalating to `SIGTERM` after `kill_timeout`) | `run_gateway` treats `SIGTERM` and `SIGINT` identically, so no platform-specific change is needed here |

The one thing every platform must still provide on its own: **single-replica
enforcement**. Nothing in this product's code can detect a second concurrent replica from
inside the process -- it is a deployment-time guarantee, not a runtime check, on every
platform equally.
