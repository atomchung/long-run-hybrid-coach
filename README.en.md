# Long Run Hybrid Coach

[繁體中文](README.md) · **English** · [简体中文](README.zh-Hans.md)

Website [paceandstaystrong.com](https://paceandstaystrong.com/) · Stuck? [Support](https://paceandstaystrong.com/support.html)

Paste one address into Claude or ChatGPT, and you have a coach that reads your real training.

It works from the activities and recovery data in your Intervals.icu account, keeps one current 28-day direction and one executable running + strength week, checks the plan against what you actually did, and puts each workout on your calendar — after you approve it. **Free, with no paid tier.**

**Garmin is not required.** Garmin is only the first downstream device path this project has verified on real hardware. Apple Watch, COROS, Polar, Suunto, Wahoo, other apps and watches, or no watch at all can use the same coach. The difference is how much trustworthy training data reaches the coach, and whether the sync path after Intervals has been verified separately.

> **Most people should use the hosted coach:** `https://mcp.paceandstaystrong.com/mcp`. You need an Intervals.icu account, but you do not need to request your own Intervals OAuth app or run a server.

---

## Connecting: two steps

The website walks the same flow as four clicks: [Get started](https://paceandstaystrong.com/#setup).

### What you need

1. **An Intervals.icu account.** Free, and Google sign-in creates one in about 30 seconds.
2. **Claude or ChatGPT.**
   - **Claude** (claude.ai / Claude Desktop): the free plan works, and a free account is limited to one custom connector. This path has completed a real production OAuth, coaching turn, and Intervals delivery.
   - **ChatGPT:** currently needs a Business, Enterprise, or Edu web workspace, where you create a custom app through Apps / developer mode. Custom MCP on a personal plan is read/fetch only, which cannot complete this coach's plan-write and delivery flow — on a personal plan, wait for this coach to appear in the ChatGPT directory. Check the latest limits in [OpenAI's own documentation](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt).
   - **Other MCP clients:** OpenClaw and self-hosted clients are [further down](#other-mcp-clients).
3. **Optional:** a watch or training app that already syncs activities into Intervals.icu. It works without one — anything the coach cannot read stays unknown rather than becoming zero.

### Step 1: paste the address into your AI

Add a remote MCP server in your AI's connector settings, using this address:

```text
https://mcp.paceandstaystrong.com/mcp
```

- **claude.ai / Claude Desktop:** Settings → Connectors → Add custom connector → paste the address.
- **ChatGPT:** Apps / developer mode → create a custom app → point it at the same address.

There is nothing else to fill in, and no key to paste.

### Step 2: authorize Intervals.icu

Your browser opens the Intervals.icu consent page. Sign in to **your own Intervals.icu account** and check all four permissions:

| Permission | What the coach does with it |
| --- | --- |
| `ACTIVITY:READ` | Read your completed training. |
| `WELLNESS:READ` | Read the recovery data Intervals holds. |
| `CALENDAR:WRITE` | Read and write the training calendar, so a workout you confirmed can be delivered and read back. |
| `SETTINGS:WRITE` | Read your threshold pace. The one thing it ever writes there is a threshold pace you **do not have yet**, during a delivery you already confirmed; one you already set is never overwritten. |

Intervals does offer a read-only settings scope; this asks for write because that one correction really does write. It is needed because without a threshold pace on the Intervals side, Intervals still accepts a paced workout and then strips the pace target on the way to your watch — you get the right distances with no targets. Skip any one of the four and the feature that needs it breaks in a way that is hard to trace back; reconnect and grant the missing permission.

**Do not paste an Intervals password, API key, or token into chat.**

### Is your Intervals.icu account new or empty?

The coach reads whatever is already in Intervals.icu. If the account is new, there are two ways to bring your history in:

- **Connect a device or app you already use.** In Intervals.icu's own Settings, connect Garmin or whatever watch or training app you have; past activity arrives automatically. Once it lands, ask the coach to read your training again.
- **Hand the coach an export.** Send it a CSV, an Apple Health export, or a `.fit` file in the conversation and ask it to import your history. The same file and the same activity are deduplicated automatically, and you are asked only when that cannot be decided. (The file goes to the coach, not into Intervals.icu.)

Anything your devices do not measure can be said in the conversation: the sets, loads and reps you actually lifted, the time and equipment you have this week, body weight and body fat, a session you did without a watch, "I'm wrecked" or "I slept badly", and the sleep, HRV, resting heart rate or readiness numbers you actually saw on your watch. The coach never turns "I feel exhausted" into a made-up readiness score.

---

## What happens once you are connected

### Just ask a normal coaching question

There is no setup questionnaire:

```text
Read my latest training and tell me what I should do this week.
```

or:

```text
I want to improve VO2max without losing strength. Build my first 28-day direction.
```

The coach reads what already exists first, then asks only for the gaps that would actually change the decision — available days, equipment, or a strength baseline no device can know.

### The first plan is a 28-day preview, and nothing is written until you approve

- **This week:** precise, executable sessions you can deliver.
- **The next three weeks:** a direction, without pretending every detail is knowable today.

Every later change works the same way: **before/after preview → one confirmation → apply**.

### Delivery is confirmed separately

**Delivery preview → one confirmation → write to Intervals.icu → read it back and verify.**

The furthest state this product can prove is that Intervals.icu accepted the workout. **An accepted Intervals workout is not proof that it reached Garmin, Apple Watch, or any other device** — the sync after Intervals is an external hop, verified per path.

---

## What Intervals.icu does in this product

Intervals.icu is the current **interoperability hub**: it collects activity and recovery data from different devices and apps for the coach to read, and receives the calendar workouts you confirmed. It is **not** where the plan itself lives.

```text
watch / training app
        │
        ▼
   Intervals.icu  ───── completed activity + recovery data ─────► Coach
        ▲                                                      │
        │                                                      │
        └──────── calendar workouts you confirmed ◄────────────┘
        │
        ▼
Garmin / Apple Watch bridge / other downstream sync
```

Responsibilities are deliberately separated:

- **Intervals.icu:** external training data, and the provider calendar.
- **Long Run Hybrid Coach:** the one current plan, the decision history, what you reported yourself, the binding of every confirmation, and the coaching workflow.
- **Your watch or app:** may send activity into Intervals and may receive workouts from it, but whether that last hop worked is a separate fact.

Only the account itself is mandatory. If activity and recovery data already sync into Intervals, the coach reads more automatically; missing fields stay unknown rather than becoming zero, and one absent optional number never blocks an ordinary coaching turn.

---

## Hosted vs self-hosted

One difference is the one you actually feel: **hosted works from your phone; self-hosted works only on the machine running the server.** Every row below is the cost of that.

| | Hosted — recommended | Self-hosted |
| --- | --- | --- |
| Works on your phone | Yes — connect any client with a mobile app | No, unless you expose your own server publicly and handle TLS |
| Address | `https://mcp.paceandstaystrong.com/mcp` | your own server, e.g. `http://127.0.0.1:8422/mcp` |
| Operations | managed for you | you start, update, back up, and operate it |
| Intervals OAuth app | **not required** | **required** — your own application credentials |
| Where the plan lives | hosted, scoped per person | your own server state root |
| Best for | normal users, several clients sharing one plan | developers, or anyone wanting a fully self-managed environment |

The hosted service handles dynamic client registration, PKCE, tokens, and per-person mapping. A normal user never needs an id, API key, client secret, or environment variable.

### Other MCP clients

- **OpenClaw:** point `openclaw mcp add` at the same address with `--auth oauth`. On an instance more than one person talks to, set the OAuth identity to per-requester, or everyone reaches one Intervals account. Setup is in [entrypoints/openclaw/](entrypoints/openclaw/README.md).
- **Anything else:** configure the same address as a remote Streamable HTTP MCP server. A client on your own machine, whose OAuth callback lands on loopback, connects as is; a client hosted elsewhere that takes the callback on its own domain is refused at registration until that origin is added to the deployment's trusted set. Details in [entrypoints/mcp/README.md](entrypoints/mcp/README.md).

Which entry points are verified end to end on real hardware, and which are packaged and waiting for a real connection, is in [entrypoints/](entrypoints/README.md).

### Running your own server

The repository uses Python 3.11 and is stdlib-only at runtime.

1. Clone the repository.
2. **Request an OAuth application from Intervals.icu.** Their public process is not self-service app creation in Settings: send the application details listed in [Intervals.icu OAuth support](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759). After Intervals creates it, **Manage App** appears in Settings and exposes the `client_id` and secret.
3. Register `<gateway-origin>/oauth/callback` as the provider callback. A local client may use loopback; a remote client needs reachable HTTPS or a secure tunnel.
4. Set the required variables:

```bash
export GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT="$HOME/.local/share/long-run-hybrid-coach-gateway"
export GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)"
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID="..."
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET="..."
```

5. Start it:

```bash
python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
```

6. Point a local MCP client at `http://127.0.0.1:8422/mcp`.

For public or remote hosting, do not treat the loopback example as a production runbook. Persistence, TLS, trusted client origins, the single-replica requirement, release identity, and verification are in [docs/deploy-gateway.md](docs/deploy-gateway.md).

### One person, one current plan

If `GARMIN_COACH_LOOP_GATEWAY_URL` points at the hosted coach, local writes are blocked unless `--offline` explicitly means a separate local plan. Existing local state can be moved across with [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md).

---

## What it can do today

- Maintain one **28-day direction**: precise sessions this week, a direction for the next three.
- Read Intervals activity, recovery data and calendar, and reconcile trustworthy planned → actual matches back into the current plan.
- Keep running and strength in one week.
- Record what you report yourself: profile, availability, long-term goals, training preferences, strength actually lifted, body measurements, activities no device recorded, and subjective state.
- Take the recovery readings you have on hand for that conversation. Hosted does not, and does not need to, read a health database on your computer.
- Import history: supported CSV, Apple Health XML, and FIT files, deduplicated deterministically and asked about only when that cannot be decided.
- Attach a coaching note to a delivered session, rather than quietly growing a second workout grammar.
- Review each week on what was actually trained, whether there is evidence of progress, and what comes next — instead of treating "the plan was completed" as fitness gained.
- Preview a plan change before applying it.
- Preview and confirm calendar delivery, with safe retry, replacement, and withdrawal of workouts this product owns.
- Export, or permanently delete in two stages, the data this product holds — from inside the conversation.

### Boundaries that matter

- Starting a coaching turn reconciles completed work and therefore **may write a new plan version**. `getCoachState` is the strictly read-only path to stored state.
- An activity you reported yourself is evidence; it is never silently upgraded into a device-backed completion.
- Recovery numbers must be observed values, never guesses synthesized from prose.
- This product does not diagnose.
- Delivery proof stops at the Intervals read-back, and never claims a watch has it.

---

## Data, export, and deletion

Hosted stores what maintaining one current plan requires: the plan's version chain, decisions and receipts, what you reported yourself, identity mapping, and unresolved delivery bookkeeping.

Exports deliberately exclude three things: the keyed **fingerprint** of an OAuth credential (a one-way bookkeeping value), raw provider payloads and **GPS** tracks (raw activity files belong to the provider), and the internal **owner id** (this product's own storage locator).

Deletion has three boundaries it cannot reach:

- workouts already written to the **Intervals.icu calendar**;
- the provider authorization you granted under **Intervals.icu Settings**;
- minimal platform **operational logs**, which contain no plan, health, or identity content.

The full lifecycle is in [docs/account-lifecycle.md](docs/account-lifecycle.md), and the public [privacy policy](https://paceandstaystrong.com/privacy.html).

---

## Current limits

- The coach does not sign in to Apple Health, Garmin Connect, or any other device account. The main automatic path is still Intervals.icu.
- Hosted does not permanently keep the recovery readings passed in with a request; give the current numbers again next time.
- This product does not observe every device-sync hop after Intervals, so it never reports "Intervals accepted it" as "your watch has it".
- Self-hosting is an operator and developer path; most people should use the hosted coach.
- Device compatibility is per-path evidence. Garmin being verified does not imply another device behaves the same.
- A remote client's OAuth callback origin is not open registration: loopback always works, claude.ai / claude.com / chatgpt.com are trusted by default, and a client on any other cloud host must be added to the trusted set by the operator first.
- This is one person's project, not a company. No uptime is promised, and it can change.

---

## When something goes wrong

- **[Support page](https://paceandstaystrong.com/support.html):** export, deletion, correction, revoking access — most of it you can do yourself inside the conversation, faster than anyone could do it for you. It also names the mailbox that reaches the developer directly.
- **[Issue tracker](https://github.com/atomchung/long-run-hybrid-coach/issues):** bugs and feature requests. It is public and permanent, so **never post** health, training, or plan content, an Intervals athlete id, a token, or any credential. To identify your own account, the opaque reference printed in your data export is enough.
- If you think you have found a security or privacy vulnerability, do not describe it publicly. Email it instead.

---

## Technical references

- [User story](docs/user-story.md)
- [User flows and the calls behind them](docs/user-flows.md)
- [Data-source boundaries](docs/data-sources.md)
- [Client entry points](entrypoints/README.md)
- [MCP protocol, OAuth, and tool behavior](entrypoints/mcp/README.md)
- [Hosted deployment](docs/deploy-gateway.md)
- [Account lifecycle](docs/account-lifecycle.md)
- [Distribution/reviewer material](docs/distribution/README.md)
- [Release inventory](docs/release-inventory.md)
- [Repository invariants](AGENTS.md)

The current release exposes **22 MCP tools**, **2 prompts**, **31 CLI commands**, **4 JSON Schema contracts**, and **9 identity tables**. Those counts are derived from the running code by a test, so this file cannot drift away from the product.

Long Run Hybrid Coach is independent and is not affiliated with, endorsed by, or sponsored by Garmin, Intervals.icu, Apple, or other device/platform vendors. Source code is released under the [MIT License](LICENSE).
