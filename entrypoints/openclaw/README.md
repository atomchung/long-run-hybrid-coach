# OpenClaw entry

Packaging for OpenClaw and other agent-CLI-style MCP clients — issue #133. Same Coach
Gateway as every other entry ([`../mcp/README.md`](../mcp/README.md)), same canonical Skill
([`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/)); this file
is connection and listing metadata, not a second implementation.

## MCP server configuration

The gateway is a remote, hosted MCP server reached over HTTP — there is no local process to
spawn, so any client config here is command-less. The shape most MCP-speaking agent CLIs
use for a remote server looks like:

```json
{
  "mcpServers": {
    "garmin-coach-loop": {
      "url": "https://<your-gateway-domain>/mcp",
      "transport": "http"
    }
  }
}
```

Confirm the exact key names against OpenClaw's own MCP configuration reference at setup
time — this snippet states the shape every remote entry in this repository shares, not a
verified OpenClaw schema, since the key naming is OpenClaw's to define. What does not vary
by client is already documented once, in [`../mcp/README.md`](../mcp/README.md): the URL is
the gateway's `/mcp` path, discovery and authorization follow RFC 8414/RFC 9728 plus PKCE
with no client secret involved, and a client's callback origin has to be trusted by the
deployment before registration succeeds — add OpenClaw's via
`GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` per "Admitting a new hosted client" in
[`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md) if it is not trusted yet.

## Installing the canonical Skill

OpenClaw skills are `SKILL.md` plus YAML frontmatter — the same shape as the Agent Skill
already at [`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/).
Point OpenClaw's skill loading at that directory — by reference, copy, or however OpenClaw's
own plugin sourcing (ClawHub, npm, git, a local directory) resolves a path — instead of
re-authoring its instructions. The installed copy is the canonical file, installed rather
than forked: a later change to `SKILL.md` or `references/hybrid-training.md` here is picked
up by re-syncing it, not by editing the installed copy.

## ClawHub

ClawHub is the intended listing channel for OpenClaw users to discover this Skill the way
they discover any other. Not listed yet — this records what a submission would carry, not a
claim that it is live:

- **Name**: the `display_name` already declared for the OpenAI entry in
  [`agents/openai.yaml`](../../.agents/skills/garmin-coach-loop/agents/openai.yaml) —
  "Long Run Hybrid Coach" — reused rather than renamed per platform.
- **Description**: that same file's `short_description`, unchanged for the same reason.
- **MCP URL**: the hosted `/mcp` endpoint above.
- **Skill payload**: [`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/)
  itself, referenced or copied at listing time — not forked for the submission.

Verifying the flow end to end — a real OAuth authorization, a real coaching turn, a real
Intervals delivery, all through an actual OpenClaw client — has not happened yet; see
[`../README.md`](../README.md) for current entry status.
