# OpenAI plugin directory

An MCP-only plugin, submitted through the plugin portal on the OpenAI Platform and listed in
the universal directory ChatGPT and Codex share. The shared facts — identity, URLs, OAuth,
scopes, data flow, the tool table, the logo, the reviewer path, the eight test cases — are
in [`README.md`](README.md); this file is the field-by-field mapping and the checklist.

Requirements below are from the official submission pages
(`developers.openai.com/plugins/deploy/submission`,
`developers.openai.com/plugins/deploy/submission-errors`,
`developers.openai.com/plugins/build/mcp-server`, `developers.openai.com/plugins/build/auth`),
read 2026-08-18, plus the eligibility research recorded on issue #97.

## Shape

**MCP-only.** The skills half is not part of the first submission: the gateway already
serves the orchestration prompt to every connecting client, so a model reaches a coaching
turn correctly with no skill installed. The canonical Skill remains available for the Claude
Code and agent-CLI paths, and can be added to a later plugin version as a bundled skill
without changing anything server-side.

## Eligibility, established before any of this

- A verified **individual** developer identity on the OpenAI Platform is enough. A Business
  or Enterprise workspace is **not** a prerequisite for a public listing — that was the
  first thing issue #97 checked, because it was the thing most likely to stop the whole
  path.
- The submitter needs **Apps Management** write access (`api.apps.write`) in the
  organization that will publish, or organization ownership.
- A public MCP submission cannot come from an **EU data-residency** project. It has to be a
  global data-residency project. Check this before building a draft, not after.
- Review is gated: submitting starts a review, and publishing is a separate step the
  developer takes after approval.

## Field mapping

| Portal field | Value | Limit | Fits |
| --- | --- | --- | --- |
| Plugin name / display name | `Long Run Hybrid Coach` | 30 | yes, 21 |
| Short description (subtitle) | `Adaptive run and strength plan` | 30 | yes, 30 |
| Long description | the description in [`README.md`](README.md) | 4,000 | yes |
| Developer name | the verified individual identity — a personal project, never a company | 80 | operator |
| Category | `Healthcare` | one of thirteen | see below |
| Capabilities | the five below | 20 entries, 120 characters each | yes |
| Starter prompts | the three below | 3 entries, 128 characters each | yes |
| Website | `https://paceandstaystrong.com/` | HTTPS, 1,024 | yes, live |
| Support | `https://paceandstaystrong.com/support.html` | HTTPS, 1,024 | yes, live |
| Privacy policy | `https://paceandstaystrong.com/privacy.html` | HTTPS, 1,024 | yes, live |
| Terms | `https://paceandstaystrong.com/terms.html` | HTTPS, 1,024 | yes, live |
| Logo / composer icon | the square PNG in [`README.md`](README.md) | square, 48–4096 px, ≤5 MiB | yes |
| MCP server URL | `https://mcp.paceandstaystrong.com/mcp`, **Universal** | — | yes |
| Availability | operator's choice of countries | — | operator |

**Every field above is settled.** The subtitle was the last one open — it sat at 45
characters against a 30-character cap until 2026-08-18, and it was resolved by shortening
`short_description` itself rather than coining a platform-only variant beside it.

**Category.** The thirteen accepted values are `Productivity`, `Creativity`,
`Developer Tools`, `Business & Operations`, `Data & Analytics`, `Communication`,
`Education & Research`, `Security`, `Finance`, `Healthcare`, `Travel`, `Entertainment`,
`Other`. `Healthcare` is the closest fit for a training-planning tool, and the portal also
asks separately whether the connector handles personal health data — the answer to that is
yes, and the privacy policy says so. `Productivity` is the defensible alternative if the
health framing invites scrutiny the product does not want; it is an owner call, not a
technical one.

### Capabilities

1. Maintains one current 28-day running and strength direction from your Intervals.icu evidence
2. Reconciles what you actually trained against what was prescribed
3. Reviews a Monday-to-Sunday week without inventing a score
4. Previews every workout exactly before it reaches your calendar
5. Exports or permanently deletes everything held about you, from inside the conversation

### Starter prompts

1. `Read my latest training evidence and reassess my 28-day direction and this week's plan.`
2. `Review last week: what did I actually train, and am I making progress?`
3. `Show me exactly what Thursday's workout would look like on my calendar.`

The first is the `default_prompt` already declared in the packaging file.

## Auth, as the portal asks it

OAuth 2.1, authorization code, PKCE `S256`, dynamic client registration at
`/oauth/register`. Discovery is at `/.well-known/oauth-protected-resource` and
`/.well-known/oauth-authorization-server`, both also served under the path-aware spelling
with `/mcp` appended, and the `401` on `/mcp` carries the `WWW-Authenticate` challenge
naming the first of them. Verified values are in [`README.md`](README.md).

Two things to say plainly on the form:

- **Client ID Metadata Documents are not implemented.** The docs name CIMD as the preferred
  method and dynamic client registration as supported; this gateway supports registration
  and refuses any client id it did not seal itself.
- **No UserInfo endpoint, no `openid`/`email` scope.** Workspace domain restrictions need an
  authorization server that returns an `email` claim with `email_verified: true`; this one
  authenticates against Intervals.icu and never learns an email address. A workspace that
  wants domain-restricted access to this plugin cannot have it. That is a consequence of not
  collecting the athlete's email, and it is the right trade.

## Domain verification

The portal generates a token and fetches it from the MCP host itself. Nothing but this
service answers on `mcp.paceandstaystrong.com`, so the gateway serves it:
`GET /.well-known/openai-apps-challenge` returns the value of
`GARMIN_COACH_LOOP_OPENAI_APPS_CHALLENGE` verbatim as `text/plain`, and `404`s while that
variable is unset. The response is the token alone — the portal rejects JSON, a list, or a
second token from the same URL.

---

## Operator checklist

Ordered. Each step says what to paste and how to tell it worked. Nothing here is
automatable from this repository: every one of them is a console, an account, or a human
review.

1. **Prove the domain answers.** Every listing URL is on it, and a reviewer who opens a dead
   privacy policy is the single most likely way this gets rejected. This is the first step
   because nothing below is worth doing until it passes:

   ```bash
   for page in "" privacy.html terms.html support.html; do
     printf '%-14s ' "/$page"
     curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 "https://paceandstaystrong.com/$page"
   done
   ```

   Four `200`s and nothing else — the state since the domain went live on 2026-08-18. A
   `000` is the name not resolving; anything in the 300s means the redirect target is what
   a reviewer will actually read, so follow it and check that instead.
2. **Confirm the upstream authorization is open.** Already proven: a second Intervals
   account — the reviewer account — completed the consent screen and reached a coaching
   turn on 2026-08-18, so the application is grantable beyond the owner and there is
   nothing to ask the Intervals.icu maintainer for. Re-verify only if the application
   registration itself changed since.
3. **Roll production to `main`.** A draft is scanned against the live server, so submitting
   before this reviews a tool surface that no longer exists. Follow
   [`../ops/roll-with-railway-cli.md`](../ops/roll-with-railway-cli.md) — production
   predates the release-identity change, so seven release variables are staged before the
   ref, not six — then confirm `curl -s https://mcp.paceandstaystrong.com/readyz` reports
   `"status": "ok"` with a `source_git_commit` equal to `main`'s head. If the roll crossed
   a scope change, reconnect the owner and reviewer grants before submitting — the cutover
   section in [`README.md`](README.md) says why, and
   [`../ops/scope-change-costs.md`](../ops/scope-change-costs.md) is the standing record.
4. **Verify the developer identity.** OpenAI Platform → organization settings → individual
   verification. Individual is the right one: this is a personal project, and business
   verification would claim a company that does not exist. Worked when the plugin form's
   **Developer Identity** field offers it.
5. **Grant Apps Management.** Platform → organization → people → roles → set **Apps
   Management** to **Write** for the submitting account. Worked when
   `platform.openai.com/plugins` loads and offers **Create plugin**.
6. **Confirm the project is global data residency**, not EU. A public MCP submission from an
   EU-residency project is refused.
7. **Create the draft.** `platform.openai.com/plugins` → **Create plugin** → **With MCP**.
   Paste the field mapping above.
8. **Upload the logo.** Download it with the `curl` in [`README.md`](README.md) and upload
   the same file for both the logo and the composer icon.
9. **Complete domain verification.** Copy the token the portal shows, set
    `GARMIN_COACH_LOOP_OPENAI_APPS_CHALLENGE` to it in the Railway service variables,
    redeploy, then confirm
    `curl -s https://mcp.paceandstaystrong.com/.well-known/openai-apps-challenge` prints
    that exact token and nothing else. Click **Verify Domain**. Worked when the portal stops
    showing **Domain not verified**.
10. **Scan tools.** Select **Scan Tools** and check the discovered catalogue against the
    table in [`README.md`](README.md): 22 tools, each with a title and the three hints that
    table carries — read-only, destructive and open-world, the three this portal asks a
    justification for. The catalogue also serves `idempotentHint`, which the table leaves
    out because nothing here is reviewed against it; `EXPECTED_HINTS` in
    `tests/test_mcp_gateway.py` is where all four are pinned. Any tool flagged for a missing
    annotation is a server fix, a redeploy and a re-scan — never a portal edit.

    Expect the record and confirm tools to scan as destructive, as that table says they
    are. It is deliberate and it matches behaviour — the criterion is the specification's
    "only additive updates", not "deletes something", and the paragraph above the table
    gives it along with the test that holds every row to it. Annotations are part of the
    reviewed snapshot, so this is settled before submission rather than after.
11. **Prepare the reviewer account.** An Intervals.icu account meeting the four requirements
    in [`README.md`](README.md), with an initialized plan on it. Confirm sign-in needs no
    MFA, SMS or email step; a reviewer who cannot get in is a rejection.
12. **Record the demo.** The portal requires a demo-recording URL showing the main use cases
    and tools. Nothing in this repository produces one — record cases 1, 4 and 5 from
    [`README.md`](README.md) as a screen capture and host it at a public URL.
13. **Paste the test cases.** Exactly five positive and three negative, from
    [`README.md`](README.md), each with its prompt, expected behaviour and expected result
    shape.
14. **Choose availability**, complete the attestations, write release notes naming this as
    the initial submission, and **Submit for Review**.
15. **After approval, publish.** Approval and publication are separate; the listing appears
    in the directory only after the second step.
