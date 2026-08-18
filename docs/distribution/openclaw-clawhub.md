# OpenClaw / ClawHub

The listing metadata for publishing the canonical Skill on ClawHub, pointed at the same
hosted endpoint. The shared facts — identity, URLs, OAuth, scopes, data flow, the tool table,
the logo, the reviewer path, the test cases — are in [`README.md`](README.md). The connection
mechanics and the Skill install are already written once, in
[`../../entrypoints/openclaw/README.md`](../../entrypoints/openclaw/README.md); this file is
only what a submission form would ask for that is not there. Issue #133 tracks the work.

## Shape

Unlike the other two, this listing is a **Skill** rather than a server: OpenClaw skills are
`SKILL.md` plus frontmatter, which is the format
[`../../.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/) is
already published in. The Skill is referenced or copied at listing time and never forked, so
a later change here is picked up by re-syncing rather than by editing the published copy.

The MCP endpoint is the same `https://mcp.paceandstaystrong.com/mcp` every other entry uses.

## Field mapping

| Field | Value |
| --- | --- |
| Name | `Long Run Hybrid Coach` |
| Short description | `Refresh one current running and strength plan` |
| Description | the description in [`README.md`](README.md) |
| Skill payload | [`../../.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/) |
| MCP server URL | `https://mcp.paceandstaystrong.com/mcp` |
| Transport | streamable HTTP |
| Website | `https://paceandstaystrong.com/` |
| Privacy policy | `https://paceandstaystrong.com/privacy.html` |
| Terms | `https://paceandstaystrong.com/terms.html` |
| Support | `https://paceandstaystrong.com/support.html` |
| Licence | MIT |
| Icon | the square PNG in [`README.md`](README.md) |

ClawHub publishes no metadata schema this repository can hold itself to, so confirm the key
names against its own reference at listing time. What does not vary by platform is above;
what does is ClawHub's to define.

## What has to be true before an OpenClaw client can connect

One thing, and it is a configuration value rather than code: OpenClaw's callback origin has
to be trusted by the deployment, or dynamic client registration is refused with a reason
saying so. Loopback callbacks need no entry, so an OpenClaw instance running on the athlete's
own machine works as-is; a hosted OpenClaw deployment needs its origin added through
`GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` — "Admitting a new hosted client" in
[`../deploy-gateway.md`](../deploy-gateway.md).

Nothing has been verified through a real OpenClaw client yet: no real OAuth authorization, no
real coaching turn, no real delivery. [`../../entrypoints/README.md`](../../entrypoints/README.md)
is the status table, and it says exactly that.

---

## Operator checklist

1. **Prove the domain answers**, with the loop in
   [`openai-plugin.md`](openai-plugin.md)'s first step. Four `200`s, or stop: every listing
   URL above is on that domain, and on 2026-08-18 it did not resolve at all.
2. **Open the upstream authorization.** The Intervals.icu application is owner-only during
   development, so nobody but the owner can authorize — see [`README.md`](README.md).
3. **Roll production to `main`** and confirm `/readyz` is `"status": "ok"` at `main`'s head.
4. **Add a `LICENSE` file** (MIT, `Copyright (c) 2026 Long Run Hybrid Coach`) to the
   repository root.
5. **Connect once from a real OpenClaw client** before listing anything. If it runs on the
   athlete's own machine, nothing needs configuring. If it is hosted, find the callback
   origin its registration attempt is refused for, validate the flow, then add that origin to
   `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` in the service variables and redeploy. Worked
   when `startCoachSession` returns a plan through that client.
6. **Run the test cases** in [`README.md`](README.md) through that client. Case 5 is the one
   that proves the delivery path; do it against a test account, not a real athlete's calendar.
7. **Update the entry status table** in [`../../entrypoints/README.md`](../../entrypoints/README.md)
   from "packaged, awaiting real-connection verification" to verified — but only after a real
   authorization, a real coaching turn and a real delivery have all run.
8. **Publish to ClawHub** with the field mapping above, checking its current manifest
   requirements as you go.
