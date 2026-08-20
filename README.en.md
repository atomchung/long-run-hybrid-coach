# Long Run Hybrid Coach

[繁體中文](README.md) · **English** · [简体中文](README.zh-Hans.md)

Long Run Hybrid Coach is an independent, Intervals-first, device-agnostic hybrid training coach. It keeps one current 28-day direction and one executable running + strength week, reconciles completed training, and can deliver confirmed workouts to your Intervals.icu calendar.

Garmin is the first downstream device path we have dogfooded, **not a requirement**. Apple Watch, COROS, Polar, Suunto, Wahoo, other apps/watches, or no watch can still use the same coach as long as useful training evidence reaches Intervals.icu or is reported in the conversation.

> **Recommended path:** use the hosted MCP at `https://mcp.paceandstaystrong.com/mcp`. You need an Intervals.icu account, but you do **not** need to create an Intervals Developer App or run a server yourself.

## Quick start — hosted MCP

### Before you start

You need:

1. an **Intervals.icu account**;
2. a ChatGPT / Claude / other MCP client that can connect to a remote MCP server;
3. optionally, a watch or training app that already syncs activities into Intervals.icu.

You do **not** need a Garmin watch. You do **not** need to create a developer app for the hosted service.

### 1. Connect the hosted coach

Use this MCP endpoint:

```text
https://mcp.paceandstaystrong.com/mcp
```

- **ChatGPT:** if your account/workspace currently supports custom MCP apps/connectors, create or add one and use the endpoint above. Complete the OAuth prompt when ChatGPT scans or connects to the server. Availability and menu names can vary as ChatGPT MCP support rolls out; if manual custom MCP is unavailable to your account, use another MCP client or the public listing once it is published.
- **claude.ai / Claude Desktop:** Settings → Connectors → Add custom connector → paste the endpoint above.
- **Other MCP clients:** configure a remote Streamable HTTP MCP server with the same endpoint.

See [entrypoints/](entrypoints/README.md) for per-client packaging and current end-to-end verification status.

### 2. Authorize Intervals.icu

The client opens an Intervals.icu consent page. Sign in to **your Intervals.icu account** and grant the permissions the coach needs:

- `ACTIVITY:READ` — read completed training;
- `WELLNESS:READ` — read available wellness evidence;
- `CALENDAR:WRITE` — read/write the training calendar so confirmed workouts can be delivered and verified;
- `SETTINGS:WRITE` — read settings and, only through a confirmation-bound delivery flow, fill a missing supported running threshold setting when needed.

These are independently selectable on the Intervals consent screen. If one is omitted, the rest of the coach may still work while the dependent capability fails clearly. Reconnect and grant the missing permission rather than entering credentials into chat.

### 3. Start with a normal coaching question

No setup questionnaire is required. For example:

```text
Read my latest training evidence and tell me what I should do this week.
```

or:

```text
I want to improve my VO2max without losing strength. Build my first 28-day direction.
```

The coach reads what already exists first. It only asks for missing information that can materially change the decision, such as available days, equipment, or a strength baseline the provider cannot know.

### 4. Confirm the plan once

For a first plan, the coach shows a 28-day preview: the current week is executable and the following three weeks are an outlook. The plan is not written until you confirm that exact preview.

Later weekly changes use the same pattern: **before/after preview → one confirmation → apply**.

### 5. Deliver workouts when you want them on the calendar

Delivery is a separate confirmation:

**delivery preview → one confirmation → write to Intervals.icu → read back and verify**.

The strongest state this product can prove is `intervals_accepted`. It does **not** claim a workout has reached Garmin, Apple Watch, or another downstream device merely because Intervals accepted it. That last hop depends on your Intervals/device integration.

---

## Why Intervals.icu is part of the product

Intervals.icu is currently the interoperability hub between training devices/apps and the coach. It is **not** the coach's source of truth for the plan.

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

The responsibilities are deliberately split:

- **Intervals.icu** normalizes external training evidence and owns the provider calendar.
- **Long Run Hybrid Coach** owns the current PlanState, decision history, athlete-reported evidence, confirmation binding, and coaching workflow.
- **Your watch/app** may feed evidence into Intervals and may receive workouts from it, but downstream device delivery is a separate compatibility fact.

### What must already exist in Intervals?

Only the account is mandatory. The experience improves when activities/wellness are already syncing there, but missing optional evidence is treated as unknown rather than as zero or as a reason to block normal coaching.

If your device does not provide something important, you can report it in the conversation: strength sets and loads, availability, body measurements, an activity that was not recorded, subjective fatigue/sleep wording, or recovery readings you can see on your device.

---

## Hosted MCP vs local / self-hosted MCP

| | Hosted MCP — recommended | Local / self-hosted MCP |
| --- | --- | --- |
| MCP URL | `https://mcp.paceandstaystrong.com/mcp` | your own gateway, e.g. `http://127.0.0.1:8422/mcp` |
| Server maintenance | none | you run and maintain it |
| Intervals Developer App | **not required** | **required** — use your own OAuth app credentials |
| Canonical plan | hosted per-athlete store | your own gateway state root |
| Best for | normal users, multiple clients/devices | developers, privacy/control needs, offline/self-hosted workflows |
| ChatGPT | connect to the remote endpoint when your plan/workspace supports the required MCP actions | ChatGPT cannot directly reach localhost; expose it through a supported secure tunnel/HTTPS path if needed |

### Run your own MCP gateway

The repository is Python 3.11 and stdlib-only; there is no `pip install` step for the product itself.

1. Clone the repository and create an Intervals.icu OAuth application under Intervals settings / developer tools.
2. Register your gateway's provider callback as `<gateway-origin>/oauth/callback` in that Intervals app. A local-only setup normally uses a loopback origin; a remote client needs an HTTPS/tunnel path that can reach the gateway.
3. Set the four required gateway variables:

```bash
export GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT="$HOME/.local/share/long-run-hybrid-coach-gateway"
export GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)"
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID="..."
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET="..."
```

4. Start the gateway:

```bash
python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
```

5. Point a local MCP client at:

```text
http://127.0.0.1:8422/mcp
```

If you expose the gateway publicly, do not treat the loopback example as a production deployment guide. Use the persistent-volume, TLS, trusted-client-origin, single-replica, release-identity, and verification requirements in [docs/deploy-gateway.md](docs/deploy-gateway.md).

### Local CLI is not a second current plan

If this machine is configured to use the hosted coach with `GARMIN_COACH_LOOP_GATEWAY_URL`, local store writes are blocked unless `--offline` explicitly means “this is a separate local plan”. One athlete should have one current writer. Existing local state can be migrated and sealed using [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md).

---

## What the coach can do now

- Maintain one current **28-day direction**, with an executable current week plus a three-week outlook.
- Read Intervals activity/wellness/calendar evidence and reconcile confident planned → actual matches.
- Keep running and strength in one weekly plan.
- Record athlete-reported profile, availability, long-term goals, training preferences, strength execution, body measurements, missed-device activity summaries, and subjective state.
- Accept request-scoped recovery readings from a client or from values you read from a device; raw uploads/credentials are not persisted as provider data.
- Import supported history payloads with deterministic deduplication, including supported CSV, Apple Health XML content, and FIT payloads handled through the binary import path.
- Attach a coach note to a session so the coach's intent can travel with the delivered Intervals event without becoming a second workout grammar.
- Review weekly progress without equating “completed the plan” with “fitness improved”.
- Preview and confirm plan changes.
- Preview, confirm, retry, replace, or withdraw product-owned calendar delivery safely.
- Export the product-held data about the current owner from the conversation.
- Permanently delete product-held owner data through preview → confirmation → receipt.

### Important boundaries

- `startCoachSession` can reconcile completed work and therefore **may write a new PlanState version**. Use `getCoachState` when you need a strictly read-only stored-state check.
- Athlete-reported activity is evidence, but it is never silently upgraded into provider-backed actual completion.
- Recovery evidence is not invented from prose. “I feel exhausted” is stored as the athlete's words, not converted into a fake readiness score.
- The product does not diagnose medical symptoms.
- Delivery proof stops at Intervals read-back; it does not prove the workout reached a watch.

---

## Data ownership, export, and deletion

The hosted service stores the current/previous PlanState chain, decisions/receipts, athlete-reported evidence, identity mappings, and unresolved delivery bookkeeping needed to keep one owner isolated from another.

An export deliberately does **not** contain OAuth credential fingerprints, raw provider payloads/GPS tracks, or the internal owner id. A fingerprint is one-way bookkeeping, GPS/raw activity data belongs with the provider, and an owner id is an internal storage locator rather than athlete data that should be portable.

Deletion removes product-held owner data, but it cannot remove three things outside that boundary:

- workouts already written to the **Intervals.icu calendar**;
- the athlete's Intervals authorization under **Intervals.icu Settings**;
- minimal platform **operational logs / 營運紀錄** that contain no plan, health, or identity content.

See [docs/account-lifecycle.md](docs/account-lifecycle.md) and the public [privacy policy](https://paceandstaystrong.com/privacy.html).

---

## Product surface and technical references

The current release publishes **22 MCP tools**, **2 prompts**, **30 CLI commands**, **3 JSON Schema contracts**, and **5 identity tables**. The exact interface is derived and checked by tests so these counts cannot drift silently.

- Product/user story: [docs/user-story.md](docs/user-story.md)
- Data-source boundaries: [docs/data-sources.md](docs/data-sources.md)
- Client entry points: [entrypoints/](entrypoints/README.md)
- MCP protocol, OAuth, and tool behavior: [entrypoints/mcp/README.md](entrypoints/mcp/README.md)
- Hosted deployment: [docs/deploy-gateway.md](docs/deploy-gateway.md)
- Account lifecycle: [docs/account-lifecycle.md](docs/account-lifecycle.md)
- Distribution/reviewer material: [docs/distribution/](docs/distribution/README.md)
- Release inventory: [docs/release-inventory.md](docs/release-inventory.md)
- Repository invariants and verification: [AGENTS.md](AGENTS.md)

Long Run Hybrid Coach is an independent project and is not affiliated with, endorsed by, or sponsored by Garmin, Intervals.icu, Apple, or other device/platform vendors. Source code is released under the [MIT License](LICENSE).
