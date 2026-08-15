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

Run the repository's deterministic deployment CLI when it is available:

```bash
python3 scripts/custom_gpt_deploy.py <verify|deploy|promote|repair|rollback> ...
```

`custom_gpt_deploy.py` is the expected deployment interface, not an instruction
to recreate deployment logic in this skill. If it is unavailable or its help
does not expose the needed operation, stop and report the missing interface;
do not substitute ad-hoc Vercel/Gateway/Builder writes.

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
guess or overwrite. Repair by rebuilding from a new green-`main` candidate and
repeating the human checkpoint and parity verification. For rollback, name the
previously receipted production release, require a fresh explicit confirmation
before each live mutation, use the deterministic deploy CLI, then produce a new
external receipt proving the restored parity. Never roll back PlanState,
athlete data, OAuth state, or provider workouts.
