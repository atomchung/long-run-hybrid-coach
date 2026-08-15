---
name: custom-gpt-production-release
description: Operate the single production Custom GPT release path. Use for deploy, promote, update, repair, rollback, or verify parity of the production Custom GPT, Gateway, Vercel deployment, and Builder configuration. Do not use for coaching, PlanState or workout operations, ordinary pull-request review, or non-production code changes.
---

# Custom GPT Production Release

Operate release evidence and checkpoints. There is exactly one production Custom GPT. A GitHub `main`
commit with green CI is only a candidate, never production proof; never use a
branch or local worktree as a candidate. Keep this canonical skill in the core
repository; the separate venture repository may link GTM, application, and
milestone material, but must not contain deploy code, receipts, secrets, or the
operator contract.

## Scope and gates

1. Classify the request as `verify`, `deploy`, `promote`/`update`, `repair`, or
   `rollback`. Stop and redirect requests about coaching, PlanState, workouts,
   ordinary PR review, or a second production GPT.
2. Identify the exact GitHub `main` commit, fixed external
   `production-target.json`, target production Gateway domain, configured external release home, and the
   external Gateway secret environment file
   `~/.config/garmin-coach-loop/gateway.env` and Vercel REST token file
   `~/.config/garmin-coach-loop/vercel.env` (both mode `0600`). Keep bundles, Builder exports, deployment logs,
   screenshots, approvals, and receipts in the release home, outside this
   repository. Do not place provider payloads, credentials, secret files, or
   live state here.
3. Before any live Vercel, Gateway, or Builder mutation, present the exact
   target and rollback target, then obtain an explicit user confirmation. Only
   after that confirmation may Codex/operator use an authorized Gateway or
   Vercel connector, or browser-assisted editing of that same production GPT
   Builder. The production GPT must never deploy itself. No confirmation means
   plan/checkpoint work only; do not call a provider API or edit Builder.

## Deterministic operator path

Run the repository's deterministic deployment state CLI. Its state lives under the
mandatory external `--home`; bootstrap an existing legacy release with
`adopt-active`, then use the explicit checkpoints:

```bash
python3 scripts/custom_gpt_deploy.py --home <external-release-home> \
  <status|prepare|repair-proxy|adopt-active|run-deployment-adapter|record-deployment|record-builder|verify|activate|rollback> ...
```

Before the first run, use `init-production-target` (it does not require
`--home`) to write the external target and compute its binding. It pins the
single production GPT ID together with the GitHub and Vercel identities. Never
hand-edit this file; target or GPT migration is a separate explicit operation.
Run `git fetch origin main` before `prepare`, because the local `origin/main`
candidate gate and the direct GitHub provider read-back must agree.

`prepare` requires `--production-target` and reads the public GitHub `main` ref
plus its exact successful `ci.yml` push run directly through `gh api`; it does
not trust a caller-written CI-success file. It also requires
`--expected-deployment-identity` for the intended production environment,
in addition to the commit, stable Gateway domain, and proxy upstream. It writes
the Builder exports, a secret-free deployment request, and a Vercel proxy payload
outside Git. `run-deployment-adapter` uses the repository-owned
`scripts/custom_gpt_vercel_create.py` by default. That adapter uploads the exact
hash-bound `vercel.json` and creates the fixed production deployment through
Vercel REST, then emits only the bounded create response. It first persists one
private create attempt bound to the proxy revision and request/config hashes. A
lost create response is reconciled through the project-scoped deployment list;
zero or multiple exact metadata matches block, and the same revision is never
submitted twice. The create payload does not assign the stable domain; authenticated
stable-alias read-back remains the production authority. An explicitly
confirmed `--runner` may override only this create step. Alternatively,
`record-deployment --provider-evidence` accepts the same bounded create
attestation from an already completed create call. None can self-certify success:
the orchestrator then uses `VERCEL_TOKEN` from the separate external `0600`
file to GET the deployment, project, deployment aliases, and production project
domains directly from Vercel. It accepts the result only when the same deployment
is `READY`, is the project's current production target, and owns the fixed stable
domain. The token and raw provider bodies are never persisted in the release
receipt. Preparing or recording a request never means the whole release passed.
Do not substitute the older release-verification script as the final state command.

At the Builder checkpoint, a human or a browser-assisted operator updates the
same production GPT and uses `record-builder` with `--builder-evidence` to bind
the GPT identity, proxy revision, and exported instructions/OpenAPI to the run.
Browser assistance is permitted only after the explicit confirmation; it is a
human/browser Builder attestation, not deterministic proof. `verify` requires
both `--smoke-evidence` and the corresponding `--browser-evidence`, then performs
the separately confirmed public check. Complete the run through
`record-deployment`, `record-builder`, `verify`, and `activate`. `activate`
requires the Vercel token file and performs fresh Vercel and public `/healthz`
read-backs immediately before changing the active pointer; it cannot reuse the
earlier verification observation.

Keep the producers distinct: direct GitHub provider read-back proves candidate
checks; a Vercel deployment ID plus deployment/project read-back prove the
recorded provider state at that observation time; a human/browser
Builder attestation proves what was observed in Builder; browser/user-visible
smoke proves only that observed path. The `verify` checkpoint records bounded
parity/health and smoke evidence; none of those sources alone proves ChatGPT
publication, OAuth consent, user traffic, or provider delivery. Matching
instructions/OpenAPI hashes do not automatically prove the selected Builder
model, authentication settings, or the rest of the saved Builder configuration;
those remain human/browser observations and user-visible smoke evidence.

## Legacy bootstrap

Use `adopt-active` only once to represent the already verified production release
before the state CLI existed. It requires the legacy bundle, Builder exports,
parity receipt and smoke evidence, plus `--current-proxy-upstream`,
`--current-proxy-config`, `--expected-deployment-identity`, and the canonical
`--production-gpt-id`; without
`--confirm-live-check` it is plan-only. This compatibility path records explicitly
weaker legacy route/deployment evidence. It is not a shortcut for a new candidate,
not proof of a modern Vercel deployment identity, and not reusable as a one-click
release path.

## Repair and rollback

Treat a parity failure or uncertain evidence as blocked, not as permission to
guess or overwrite. Under the stable production Vercel proxy, a changed tunnel
updates only the proxy upstream: do not change the production Builder schema or
OAuth token URL. Use `repair-proxy`; this route-only revision may explicitly reuse
the already recorded Builder evidence because its release and Builder content did
not change. A direct development tunnel is separate: its Builder action
configuration may point at that temporary development origin and is never the
production Builder. Changed code or Builder content requires a new green-`main`
candidate. Repeat the appropriate recorded deployment, Builder attestation, and
browser/user-visible smoke before `activate`.

`rollback` without `--confirm` only selects the previous verified target and
reports the plan. With confirmation it creates a fresh restore proxy revision;
it still does not deploy or activate that revision. Unlike route-only
`repair-proxy`, restore does not reuse Builder evidence: record fresh Builder
evidence, deployment receipt, parity, and browser/user-visible smoke, then
`activate`. Restoring the one pre-orchestrator legacy target additionally
requires `--production-target`; the new restore revision binds that target to
the prior verified activation, but does not pretend legacy GitHub/Vercel evidence
was modern provider proof. Never roll back PlanState, athlete data, OAuth state,
or provider workouts.
