# OpenClaw entry

Packaging for OpenClaw and other agent-CLI-style MCP clients — issue #133. Same Coach
Gateway as every other entry ([`../mcp/README.md`](../mcp/README.md)), same canonical Skill
([`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/)); this file
is connection and listing metadata, not a second implementation.

## MCP server configuration

The gateway is a remote MCP server reached over HTTP — there is no local process to spawn,
so the entry is command-less. OpenClaw keeps servers under `mcp.servers.<name>`:

```json5
{
  mcp: {
    servers: {
      "garmin-coach-loop": {
        url: "https://mcp.paceandstaystrong.com/mcp",
        transport: "streamable-http",
        auth: "oauth",
        oauth: {
          identity: "per-requester"
        }
      }
    }
  }
}
```

The CLI writes the same entry:

```bash
openclaw mcp add garmin-coach-loop \
  --url https://mcp.paceandstaystrong.com/mcp \
  --transport streamable-http \
  --auth oauth
openclaw mcp login garmin-coach-loop
```

Every key above is load-bearing, and the failure each one prevents is worth naming:

- **`auth: "oauth"`.** Without it OpenClaw presents no token, and every call comes back
  `401` with the challenge [`../mcp/README.md`](../mcp/README.md) describes. There is no
  other way in: the gateway accepts the token it issued and nothing else, including a bare
  Intervals one.
- **`transport: "streamable-http"`.** A `type` of `"http"` normalises to the same value.
  `sse` is a transport this gateway does not serve.
- **`oauth.identity: "per-requester"`.** The default is `"shared"`, which authorizes once
  for the whole OpenClaw instance — so everyone talking to that instance reaches whichever
  account signed in first. `"shared"` is correct only where the instance serves exactly one
  person, which the messaging channels OpenClaw is usually reached through are not.

`openclaw mcp login` completes on a loopback callback, and loopback needs nothing added to
the deployment. An OpenClaw running where a browser cannot reach that callback — a server
behind a messaging channel, which is the common shape — takes the code out of band instead:
`openclaw mcp login garmin-coach-loop --code <code>`. `openclaw mcp status --verbose` and
`openclaw mcp doctor --probe` answer whether the connection is live without spending a
coaching turn to find out.

Scope is deliberately absent. A client that names none is authorized for everything this
product declares; `--oauth-scope` can ask for less, and then the call that needed the
missing one refuses. The narrowing rule is in [`../mcp/README.md`](../mcp/README.md), and
the scopes themselves are in [`../../README.md`](../../README.md).

What does not vary by client is documented once, also in
[`../mcp/README.md`](../mcp/README.md): discovery and authorization follow RFC 8414/RFC 9728
plus PKCE with no client secret, and a client's callback origin has to be trusted by the
deployment before registration succeeds. Loopback is always trusted, so an OpenClaw on the
athlete's own machine needs no deployment change; a hosted one needs its origin added
through `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` — "Admitting a new hosted client" in
[`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md).

These key names are OpenClaw's own configuration and MCP CLI reference, read 2026-08-20.
The same reference records what its client keeps per server — discovery metadata, a dynamic
registration secret, and the PKCE verifier — which is the shape this gateway requires, so
the two are expected to meet. Expected is not verified: no OpenClaw client has run this flow
yet. Confirm the key names again if that reference has moved since.

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
