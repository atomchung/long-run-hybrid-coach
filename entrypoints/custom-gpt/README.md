# Custom GPT entry

A private ChatGPT Custom GPT front end over the Coach Gateway. PlanState continuity lives
server-side in the gateway's store; the GPT itself holds no memory between turns.

## Prerequisites

- Python 3.11+
- This repository
- An Intervals.icu account
- A way to expose the local gateway over HTTPS — ChatGPT Actions require HTTPS. The steps
  below use a `cloudflared` quick tunnel as the example; any HTTPS tunnel or host works.
  For a persistent host instead of a machine-and-tunnel pair — so the domain survives a
  reboot and the gateway keeps running when your laptop is closed — see
  [`docs/deploy-gateway.md`](../../docs/deploy-gateway.md).

## Environment variables

Set these before starting the gateway. No values are shown here — production secrets
belong only in `~/.config/garmin-coach-loop/gateway.env`, outside this repository,
with mode `0600`; never put them in a repository `.env` (even gitignored) or commit
them anywhere. The deploy operator's Vercel token belongs in the separate external
`~/.config/garmin-coach-loop/vercel.env` file, also mode `0600`; the gateway process
does not load that file.

| Variable | Purpose |
| --- | --- |
| `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` | Private multi-athlete state root (one directory per athlete). Must be outside the repository. |
| `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` | Secret key (32+ characters) used to fingerprint OAuth access tokens. Raw tokens are never stored or logged. |
| `GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID` | The OAuth client ID issued by Intervals for this app (Step B). |
| `GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET` | The OAuth client secret issued by Intervals. Stays server-side; the GPT never sees it. |
| `GARMIN_COACH_LOOP_DEPLOYMENT_ENVIRONMENT` | Release-mode environment identity. Production uses the literal `production`. |
| `GARMIN_COACH_LOOP_DEPLOYMENT_INSTANCE_ID` | Stable non-secret identifier for this one production Gateway instance. |

Start the gateway:

```bash
python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
```

It binds loopback only; TLS comes from the tunnel in front of it, for example:

```bash
cloudflared tunnel --url http://127.0.0.1:8422
```

which prints an HTTPS URL such as `https://example-quick-tunnel.trycloudflare.com` (yours
will differ, and a quick tunnel's URL changes every time you restart it).

Steps A and B are independent. Registration is the step with human turnaround, so it is
fine to send the Step B email first with only `http://localhost/` as the redirect URI and
add the ChatGPT callback URLs later yourself — every registered app gets a self-service
"Manage App" page on Intervals `/settings` where redirect URIs can be changed at any time.

## Step A — create the GPT draft and get the callback URL

1. In ChatGPT: Explore GPTs -> Create -> Configure tab -> Create new action.
2. Under Authentication, choose OAuth. A placeholder Client ID/Secret is fine for now —
   you only need this step to reveal the callback URL.
3. Set Authorization URL to `https://intervals.icu/oauth/authorize` and Token URL to your
   gateway's `/oauth/intervals/token` (see Step C). Save.
4. The editor now shows a callback URL bound to this GPT's id. It has appeared under both
   `https://chat.openai.com/aip/<gpt-id>/oauth/callback` and
   `https://chatgpt.com/aip/<gpt-id>/oauth/callback` across ChatGPT versions — note
   whichever the editor displays; you will register both forms with Intervals in Step B to
   be safe. The schema itself can be pasted later.

## Step B — register the OAuth app with Intervals

OAuth client registration is not self-service. Email David at the address published in
the first post of the
[Intervals OAuth support thread](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759),
including:

- App name and description
- Website URL
- Logo URL (square, at least 128x128)
- Privacy policy URL — only required before the app is made visible to all users; a
  private app may start without one
- Redirect URIs: `http://localhost/` (always allowed) is enough to start; add both
  callback forms from Step A whenever you have them, here or later via Manage App
- Your Intervals.icu ID, from the bottom of `/settings`

Once registered, the app appears on your own `/settings` page with a "Manage App" button
holding `client_id`, `client_secret`, and editable redirect URIs. An unlisted app works
for specific users via a direct consent link — no public app review is needed for
private, single-operator use. Emails to this address have been lost before; if there is
no reply after a few days, follow up with a post in the thread above.

## Step C — configure the GPT

1. **Instructions**: paste the contents of
   [`orchestration.md`](../../garmin_coach_loop/orchestration.md) into the GPT's
   Instructions field verbatim. It lives in the package rather than here because the
   MCP entry serves the same file as a prompt; this entry is one of its two readers,
   not its owner.
2. **Action schema**: paste the contents of [`openapi.yaml`](openapi.yaml), first replacing
   every `YOUR-GATEWAY-DOMAIN` with your actual tunnel or host domain (both the `servers`
   URL and the OAuth `tokenUrl`).
3. **Authentication** (OAuth):
   - Client ID / Client Secret: from Intervals `/settings` (Step B)
   - Authorization URL: `https://intervals.icu/oauth/authorize`
   - Token URL: `https://<your-gateway-domain>/oauth/intervals/token`
   - Scope: `ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:READ` — exactly what the registered app
     holds; calendar read comes with the write scope, while Settings read is used only by
     the bounded connection diagnostic. Adding a scope the registration
     does not grant fails the whole authorization, not just that scope.
   - Token Exchange Method: POST request (default)
4. Save.

## Step D — give the connected account a PlanState

Every OAuth identity gets its own state directory, and the gateway never fills one in by
itself. After the first successful sign-in, exactly one of these applies.

**A new athlete** — ask the GPT for a plan. It asks what you are training for, when you can
train, and what you can already do; shows the exact first 28-day direction and week; and
creates the PlanState only after one confirmation.

**An existing local store** — adopt it, so the GPT continues the plan you already have
instead of starting a second one beside it. `--athlete-id` is your Intervals.icu ID from
the bottom of `/settings`; the owner it names must already have signed in once, because an
athlete id typed at a terminal is not an authorization.

```bash
python3 -m garmin_coach_loop.cli adopt-owner-store \
  --athlete-id YOUR-INTERVALS-ID \
  --from ~/.local/share/garmin-coach-loop
```

The destination comes from `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT`; pass `--state-root`
when that variable is not set in this shell.

Without `--confirm` this only prints the exact source, destination, plan id and version it
would adopt; add `--confirm` to perform it. `--mode link` (the default) leaves one plan
that the CLI and the GPT both read and write; `--mode copy` duplicates the whole history
into the owner directory, after which the two plans diverge from the next decision onward.
An owner directory that already holds state is refused either way — nothing is merged, and
the source is never modified.

## Step E — Preview test sequence

1. In the GPT preview, ask something like "今天練什麼" (what should I do today). ChatGPT
   prompts "Sign in with intervals.icu" on the first action call — complete it, then
   confirm the reply reflects the actual current plan.
2. Ask for a real weekly change; confirm once when asked; start a new conversation and
   confirm it now reads the new plan version.
3. Ask to deliver one workout; confirm once when asked; open Intervals' own calendar and
   verify the workout appears with the expected prescription.

## Release gate — required before treating this GPT as current

The release state CLI below is canonical. Do not use the older release-artifact verifier
as the final production-state command. Its artifact comparison may support a checkpoint,
but `custom_gpt_deploy.py --home ... verify` owns the recorded state transition.

### Production deploy state machine

`custom_gpt_deploy.py` coordinates the release without treating a Builder save, proxy
update, or local pointer as proof of a live deployment. Its `--home` is mandatory and must
resolve outside this repository. The first use must adopt the currently verified production
release; a new candidate cannot become the first active pointer and silently lose the real
rollback target.

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases status
python3 scripts/custom_gpt_deploy.py --home /secure/releases adopt-active \
  --legacy-dir /secure/releases/PREVIOUS_COMMIT \
  --current-proxy-upstream https://current-tunnel.example \
  --current-proxy-config /secure/evidence/current-vercel.json \
  --expected-deployment-identity /secure/evidence/production-identity.json \
  --production-gpt-id CANONICAL_PRODUCTION_GPT_ID
# Plan only. Add --confirm-live-check to perform public /healthz parity verification
# and create the canonical active pointer.
```

`adopt-active` exists only to bootstrap the already verified production release from
the pre-orchestrator layout. The legacy directory must contain its bundle, Builder
exports, release receipt, and live smoke evidence; the separate current Vercel config
must prove the declared upstream. Adoption labels this as weaker legacy evidence — it
does not retroactively prove a modern deployment identity and must not be used for new
candidates. A first normal `activate` is refused until this production rollback target
has been adopted.

Prepare an exact current-main candidate. The stable public Gateway origin belongs in
`--gateway-domain`; the ephemeral tunnel belongs only in the external proxy revision.
Changing a production tunnel updates only that proxy revision: do not edit the production
GPT Builder schema or OAuth token URL. It remains the same release.

```bash
git fetch origin main
python3 scripts/custom_gpt_release.py deployment-identity \
  --env-file ~/.config/garmin-coach-loop/gateway.env \
  --output /secure/evidence/production-identity.json
python3 scripts/custom_gpt_deploy.py init-production-target \
  --output ~/.config/garmin-coach-loop/production-target.json \
  --repository OWNER/long-run-hybrid-coach \
  --team-id VERCEL_TEAM_ID \
  --project-id VERCEL_PROJECT_ID \
  --project-name long-run-hybrid-coach-gateway \
  --stable-domain gateway.example \
  --production-gpt-id CANONICAL_PRODUCTION_GPT_ID
python3 scripts/custom_gpt_deploy.py --home /secure/releases prepare \
  --git-commit FULL_ORIGIN_MAIN_SHA \
  --gateway-domain https://gateway.example \
  --proxy-upstream https://ephemeral-tunnel.example \
  --production-target ~/.config/garmin-coach-loop/production-target.json \
  --expected-deployment-identity /secure/evidence/production-identity.json
```

The external production-target file contains no credentials. It fixes the one
production GPT ID, public GitHub repository/`main`/`ci.yml`, and the Vercel team,
project, project name, and stable domain under one canonical hash. Generate it
with `init-production-target`; do not hand-edit it. Ordinary deploys fail closed
if any target changes, because GPT or provider migration is a separate operation.
`prepare` uses `gh api` to read public
`main` and its exact successful push run; a caller-written green-CI JSON is not
accepted. The preceding `git fetch` keeps the independent local `origin/main`
candidate check aligned with that provider observation.

The run directory contains the exact-commit bundle, expected Builder exports,
`proxy/vercel.json`, and a secret-free deployment request. After the user explicitly
confirms the exact target and rollback target, a Codex/operator may use an authorized
Gateway/Vercel connector and browser assistance to update the same production Builder.
The GPT itself may never deploy. The built-in, repository-owned Vercel create
adapter uploads only the exact hash-bound proxy config, creates the fixed
production deployment, and emits a bounded create response. Before the production
POST it durably records a single create attempt keyed by the proxy revision and
request/config hashes. A lost response is reconciled through Vercel's project-scoped
deployment list; a retry never sends a second production POST for that revision.
Run it through the
state machine; the orchestrator then performs its own authenticated Vercel REST
reads. `record-deployment` remains available when an authorized external create
call already produced the same bounded attestation. Record the same production GPT's exported instructions/OpenAPI
plus Builder attestation with `record-builder`:

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases run-deployment-adapter \
  --run-id RUN_ID \
  --secret-env-file ~/.config/garmin-coach-loop/vercel.env \
  --confirm
# Alternative only when a create call already happened:
python3 scripts/custom_gpt_deploy.py --home /secure/releases record-deployment \
  --run-id RUN_ID \
  --provider-evidence /secure/evidence/vercel-create-attestation.json \
  --secret-env-file ~/.config/garmin-coach-loop/vercel.env \
  --confirm-live-check
python3 scripts/custom_gpt_deploy.py --home /secure/releases record-builder \
  --run-id RUN_ID \
  --builder-instructions /secure/evidence/builder-instructions.md \
  --builder-openapi /secure/evidence/builder-openapi.yaml \
  --builder-evidence /secure/evidence/builder-evidence.json
```

The built-in adapter calls Vercel's file-upload and production-deployment APIs;
its evidence contains exactly one normalized production create response and cannot
claim deployment success. The create payload contains deterministic release metadata
but no unsupported domain alias field; the later authenticated stable-alias read-back
is the authority for which deployment is serving production. The durable create
attempt and bounded attestation remain private release artifacts, so a temporary
read-back outage resumes the same deployment ID instead of creating another one.
`~/.config/garmin-coach-loop/vercel.env` is a separate
external `0600` file containing exactly one `VERCEL_TOKEN`. The orchestrator uses
that token to GET the exact deployment, project, stable alias, deployment aliases,
and production project domains from Vercel, then records only normalized hashes
and identities. It requires `READY`, exact team/project/name, the stable alias to
point to this deployment, and the configured domain to belong to both deployment
and project. The token and raw provider bodies are never persisted. A create
response or hand-written `status: succeeded` receipt alone is rejected. A custom
`--runner` receives `--request`, `--secret-env-file`, a private
`--evidence-output`, and the durable `--attempt-state`; it must obey the same
single-submission state transitions. It can replace only the create call; the built-in REST
read-back still owns the success decision. Public `/healthz` with the exact proxy revision, release
identity, and deployment identity remains a separate later verification.

Create the Vercel access token from the account token settings, select the exact
production team/account scope offered by that UI, and give it the shortest practical
expiry. The token must be able to create deployments and read that exact project's
deployment, alias, and domain state; do not claim finer-grained capabilities unless
the token UI actually offers them. Store
it as the single line `VERCEL_TOKEN=...` in `vercel.env`; before release, inspect
only the file metadata (for example `stat -f '%Sp %N' ~/.config/garmin-coach-loop/vercel.env`)
and confirm it is one regular `0600` file. Never print or paste the token.

The Builder evidence identifies the GPT and current proxy revision. Matching instruction
and OpenAPI hashes does not automatically prove the Builder's selected model,
authentication settings, or other saved configuration. Those require the explicit
human/browser attestation and the user-visible browser smoke; Builder has no supported
deployment API and this is not a one-click flow.

`verify` defaults to a network-free plan. With its explicit live-check confirmation it
compares recorded Builder exports with public `/healthz` and requires smoke evidence bound
to the exact run and release. It does not run a coach, mutate PlanState, or write to a
provider. `activate` changes only the external orchestrator pointer, and only after both
checks pass.

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases verify \
  --run-id RUN_ID \
  --smoke-evidence /secure/evidence/smoke.json \
  --browser-evidence /secure/evidence/browser-evidence.json
python3 scripts/custom_gpt_deploy.py --home /secure/releases activate \
  --run-id RUN_ID \
  --secret-env-file ~/.config/garmin-coach-loop/vercel.env
# Re-run verify with --confirm-live-check and activate with --confirm.
```

Activation performs a new Vercel provider read-back and a new stable public
`/healthz` check immediately before the active pointer is written. A route,
deployment, or production target change after `verify` therefore blocks activation
instead of reusing stale evidence.

For a tunnel-only change on the same release, create a new route revision with
`repair-proxy --run-id RUN_ID --proxy-upstream NEW_UPSTREAM`. That route-only repair may
reuse the prior Builder evidence; it still needs a new Vercel deployment receipt, verify
evidence, and activation. The stable Builder schema and OAuth Token URL do not change.

`rollback --run-id ACTIVE_RUN` without `--confirm` only prints the previous verified
target. With `--confirm` it creates a fresh restore revision while leaving live deployment
and the active pointer unchanged. A restore must record fresh Builder evidence — unlike a
route-only `repair-proxy`, it cannot reuse the old Builder attestation — then record the
redeployment, verify with fresh smoke and browser evidence, and `activate`.
If that previous target is the one legacy-adopted release, add
`--production-target ~/.config/garmin-coach-loop/production-target.json` to the
confirmed rollback. This binds the fresh restore revision to current Vercel
provider read-back without rewriting the weaker historical legacy evidence.

## Step F — phone

Open the same GPT from the ChatGPT mobile app's sidebar (it is listed under your GPTs,
not the public store). The OAuth connection is per ChatGPT account, so the first action
call on a new device still prompts "Sign in with intervals.icu" once.

One athlete keeps exactly one live connection: authorizing again — from a second device,
a second browser, or after "Connection expired" — replaces the previous one. Whichever
entry point was not just used for that authorization gets `unauthorized` on its next
call, with no other symptom, and the fix is the same "Sign in with intervals.icu" prompt.
This is expected — a deliberate one-connection-per-athlete design (`identity.py`'s
`record_token_fingerprint`), not a bug — and it applies to every athlete on this GPT, not
only whoever set it up.

## Inviting another athlete

The gateway is already multi-athlete: every ChatGPT user who completes their own "Sign in
with intervals.icu" gets their own owner and state directory, resolved from their token
and unreachable from anyone else's (`resolve_state_dir`'s canonical-UUID check is what
enforces that — see `garmin_coach_loop/identity.py` and `garmin_coach_loop/store.py`).
Inviting someone else is a ChatGPT sharing setting, not a code or gateway change.

1. In the GPT editor, open Share and set visibility to **Anyone with a link**, then send
   that link. **Only me** is the default, and it makes the link open nothing for anyone
   but you — this is the one setting that actually invites someone.
2. The invited athlete needs their own Intervals.icu account (the free tier is enough)
   and their own ChatGPT account. Neither needs to know anything about this repository,
   the gateway, or your own Intervals account.
3. Their first action call in the GPT prompts them to "Sign in with intervals.icu," the
   same as Step D above. Completing it creates their own owner and an empty state — they
   go through the same "ask what you are training for" first-plan conversation you did.
4. Their data — plan, decisions, availability, reported strength sets, and identity
   mapping — is stored under their own owner id and is never visible to you or to any
   other invited athlete. No route in `openapi.yaml` accepts or exposes an owner id; every
   request resolves to its caller's own token and nothing else.
5. Disconnecting or requesting deletion is theirs to do, the same way it is yours. See
   [`../../docs/account-lifecycle.md`](../../docs/account-lifecycle.md) for what each one
   actually does.

## Troubleshooting

- **Which permission is actually missing?** Ask the private GPT to inspect its Intervals
  connection. The diagnostic returns only normalized scopes observed at that token's
  exchange and one `settings_read` result: `readable` (provider returned 200), `denied`
  (403), or `invalid_or_expired` (401). It never returns sport settings, token material,
  an athlete id, or an owner id. A missing stored scope observation means the connected
  token predates this gateway feature; re-authorize to record a new one.
- **Do not use Manage App as scope proof.** The Intervals Manage App page is useful for
  client credentials and redirect URIs, but it is not a source of truth for the scopes
  on the token currently held by this GPT. Use the diagnostic after a fresh authorization.

- **401 on any call**: the gateway no longer recognizes the token — revoked on Intervals,
  or superseded by a newer authorization (the gateway honors the most recent one only).
  Re-run "Sign in with intervals.icu" from the action's connection settings in the GPT.
- **Production tunnel/proxy upstream changed**: update only the stable production Vercel
  proxy upstream and record the proxy recovery checkpoint. Do not change the production
  Builder schema or OAuth Token URL; its stable origin stays the same.
- **Direct development tunnel changed**: this is a separate development Builder action.
  Update that development action's `servers` URL and OAuth Token URL to the temporary
  tunnel. The Intervals OAuth app registration is unaffected — its redirect URI is
  ChatGPT's callback domain, not the gateway's.
- **"Connection expired"**: Intervals issues no refresh tokens. Re-authorizing (sign in
  again) mints a new access token; nothing else is needed.

## Boundaries

- Delivery evidence stops at `intervals_accepted`. The GPT must never claim Garmin Connect
  or the watch received a workout — this product cannot observe that hop.
- Keep the GPT private (not published to the GPT store). The deployment itself is already
  multi-athlete — see "Inviting another athlete" above — so this boundary is about GPT
  Store discovery and review, not about how many athletes one deployment may serve.
