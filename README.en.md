# Long Run Hybrid Coach

[繁體中文](README.md) · **English** · [简体中文](README.zh-Hans.md)

Long Run Hybrid Coach is an independent, Intervals-first, device-agnostic hybrid training coach. It keeps one current 28-day direction and one executable running + strength week, reconciles trustworthy completed training, and can deliver confirmed workouts to your Intervals.icu calendar.

**Garmin is not required.** Garmin is the first downstream device path we have dogfooded. Apple Watch, COROS, Polar, Suunto, Wahoo, other apps/watches, or no watch can use the same coach. The difference is how much trustworthy evidence reaches the loop and whether a downstream device-sync path has been separately verified.

> **Recommended for most users:** use the hosted MCP at `https://mcp.paceandstaystrong.com/mcp`. You need an Intervals.icu account, but you do not need to create your own Intervals OAuth app or operate a gateway.

---

## Quick Start — Hosted MCP

### What you need

1. An **Intervals.icu account**.
2. An AI client that can connect to a remote MCP server and expose the actions this product needs.
3. Optionally, a watch or training app that already syncs activities into Intervals.icu.

You do not need a Garmin watch. Missing optional recovery evidence remains unknown rather than becoming zero.

### 1. Connect the hosted coach

MCP endpoint:

```text
https://mcp.paceandstaystrong.com/mcp
```

- **claude.ai / Claude Desktop:** Settings → Connectors → Add custom connector → paste the endpoint. This path has completed a real production OAuth, coaching turn, and Intervals delivery.
- **ChatGPT:** according to OpenAI's current documentation, full MCP with write/modify actions is available in beta on ChatGPT Business, Enterprise, and Edu on web. Pro custom MCP currently supports read/fetch only, so it cannot complete this Coach's plan-write and delivery flow. If your workspace has full MCP, create a custom app in Apps/developer mode and point it at the remote endpoint. Check the latest limitations in the [official OpenAI documentation](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt).
- **OpenClaw:** point `openclaw mcp add` at the same endpoint with `--auth oauth`. On an instance more than one person talks to, set the OAuth identity to per-requester or everyone reaches one Intervals account. Setup is in [entrypoints/openclaw/](entrypoints/openclaw/README.md).
- **Other MCP clients:** configure the same endpoint as a remote Streamable HTTP MCP server. Full support depends on that client's MCP/OAuth capabilities.

See [entrypoints/](entrypoints/README.md) for the current per-client end-to-end verification status.

### 2. Authorize Intervals.icu

The first connection opens an Intervals.icu consent page. Sign in to **your own Intervals.icu account** and grant the capabilities the Coach needs:

- `ACTIVITY:READ` — completed training.
- `WELLNESS:READ` — available wellness evidence.
- `CALENDAR:WRITE` — read/write the training calendar so confirmed workouts can be delivered and read back.
- `SETTINGS:WRITE` — read settings and, only inside the confirmation-bound delivery flow, fill a missing supported running threshold setting when needed.

Intervals exposes these permissions separately. If one is omitted, the dependent capability fails clearly while unrelated capabilities may still work. Reconnect and grant the missing permission. **Do not paste an Intervals password, API key, or token into chat.**

### 3. Ask a normal coaching question

No setup questionnaire is required. For example:

```text
Read my latest training and tell me what I should do this week.
```

or:

```text
I want to improve VO2max without losing strength. Build my first 28-day direction.
```

The Coach reads existing evidence first and asks only for missing facts that materially change the decision, such as available days, equipment, or a strength baseline the provider cannot know.

### 4. Review the 28-day preview before a plan is written

A first plan shows:

- **this week:** precise, executable sessions;
- **the next three weeks:** an outlook rather than fake precision.

The plan is written only after you confirm the preview. Weekly changes use the same pattern:

**before/after preview → one confirmation → apply**.

### 5. Confirm delivery separately

Calendar delivery has its own confirmation:

**delivery preview → one confirmation → write to Intervals.icu → read back and verify**.

The strongest state this product can prove is `intervals_accepted`. An accepted Intervals workout is **not** proof that it reached Garmin, Apple Watch, or another downstream device.

---

## What Intervals.icu does in this product

Intervals.icu is the current **interoperability hub**. It brings external activity/wellness evidence into the loop and receives confirmed calendar workouts. It is **not** the Coach's PlanState source of truth.

```text
watch / training app
        │
        ▼
   Intervals.icu  ───── completed activity + wellness evidence ─────► Coach
        ▲                                                         │
        │                                                         │
        └──────── confirmed calendar workouts ◄───────────────────┘
        │
        ▼
Garmin / Apple Watch bridge / other downstream sync
```

Responsibilities are deliberately separated:

- **Intervals.icu:** external training evidence and the provider calendar.
- **Long Run Hybrid Coach:** the current PlanState, decision history, athlete-reported evidence, confirmation binding, and coaching workflow.
- **Your watch/app:** may send evidence into Intervals and may receive workouts from it, but downstream delivery is a separate compatibility fact.

Only the Intervals account itself is mandatory. If activity/wellness data already syncs there, the automatic evidence is richer. Missing optional fields remain unknown.

You can report device-missing facts in conversation: strength sets/loads/reps, availability, body measurements, an unrecorded activity, subjective fatigue/sleep wording, or recovery readings actually shown by your device. The Coach never converts “I feel exhausted” into a made-up readiness number.

---

## Hosted MCP vs Local / Self-hosted MCP

One difference is the one you actually feel: **hosted works from your phone; local works only on the machine running the gateway.** Every other row below is the cost of that.

| | Hosted MCP — recommended | Local / self-hosted MCP |
| --- | --- | --- |
| Works on your phone | Yes — connect any client that has a mobile app | No, unless you expose your own gateway publicly and handle TLS |
| MCP URL | `https://mcp.paceandstaystrong.com/mcp` | your gateway, e.g. `http://127.0.0.1:8422/mcp` |
| Operations | managed for you | you operate/update/back up it |
| Intervals OAuth app | **not required** | **required** — your own application credentials |
| Current plan | hosted per-athlete owner store | your own gateway state root |
| Best for | normal users, multiple clients | developers or fully self-managed environments |

### Hosted in five steps

1. Add a remote MCP app/connector in a compatible client.
2. Use `https://mcp.paceandstaystrong.com/mcp`.
3. Complete the client OAuth flow.
4. Authorize Intervals.icu in the browser.
5. Return to chat and ask the first coaching question.

The hosted service handles dynamic client registration, PKCE, gateway tokens, and per-athlete owner mapping. Normal users never need an owner id, athlete id, API key, Intervals client secret, or server environment variable.

### Run a local / self-hosted gateway

The repository uses Python 3.11 and is stdlib-only at runtime.

1. Clone the repository.
2. **Request an OAuth application from Intervals.icu.** Their public process is not self-service app creation in Settings: send the application details listed in [Intervals.icu OAuth support](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759). After Intervals creates it, **Manage App** appears in Settings and exposes the `client_id` and secret.
3. Register `<gateway-origin>/oauth/callback` as the provider callback. A local client may use loopback; a remote client needs reachable HTTPS or a secure tunnel.
4. Set the required gateway variables:

```bash
export GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT="$HOME/.local/share/long-run-hybrid-coach-gateway"
export GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)"
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID="..."
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET="..."
```

5. Start the gateway:

```bash
python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
```

6. Point a local MCP client at:

```text
http://127.0.0.1:8422/mcp
```

For public/remote hosting, follow [docs/deploy-gateway.md](docs/deploy-gateway.md) for persistence, TLS, trusted client origins, the single-replica requirement, release identity, and verification.

One athlete should have one current writer. If `GARMIN_COACH_LOOP_GATEWAY_URL` points at hosted, local store writes are blocked unless `--offline` explicitly means a separate local plan. Existing local state can be moved with [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md).

---

## Current capabilities and boundaries

The Coach currently maintains one 28-day direction; reconciles trustworthy planned → actual matches; keeps running and strength in one week; records athlete-reported profile, availability, goals, preferences, strength execution, body measurements, unrecorded activities, and subjective state; accepts request-scoped recovery readings; imports supported CSV, Apple Health XML, and FIT history with deterministic deduplication; attaches `coach_note` text to delivered sessions; reviews weekly progress; previews/commits plan changes; and previews/confirms/retries/replaces/withdraws product-owned calendar delivery.

Important boundaries:

- `startCoachSession` may reconcile completed work and therefore **may write a new PlanState version**. `getCoachState` is the strictly read-only stored-state path.
- Athlete-reported activity is evidence, never silently upgraded into provider-backed actual completion.
- Recovery numbers must be observed values, not guesses synthesized from prose.
- The product does not diagnose medical symptoms.
- Delivery proof stops at Intervals read-back.

---

## Data, export, and deletion

Hosted stores the product state required to maintain one isolated owner plan: PlanState versions, decisions/receipts, athlete-reported evidence, identity mapping, and unresolved delivery bookkeeping.

Exports deliberately exclude keyed OAuth credential **fingerprints**, raw provider payloads/**GPS** tracks, and the internal **owner id**.

Product deletion cannot remove three things outside the product boundary: workouts already written to the **Intervals.icu calendar**, the provider authorization granted under **Intervals.icu Settings**, and minimal platform **營運紀錄 / operational logs** that contain no plan, health, or identity content.

See [docs/account-lifecycle.md](docs/account-lifecycle.md) and the public [privacy policy](https://paceandstaystrong.com/privacy.html).

---

## Technical references

The current release exposes **22 MCP tools**, **2 prompts**, **31 CLI commands**, **3 JSON Schema contracts**, and **8 identity tables**.

- [User story](docs/user-story.md)
- [Data-source boundaries](docs/data-sources.md)
- [Client entry points](entrypoints/README.md)
- [MCP protocol, OAuth, and tool behavior](entrypoints/mcp/README.md)
- [Hosted deployment](docs/deploy-gateway.md)
- [Account lifecycle](docs/account-lifecycle.md)
- [Distribution/reviewer material](docs/distribution/README.md)
- [Release inventory](docs/release-inventory.md)
- [Repository invariants](AGENTS.md)

Long Run Hybrid Coach is independent and is not affiliated with, endorsed by, or sponsored by Garmin, Intervals.icu, Apple, or other device/platform vendors. Source code is released under the [MIT License](LICENSE).
