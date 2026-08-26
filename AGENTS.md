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

## Verification

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_repo_safety.py
```

Both run on a bare Python 3.11 with nothing installed, and CI runs the same two
commands — a green local run is the same run, not a weaker one.
