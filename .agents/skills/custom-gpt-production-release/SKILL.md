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
2. Identify the exact GitHub `main` commit, its green CI evidence, target
   production Gateway domain, configured external release home, and the
   external secret environment file
   `~/.config/garmin-coach-loop/gateway.env` (mode `0600`). Keep bundles, Builder exports, deployment logs,
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
  <status|adopt-active|prepare|repair-or-restore|record-deployment|record-builder|verify|activate|rollback-plan> ...
```

Use the installed CLI's exact spelling when it is available: hardening may call
the proxy-only recovery checkpoint `repair` or `restore`, and the planning
checkpoint `rollback-plan`. Do not substitute the older release-verification
script as the final state command. `prepare` binds one exact `main` commit and
writes the Builder exports, a secret-free deployment request, and a Vercel proxy
payload outside Git. `record-deployment` records an exact receipt from the
confirmed deployment system; preparing or recording a request never means a
deployment happened.

At the Builder checkpoint, a human or a browser-assisted operator updates the
same production GPT and uses `record-builder` to bind its exported evidence to
the run. Browser assistance is permitted only after the explicit confirmation;
it is a human/browser Builder attestation, not deterministic proof. Complete
the run through `record-deployment`, `record-builder`, `verify`, and `activate`.

Keep the producers distinct: GitHub CI proves candidate checks; a Vercel
deployment ID and read-back prove the recorded deployment; a human/browser
Builder attestation proves what was observed in Builder; browser/user-visible
smoke proves only that observed path. The `verify` checkpoint records bounded
parity/health and smoke evidence; none of those sources alone proves ChatGPT
publication, OAuth consent, user traffic, or provider delivery.

## Repair and rollback

Treat a parity failure or uncertain evidence as blocked, not as permission to
guess or overwrite. Under the stable production Vercel proxy, a changed tunnel
updates only the proxy upstream: do not change the production Builder schema or
OAuth token URL. It is a proxy revision of the same release. A direct development
tunnel is separate: its Builder action configuration may point at that temporary
development origin and is never the production Builder. Changed code or Builder
content requires a new green-`main` candidate. Repeat the appropriate recorded
deployment, Builder attestation, and browser/user-visible smoke before `activate`.

The rollback-plan checkpoint only selects and records the previous verified
target. It deliberately does not mutate the live proxy, Gateway, Builder, or
active pointer. Require a fresh explicit confirmation for the external live
deployment, record its exact receipt, produce fresh parity and browser/user-visible
smoke evidence, then `activate` the restored target. Never roll back PlanState,
athlete data, OAuth state, or provider workouts.
