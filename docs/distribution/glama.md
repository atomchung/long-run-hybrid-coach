# Glama

The listing here was never submitted. Glama indexed the repository on its own once the
topics were filled in, so the entry existed before anything was sent — and it sat there
describing a server its own checks could not reach, because registration was refused by
this deployment's trusted-origin list.

The shared facts — identity, URLs, OAuth, scopes, the tool table, the reviewer path — are
in [`README.md`](README.md).

| | |
| --- | --- |
| Listing | `https://glama.ai/mcp/servers/atomchung/long-run-hybrid-coach` |
| Indexed | automatically, no submission |
| Admitted | 2026-08-21, and the whole authorization chain verified the same day |

## One origin, three surfaces

Glama is not one thing, and the distinction matters before deciding to trust it:

- **The registry** — the listing itself. It reads the repository and this endpoint's
  public metadata, and needs no authorization at all.
- **The Inspector** — a browser client for testing a server by hand. Its own page says
  requests go directly to the MCP server, and that is true only with its proxy checkbox
  cleared: the box is **ticked by default**, and every call then travels through Glama's
  own proxy endpoint. The default is the proxied path, not the direct one.
- **The Gateway** — a reverse proxy Glama sells in front of whatever servers an agent
  uses, holding OAuth credentials on the athlete's behalf and forwarding requests.

All three register their callback on `https://glama.ai`. **Trusting that origin admits all
three**, not the one that was tested, so the shape to plan around is the strongest of them:
an intermediary holding a token this gateway issued, the same trade already accepted for
Smithery in [`smithery.md`](smithery.md).

## What made it work, and what it cost

One configuration value, no code, exactly as the "Admitting a new hosted client" procedure
in [`../deploy-gateway.md`](../deploy-gateway.md) describes. Registration was refused with
`untrusted_redirect_origin` until `https://glama.ai` joined
`GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` beside the entry already there.

Nothing about the release moved. `/readyz` was read on both sides of the redeploy and
reported an identical `release_id`, `tool_catalogue_sha256` and `configuration_binding` —
the trusted-origins variable is bound into neither identity, which is what made this safe
to do with a plugin submission under review. The general rule is the table in
[`openai-review-conformance.md`](openai-review-conformance.md).

## What was validated, on 2026-08-21

Against production at commit `9b2a038`, driven through the Inspector and read from this
gateway's own security log rather than from the platform's console. The athlete was the
test account, not the owner's own:

| Stage | Result |
| --- | --- |
| `client_registration` | accepted, origin `https://glama.ai` |
| `authorization` | accepted |
| `provider_callback` | accepted |
| `token_issuance` | accepted |
| `mcp_authentication` | accepted; one `getCoachState` call returned stored plan state |

And read back from the client rather than from the scan's own output: all 22 tools appear,
matching the running catalogue.

## Two things to know before pointing anyone at the Inspector

- **It puts the access token in the page URL.** "State persisted in URL for easy sharing
  and bookmarking" includes the bearer token this gateway issued, so that URL *is* a live
  credential — sharing it hands over the connection, and it lands in browser history either
  way. Disconnecting clears it from the URL. Treat an Inspector URL as a secret, and do not
  paste one into an issue, a screenshot or a chat.
- **The authorization opens a popup.** A browser that blocks popups stalls on "OAuth
  Required" with nothing on the page to say why; the only trace is a console warning. That
  is a client-side limit, not a refusal — this gateway sees no request at all.

## The listing is still unclaimed, and that costs discoverability

Glama polls `/.well-known/glama.json` and gets a `404` — three times in one morning on
2026-08-21 alone. That file is its ownership proof: a small document naming a maintainer
email that matches a Glama account. Publishing it claims the listing, which is what unlocks
control of the description and metadata, usage reports and health status. Until then the
page says unclaimed servers have limited discoverability, so this is not cosmetic.

It is deliberately **not** done here, and the reason is a decision rather than an effort:
the file has to carry an email address, and which account owns this listing is the
publisher question in [`README.md`](README.md) rather than a detail to settle in a route.
When it is settled, note that serving the file is gateway code and therefore a production
roll, not a variable — and that gateway code touching no tool moves `release_id` and
nothing a directory ever snapshotted.

## Removing the origin is a revocation, not a closed door

It is rechecked at `/oauth/authorize`, so taking `https://glama.ai` off the list stops every
connection made through any of the three surfaces at once, working ones included, and those
athletes must reconnect through a platform this deployment trusts.
