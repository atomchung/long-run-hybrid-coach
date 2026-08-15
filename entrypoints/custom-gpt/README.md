# Custom GPT entry

A private ChatGPT Custom GPT front end over the Coach Gateway. PlanState continuity lives
server-side in the gateway's store; the GPT itself holds no memory between turns.

## Prerequisites

- Python 3.11+
- This repository
- An Intervals.icu account
- A way to expose the local gateway over HTTPS — ChatGPT Actions require HTTPS. The steps
  below use a `cloudflared` quick tunnel as the example; any HTTPS tunnel or host works.

## Environment variables

Set these before starting the gateway. No values are shown here — put the real ones in
your shell environment or a local, gitignored `.env`, never in this repo.

| Variable | Purpose |
| --- | --- |
| `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` | Private multi-athlete state root (one directory per athlete). Must be outside the repository. |
| `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` | Secret key (32+ characters) used to fingerprint OAuth access tokens. Raw tokens are never stored or logged. |
| `GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID` | The OAuth client ID issued by Intervals for this app (Step B). |
| `GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET` | The OAuth client secret issued by Intervals. Stays server-side; the GPT never sees it. |

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

1. **Instructions**: paste the contents of [`instructions.md`](instructions.md) into the
   GPT's Instructions field verbatim.
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

Builder has no supported deployment API. Build the external-only bundle from the exact
Gateway commit, paste its `instructions` and rendered `openapi` fields into Builder, then
export those two saved texts outside the repository. Start the Gateway with every
`GARMIN_COACH_LOOP_RELEASE_*` value from the bundle. `/healthz` is data-free and must return
the same `release_identity`; a missing identity is blocked, not a valid release.

```bash
python3 scripts/custom_gpt_release.py build --gateway-domain https://gateway.example \
  --output /secure/release/builder-bundle.json
python3 scripts/custom_gpt_release.py verify --bundle /secure/release/builder-bundle.json \
  --builder-instructions /secure/release/builder-instructions.md \
  --builder-openapi /secure/release/builder-openapi.yaml \
  --receipt /secure/release/receipt.json
```

The receipt certifies artifact and Builder parity only. Record owner/store binding and live
user-visible smoke results separately outside Git; neither can be truthfully certified from
Builder text. Keep all evidence, secrets, state and provider data out of Git. A domain
placeholder, stale Builder copy, stale runtime identity or missing runtime identity fails
this gate.

### Production deploy state machine

`custom_gpt_deploy.py` coordinates that manual gate without treating a Builder save, proxy
update, or local pointer as proof of a live deployment. Its `--home` is mandatory and must
resolve outside this repository. The first use must adopt the currently verified production
release; a new candidate cannot become the first active pointer and silently lose the real
rollback target.

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases status
python3 scripts/custom_gpt_deploy.py --home /secure/releases adopt-active \
  --legacy-dir /secure/releases/PREVIOUS_COMMIT
# Plan only. Add --confirm-live-check to perform public /healthz parity verification
# and create the canonical active pointer.
```

Prepare an exact current-main candidate. The stable public Gateway origin belongs in
`--gateway-domain`; the ephemeral tunnel belongs only in the external proxy revision.
Changing the tunnel creates a new proxy revision of the same release, not a new release.

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases prepare \
  --git-commit FULL_ORIGIN_MAIN_SHA --main-ref origin/main \
  --gateway-domain https://gateway.example \
  --proxy-upstream https://ephemeral-tunnel.example
```

The run directory now contains the exact-commit bundle, expected Builder exports,
`proxy/vercel.json`, and a secret-free `deploy-request.json`. This repository intentionally
does **not** provide a live Gateway/tunnel/Vercel runner. A connector or operator may deploy
that request and then submit its minimal exact-run receipt with `record-deployment`.
Alternatively, `run-deployment-adapter` invokes a separately supplied executable only after
`--confirm`; without it the command prints the exact plan and executes nothing. Its secret env
file must be a regular external file with mode `0600`. The orchestrator checks only metadata
and passes the path to the adapter; it never opens, hashes, copies, or prints secret contents.

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases run-deployment-adapter \
  --run-id RUN_ID --runner /secure/bin/deploy-adapter \
  --secret-env-file /secure/config/gateway.env
python3 scripts/custom_gpt_deploy.py --home /secure/releases record-deployment \
  --run-id RUN_ID --receipt /secure/evidence/deployment-receipt.json
python3 scripts/custom_gpt_deploy.py --home /secure/releases record-builder \
  --run-id RUN_ID --builder-instructions /secure/evidence/builder-instructions.md \
  --builder-openapi /secure/evidence/builder-openapi.yaml
```

`verify` defaults to a network-free plan. With `--confirm-live-check` it reuses the release
gate above to compare the recorded Builder exports with public `/healthz`, and also requires
a minimal external smoke attestation bound to the exact run and release. It does not run a
coach, mutate PlanState, or write to a provider. `activate` changes only the external
orchestrator pointer, and only after both checks pass.

```bash
python3 scripts/custom_gpt_deploy.py --home /secure/releases verify \
  --run-id RUN_ID --smoke-evidence /secure/evidence/smoke.json
python3 scripts/custom_gpt_deploy.py --home /secure/releases activate --run-id RUN_ID
# Re-run the two commands with --confirm-live-check and --confirm respectively.
```

`rollback --run-id ACTIVE_RUN` is deliberately a rollback **plan**, not a live rollback. It
selects the previous verified target and, with `--confirm`, records that intent while leaving
the active pointer unchanged. The target still has to be redeployed through an external
adapter or connector, pass fresh public parity and smoke evidence, and then be activated.

## Step F — phone

Open the same GPT from the ChatGPT mobile app's sidebar (it is listed under your GPTs,
not the public store). The OAuth connection is per ChatGPT account, so the first action
call on a new device still prompts "Sign in with intervals.icu" once.

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
- **Tunnel URL changed**: quick tunnels are ephemeral. Update the `servers` URL and the
  OAuth Token URL in the GPT editor's schema/action config to the new tunnel URL. The
  Intervals OAuth app registration is unaffected — its redirect URI is ChatGPT's own
  callback domain, not the gateway's, so a new tunnel never requires re-registering with
  Intervals.
- **"Connection expired"**: Intervals issues no refresh tokens. Re-authorizing (sign in
  again) mints a new access token; nothing else is needed.

## Boundaries

- Delivery evidence stops at `intervals_accepted`. The GPT must never claim Garmin Connect
  or the watch received a workout — this product cannot observe that hop.
- Keep the GPT private (not published to the GPT store). This entry assumes one operator's
  own Intervals account per deployment.
