# OpenClaw entry

Packaging for OpenClaw and other agent-CLI-style MCP clients — issue #133. Same Coach
Gateway as every other entry ([`../mcp/README.md`](../mcp/README.md)), same canonical Skill
([`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/)); this file
is connection and listing metadata, not a second implementation.

## MCP server configuration

The Coach Gateway is a remote Streamable HTTP MCP server, not a local command to spawn.
Choose who owns the OAuth credentials before installing it. A saved configuration, an
operator login, a sender login, and a successful coaching turn are different evidence.

### Separate accounts for channel senders

Use `per-requester` for sender-bearing messaging channels. Merge this example into the
OpenClaw configuration, replacing the example origin with the OpenClaw operator's own
reachable HTTPS origin. It is not the Coach server's origin.

```json
{
  "gateway": {
    "publicOrigin": "https://your-openclaw.example"
  },
  "mcp": {
    "servers": {
      "garmin-coach-loop": {
        "url": "https://mcp.paceandstaystrong.com/mcp",
        "transport": "streamable-http",
        "auth": "oauth",
        "oauth": {
          "identity": "per-requester"
        }
      }
    }
  }
}
```

The sender first requests a coach tool, follows the sign-in link, then retries after
consent returns to `<gateway.publicOrigin>/oauth/mcp/callback`. The Coach deployment
operator must trust that exact callback origin through
`GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` before registration can succeed; see
"Admitting a new hosted client" in [`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md).
Do not solve a rejected registration by disabling callback validation or sharing a token.

Operator login does not connect these sender accounts. Missing `gateway.publicOrigin`
is an OpenClaw setup error, not evidence that Intervals authorization failed. Upstream
also warns that sign-in links are single-use bearer links: another participant opening
one can bind their account to the intended sender. Do not offer this flow in an untrusted
shared channel; keep the sign-in handoff private.

A browser Control UI is not automatically a sender-bearing channel. Upstream
[issue #138113](https://github.com/openclaw/openclaw/issues/138113) reports missing
requester identity in that path. Verify the installed version and entry before claiming
support; falling back to shared credentials would defeat the account boundary.

### One person's private instance

Use `shared` only when the entire instance is restricted to that one athlete. For a new
single-user installation, this command declares the different identity explicitly:

```bash
openclaw mcp set garmin-coach-loop '{"url":"https://mcp.paceandstaystrong.com/mcp","transport":"streamable-http","auth":"oauth","oauth":{"identity":"shared"}}'
openclaw mcp login garmin-coach-loop
```

`set` replaces that server's definition; preserve any existing filters and settings when
updating an installation. Omitting `oauth.identity` also defaults to shared, so a bare
`mcp add --auth oauth` is not equivalent to the per-requester configuration above.

This operator-only login uses a loopback callback. On a VM or headless host where the
browser cannot reach it, the printed `openclaw mcp login garmin-coach-loop --code <code>`
is the manual fallback. Handle the code only in the trusted operator terminal, not chat,
issues, or logs. A remote machine using a loopback redirect needs no additional trusted
remote origin: the actual callback origin, not the machine's location, determines trust.
This fallback does not replace the per-requester callback flow.

### Connection checks and shared protocol rules

`openclaw mcp status --verbose` inspects saved settings; it does not prove a live
connection. `openclaw mcp doctor --probe` adds a live connection/tool-discovery check,
not a sender-specific coaching or delivery acceptance test. Configuration changes must
reach the actual running Gateway/agent process; a CLI-only reload is not that proof.

Keep `auth: "oauth"` and `transport: "streamable-http"` in either mode. The gateway
accepts its own issued credentials, not a bare Intervals token, and does not serve the
legacy SSE transport. Scope is deliberately absent: the narrowing rule is in
[`../mcp/README.md`](../mcp/README.md), and the declared scopes are in
[`../../README.md`](../../README.md).

Discovery, PKCE and callback trust are documented once in
[`../mcp/README.md`](../mcp/README.md). Authentication behavior was checked against the
[upstream MCP reference](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp.md)
on 2026-09-07. That is a documentation check, not an OpenClaw end-to-end receipt.
Record the installed client version, entry, identity mode, callback origin and last
successful stage on issue #133, without tokens, authorization codes or athlete data.
A release claim additionally needs a real coaching turn, exact confirmed delivery and
read-back, restart/re-authentication, and two-sender isolation when offering that mode.

## Hosted and local are one line apart here

OpenClaw is the entry where both deployment modes are the same entry with a different
`url`, because it can run on the athlete's own machine:

| | Hosted | Local |
| --- | --- | --- |
| `url` | `https://mcp.paceandstaystrong.com/mcp` | `http://127.0.0.1:8422/mcp` |
| Who runs the server | this project's deployment | the athlete, with `serve-gateway` |
| Intervals OAuth application | not needed | the athlete registers their own |
| Where the current plan lives | the hosted owner store | that gateway's own state root |

Everything else is unchanged: same `auth: "oauth"`, same discovery, same catalogue. OpenClaw
accepts plain HTTP for a localhost `url` and refuses it elsewhere, which is the line the
gateway already draws at registration.

They are a choice rather than a pair. One athlete has one current plan, so a local gateway
and the hosted one both pointed at one Intervals calendar is the divergence issue #40 is
about. Standing a local one up is the local section of [`../../README.md`](../../README.md);
what it needs before it is reachable by anything but loopback is
[`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md).

## Installing the canonical Skill

OpenClaw skills are `SKILL.md` plus YAML frontmatter — the same shape as the Agent Skill
already at [`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/),
and the same AgentSkills spec it is written to. Point OpenClaw's skill loading at that
directory — by reference, copy, or however OpenClaw's own plugin sourcing (ClawHub, npm,
git, a local directory) resolves a path — instead of re-authoring its instructions. The
installed copy is the canonical file, installed rather than forked: a later change to
`SKILL.md` here is picked up by re-syncing it, not by editing the installed copy. The
training judgment is served over the MCP connection rather than shipped in the Skill, so it
is current without a re-sync.

The Skill is not a prerequisite for coaching correctly. The gateway serves its orchestration
prompt to every connecting client, so a model that fetched that prompt sequences correctly
with no Skill installed; the Skill is what makes the entry discoverable and gives it a
trigger.

## ClawHub

ClawHub is the registry OpenClaw users install skills from, and the intended listing channel
for this one. What a submission carries, what has to change in the Skill before it can be
published, and the publish commands themselves are in
[`../../docs/distribution/openclaw-clawhub.md`](../../docs/distribution/openclaw-clawhub.md)
rather than here, for the reason every other listing fact lives there: a form field
restated in two files drifts in one of them.

Verifying the flow end to end — a real OAuth authorization, a real coaching turn, a real
Intervals delivery, all through an actual OpenClaw client — has not happened yet; see
[`../README.md`](../README.md) for current entry status.
