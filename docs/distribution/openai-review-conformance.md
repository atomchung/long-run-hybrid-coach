# OpenAI review conformance

What OpenAI's plugin platform requires of a submission, quoted from its own pages in one place, so a
review round checks a fixed list instead of a reviewer re-reading `developers.openai.com` from memory.
Not the operator checklist — the field values and the paste-this-here steps stay in
[`openai-plugin.md`](openai-plugin.md); this file states only the requirement and whether this
repository already holds it. Read 2026-08-20; every path cited below is on `developers.openai.com`
unless another host is named.

## Tool responses and data minimization

"Remove unnecessary personal data, auth secrets, debug payloads, internal identifiers, and undisclosed
user-related fields from tool responses" (`/plugins/deploy/submission`). More specifically: "Do not
include diagnostic, telemetry, or internal identifiers—such as session IDs, trace IDs, request IDs,
timestamps, or logging metadata—unless they are strictly required to fulfill the user's query"
(`/apps-sdk/app-submission-guidelines`).

**Where this repo holds it:** `FORBIDDEN_KEYS` in `tests/test_mcp_output_contract.py` scans every
workflow tool result for the store's audit material (receipt ids, content-hash record ids, provider
event ids) and fails on a leak. `test_no_documented_step_takes_a_caller_supplied_owner_id` in
`tests/test_distribution_surface.py` checks every tool's input schema for an `owner` or `athlete_id` field.

## Tool annotations, as OpenAI defines them

`readOnlyHint` is "`true` only when the tool cannot change state"; `destructiveHint` is "`true` when a
tool can cause irreversible or difficult to reverse outcomes"; `openWorldHint` is "`true` when a tool
can affect public or external systems" (`/plugins/build/mcp-server`).

That last one is not the MCP specification's own definition. MCP 2025-06-18's schema, of
`openWorldHint`: If true, this tool may interact with an "open world" of external entities. If false,
the tool's domain of interaction is closed. For example, the world of a web search tool is open,
whereas that of a memory tool is not (`modelcontextprotocol.io/specification/2025-06-18/schema`). Read
literally, that would flag a plain read against an external system as open-world too; OpenAI's
definition above is write-scoped — it asks only whether a call can leave the provider account changed.

**Where this repo holds it:** the comment above `EXPECTED_HINTS` in `tests/test_mcp_gateway.py`, and
the `_hints()` docstring in `garmin_coach_loop/mcp_transport.py`, quote both definitions and say which
one wins — every provider-read tool answers `openWorldHint: false`; only `applyWorkoutDelivery`
answers `true`. `McpToolAnnotationTests`, same file, calls every tool for real and checks the claim
against behavior. `docs/distribution/README.md`, "The tool catalogue and its annotations," is the
human-readable table, asserted row-for-row by `tests/test_distribution_surface.py`.

## Schemas and the Scan Tools snapshot

Every tool needs "an explicit input schema" and "an output schema when the tool returns structured
data" (`/plugins/build/mcp-server`). A scan is a snapshot, not a live link: "Skills imported from MCP
are submission-time snapshots... After changing a skill, run **Scan Tools** again... and submit a new
plugin version" (same page); a stale scan blocks submission outright — `scan_required`: "MCP tools
must have a successful, current scan of the production MCP server" (`/plugins/deploy/submission-errors`).

**Where this repo holds it:** `input_schema` and `output_schema` are non-optional fields of the `Tool`
dataclass (`garmin_coach_loop/mcp_transport.py`); `tests/test_mcp_output_contract.py` asserts every
result's `structuredContent` conforms to the declared `outputSchema`. The skill-scan snapshot rule is
**not held anywhere** — skills are out of scope for the first submission (`openai-plugin.md`, "Shape").
Scan-matches-production is held: `tool_catalogue_sha256()` is bound into `release_id`
(`garmin_coach_loop/release_identity.py`, `gateway.py`) and rechecked at every `/readyz`.

## Server instructions

"Keep the most important details in the first 512 characters" of the server's `instructions` field
(`/plugins/build/mcp-server`) — a host may truncate or summarize the rest.

**Where this repo holds it:** nowhere mechanical yet. The served instructions are
`garmin_coach_loop/orchestration.md` (~7.6 KB); `tests/test_mcp_gateway.py` pins content equality and
that coaching prose stays out, but nothing checks that the opening 512 characters carry the
sequencing that matters most. Known gap, noted here rather than silently absent.

## Listing URLs and the privacy policy

"Plugin submissions must include a clear, published privacy policy explaining, at minimum, the
categories of personal data collected, the purposes of use, the categories of recipients, data
retention timelines, and any controls offered to your users" (`/apps-sdk/app-submission-guidelines`).
The four listing URLs "must use HTTPS and be at most 1,024 characters" (`/plugins/deploy/submission-errors`).

**Where this repo holds it:** `docs/distribution/README.md`, "Policy and contact URLs" — the four URLs
with a live-as-of date. Liveness is a manual step, not a test: `openai-plugin.md` operator checklist
step 1 is a `curl` probe run by hand; no CI job re-checks that these URLs still answer `200`.

## Auth requirements

The MCP authorization spec, as the auth page lists it: "Host protected resource metadata on your MCP
server," "Publish OAuth metadata from your authorization server," "Echo the `resource` parameter
throughout the OAuth flow," "Choose how the OpenAI host identifies or registers its OAuth client:
CIMD, DCR, or a predefined OAuth client," and "Publish the token endpoint authentication methods your
authorization server accepts" (`/plugins/build/auth`). For workspace domain restrictions specifically,
the same page adds:
"Advertise and enable the `openid` and `email` scopes" and "Advertise a UserInfo Endpoint that returns
the user's `email` claim and `email_verified: true`."

**Where this repo holds it:** `docs/distribution/README.md`, "Authorization" — OAuth 2.1, PKCE `S256`,
DCR at `/oauth/register`, no CIMD, no refresh tokens, verified live against production.
`McpDiscoveryTests` in `tests/test_mcp_gateway.py` asserts both discovery documents field by field.
Workspace-domain restriction is a **declared non-conformance**: `openai-plugin.md`, "Auth, as the
portal asks it," states there is no UserInfo endpoint and no `openid`/`email` scope, because this
deployment never learns an email address.

## Demo, test cases, and the reviewer account

A directory submission needs "exactly five positive test cases, three negative test cases, and
release notes," "a demo-recording URL that shows the main use cases and tools across supported
platforms," and "reviewer-ready demo credentials when the server uses OAuth"
(`/plugins/deploy/submission-errors`). Credentials must "complete each test without MFA, SMS, email
confirmation, or private-network access" (`/plugins/deploy/submission`).

**Where this repo holds it:** `docs/distribution/README.md`, "The reviewer's path" and "What a
reviewer test account requires" — no MFA/SMS/email step, populated history, threshold heart rate set,
a plan already initialized. Its "Test cases" section carries the five positive and three negative
cases, each with prompt, expected tool, and result shape. The demo-recording URL is **not held
anywhere**: `openai-plugin.md` step 12 says nothing here produces one; it is a manual step still open.

## Re-scan and resubmission

`scan_required`: "MCP tools must have a successful, current scan of the production MCP server"
(`/plugins/deploy/submission-errors`). "Plugins with MCP publish reviewed metadata and skill
snapshots. To change a snapshot, scan the MCP server, submit a new version for review, and publish the
approved version" (`/plugins/deploy/submission`).

**Where this repo holds it:** `tool_catalogue_sha256`, `skill_sha256`, and `instructions_sha256` are
bound into `release_id` (`garmin_coach_loop/release_identity.py`, `gateway.py`) and recomputed at
every `/readyz` — a changed tool title or a flipped hint moves `release_id`, and a stale deployment
reports `blocked`. Operator steps 3 (roll production to `main` before scanning) and 10 (re-scan, never
a portal-only edit) in `openai-plugin.md` are the procedural half.

## Which changes move the snapshot, and which do not

The rule above — re-scan, resubmit, republish — is expensive enough that the useful question
is not "what does it say" but "does this particular change trigger it". Answering that from
memory each time is how a listing quietly stops describing the server, so it is answered here
once, derived from what the identity hashes actually bind rather than from judgement.

`release_id` binds `git_commit`, `instructions_sha256`, `tool_catalogue_sha256`,
`skill_sha256`, `gateway_artifact_sha256` and the gateway domain. `configuration_binding`
binds the resolved state root, the Intervals client id, the environment, the instance id and
the token key. Anything not in one of those two lists moves neither, and `/readyz` reports
both — so the check is mechanical.

| Change | Moves the tool catalogue | Moves `release_id` | Needs re-scan and a new plugin version | Needs every grant to reconnect |
| --- | --- | --- | --- | --- |
| A trusted client origin added or removed | no | no | no | no |
| Documentation, README, this dossier | no | no | no | no |
| The canonical Skill | no | yes | no, while the submission is MCP-only | no |
| Gateway code that touches no tool | no | yes | no | no |
| A tool's name, title, description, schema or annotation | **yes** | yes | **yes** | no |
| A tool added or removed | **yes** | yes | **yes** | no |
| The orchestration prompt served at connect time | no | yes | takes effect immediately either way; whether it also needs a new version is not established — below | no |
| The scopes requested upstream | no | yes | no | **yes** — [`../ops/scope-change-costs.md`](../ops/scope-change-costs.md) |
| The gateway domain | no | yes | **yes**, and domain verification again | yes |
| Listing metadata: name, description, URLs, logo | no | no | a new version, but no re-scan | no |

Three of these are worth stating in words rather than leaving in a cell:

- **Whether a change takes effect and whether it needs resubmitting are different questions.**
  The served instructions are returned in the `initialize` response, so every client gets
  whatever the server sends at connect time — a snapshot cannot intercept that, and an edit is
  live for everyone the moment it deploys. What is *not* established is whether the reviewed
  version also has to move with it. So the risk in editing that text is never "the change did
  not apply"; it is "behaviour moved and the reviewed copy did not". Read the platform's own
  pages before assuming either answer, and treat the two as separable everywhere else in this
  table too.


- **A moved `release_id` is not by itself a resubmission.** It is this repository's own
  detector, and it is deliberately more sensitive than the platform's: it moves for a
  changed Skill or a rebuilt artifact, neither of which the platform snapshotted. Reading a
  moved `release_id` as "we must resubmit" would resubmit for nothing, and often.
- **The row that actually costs money is the tool row.** Everything a directory listing
  promises about behaviour is in the tool catalogue, so any change there makes the published
  snapshot wrong until a new version is approved. A change made for one directory's sake --
  an extra tool, a reworded description -- pays that cost on every directory at once.

## When this file goes stale

One read-date covers every quote above; they were all read in a single pass. The trigger to redo that
pass is a submission rejection citing a requirement not listed here, or a platform-doc change noticed
in passing — re-read the five pages, not memory, and update the quotes and the date together. The
how-to steps stay in [`openai-plugin.md`](openai-plugin.md); this file never grows a step, only a requirement.
