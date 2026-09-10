# Long Run Hybrid Coach repository rules

This is a Codex-native product. The repository must not call an LLM API or
require an OpenAI API key.

## Repository invariants

1. Keep the product runnable without importing PersonalOS. PersonalOS may be a
   migration reference, never a runtime dependency.
2. Keep credentials, raw provider payloads, GPS tracks, FIT activities, private
   contexts, plans, approvals, receipts, and provider state outside the
   repository. Only anonymous fixtures may be committed.
3. Treat missing, stale, partial, and failed reads as unknown. Never convert
   them to zero or evidence of recovery.
4. The model owns coaching judgment. Deterministic code owns data acquisition,
   reconciliation with identity-backed actuals, validation, persistence,
   approval binding, idempotency, provider writes, read-back, and delivery state.
5. Deterministic validation must not become a shadow coach. Block only when an
   action is structurally invalid, contradicts verified state or identity,
   invents unsupported precision, crosses an authorization or delivery boundary,
   or conflicts with an explicit positive safety signal. Missing, stale, partial,
   or failed optional evidence may lower confidence but must not by itself force
   rest or human review, or block an otherwise valid coaching action.
6. Before adding a blocking validator, document the exact invariant and concrete
   harm, why a warning, model judgment, or narrower capability boundary is
   insufficient, which valid workflows remain possible, and the false-positive
   cost. Add both a harmful-case regression and a false-positive control. Prefer
   provenance, warnings, bounded writes, and targeted checks over blanket denial.
7. Publishing requires approval bound to the exact proposed delivery. Only
   product-owned workouts may be written or updated.
8. Report only delivery evidence the product can observe. The contract is the
   owner of valid delivery states; an earlier state never proves a later hop.
9. Do not diagnose. Pain, illness, chest pain, dizziness, or unusual symptoms
   require a lower-risk human decision.
10. Coaching capability is entry-agnostic: every entry, including a new one,
    must be able to express any coaching act the validation layer accepts.
    Entries differ only in data sources and in operator tooling.
11. Invariant 5 binds the validator; this binds the prompt. A Skill or hosted
    instruction must not become a shadow coach either. It owns only the
    product-specific orchestration a competent model cannot infer: what the
    source of truth is, which boundary needs an explicit confirmation, and what
    the product may claim to have observed. Field semantics belong to
    `contracts/`, training judgment to `hybrid_training.md` served beside it as
    its own prompt, structural and authorization rules to the validator, and the command surface
    to README. An observation is never mapped to an assumed cause and a fixed
    adjustment: `none_found` means no matching evidence was observed, never that
    the athlete's week was too full.
12. A new capability changes evidence, context, schema, or tool descriptions.
    Changing a coaching instruction instead is the exception and carries the
    burden: name the concrete, reproducible eval failure it fixes, and why a
    better field description, context shape, or tool contract cannot fix it. A
    single dogfood incident is not that failure. No fixed threshold, progression
    percentage, or decision tree enters an instruction unless it is a structural
    or safety invariant. When deleting an instruction leaves the coaching evals
    and the safety boundary unchanged, keep it deleted.
13. Everything the model reads is one finite budget: tool descriptions, input
    schemas, the orchestration prompt, the Skill, and every context field. A
    client is handed all of it before the first turn and carries it through the
    conversation, so an addition is paid for by every later turn. Growth is
    therefore accounted for, not assumed: adding surface means saying what it
    buys, and adding it to the orchestration prompt means naming the paragraph it
    replaces.
14. A tool is one act with one set of defaults. Do not merge operations whose
    side effects, destructiveness, or defaults differ — a single tool whose
    unstated field means "not stated" on one path and "as prescribed" on another
    invents data on behalf of the athlete, and one whose annotations must
    describe both paths can only describe them dishonestly. Equally, do not split
    one act into a sequence the model has to rediscover. Fewer tools is not the
    goal; each tool being truthfully describable in its own sentence is.
15. Prefer changing what the model reads over adding to what it must choose
    between. A better field description, a clearer context shape, or a tighter
    input contract is cheaper than a new tool and cannot be called at the wrong
    time. When a new surface is genuinely the answer, an eval case that fails
    before it and passes after is what shows it was.
16. A comment reading `archived issue #NN` names an issue in the archived
    private repository this product migrated from; the number does not resolve
    here and is provenance for finished work, never a link to follow. Cite this
    repository's own tracker as plain `issue #NN`. Before adding a new citation,
    open the number and confirm its topic actually matches what you are writing
    next to — the archived repository used the same low numbers this one has
    since reissued for unrelated work, so a remembered number is not enough.

## Product boundaries and prioritization

A real bug is not automatically the next bug. Keep **existence**, **severity**, and
**priority** separate so a review does not turn into an edge-case generator.

- Start every finding with the normal user-visible scenario: who does what, what
  they see, how often that path is expected, and what goes wrong. An internal
  inconsistency without a material user consequence is not a current blocker.
- Product boundaries are decided once and then treated as constraints. A later
  edge case may test the boundary, but does not reopen it or invent a new product
  model unless normal use demonstrates that the boundary is wrong.
- For coaching, keep two primary truths: **the Coach prescription (PlanState)**
  and **actual execution**. A provider calendar is a delivery projection, not a
  third coaching source of truth. Do not build continuous bidirectional calendar
  reconciliation or conflict-resolution machinery for manual provider edits.
  Product-owned future delivery should follow the latest confirmed PlanState;
  past calendar entries are history. What the athlete actually did remains
  evidence even when it did not match a planned session.
- Classify work before changing code:
  - **Blocker:** normal flow is broken/materially misleading; meaningful user data
    can be lost or corrupted; there is a material authorization, privacy,
    security, or safety problem; or public release cannot proceed safely.
  - **Current:** common or material user friction / coaching-quality loss that
    belongs to the current product lane.
  - **Scheduled:** a real but uncommon, recoverable, or out-of-band failure. File
    it with a trigger and priority, then finish the current higher-value work.
  - **Deferred:** hypothetical/narrow race or low-impact cleanup without observed
    product harm. Preserve the evidence and the condition that would reopen it;
    do not build architecture for it now.
  Security/privacy issues may remain blockers even when the exploit path is rare;
  rarity alone is not a reason to ignore high-impact boundaries.
- When review discovers a scheduled/deferred issue, record it without cascading
  into adjacent fixes. Finish the current issue/PR first. A new finding may
  interrupt only when it independently meets the blocker/current threshold.
- Do not let edge cases create new state machines, scores, warnings, tools,
  confirmation steps, or reconciliation systems unless the normal product flow
  needs them. Prefer the smallest repair at the layer that owns the fact.
- Every issue or PR intended to interrupt the roadmap must state **before →
  after** in user terms and name its priority. Reports must list **current
  blockers/current work separately from scheduled/deferred findings**; never mix
  them into one undifferentiated bug list.

## A step the athlete has to take is the owner's decision

Anything that costs the athlete an action -- a client tool approval, an OAuth prompt, a
confirmation turn, a page to read, a reauthorization -- is a product decision, not an
implementation detail. Before landing a change that adds one, or that changes what a
client asks for, show the owner the concrete before → after in user terms (which client,
which screen, how many approvals for the same task) and the tradeoff, and wait for an
answer. Naming the change security work, compliance work, or a platform review
requirement does not waive this: when a requirement and the approved experience conflict,
report the conflict in the same before → after form and let the owner choose. Removal is
the same decision in the other direction -- a disclosure or confirmation the owner asked
for does not come out quietly either.

Issue #408 and issue #409 are what this rule is made of. A truthful annotation change
added a client approval to every preview, and the release that removed a warning page
removed the only place an athlete was told which client had been authorized. Both were
defensible in isolation and neither was the owner's call to skip.

## Version numbers

`PRODUCT_VERSION` (`garmin_coach_loop/gateway.py`), `server.json` and the Codex
plugin manifest carry one number, and a test holds the three equal. What the parts
mean was settled by the owner on 2026-09-09, against this repository's own prior
habit of bumping the minor for every release that changed anything:

- **Minor is a product-level release the owner declares**, not something a change
  earns. 1.3 to 1.4 was one. Reserving it is the point: a minor spent on an
  ordinary release is a minor unavailable for the next real one.
- **Patch carries everything else**, including a changed tool description, a
  renamed response field, and a moved `instructions_sha256`. This product does not
  maintain backward compatibility before it is stable, so a rename is not by itself
  a reason to reach for a larger number.
- **The number does not decide whether a submission is a new reviewed surface.**
  The changed surface does. A patch that moves `tool_catalogue_sha256`,
  `instructions_sha256` or `skill_sha256` still creates one, and still cannot roll
  under a pending review's snapshot (issue #182). Read the digests, not the version.

## Development and release gates

The repository has one inexpensive local feedback path and two correctness boundaries:

```bash
# default local feedback: committed changes against origin/main, plus staged,
# unstaged and untracked work in this checkout
python3 scripts/test_selection.py

# see the exact manual gates before touching a live client
python3 scripts/change_gates.py --base origin/main

# the confidence boundary kept for every pull request and every main push
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_repo_safety.py
```

`test_selection.py` runs only the directly affected tests when it has a mapping. An
unknown executable change falls back to the full suite; documentation-only changes run
no product tests. This is a developer feedback optimization, not evidence for merge or
release. Pull requests and `main` continue to run the full suite, repository safety, and
the clean-tree check in CI. CI concurrency cancels an older run for the same pull request
or branch when a newer commit supersedes it.

`change_gates.py` is the mechanical decision point for expensive manual work:

| Change surface | Additional gate | Why |
| --- | --- | --- |
| OAuth, gateway, provider delivery, delivery-boundary, or local MCP client code | Corresponding live smoke | The provider/auth hop is not proven by unit tests alone. |
| Tool catalogue, input/output schema, annotation, or served prompt/instructions | Real client acceptance, Scan Tools, and a new plugin version before resubmission | These are model-facing or reviewed MCP bytes. |
| Canonical Skill only | Client acceptance for Skill-consuming entries; no Scan Tools for the current MCP-only OpenAI submission | The Skill is packaged separately from the MCP snapshot. |
| Submission packet, registry entry, or plugin manifest | A new plugin version before resubmission; no Scan Tools on its own | They are the bytes a reviewer or the registry receives, not the served tool catalogue. |
| Internal code, tests, docs, release notes, or CI-only changes | No live ceremony | They do not change a live provider or reviewed client surface. |
| A package file no list names | Reported as unclassified: live smoke and client acceptance until it is named | Silence is not evidence that a new module is internal. |

The table is exhaustive by construction for `garmin_coach_loop/`: `change_gates.py`
names every `.py` and `.md` file in the package as live, model-facing, diff-gated or
internal, and `tests/test_process_gates.py` fails when a file is in none of them. So a
new module cannot inherit "no gate" by being new. Only pull requests cancel a
superseded CI run; a `main` run always completes, because the promotion gate asks
whether one exact SHA has a successful `main` push run and a cancelled run cannot
answer that.

Every deployment still needs the production `/readyz` read-back. The `production` branch
is only a release pointer: its CI job does not repeat the full suite. Before Railway can
deploy, `scripts/verify_production_promotion.py` requires that the exact `production` SHA
is the current `main` head, has a successful `main` push run of the full CI workflow, and
builds a valid release identity. Railway's **Wait for CI** waits for this lightweight
production job; `/readyz` then proves the staged private deployment identity and running
release match after startup. A failed or stale main run blocks promotion rather than
falling back to a partial test result.

Do not infer a client acceptance, provider smoke, Scan Tools result, submission, or
deployment receipt from a green local test, a green PR, or a release bundle. Use the
change-gate output and the evidence boundary each gate names.

## Verification

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_repo_safety.py
```

Both run on a bare Python 3.11 with nothing installed. They remain the merge and main
confidence boundary; the production promotion job intentionally proves reuse of that
boundary for the same commit instead of executing it a second time.
