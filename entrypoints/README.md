# Entry points

One Coach Gateway, one PlanState per athlete, one canonical Agent Skill, one MCP tool
surface — every row below is packaging around that, not a second implementation.
[`mcp/README.md`](mcp/README.md) is the protocol-level reference most rows share; read it
once for the tool surface, the orchestration prompt, and OAuth, and treat everything else
here as setup mechanics layered on top of it. "Verified end-to-end" below means a real OAuth
authorization, a real coaching turn, and a real Intervals delivery have run against
production through that exact path; "packaged, awaiting real-connection verification" means
the setup steps exist and are held to the same contract tests as everything else here, but
that real run has not happened yet through this specific client.

| Channel | Entry | Status |
| --- | --- | --- |
| ChatGPT MCP connector | [`mcp/`](mcp/README.md) | Packaged, awaiting real-connection verification |
| claude.ai / Claude Desktop connector | [`claude/`](claude/README.md) | Verified end-to-end against production |
| Claude Code / Agent SDK Skill | [`claude/`](claude/README.md) | Packaged, awaiting real-connection verification |
| OpenClaw | [`openclaw/`](openclaw/README.md) | Packaged, awaiting real-connection verification |
| Smithery | [`mcp/`](mcp/README.md) | Verified end-to-end against production |
| Hermes Agent | [`mcp/`](mcp/README.md) | Packaged, awaiting real-connection verification |

## Not a channel: the public demo

[`demo/`](demo/README.md) is the Product Hunt playground, and it is the one thing under this
directory that is not packaging around the sentence at the top of this page. It has no
athlete, no PlanState, no provider connection and no write path: it reads one committed
synthetic fixture, reasons over it with the product's own contracts and projectors, and
stops at a preview. It deploys as a second Railway service from this same repository with
its own secret and its own limits, so a visitor hammering it cannot reach, slow or break a
connected athlete's plan.

Getting a channel *listed* rather than merely reachable — the metadata a directory asks for,
the reviewer's path, and the steps that happen in somebody's console — is
[`../docs/distribution/`](../docs/distribution/README.md).
