# The OpenAPI surface (and the retired Custom GPT entry)

`openapi.yaml` in this directory is the HTTP contract for every coach operation the
gateway serves: one operation per MCP tool, named identically, over the same
`/v1/coach/*` routes and the same `/oauth/intervals/*` authorization. It is maintained,
tested against `garmin_coach_loop.gateway.ROUTES` on every commit, and is what an OpenAI
plugin-style integration is generated from.

**The Custom GPT entry it was originally written for is no longer maintained.** The
distribution mainline is the canonical Agent Skill plus the hosted MCP endpoint, reached
through [`../mcp/README.md`](../mcp/README.md) — that is the path claude.ai already runs
on and the one ChatGPT's own connector uses. A Custom GPT built by pasting an Action
schema and an instructions block into the GPT Builder is a fourth copy of a contract that
already exists in three places, and keeping it in step was its own release ritual: a
Vercel reverse proxy, a Builder attestation step, and a state machine to drive them. That
ritual is gone, along with the scripts and the operator Skill that ran it.

What did *not* go:

- **This document.** The plugin surface needs it, and the contract tests hold it to the
  gateway's real routes and responses.
- **`/oauth/intervals/authorize` and `/oauth/intervals/token`.** They are this document's
  own `securitySchemes`, not a compatibility shim: a configured Action cannot re-run a
  discovery document, so the OpenAPI surface authorizes through a fixed pair of routes
  where the MCP surface uses dynamic registration and PKCE.
- **The release identity.** `scripts/release_bundle.py` and `/readyz` prove that the code,
  the orchestration prompt and this document serving live traffic are the ones a given
  commit declared. That was never about any one client; it is what every deploy is
  verified against. See [`docs/ops/verify-production-status.md`](../../docs/ops/verify-production-status.md).

An existing Custom GPT that someone already built will keep working for as long as the
routes above do — nothing was removed from the gateway. It simply is not something this
repository keeps in step, documents a build procedure for, or tests a release path
against.

## Setting one up anyway

Point an Action at this document, set the OAuth client credentials against
`/oauth/intervals/authorize` and `/oauth/intervals/token` on your gateway's domain, and
paste `garmin_coach_loop/orchestration.md` in as the instructions. Deploying the gateway
itself is [`docs/deploy-gateway.md`](../../docs/deploy-gateway.md), which is entry-agnostic.
Nothing here verifies that what you pasted matches what the gateway serves, which is
precisely the check the retired release ritual existed to perform.
