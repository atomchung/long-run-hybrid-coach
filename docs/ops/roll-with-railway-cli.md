# Rolling a release with the Railway CLI

`deploy-gateway.md`'s "Railway production promotion lane" and "code-only roll" sections define
what a release requires: a bundle built for one commit, the six `GARMIN_COACH_LOOP_RELEASE_*`
variables set to match it, then `production` fast-forwarded to that commit. This file is the CLI
path through that same lane, run start-to-finish from a terminal instead of the Railway
dashboard and Builder UI, and verified end-to-end once already (2026-08-16). Reach for this
instead of re-deriving the flag names from `railway --help` each time.

## One-time setup, per machine

`railway login` opens a browser; without one (SSH, a sandbox, a headless shell) it falls back to
a pairing code automatically -- `--browserless` just makes that explicit:

```bash
brew install railway
railway login --browserless   # prints a pairing URL; open it anywhere and click Authorize
railway whoami                # confirms the login took
```

The resulting token is long-lived (`offline_access`), so this does not repeat per session.

## One-time setup, per checkout

```bash
railway link --project adequate-victory --environment production
railway status   # confirms project, environment, and service
```

`--project` and `--environment` are enough to link -- the environment has exactly one service
(`long-run-hybrid-coach`, per `verify-production-status.md`), so Railway selects it without an
explicit `--service` flag.

This writes a `.railway/` directory into the project root holding the link (Railway's own
convention treats it as local, gitignored state, not something to commit). It is not yet in
this repository's `.gitignore` -- check `git status` before committing anything else from a
checkout where this has been run.

## Rolling a release

This is the CLI form of `deploy-gateway.md`'s code-only roll: a `main` commit that touches
`garmin_coach_loop/` but neither Builder file. The order below is not incidental -- variables
staged before the ref is pushed is the entire point of step 2, and reversing it produces exactly
the mismatch `/readyz` exists to catch.

1. **Build the release bundle for the exact commit being promoted.**

   ```bash
   python3 scripts/custom_gpt_release.py build \
     --gateway-domain https://mcp.paceandstaystrong.com \
     --git-commit <full 40-character commit SHA> \
     --output <path outside this repository>
   ```

   `--gateway-domain` must be the domain the deployment actually serves --
   `https://mcp.paceandstaystrong.com` since the custom domain was bound -- because the
   domain is substituted into the OpenAPI document before `openapi_sha256` is computed,
   and `release_id` binds that hash. A bundle built for any other domain (an earlier
   revision of this file showed the pre-custom-domain `*.up.railway.app` host here)
   produces a release identity the runtime preflight refuses at startup. **On this
   deployment that refusal is an outage, not a rejection**: the volume can mount to only
   one container, so Railway stops the old container before starting the new one, and a
   new one that refuses to start leaves nothing serving until corrected variables
   trigger the next deploy (observed 2026-08-18, ~26 minutes of downtime). The
   pre-push check: the bundle's `openapi_sha256` may move only when `openapi.yaml`
   itself changed in the commit -- an unexpected move is the wrong-domain signal.

   `--git-commit` must be the full 40-character SHA -- `make_release_id`
   (`garmin_coach_loop/release_identity.py`) rejects anything shorter, including the short form
   `git log --oneline` prints by default. When neither Builder file changed, only three fields
   in the output bundle move from the currently-live values: `release_id`, `git_commit`, and
   `gateway_artifact_sha256`. `instructions_sha256` and `openapi_sha256` only move if the
   matching Builder file (`garmin_coach_loop/orchestration.md` or
   `entrypoints/custom-gpt/openapi.yaml`) changed in this commit -- and a change to the
   first moves `gateway_artifact_sha256` too, because the gateway serves that file over MCP.

2. **Stage the changed variables without deploying.**

   ```bash
   railway variables --skip-deploys \
     --set "GARMIN_COACH_LOOP_RELEASE_ID=<release_id from the bundle>" \
     --set "GARMIN_COACH_LOOP_RELEASE_COMMIT=<git_commit from the bundle>" \
     --set "GARMIN_COACH_LOOP_RELEASE_GATEWAY_ARTIFACT_SHA256=<gateway_artifact_sha256 from the bundle>"
     # add RELEASE_INSTRUCTIONS_SHA256 / RELEASE_OPENAPI_SHA256 only if step 1 changed them
   ```

   `--skip-deploys` is why this is a safe two-step sequence instead of a race: without it,
   `railway variables --set` deploys immediately, pairing the *new* release identity with the
   *old* code still running -- precisely the source/release mismatch `/readyz` is designed to
   refuse. Skipping the deploy here means the variables and the new code both take effect in the
   same deployment, once step 3 pushes the ref.

3. **Push the commit to the `production` branch.**

   ```bash
   git push origin <full 40-character commit SHA>:production
   ```

   An ordinary ref push, not a force push -- it only succeeds if the target commit is a
   fast-forward of `production`. This is what triggers Railway's GitHub integration, which waits
   for `.github/workflows/ci.yml` to go green on `production` (Railway's **Wait for CI**, already
   enabled) before it deploys.

4. **Verify.** Poll `/readyz` the way `verify-production-status.md` already documents -- that
   file owns the exact command, not repeated here -- until it reports `"status": "ok"` and its
   `source_git_commit` field equals the SHA just pushed. Observed today: roughly 60-90 seconds
   from push to the switch.

## Things that bit once

- **Step order is 2 then 3, never the reverse.** Stage variables with `--skip-deploys` first,
  push the ref second. Reversed, the ref push deploys the new commit against the still-old
  release variables -- the same guaranteed `/readyz` mismatch, just triggered from the other
  direction.
- **`railway variables --kv` prints every variable's real value**, including
  `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` and the Intervals client secret, unmasked -- unlike the
  Railway dashboard's Variables tab, which masks by default (see `verify-production-status.md`).
  Never run it bare in any shell whose output is captured to a transcript or log; filter through
  `grep` for only the names actually needed.
