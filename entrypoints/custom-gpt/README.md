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
   - Scope: `ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE` — exactly what the registered app
     holds, and calendar read comes with the write scope. Adding a scope the registration
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

## Step F — phone

Open the same GPT from the ChatGPT mobile app's sidebar (it is listed under your GPTs,
not the public store). The OAuth connection is per ChatGPT account, so the first action
call on a new device still prompts "Sign in with intervals.icu" once.

## Troubleshooting

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
