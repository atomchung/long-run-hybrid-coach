# Hermes Agent catalog

Nous Research's Hermes Agent keeps a curated catalog of MCP servers its users install by
name. Unlike the other entries here, the thing being listed is the server itself rather than
a skill or a plugin package, which makes this the closest fit of any directory to what this
product actually is. The shared facts — identity, URLs, OAuth, scopes, data flow, the tool
table, the reviewer path, the test cases — are in [`README.md`](README.md).

Requirements below are from the catalog's own entries and Hermes's MCP documentation, read
2026-08-20.

## Shape

A catalog entry is one file: `optional-mcps/<name>/manifest.yaml` in the `hermes-agent`
repository, submitted as a pull request. Presence in that directory is the approval — there
is no separate portal, no form, and no community tier beside the reviewed one.

Nothing is built for it. Hermes's own client handles discovery, dynamic client registration,
PKCE, token exchange and refresh against a remote OAuth MCP server, which is the flow
[`../../entrypoints/mcp/README.md`](../../entrypoints/mcp/README.md) already serves. Its
users can also add the same server by hand under `mcp_servers` in their own config without
any listing at all — the catalog is discovery, not access.

## The entry

Matching the conventions the existing entries share, including comments explaining the
choices a reviewer would otherwise have to infer:

```yaml
manifest_version: 1

name: long-run-hybrid-coach
description: >-
  One current running and strength plan, kept in step with your Intervals.icu evidence.
source: https://github.com/atomchung/long-run-hybrid-coach

# Remote MCP over Streamable HTTP with native OAuth 2.1 and Dynamic Client
# Registration. Nothing runs locally: no process to spawn, no credential to
# store, no API key to issue.
transport:
  type: http
  url: https://mcp.paceandstaystrong.com/mcp

auth:
  type: oauth
  # Native MCP OAuth. The athlete consents at Intervals.icu; the gateway
  # exchanges upstream server-side and issues its own token.

suggest:
  keywords:
    - training plan
    - long run
  hosts:
    - paceandstaystrong.com

post_install: |
  On first connection Hermes opens a browser to authorize (or run
  `hermes mcp login long-run-hybrid-coach`). Approve access at
  Intervals.icu, then restart the session so the tools load.

  Every calendar write is previewed first and applied only with your
  explicit confirmation.
```

Two choices in there are deliberate:

- **`hosts` names this product's own domain and not `intervals.icu`.** The field triggers a
  suggestion when the athlete is on that host, and every existing entry names the host of the
  service it *is*. This product reads and writes through Intervals.icu without being it, and
  the dossier's own non-affiliation statement is the reason not to claim its brand pill.
- **`name` is the listing slug, not the Skill's.** Same reasoning as the other slug decision
  in [`openclaw-clawhub.md`](openclaw-clawhub.md): the public name does not contain Garmin,
  and the Skill's trigger is not renamed for a directory.

There is no tool subset to declare. Leaving `tools.default_enabled` unset starts the
install-time checklist with everything pre-checked, which is right here — the preview and
apply halves of a write are separate tools, and a user who pruned one of them would have an
entry that proposes changes it cannot carry out.

## What is honestly uncertain

Every entry in the catalog today is a vendor-hosted server for a widely used commercial
service. This is a personal open-source project, and "presence means approval" with no
community tier is a stated bar rather than an inferred one. The submission may simply be
declined, and that is a reasonable outcome to plan for rather than argue with.

The cost of finding out is one file in one pull request, which is why this is worth doing
before the heavier submissions rather than after. What it is not is a reason to change the
product: the same endpoint, the same OAuth, and the same catalogue serve a Hermes user who
adds the server by hand whether or not the listing is ever merged.

---

## Operator checklist

1. **Prove the domain answers**, with the loop in [`openai-plugin.md`](openai-plugin.md)'s
   first step, and confirm `/readyz` is `"status": "ok"` at `main`'s head. A reviewer reads
   the live server, not this file.
2. **Connect once from a real Hermes client** before submitting anything. It runs on the
   athlete's own machine, so its callback is loopback and no deployment change is needed;
   `hermes mcp add` with the url and `auth: oauth` above, then `hermes mcp login`.
3. **Run the test cases** in [`README.md`](README.md) through that client, against a test
   account rather than a real athlete's calendar.
4. **Open the pull request** against `optional-mcps/` with the manifest above as
   `optional-mcps/long-run-hybrid-coach/manifest.yaml`, and say in the description that the
   server is already reachable and what a reviewer can do with it without a device.
5. **Update the entry status table** in
   [`../../entrypoints/README.md`](../../entrypoints/README.md) once a real authorization, a
   real coaching turn and a real delivery have run through Hermes — which is worth doing
   whether or not the catalog entry is merged.
