# OpenClaw entry

Packaging for OpenClaw and other agent-CLI-style MCP clients — issue #133. Same Coach
Gateway as every other entry ([`../mcp/README.md`](../mcp/README.md)), same canonical Skill
([`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/)); this file
is connection and listing metadata, not a second implementation.

## Connecting one athlete's own OpenClaw

The Coach Gateway is a remote Streamable HTTP MCP server; there is no local process to
spawn, so the entry is command-less. This section is the supported path: **one athlete,
their own OpenClaw, the hosted coach.** It needs no change on the deployment side.

Merge this into the OpenClaw configuration:

```json
{
  "mcp": {
    "servers": {
      "garmin-coach-loop": {
        "url": "https://mcp.paceandstaystrong.com/mcp",
        "transport": "streamable-http",
        "auth": "oauth",
        "oauth": {
          "identity": "shared"
        }
      }
    }
  }
}
```

Or from the CLI, which writes the same entry:

```bash
openclaw mcp set garmin-coach-loop '{"url":"https://mcp.paceandstaystrong.com/mcp","transport":"streamable-http","auth":"oauth","oauth":{"identity":"shared"}}'
openclaw mcp login garmin-coach-loop
```

`set` replaces that server's whole definition, so an existing installation keeps its other
filters and settings only if they are carried into the same object.

Every key is load-bearing, and the failure each one prevents is worth naming:

- **`auth: "oauth"`.** Without it OpenClaw presents no token and every call comes back
  `401` with the challenge [`../mcp/README.md`](../mcp/README.md) describes. There is no
  other way in: the gateway accepts the token it issued and nothing else, including a bare
  Intervals one.
- **`transport: "streamable-http"`.** A `type` of `"http"` normalises to the same value.
  `sse` is a transport this gateway does not serve.
- **`oauth.identity: "shared"`.** One authorization for the whole OpenClaw instance, which
  is what one person's own instance wants. It is also OpenClaw's default, so
  `openclaw mcp add --auth oauth` reaches the same behaviour — the value is written out
  anyway because a later reader otherwise cannot tell whether one account was chosen or
  merely inherited. Choosing it means the instance is that athlete's: whoever can talk to
  it reaches their plan and their Intervals calendar.

`openclaw mcp login garmin-coach-loop` opens the Intervals consent page and completes on a
loopback callback. **Loopback is verified by this gateway unconditionally, so a single
athlete's OpenClaw needs no `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` entry, no deployment
change, and no consent page of this gateway's own** — including on a remote VM, because what
is checked is the callback origin, not where the machine is. Where a browser cannot reach
that callback, the printed `openclaw mcp login garmin-coach-loop --code <code>` takes the
code out of band; handle it in the operator's own terminal, never in chat, an issue, or a
log.

Re-authorizing later is safe and disconnects nothing: earlier tokens are kept
deliberately, so this OpenClaw and, say, a claude.ai connector hold two tokens against one
store. Revoking at intervals.icu is the destructive one — authorization there is granted
per application per athlete, so taking it back signs *every* connected client out at once,
and each has to reconnect on its own. Taking access back from this side instead is
`revoke-connections` in [`../mcp/README.md`](../mcp/README.md).

Scope is deliberately absent. A client that names none is authorized for everything this
product declares; `--oauth-scope` can ask for less, and then the call that needed the
missing one refuses. The narrowing rule is in [`../mcp/README.md`](../mcp/README.md), and
the scopes themselves are in [`../../README.md`](../../README.md).

### Four different things, and only the last one is a working coach

A saved configuration, an operator login, a live probe and a real coaching turn are
separate evidence, and reporting an earlier one as a later one is how an onboarding gets
called done while the athlete still cannot train from it:

| Check | What it actually establishes |
| --- | --- |
| `openclaw mcp status --verbose` | the saved settings — **not** that any connection works |
| `openclaw mcp doctor --probe` | a live connection and tool discovery — not that coaching works |
| `startCoachSession` returns a plan | the token resolves to this athlete's owner store |
| a confirmed delivery, read back from Intervals | the whole path, which is the only release claim |

Configuration changes have to reach the running agent process; reloading the CLI is not
that proof. Record on issue #133 the installed client version, entry, identity mode and
the last stage that actually succeeded — never tokens, authorization codes or athlete data.

These key names were checked against OpenClaw's own
[MCP reference](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp.md) on
2026-09-07 — a documentation check, not a receipt for any row of that table. Confirm them
again if that reference has moved.

### One OpenClaw serving several people is not this round

OpenClaw supports `oauth.identity: "per-requester"`, which authorizes each message sender
separately instead of once for the instance. That is the right shape for a shared
messaging channel, and it is out of scope here: it is not the path being verified, and it
carries prerequisites this deployment has not accepted for anyone. Recorded so that
`shared` above is read as a choice rather than an oversight:

- Per-sender consent returns to `<gateway.publicOrigin>/oauth/mcp/callback` — OpenClaw's
  own public HTTPS origin, which is a separate key from the Coach `url`. Nothing requires
  the Coach deployment operator to trust that origin first any more: it registers on its
  own and proceeds directly to Intervals OAuth without a Coach page. A missing
  `gateway.publicOrigin` is an OpenClaw setup error, not an Intervals authorization
  failure.
- Operator `openclaw mcp login` does not connect those sender accounts, and a shared token
  is never the workaround for a per-sender failure — it would hand every participant one
  athlete's plan.
- Upstream describes the sign-in link as a single-use bearer link, so another participant
  who opens it binds their own account to the intended sender's slot.
- A browser Control UI is not automatically sender-bearing: upstream
  [issue #138113](https://github.com/openclaw/openclaw/issues/138113) reports missing
  requester identity on that path.

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

The Skill is not a prerequisite for reaching the coach: the gateway serves its
orchestration prompt to every connecting client, and a model that *fetched* that prompt
sequences correctly with no Skill installed. Serving is not delivery, though — MCP prompts
are user-controlled by specification, and the `instructions` field the same text is served
on is optional and unevenly implemented. Treat "the client has it" as something to verify
against a real connection. What the Skill adds that no server can: the trigger that makes
the entry discoverable in the first place, and an answer for the turn before a connection
exists.

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
