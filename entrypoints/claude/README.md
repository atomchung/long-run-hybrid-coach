# Claude entry

Packaging for the two ways Claude products reach this coach: claude.ai and Claude Desktop
through a custom connector, Claude Code and the Agent SDK through the canonical Skill. Both
sit on the same Coach Gateway [`../mcp/README.md`](../mcp/README.md) documents in full —
this file is setup mechanics on top of that, not a second description of what the product
does.

## claude.ai / Claude Desktop — custom connector

1. Settings → Connectors → **Add custom connector**.
2. Paste `https://mcp.paceandstaystrong.com/mcp` — the hosted gateway's MCP endpoint (a
   self-deployed gateway uses its own domain instead; see
   [`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md)).
3. Save, then authorize with Intervals when prompted.

What "authorize" does is the OAuth flow [`../mcp/README.md`](../mcp/README.md)'s
"Authorization" section documents in full: dynamic client registration, a PKCE-protected
authorize/token exchange, an access token scoped to this gateway alone. Nothing about it is
Claude-specific — claude.ai's origin is already trusted by this deployment, so nothing needs
configuring before the first connection attempt, unlike a platform whose origin still needs
adding (see "Admitting a new hosted client" in
[`../../docs/deploy-gateway.md`](../../docs/deploy-gateway.md)).

The orchestration prompt is not something to paste in anywhere here. A conforming MCP
client calls `prompts/list`, receives the one prompt this gateway serves
(`coach_orchestration`), and puts it in front of its model before the first coaching turn —
Claude does this automatically on connection. Contrast the Custom GPT entry
([`../custom-gpt/README.md`](../custom-gpt/README.md)), where the same file is pasted into
the Builder's Instructions field by hand; the connector path has no equivalent step.

## Claude Code / Agent SDK — the canonical Skill

[`.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/) is the one
Skill this product has. Claude Code and the Agent SDK read the same Agent Skills format
(`SKILL.md` plus `references/`) this repository already publishes it in, so packaging it
for Claude means installing that directory wherever a given Claude Code or Agent SDK setup
loads Skills from — a project's `.claude/skills/`, a personal `~/.claude/skills/`, or an
SDK-configured skills path — by copy or by reference. It is installed, not forked: the
file that lands there is the canonical one, and a later change to `SKILL.md` or
`references/hybrid-training.md` here is picked up by re-syncing that copy, not by editing
the installed one by hand.

The Skill carries the coaching judgment; it assumes an MCP connection already exists to
call the tools it refers to. Wire that up the same way as above — the hosted gateway, or a
loopback client on the athlete's own machine per
[`../mcp/README.md`](../mcp/README.md)'s "Connecting a client" section — in whatever
config surface that Claude Code or Agent SDK setup uses for MCP servers.
