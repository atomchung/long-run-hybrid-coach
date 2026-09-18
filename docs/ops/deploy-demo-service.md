# Deploy the public demo service

The Product Hunt playground ([`entrypoints/demo/`](../../entrypoints/demo/README.md)) runs
as a **second Railway service in the same project**, built from this same repository. It is
not a route on the Coach Gateway, and the gateway's own service, domain, volume and
variables are not touched by any of this.

## What the repository already carries

Everything that can be a file is a file, and all of it is committed:

| | |
| --- | --- |
| `Dockerfile.demo` | the image — copies `garmin_coach_loop/` and `entrypoints/`, nothing else |
| `railway.demo.toml` | builder, `Dockerfile.demo`, health check `/healthz`, restart policy |
| `entrypoints/demo/config.py` | `PUBLIC_HOST`, the port precedence, every limit and its default |

The service reads Railway's injected `PORT` first and binds it. `COACH_DEMO_PORT` exists
only for a host that injects nothing. Nothing needs setting for the port.

## The steps only the owner can take

These five happen in somebody's browser or console, in a console this repository cannot reach. Nothing
else is left.

1. **Create the service.** Railway → the existing project → *New* → *GitHub Repo* →
   `long-run-hybrid-coach`. In its *Settings*, set **Config-as-code path** to
   `railway.demo.toml` and the **deploy branch** to `main`. Name it `coach-demo`.
   *Do not attach a volume.* The demo holds its sessions in memory and has nothing to
   persist; a disk here is the first step towards a second store for somebody's plan.
2. **Set one variable.** `OPENAI_API_KEY`, on this service only. Do not copy the gateway's
   Intervals OAuth variables across — the demo has no provider client to give them to, and
   a credential on a public anonymous service is a credential with a wider blast radius
   than it needs.
   (Optional: `COACH_DEMO_ALLOWED_ORIGINS` if a preview origin has to reach it. The
   production site origin is compiled in and needs no variable.)
3. **Add the custom domain.** Railway → the `coach-demo` service → *Settings* → *Networking*
   → *Custom Domain* → `demo-api.paceandstaystrong.com`. Railway prints a `CNAME` target.
4. **Cap the spend.** On the OpenAI project this key belongs to, set a monthly budget and
   an alert. Everything in the service bounds *shape* — turns per session, tool calls per
   round, requests per minute per client and for the whole process — and a determined
   caller still gets the global ceiling, which is 120 requests a minute of somebody else's
   money. The budget is the only limit that is denominated in dollars, and nothing in this
   repository can set it.
5. **Point DNS at it.** In Cloudflare, add a `CNAME` record for `demo-api` to the target
   Railway printed. Leave it **DNS-only** (grey cloud) until Railway reports the domain as
   issued, then proxy it if you want to.

`demo-api` is deliberately its own host. `mcp.paceandstaystrong.com` serves connected
athletes and their OAuth; anonymous demo traffic on it would put a public playground inside
the production failure domain and behind the reviewed MCP surface.

## Proving it

In this order, because each one is cheap and rules out the next one's ambiguity.

```bash
# 0. what is actually running: the branch and the commit, not the one you merged to
railway deployment list --json --service coach-demo | python3 -c \
  'import json,sys; d=json.load(sys.stdin)[0]["meta"]; print(d["branch"], d["commitHash"][:8])'
# main 5a7e9877

# 1. the deployment answers at all, and says whether it can coach
curl -s https://demo-api.paceandstaystrong.com/healthz
# {"status":"ok","model":"gpt-5.6-luna","model_credential":"present","fixture":"valid",...}

# 2. the three committed acceptance turns, against the deployed service
python3 -m entrypoints.demo.acceptance --base-url https://demo-api.paceandstaystrong.com

# 3. the account can pay: model_quota is what those turns just found out
curl -s https://demo-api.paceandstaystrong.com/healthz
# {"status":"ok", ..., "model_quota":"ok", ...}
```

**A missing credential does not show up here.** `build_service` refuses to start without
one, so the symptom on Railway is not a `degraded` health response — it is a container that
exits, three restart attempts, a failed deploy, and one line on stderr saying
`OPENAI_API_KEY is not set`. Same for a fixture that no longer validates against
`contracts/`, which is a repository problem rather than a deployment one. If `/healthz`
answers at all, the credential is present.

**A credential that cannot pay shows up one step later.** `model_quota` on `/healthz` is
what the last provider call found out: `unknown` until this container has asked,
`ok` after a call that answered, `exhausted` after a `429` whose body said the account has
no credit. That last one reports `degraded` (a 503) and means add credit on the OpenAI
project; it is not a rate limit and does not clear by waiting (issue #476). A fresh
container starts at `unknown`, so step 2 is what turns the field into evidence.

Step 2 of this list is the only thing that prevents that failure, and the acceptance
command is the only check in this repository that reaches the real Responses API: the
pinned model id, the continuation shape and every provider-side error are proven there or
nowhere. Run it against the deployed service before the launch link goes out.

**The branch is the one that fails silently.** This service deploys from `main`; the
gateway deploys from its own `production` release lane, and the difference is deliberate --
the demo has no release identity, no volume and no athlete, so a merge to `main` is the
whole of its release process. A service left pointed at a working branch keeps answering
perfectly from code that has stopped moving: nothing reports `degraded`, no check fails,
and a merged change simply never appears. On 2026-09-18 this service spent a day on
`codex/demo-luna`, and a merge to `main` plus a `railway redeploy --from-source` rebuilt
the same commit twice before step 0 above said why. Read the branch, not the merge.

A `403 origin_not_allowed` from the browser and a `200` from `curl` is CORS: the page's
origin is not on the allowlist. The production site origin is compiled in, so this means
the page is being served from somewhere else — add that origin to
`COACH_DEMO_ALLOWED_ORIGINS`.

## The site

The page lives in the website repository and calls
`https://demo-api.paceandstaystrong.com/demo/v1/respond` from `data-endpoint` on
`demo.html` and `zh/demo.html`. Its own check script holds that value equal to this one, and
so does `tests/test_demo_deployment.py` here. If the host ever moves, both change together
or the page starts calling a service that is not there.

## What this does not do

It does not touch `railway.toml`, `Dockerfile`, `fly.toml`, the gateway's service, its
volume, its domain, or its variables. It adds no route to the gateway. It needs no
production `/readyz` check, because it deploys nothing the gateway serves — and passing one
would prove nothing about this service.
