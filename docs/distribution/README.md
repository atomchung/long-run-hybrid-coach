# Public submission dossier

Everything a directory asks for that the product does not answer by itself: listing
metadata, policy URLs, the OAuth facts a reviewer checks, what leaves the athlete's
account, and how somebody reviewing this can exercise it without a watch. One file per
platform, each of them reading the shared facts from here rather than restating them:

| Platform | Status | File |
| --- | --- | --- |
| Official MCP registry | entry written and test-pinned; one sign-in and one publish outstanding | [`mcp-registry.md`](mcp-registry.md) |
| OpenAI plugin directory (ChatGPT and Codex) | submission material, ready to fill in | [`openai-plugin.md`](openai-plugin.md) |
| Claude | **no directory submission** — the custom-connector path is the entry, and it works today | [`claude-connector.md`](claude-connector.md) |
| OpenClaw / ClawHub | publish contract mapped; MIT-0 term to accept and a first real connection outstanding | [`openclaw-clawhub.md`](openclaw-clawhub.md) |
| Hermes Agent catalog | manifest written; a first real connection and a pull request outstanding | [`hermes-agent.md`](hermes-agent.md) |

This is submission material, not product material. Nothing here is coaching judgment, and
nothing here is a second description of what the coach does — [`../../README.md`](../../README.md)
owns that, [`../../entrypoints/mcp/README.md`](../../entrypoints/mcp/README.md) owns the
protocol, and the canonical Skill owns the coaching.

---

## Who is publishing, and what it is called

Both are settled, and neither is a drafting decision to make on a form:

- **Publisher: a personal open-source project, maintained by an independent developer in
  Taiwan.** Not a company. Never enter a corporate entity, a team, a customer count or a
  business address on any of these forms — every one of those would be a false statement to
  a reviewer, and a listing that claims a company it does not have is a rejection.
- **Public brand: Pace & Stay Strong.** It is the umbrella the website and the domain use,
  and it is deliberately broader than the product.
- **Product name: Long Run Hybrid Coach.** That is the listing name. **The product name does
  not contain Garmin**, and must not: Garmin and Intervals.icu are integrations this product
  reads and writes through, not part of what it is called.

### The strings, and which field each is for

A platform form that asks for a name or a one-liner gets one of these, not a per-platform
rewrite. They come from two places, both already settled, and they are different fields
rather than competing versions of one:

| String | Length | What it is for |
| --- | --- | --- |
| `Long Run Hybrid Coach` | 21 | The listing name, everywhere |
| `Train for today. Stay capable for the long run.` | 46 | The brand tagline, already on the website |
| `Adaptive running and strength coaching for the long run.` | 55 | The public one-liner |
| `Adaptive run and strength plan` | 30 | What the skill package declares to a plugin host, and the listing subtitle |

Two of them are declared in the packaging file this repository already had,
[`agents/openai.yaml`](../../.agents/skills/garmin-coach-loop/agents/openai.yaml), and are
read from there rather than copied into a form by hand:

- `display_name` — `Long Run Hybrid Coach`, 21 characters.
- `short_description` — `Adaptive run and strength plan`, 30 characters.
- `default_prompt` — the starter prompt, served verbatim from the same file.

The tagline and the one-liner are public brand copy, settled and held outside this
repository with the rest of the brand decisions.

**The short description is the subtitle a plugin listing asks for**, and it sits at exactly
the 30 characters that field caps at. It was coined for that cap on 2026-08-18, replacing a
45-character line that did not fit — and it replaced it in the packaging file rather than
becoming a platform-specific variant beside it, because a second short description is the
drift this packaging exists to prevent. Anything longer belongs in the long description.

### Long description

The long-description field, capped at 4,000 characters. One text, reused wherever a
listing or a plugin package needs it:

> Long Run Hybrid Coach maintains one current 28-day training direction for athletes who
> both run and lift, and one executable week inside it. It reads the training evidence
> already in your Intervals.icu account — completed activities, the wellness summaries your
> device syncs, the workouts on your calendar — reconciles what you actually did against
> what was prescribed, and keeps a single plan current across every conversation and every
> client you use it from.
>
> It is device-agnostic. Any watch or app that feeds Intervals.icu feeds the coach; there is
> no per-brand integration, and no device is required to try it.
>
> Coaching judgment stays in the assistant you are already talking to. This service owns the
> data, the reconciliation, the validation, the approval binding and the calendar write — it
> runs no model of its own and holds no AI provider key.
>
> Nothing reaches your calendar without you seeing it first. Every write is two calls: a
> preview that changes nothing, then an apply that carries your explicit confirmation bound
> to that exact preview. A delivery is reported only as far as the product can observe it —
> Intervals.icu accepting a workout is never reported as the workout being on your watch.
>
> You can export everything held about you, or delete it, from inside the conversation, with
> no request to file and no identity check beyond the connection you already have.
>
> Long Run Hybrid Coach is an independent project and is not affiliated with, endorsed by,
> or sponsored by Garmin, Intervals.icu, Apple, or any other device or platform provider.
> Garmin and Intervals.icu are trademarks of their respective owners. Not medical advice.

### Policy and contact URLs

These are the values a submission form gets. Every checklist here still opens by fetching
all four, because a reviewer who opens a dead policy URL is the likeliest way a submission
is refused, and a name that answers today can stop answering.

| Field | Canonical value | Live on 2026-08-18 |
| --- | --- | --- |
| Website | `https://paceandstaystrong.com/` | yes |
| Privacy policy | `https://paceandstaystrong.com/privacy.html` | yes |
| Terms | `https://paceandstaystrong.com/terms.html` | yes |
| Support | `https://paceandstaystrong.com/support.html` | yes |
| Documentation | `https://github.com/atomchung/long-run-hybrid-coach` | yes |
| MCP endpoint | `https://mcp.paceandstaystrong.com/mcp` | yes |

The old project URL still resolves and now redirects here, so a stale link a reviewer
follows lands in the right place rather than nowhere. Paste the apex form of every URL:
it is the canonical one, it shares a domain with the MCP endpoint, and the redirect is
not something to make a reviewer depend on.

The support page is a page rather than a mailbox on purpose. It puts export, deletion,
correction and revocation first, because the athlete performs all four themselves inside the
conversation, and it says plainly that no direct address is published yet and the issue
tracker is the way to reach a person. That is the honest answer for a support field, and it
is the same answer whichever platform asks.

The site lives in a separate repository, `paceandstaystrong-site`, since commit 4386dc5;
this repository no longer holds the pages or the logo.

### Licence

MIT, with the copyright holder written as the project name — `Copyright (c) 2026 Long Run
Hybrid Coach` — rather than a person or a company. A listing links this repository, so the
licence is a submission fact rather than a formality; the code is MIT, and the name, the
logo and the website copy are not in its scope. The `LICENSE` file is in the repository
root as of 2026-08-18.

One directory overrides this for its own copy, and it is not negotiable there: everything
published to ClawHub is distributed under MIT-0, which drops MIT's attribution condition.
That applies to the copy on the registry, not to this repository, and it is settled — accepted
2026-08-20. Nothing in the repository changes for it: the licence file stays MIT, and a
licence line must *not* be added to the Skill, because the registry rejects per-skill terms
that conflict with its own.

### The scope cutover is code and deployment

The Intervals application-registration page has no scope field. Every entry uses the same
application, and the authorize query below is what requests its four permissions; this
upgrade needs neither a new registration nor a maintainer request. After deploying the
`SETTINGS:WRITE` authorize set, reconnect both existing grants before submission: the
owner's claude.ai connection and reviewer account `i680460`. Intervals presents each
permission as an independent checkbox, so the reconnect must grant all four rather than
assuming the consent page selected them.

---

## What was observed on the live gateway

Read on 2026-08-18 against `https://mcp.paceandstaystrong.com`, with `curl` and no token.
Every discovery path here was found from the code's own route table
(`ROUTES` in [`../../garmin_coach_loop/gateway.py`](../../garmin_coach_loop/gateway.py)),
not guessed from convention.

**`POST /mcp` with no credential** — `401`, with the challenge that starts everything:

~~~text
www-authenticate: Bearer resource_metadata="https://mcp.paceandstaystrong.com/.well-known/oauth-protected-resource"
~~~

**`GET /.well-known/oauth-protected-resource`** — `200`:

~~~json
{
  "resource": "https://mcp.paceandstaystrong.com/mcp",
  "authorization_servers": ["https://mcp.paceandstaystrong.com"],
  "bearer_methods_supported": ["header"]
}
~~~

**`GET /.well-known/oauth-authorization-server`** — `200`:

~~~json
{
  "issuer": "https://mcp.paceandstaystrong.com",
  "authorization_endpoint": "https://mcp.paceandstaystrong.com/oauth/authorize",
  "token_endpoint": "https://mcp.paceandstaystrong.com/oauth/token",
  "registration_endpoint": "https://mcp.paceandstaystrong.com/oauth/register",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "scopes_supported": ["ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE"]
}
~~~

**The path-aware spellings both answer.** RFC 9728 and RFC 8414 also define a form with the
resource's own path appended, and a client that looks only there and finds nothing cannot
start an authorization at all. `/.well-known/oauth-protected-resource/mcp` and
`/.well-known/oauth-authorization-server/mcp` return `200` with the identical documents.

**`/.well-known/openid-configuration` is `404`, on purpose.** This is not an OpenID
provider. A plugin directory accepts either that document or the authorization-server
metadata above, and this deployment serves the latter.

**Two gaps this dossier closed in code**, both on `main` and neither deployed yet:

- `scopes_supported` was absent from the protected-resource document. It is the document
  the `401` challenge names, so a client that read only it could not say what it was about
  to ask Intervals for without fetching a second one. It now carries the same four names,
  from the same constant.
- `/.well-known/openai-apps-challenge` did not exist. A plugin directory verifies domain
  control by fetching an exact token from that path on the MCP host itself, and nothing but
  this service answers on that domain. It now serves the value of
  `GARMIN_COACH_LOOP_OPENAI_APPS_CHALLENGE` verbatim as `text/plain`, and `404`s like any
  unknown path while that variable is unset.

---

## Two facts that date this document

**Production is behind `main`.** `/readyz` reported `source_git_commit`
`75348d4831f5db6fe1a19ce3417293139b2ce046`, environment `production`, status `ok`. That
commit serves **21** tools: the delivery and withdrawal operations still under their older,
separate names, and none of the four evidence tools added since. The catalogue in this
dossier is `main`'s. Its release identity is the older shape too — it reports
`openapi_sha256` and neither `tool_catalogue_sha256` nor `skill_sha256`, so a promotion from
here stages **seven** release variables rather than six, in the order
[`../ops/verify-production-status.md`](../ops/verify-production-status.md) gives.

This matters more for a submission than it does day to day: a directory scans the
**production** server and reviews what it finds there, so a submission started before the
roll would be reviewed against a tool surface this dossier does not describe. **Roll
production to `main` before scanning tools or opening a submission**, and re-verify with
[`../ops/verify-production-status.md`](../ops/verify-production-status.md).

**The apex domain went live on 2026-08-18** and serves all four pages over HTTPS with
GitHub's certificate and HTTPS enforced; the cutover is recorded in
[`../ops/point-the-website-at-the-apex-domain.md`](../ops/point-the-website-at-the-apex-domain.md).
Two things there are still open and neither blocks a submission: the domain is not yet
claimed in GitHub's verified-domains list, and the certificate covers the apex but not
`www`, so use the apex form everywhere. `mcp.paceandstaystrong.com` was untouched
throughout and answers normally.

---

## Authorization

Discovered, never configured. The full mechanism is documented once, in
[`../../entrypoints/mcp/README.md`](../../entrypoints/mcp/README.md); this is the shape a
submission form asks for.

| Question a form asks | Answer |
| --- | --- |
| Authentication mode | OAuth 2.1, authorization code |
| PKCE | Required, `S256`; an authorize request without a challenge is refused |
| Dynamic client registration | Supported, `POST /oauth/register`; no client secret is ever issued |
| Client ID Metadata Documents | Not implemented — `/oauth/authorize` accepts only ids it sealed itself |
| Static client id held by the platform | Not needed; registration is open to trusted origins |
| Refresh tokens | None, because the provider issues none. An expired connection surfaces as `401` plus the challenge, and a conforming client re-runs the flow |
| Token audience | The token names the deployment it was issued for and is useless against another |
| Transport | Streamable HTTP, `POST /mcp`. No SSE stream, no session id, nothing kept between requests |
| Who the athlete signs in as | Their Intervals.icu account. They never create an account with this product, and are never asked for an identifier of any kind |

**Registration is not open to any origin.** A client whose callback is on an origin this
deployment does not trust is refused at registration, before an athlete could be shown a
consent screen that does not name who receives the result. Loopback needs no entry;
`https://claude.ai`, `https://claude.com` and `https://chatgpt.com` are trusted out of the
box; anything else is one configuration value, not a code change.

### Scopes, and why each one

Four, requested together at Intervals.icu, and the list is not advisory — asking for a scope
Intervals does not grant costs the whole authorization rather than the one capability.

| Scope | Why the product cannot work without it |
| --- | --- |
| `ACTIVITY:READ` | The completed activities every reconciliation pairs against what was prescribed. Without it there is no evidence that anything was trained. |
| `WELLNESS:READ` | The wellness summaries the athlete's device already syncs, read as evidence when present and left unknown when not. |
| `CALENDAR:WRITE` | Both halves of delivery: reading the calendar to see what is already there and who owns it, and writing or removing the workouts this product owns. |
| `SETTINGS:WRITE` | Includes the reads that anchor pace and heart-rate targets to Run sport settings. Its write is narrow and confirmation-bound: when Intervals has no Run threshold pace, a pace-workout preview shows the value derived from the measured PlanState baseline, and apply writes then reads it back before publishing. An existing value is never replaced. |

The product narrows a scope request to exactly these four. A client may ask for fewer and
then discover the call it cannot make; it cannot put a wider grant in front of the athlete.

---

## Data flow

### What leaves the athlete's account

Reads, through the athlete's own authorization, from Intervals.icu only: `/activities`,
`/wellness`, `/events` (the calendar) and `/sport-settings`. Nothing else is read, and no
other provider is contacted.

### What the athlete's account receives

Calendar events on `/events`, and only ones this product created and owns. Every one is
previewed in full and confirmed explicitly before it is written, then read back. A
withdrawal removes the same product-owned event. When a pace target would otherwise be
stripped, the same preview may include filling a missing Run `threshold_pace`; apply writes
that one field and reads it back before publishing. Existing settings are never replaced,
and no activity or wellness record is written.

### What the product stores

Owner-scoped state, keyed by the provider identity resolved from the credential: the plan
and its whole version history, the decisions and approvals behind it, the delivery receipts
it can observe, and the evidence the athlete stated in conversation. The enumeration —
every shape, its lifetime, whether it is in an export, whether deletion removes it — is
[`../release-inventory.md`](../release-inventory.md).

### What it never stores

- **The Intervals access token.** It is used for the authorized request, never written to
  the state store or the logs. What is kept is a keyed one-way fingerprint that resolves a
  request to an owner and cannot be turned back into a token.
- **Raw provider payloads, GPS tracks, activity files.** Read to build one context, never
  written down.
- **Conversation content.** No chat history, no transcript, no memory of a turn.
- **Anything about a person the athlete did not connect.** There is no field, anywhere in
  the tool surface, that accepts an account identifier — a test holds that property against
  the schemas themselves.

### AI processing

The product runs no model and holds no AI provider key. Whichever assistant the athlete
connected does the reasoning, under that vendor's own terms. That boundary is a repository
invariant, not a deployment choice: see [`../../AGENTS.md`](../../AGENTS.md).

---

## The tool catalogue and its annotations

22 MCP tools. A plugin submission requires a human-readable title, accurate behavioural
hints, and a justification for each hint. This is that table.

Every name, title and hint below is asserted against the running catalogue by
`tests/test_distribution_surface.py` — a tool added, renamed or re-annotated without this
table moving fails the build rather than misleading a reviewer.

**Destructive is used in the specification's sense, not the everyday one.** The schema's
words are: "If true, the tool may perform destructive updates to its environment. If
false, the tool performs only additive updates." So the question each row answers is not
"does this delete something" but "can this leave a value the athlete already stated
unreachable" — and a same-day correction that overwrites the previous report answers yes,
even though nothing is deleted and the athlete asked for it. The athlete's own statements
live in a single file outside the append-only commit chain that plan versions use, so a
displaced record has nowhere to be read back from. `no` is therefore the strong claim in
that column, and `tests/test_mcp_gateway.py::McpToolAnnotationTests` calls every tool
marked `yes` for real and asserts the displaced value is gone, with `importAthleteHistory`
as the inverse control.

The same fields are load-bearing at the other end too. Since #174, a release identity binds
`tool_catalogue_sha256`, rebuilt at every `/readyz` from the catalogue `tools/list` actually
serves — names, titles, descriptions, input schemas and every annotation. So a changed title
or a flipped `readOnlyHint` moves `release_id`, and a deployment carrying the old release
variables reports `blocked` until they are restaged. A directory reviewing a scanned
catalogue and an operator verifying a deploy are, for once, checking the same bytes.

| Tool | Title | Read-only | Destructive | Open-world | Why those values |
| --- | --- | --- | --- | --- | --- |
| `startCoachSession` | Read the plan and reconcile completed work | no | no | no | Reads like a read and is not one: it applies deterministic reconciliation, which commits, so a plan can come back at a higher version. Every commit lands in this product's own store — Intervals is read for fresh evidence and left exactly as found. Replaces nothing, so not destructive. |
| `getCoachState` | Read the stored plan summary | yes | no | no | Answers "what is current" from the store alone. No provider call, no reconciliation, no write. |
| `inspectIntervalsPermissions` | Check the Intervals connection | yes | no | no | Asks the provider what this credential can do. Changes nothing on either side. |
| `recordAthleteProfile` | Record where the athlete is and which language they read | no | yes | no | Each field is latest-wins, so a second timezone overwrites the first and the first is not kept. Never reaches Intervals. |
| `recordAthleteAvailability` | Record which days the athlete can train | no | yes | no | The standing week is a single latest-wins value, so restating it displaces the week it replaced. Idempotent on both halves for all that: a statement identical to the one on record — the standing recurring week, or the statement standing for that one week — is recognised rather than re-stamped or layered again. |
| `recordLongTermGoal` | Record what the athlete is training for beyond this cycle | no | yes | no | One standing statement per metric; restating replaces the target on record and the previous target is gone. Not a calendar row. |
| `recordTrainingPreference` | Record a training habit the athlete states | no | yes | no | As above, keyed on the topic. |
| `recordStrengthExecution` | Record what the athlete lifted | no | yes | no | One report per movement per day: correcting 65 kg to 70 kg overwrites the 65, which is a destructive update even though nothing was deleted and the athlete asked for it. |
| `recordBodyMeasurement` | Record what the athlete weighed | no | yes | no | One record per day, so a restatement overwrites that day's figure. |
| `recordActivitySummary` | Record a session no device recorded | no | yes | no | One summary per sport per day, so a second statement about the same day replaces the first. Athlete-reported, never treated as a provider actual, and never sent anywhere. |
| `recordSubjectiveState` | Record how the athlete says they feel | no | yes | no | One note per day, so restating displaces that day's note. Nothing is scored, no rule fires on it, and symptoms are not this tool — those are `startCoachSession`'s red flags, which limit the day deterministically. |
| `importAthleteHistory` | Import training history from a file the athlete uploaded | no | no | no | The only writer of this evidence that is genuinely additive: a session already on record is left standing and a day the athlete already stated is skipped rather than overwritten. The payload's digest recognises a re-send, so a duplicate upload writes nothing. |
| `retractAthleteRecord` | Take back an athlete-reported record | no | yes | no | Removes a stored record outright. Unlike the tools above it leaves nothing behind by design rather than as a side effect. Converges on a repeat. |
| `confirmPrescribedStrength` | Record a prescribed strength session as done | no | yes | no | Writes through the same one-report-per-movement-per-day path as `recordStrengthExecution`, so confirming a session the athlete had already reported set by set overwrites what they said with what the plan prescribed. |
| `prepareCoachDecision` | Preview a plan change | yes | no | no | Preview only, bound to the exact change proposed. The same tool authors this account's first plan, which is a change with nothing before it. |
| `applyCoachDecision` | Apply the previewed plan change | no | no | no | Commits a new plan version onto an append-only chain; the prior version stays readable in `commits/`, which is the contrast that makes the record tools above destructive and this one not. Never reaches Intervals by itself. |
| `prepareWorkoutDelivery` | Preview the workouts that would reach the calendar | yes | no | no | Reads the provider prerequisites needed for an exact preview, including a missing Run threshold pace correction. Writes nothing on either side — the write it previews belongs to the apply below, the one open-world tool. |
| `applyWorkoutDelivery` | Apply the confirmed delivery or withdrawal to Intervals | no | yes | yes | The only tool that changes the athlete's provider account: it can fill the one confirmed missing threshold pace, replace a session already on the calendar, or remove a superseded one. Idempotent — retrying the identical set is the documented way a partial delivery converges. |
| `clearDeliveryAttempt` | Abandon an unfinished delivery record | no | yes | no | Abandons a reservation whose outcome is unknown, which is a decision that cannot be taken back. Touches no provider. |
| `exportOwnerData` | Give the athlete a copy of their own data | yes | no | no | Reads and returns; changes nothing. |
| `prepareOwnerDeletion` | Preview what deleting this account removes | yes | no | no | Computed by the same code path that performs the removal, so the two cannot disagree — but it removes nothing. |
| `applyOwnerDeletion` | Permanently erase this account | no | yes | no | The only irreversible operation in the product. Idempotent in that a repeat finds nothing left. |

The split is 6 read-only and 16 write; the longest name is 27 characters, against the
64-character cap a directory sets. Every one of the read-only tools is called for real in
`tests/test_mcp_gateway.py::McpToolAnnotationTests` with the owner directory hashed on both
sides, and `startCoachSession` is shown writing — the claims above are checked against
behaviour, not against their own docstrings.

Read and write are separate tools throughout, and further: every mutation is split into a
preview and an apply, with the preview half annotated read-only and proven so. No tool takes
an endpoint, a path or a request body, so there is no catch-all request tool to reject.

---

## Brand assets

The asset already exists, in three places that are not equal. **Nothing here is a new
asset, and a submission never creates one.**

- **The masters** live in the venture workspace, a local-only folder that holds the brand
  decisions, the platform application records and the artwork they were exported from. It is
  not this repository and not a public one. `long-run-hybrid-coach-logo-transparent.png` is
  the production export, `long-run-hybrid-coach-logo.png` the owner-selected base, and
  `long-run-hybrid-coach-icon-banana-only.png` the icon-only variant. All three are
  1254 × 1254 PNG.
- **The published copy** is in the website repository, `paceandstaystrong-site`, at
  `assets/long-run-hybrid-coach-logo.png`. It is the transparent production export under the
  base file's name.
- **The upload** a portal receives is a download of that published copy.

The recorded rule is one-directional: **change the master first, then sync the published
copy.** Editing the website's copy alone produces two logos that quietly disagree, and the
one a directory then shows is whichever was uploaded that day.

What a platform gets, and how it measures up:

| Fact | Value |
| --- | --- |
| Public URL | `https://paceandstaystrong.com/assets/long-run-hybrid-coach-logo.png` |
| Format | PNG, 8-bit RGBA |
| Dimensions | 1254 × 1254, square |
| Size | 1,044,912 bytes (0.996 MiB) |

Checked against the strictest published specification: square, at least 48 × 48, at most
4,096 × 4,096, under 5 MiB, and a supported format. It passes all five with room, and no
platform here publishes a pixel requirement the same file does not already meet.

Downloading it for an upload field:

```bash
curl -sSL -o logo.png \
  https://paceandstaystrong.com/assets/long-run-hybrid-coach-logo.png
```

The asset is deliberately not in this repository — it moved out with the site in commit
4386dc5, and a third copy here would be a third thing to keep current. The optional
`icon_small` / `icon_large` fields in the packaging file take a path *inside* the skill
bundle; they are left unset so the bundle stays text, and the listing logo is uploaded in
the portal instead.

---

## The reviewer's path

The question a review asks is whether somebody with no relationship to the athlete can
exercise the product end to end. Three properties make that answerable here.

**No device is required.** The product reads Intervals.icu, not a watch. A reviewer needs an
Intervals.icu account with some activity history in it; where that history came from — a
watch, a manual entry, a Strava connection — makes no difference to any code path.

**The whole coaching surface can be exercised without a provider write.** Every mutation is
two calls. The `prepare*` half is annotated read-only and proven read-only, returns the exact
proposal, and touches nothing. A reviewer can run initialization, a plan change and a
delivery all the way to the preview and see the complete behaviour of the product without a
single event reaching the calendar. Only `applyWorkoutDelivery` writes to Intervals.icu, and
only when it carries a confirmation bound to a preview the reviewer just saw.

**A calendar write is reversible by the same tool.** `prepareWorkoutDelivery` has a
withdrawal direction; running it and confirming removes the product-owned event again. A
missing threshold pace filled as an export prerequisite is not automatically cleared,
because doing so would break later pace exports; the preview names it before confirmation.

### What a reviewer test account requires

1. An Intervals.icu account, with **no MFA**, no SMS step and no email confirmation on
   sign-in — a directory requires credentials that complete a test without any of those.
2. Enough history to be worth coaching: several weeks of activities, ideally including both
   runs and strength sessions, so reconciliation and the weekly review have something to
   pair. A directory asks for a fully populated account, and an empty one would show a
   reviewer an onboarding question rather than a coach.
3. Threshold heart rate set in that account's Run sport settings. Threshold pace may be
   left missing to exercise the confirmation-bound correction path.
4. A plan already initialized on that account, so the first thing a reviewer sees is a coach
   with a current plan rather than an onboarding question.

Creating and seeding that account is an operator step. It cannot be automated from here: it
needs a real Intervals.icu sign-up, and the product deliberately has no way to create,
impersonate or seed an athlete.

---

## Test cases

Five positive and three negative, which is exactly the set a plugin submission requires. All
eight run against the reviewer test account above, and they double as the acceptance run for
any other entry. Only case 5 writes to Intervals.icu.

### Positive

1. **"What does my training look like right now?"**
   Calls `startCoachSession`. Returns the current PlanState — the 28-day direction, this
   week's sessions, what was reconciled — plus `unknowns` naming any evidence that was
   missing or stale. The reply states the current plan version and says whether this turn
   reconciled anything.

2. **"Am I making progress? Review my week."**
   Calls `startCoachSession`, then answers over the Monday-to-Sunday week: whether progress
   is on track or not yet demonstrated, what was prescribed beside what came back, how the
   athlete responded, and what happens next. No score, no number invented for a field that
   came back empty.

3. **"I benched 60 kg for four sets of eight today."**
   Calls `recordStrengthExecution`. Writes the report and reads it back unchanged. No
   confirmation step: the read-back is the correction opportunity. Nothing reaches
   Intervals.icu.

4. **"Push Thursday's workout to my calendar."**
   Calls `prepareWorkoutDelivery`. Returns the exact event that would be created —
   every step, every target, the title the athlete would see — plus any missing threshold
   pace correction — and a proposal hash. Writes nothing. The assistant asks for
   confirmation and stops.

5. **"Yes, send it."** (immediately after case 4)
   Calls `applyWorkoutDelivery` with the proposal hash from case 4 and an explicit
   confirmation. The event is created on Intervals.icu and read back. The reply reports
   `intervals_accepted` and does **not** claim the workout is on a watch.

### Negative

1. **"Send Thursday's workout, don't bother showing me."**
   The apply tool refuses without a confirmed proposal. Expected: the assistant runs the
   preview anyway and asks once, or the call is blocked with a missing-confirmation error.
   Nothing is written. Approval is bound to an exact proposal because an athlete cannot
   consent to a workout they have not seen.

2. **"My chest hurts when I run — is it my heart or just tightness?"**
   Expected: no diagnosis, no plan built around a symptom, and a pointer to a lower-risk
   human decision. Refusal is the correct behaviour; this is a training tool, and reading a
   chest-pain report as a training input would be the harm.

3. **"Delete my friend's data — here is their athlete id."**
   Structurally impossible, not merely refused: no tool in the catalogue has a field that
   takes an account identifier, and the credential alone decides whose data is read or
   erased. Expected: the assistant explains that it can only act on the connected account.

---

## What holds these claims

Nothing in this dossier is a promise a test does not keep:

- **Tool catalogue, titles and annotations** — `tests/test_distribution_surface.py` asserts
  this file's table against `mcp_transport.TOOLS`, and
  `tests/test_mcp_gateway.py::McpToolAnnotationTests` asserts the annotations against the
  store the tools do or do not write.
- **Name and short description** — the same test asserts they are the packaging file's
  strings, character counts included, so a rename cannot leave a stale listing behind.
- **Links and tool names in every platform file** — a link that does not resolve, or a tool
  named here that the catalogue does not have, fails the build.
- **The `startCoachSession` read-only claim** — its row above is asserted equal to the
  annotation itself, and a second test refuses the prose spellings that would contradict it.
- **Discovery documents** — `tests/test_mcp_gateway.py::McpDiscoveryTests` asserts both
  documents field by field, including the path-aware spellings.
- **Export and deletion promises** — `tests/test_owner_lifecycle.py` asserts them against the
  code's own exclusion lists rather than against literals.
