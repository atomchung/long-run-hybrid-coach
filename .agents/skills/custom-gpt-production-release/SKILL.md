---
name: custom-gpt-production-release
description: Operate the single production Custom GPT release path. Use for deploy, promote, update, repair, rollback, or verify parity of the production Custom GPT, Gateway, Vercel deployment, and Builder configuration. Do not use for coaching, PlanState or workout operations, ordinary pull-request review, or non-production code changes.
---

# Custom GPT Production Release

Operate release evidence and checkpoints; do not implement or directly perform
provider mutations. There is exactly one production Custom GPT. A GitHub `main`
commit with green CI is only a candidate, never production proof; never use a
branch or local worktree as a candidate. Keep this canonical skill in the core
repository; the separate venture repository may link GTM, application, and
milestone material but does not own this operator contract.

## Scope and gates

1. Classify the request as `verify`, `deploy`, `promote`/`update`, `repair`, or
   `rollback`. Stop and redirect requests about coaching, PlanState, workouts,
   ordinary PR review, or a second production GPT.
2. Identify the exact GitHub `main` commit, its green CI evidence, target
   production Gateway domain, configured external release home, and external
   Gateway secret file. Keep bundles, Builder exports, deployment logs,
   screenshots, approvals, and receipts in the release home, outside this
   repository. Do not place provider payloads, credentials, secret files, or
   live state here.
3. Use only deterministic release commands. Do not write to a provider, call a
   deployment API, or paste/edit Builder content as an agent action. Before any
   live Vercel, Gateway, or Builder mutation, present the exact target and
   rollback target, then obtain an explicit user confirmation. Do not grant the
   production GPT Vercel or Builder permissions.

## Deterministic operator path

Build the candidate bundle with the existing release contract:

```bash
python3 scripts/custom_gpt_release.py build \
  --git-commit <green-main-sha> --gateway-domain <production-gateway-origin> \
  --output <external-evidence-dir>/bundle.json
```

Run the repository's deterministic deployment CLI. Its state lives under the
mandatory external `--home`; bootstrap an existing legacy release with
`adopt-active`, then use the explicit checkpoints:

```bash
python3 scripts/custom_gpt_deploy.py --home <external-release-home> \
  <status|adopt-active|prepare|record-builder|record-deployment|verify|activate|rollback> ...
```

`prepare` binds one exact `main` commit and writes the Builder exports, a
secret-free deployment request, and a Vercel proxy payload outside Git.
`record-deployment` accepts only a receipt bound to that request. This repository
does not own a live deployment runner. If an external adapter is intentionally
provided, `run-deployment-adapter` is plan-only until `--confirm`; otherwise use
the separately confirmed deployment system and record its receipt. Never imply
that preparing or recording a request performed the deployment.

At the Builder checkpoint, a human manually copies the bundle's rendered
instructions and OpenAPI into the one production GPT and records the external
evidence. The agent may compare hashes but must not claim it made that edit.

After the human checkpoint and any separately confirmed live deployment, verify
parity and write an external receipt:

```bash
python3 scripts/custom_gpt_release.py verify \
  --bundle <external-evidence-dir>/bundle.json \
  --builder-instructions <external-evidence-dir>/builder-instructions.md \
  --builder-openapi <external-evidence-dir>/builder-openapi.yaml \
  --receipt <external-evidence-dir>/parity-receipt.json
```

Report only the observed receipt: Builder-content and Gateway-artifact parity
is not proof of ChatGPT publication, OAuth consent, user traffic, or provider
delivery.

## Repair and rollback

Treat a parity failure or uncertain evidence as blocked, not as permission to
guess or overwrite. A changed ephemeral tunnel is a proxy revision of the same
release; changed code or Builder content requires a new green-`main` candidate.
Repeat the deployment receipt, human checkpoint when its content changed, and
public parity/smoke verification before `activate`.

`rollback` only selects and records the previous verified target. It deliberately
does not mutate the live proxy, Gateway, Builder, or active pointer. Require a
fresh explicit confirmation for the external live deployment, record its exact
receipt, produce fresh public parity and smoke evidence, then `activate` the
restored target. Never roll back PlanState, athlete data, OAuth state, or
provider workouts.
