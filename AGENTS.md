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
    `contracts/`, training judgment to `references/hybrid-training.md`,
    structural and authorization rules to the validator, and the command surface
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

## Verification

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_repo_safety.py
```
