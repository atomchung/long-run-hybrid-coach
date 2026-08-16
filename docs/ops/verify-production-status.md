# Verifying production status

`deploy-gateway.md` is the runbook for standing the service up or promoting a release. This
file is for the narrower, more frequent question that follows: **is what's already standing
up actually healthy right now.** Reach for this before re-deriving a check from scratch --
this exact sequence was worked out the hard way once already (2026-08-16) and shouldn't need
redoing.

## Start here: curl the health endpoints directly

No login, no tooling beyond `curl`, works from anywhere:

```bash
curl -s https://long-run-hybrid-coach-production.up.railway.app/healthz | python3 -m json.tool
curl -s https://long-run-hybrid-coach-production.up.railway.app/readyz | python3 -m json.tool
```

`/readyz` returning `"status": "ok"` is the authoritative signal -- see `deploy-gateway.md`'s
"Post-deploy verification" section for exactly what that status certifies (the six
`GARMIN_COACH_LOOP_RELEASE_*` variables are set and match this deployed commit, not just that
the process is alive). Cross-check the `git_commit` field in the response against what was
actually meant to ship:

```bash
git log origin/production --oneline -1
```

If the two match, production is serving the release everyone thinks it's serving. This one
check answers "is it up right now" on its own -- try it before anything below, and stop here
if it comes back `ok`.

## If you need deployment history, not just current state

Railway's GitHub integration writes deployment records to this repo's GitHub Deployments API,
readable without any Railway login:

```bash
# find the environment name if you don't already know it
gh api repos/atomchung/long-run-hybrid-coach/environments

# list recent deployments for that environment (URL-encode the name)
gh api "repos/atomchung/long-run-hybrid-coach/deployments?environment=<name>&per_page=10"

# get the state history for one deployment
gh api repos/atomchung/long-run-hybrid-coach/deployments/<id>/statuses
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
- Public domain: `long-run-hybrid-coach-production.up.railway.app`

The Variables tab shows names with values masked by default -- confirming a variable is
*present* never requires revealing its value, and its value should never be pasted into a
chat transcript or committed anywhere.
