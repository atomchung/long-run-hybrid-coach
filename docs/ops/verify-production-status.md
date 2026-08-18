# Verifying production status

`deploy-gateway.md` is the runbook for standing the service up or promoting a release. This
file is for the narrower, more frequent question that follows: **is what's already standing
up actually healthy right now.** Reach for this before re-deriving a check from scratch --
this exact sequence was worked out the hard way once already (2026-08-16) and shouldn't need
redoing.

## Start here: curl the health endpoints directly

No login, no tooling beyond `curl`, works from anywhere:

```bash
curl -s https://mcp.paceandstaystrong.com/healthz | python3 -m json.tool
curl -s https://mcp.paceandstaystrong.com/readyz | python3 -m json.tool
```

Use the custom domain, not the generated `*.up.railway.app` one. Both resolve to the same
service, but the custom domain is what `release_id` binds and what every connected client
reaches, so it is the host whose answer is the release's own.

`/readyz` returning `"status": "ok"` is the authoritative signal -- see `deploy-gateway.md`'s
"Post-deploy verification" section for exactly what that status certifies (the seven
`GARMIN_COACH_LOOP_RELEASE_*` variables are set and match this deployed commit, not just that
the process is alive). Cross-check the `git_commit` field in the response against what was
actually meant to ship:

```bash
git log origin/production --oneline -1
```

If the two match, production is serving the release everyone thinks it's serving. This one
check answers "is it up right now" on its own -- try it before anything below, and stop here
if it comes back `ok`.

## Deploying across the release-identity change

The release identity stopped binding the Custom GPT OpenAPI document and started binding the
MCP tool catalogue and the canonical Agent Skill instead (issue #117). That changed the
`release_identity` object `/healthz` and `/readyz` return, and it changed the variable set a
deployment needs, so one promotion in this repository's history crosses a shape boundary.

**Reading which side a deployment is on** takes one field:

| `release_identity` contains | The deployment is |
| --- | --- |
| `openapi_sha256` | older than the change |
| `tool_catalogue_sha256` and `skill_sha256` | at or after it |

**The variable set is seven, not six.** `GARMIN_COACH_LOOP_RELEASE_OPENAPI_SHA256` is gone;
`GARMIN_COACH_LOOP_RELEASE_TOOL_CATALOGUE_SHA256` and `GARMIN_COACH_LOOP_RELEASE_SKILL_SHA256`
replace it. Both come out of `scripts/release_bundle.py build` like every other release value.

**The order is the same order as any other roll, and it matters more here.** Stage the
variables first (`railway variables --skip-deploys`, see `roll-with-railway-cli.md`), push the
`production` ref second. Getting it backwards fails loudly on both sides, but differently:

- **New code, old variables** -- the container *refuses to start*: `gateway runtime release
  variables predate the release-identity change`. On this single-volume deployment the old
  container is already stopped, so this is an outage until corrected variables trigger the next
  deploy, not a blocked-but-serving `/readyz`. This is the failure to be careful about.
- **Old code still serving, new bundle** -- `scripts/release_bundle.py verify` says `this
  deployment predates the release-identity change`, in those words, instead of reporting a hash
  mismatch on fields the two sides do not both have. Nothing is broken; the deploy simply has
  not landed yet. Poll again.

**Rolling back across the boundary** means restoring the *target commit's* variable set, which
for a pre-change commit includes `GARMIN_COACH_LOOP_RELEASE_OPENAPI_SHA256` and excludes the two
new ones. A leftover variable is harmless in either direction -- each side reads only the names
it knows -- but a missing one is a refused start, so restore the whole set rather than the diff.

## If you need deployment history, not just current state

Railway's GitHub integration writes deployment records to this repo's GitHub Deployments API,
readable without any Railway login:

```bash
# find the environment name if you don't already know it
# (`:owner/:repo` resolves from the current checkout; spelling the repo out
#  would trip the repository safety check's account-handle scan)
gh api repos/:owner/:repo/environments

# list recent deployments for that environment (URL-encode the name)
gh api "repos/:owner/:repo/deployments?environment=<name>&per_page=10"

# get the state history for one deployment
gh api repos/:owner/:repo/deployments/<id>/statuses
```

**Known trap, hit 2026-08-16:** a deployment's most-recently-fetched status here can show
`failure` even when Railway went on to retry and succeed. This API only reports what had
happened *as of the moment you called it* -- it is a history/trend view, not a live one.
Finding a `failure` this way proves that one attempt failed; it does not prove production is
currently down, and it is not a substitute for the `/readyz` curl above. Use this section to
understand what happened, not to answer whether things are fine right now.

## If you need logs, variables, or anything not exposed via API

That needs the owner's own login -- there is no service account for this, and per this
repository's own rule, an assistant should never be given one or asked to type a password in.
Open a browser that is already authenticated as the owner and go to the project. Useful,
non-secret identifiers so this doesn't need rediscovering:

- Environment: `production`
- Service: `long-run-hybrid-coach` (GitHub-connected, deploys from the `production` branch)
- Public domain: `mcp.paceandstaystrong.com` (the generated
  `long-run-hybrid-coach-production.up.railway.app` still resolves to the same service)

The Variables tab shows names with values masked by default -- confirming a variable is
*present* never requires revealing its value, and its value should never be pasted into a
chat transcript or committed anywhere.
