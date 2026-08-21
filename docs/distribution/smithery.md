# Smithery

The only listing here that is **already live**. It is also the first hosted client other than
the Claude connector to complete the whole authorization chain against production, which is
why this file records what was validated rather than what a submission would carry.

The shared facts — identity, URLs, OAuth, scopes, the tool table, the reviewer path — are in
[`README.md`](README.md).

| | |
| --- | --- |
| Listing | `https://smithery.ai/servers/tingcctw/long-run-hybrid-coach` |
| Install | `npx -y smithery mcp add tingcctw/long-run-hybrid-coach` |
| Published | 2026-08-21 |

## Shape: it proxies, it does not link

Smithery puts its own gateway between the client and this one. A client connects to a
Smithery address and Smithery forwards to `https://mcp.paceandstaystrong.com/mcp`; the
athlete's browser still reaches Intervals for consent, and the token this gateway issues is
held by Smithery's gateway rather than by the athlete's own client.

That is the same shape as any connector host holding a token, with one difference worth
stating plainly: it is an intermediary the athlete did not choose by name. The Intervals
credential inside the token stays encrypted under a key only this gateway holds, so the
athlete's provider account is not exposed by it — reading and writing *their plan* is.

## What made it work, and what it cost

One configuration value, no code. Registration was refused until
`https://connect.smithery.ai` was added to `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` — the
"Admitting a new hosted client" procedure in [`../deploy-gateway.md`](../deploy-gateway.md),
followed exactly: try, read the refused origin out of the security log, decide, add,
redeploy, verify the whole flow.

Nothing about the release moved. The redeploy carried the same commit, and `/readyz` reported
an identical `release_id`, `tool_catalogue_sha256` and `configuration_binding` before and
after — the trusted-origins variable is bound into neither identity, which is why this was
safe to do while a plugin submission was under review. The general rule is the table in
[`openai-review-conformance.md`](openai-review-conformance.md).

## What was validated, on 2026-08-21

Against production at commit `9b2a038`, read from this gateway's own security log rather than
from the platform's console:

| Stage | Result |
| --- | --- |
| `client_registration` | accepted, origin `https://connect.smithery.ai` |
| `authorization` | accepted |
| `provider_callback` | accepted |
| `token_issuance` | accepted |
| `mcp_authentication` | accepted, repeatedly |

And read back from the public listing rather than from the scan's own output: all 22 tools
appear, matching the running catalogue name for name.

## Two things to know before changing anything

- **Removing the origin is a revocation, not a closed door.** It is rechecked at
  `/oauth/authorize`, so taking it off stops every Smithery connection at once, working ones
  included, and those athletes must reconnect through a platform this deployment trusts.
- **Re-running the publish flow starts a new release**, which is why the console asks for the
  URL again. An existing successful release is already the live listing; nothing needs
  republishing unless the server changed.
