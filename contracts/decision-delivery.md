# One confirmed plan and its future calendar projection

Issue #272 is current work: an athlete changes a delivered Thursday workout or
changes it to rest. Before, the plan changed under one confirmation while the
calendar kept the old workout until a separate delivery confirmation. Now
`prepareCoachDecision` shows both effects and `applyCoachDecision` commits the
plan and attempts those exact calendar effects under the same confirmation.

PlanState remains the prescription; actual execution remains evidence. The
calendar is a projection. This does not import manual calendar edits as coaching
intent or continuously reconcile calendars.

## Input and observable output

The existing decision scope and coaching request still describe the plan. Optional
`publish_new_workouts: true` also includes new, undelivered, actionable future
running/strength sessions in the preview. Its default is false: planning alone
still does not publish new workouts. Changed previously delivered future sessions
are automatically included as exact replacements or withdrawals. No affected
existing delivery and no publication request means no additional provider reads.

`preview.calendar_delivery` contains `workouts` (each exact workout and its
existing human-readable delivery preview, with `operation: publish|replace`),
`withdrawals` (session, observed presence/date/name), `settings_changes`, and
`unresolved`. The existing exact prerequisite-setting approval is preserved.
Unavailable optional reads leave an effect explicitly unresolved and unapproved;
they do not manufacture a preview or block a structurally valid plan save.

Apply accepts `proposal` plus `confirmed: true`; plan identity and held authoring
may be inferred from the signed proposal. Supplied context/request must still
match. Once confirmation has been committed, the same proposal can retry its
stored effects without another yes, retained context, or process-local cache.
The existing plan-only path remains compatible.

A successful plan commit returns top-level `status: passed` even if a provider
operation is partial. `calendar_delivery` separately reports `status`,
`delivered`, `withdrawn`, `skipped`, `unresolved`, `settings_changes`,
`attempt_open`, and the resulting `plan_version`. `delivered` proves only
`intervals_accepted`; it does not prove Garmin Connect or watch receipt.

## Persistence and retry

The signed proposal binds the exact prepared sets and full rendered preview.
The PlanState commit atomically includes `receipt.confirmed_delivery`, containing
its signed-proposal `approval_key` and those exact sets. There is no second
approval file, current-calendar truth, or tombstone plan state. The receipt hash
covers this field. Resuming checks the historical receipt, approved plan, signed
effect hash and decision/context bindings before using it.

Each session uses the existing delivery/withdrawal set and mutation journal. A
retry may rebind only plan versions after proving the current prescription is the
approved one, ignoring delivery bookkeeping and resolved execution status. Exact
workouts, dates, settings changes and withdrawal targets do not change. The
existing journal's version/hash must still match any interrupted attempt.

When an early week roll removes an old future session, its exact previously owned
event is withdrawn using the same approved before/after evidence and existing
journal. The removed session is not reintroduced into PlanState. Only exact
verified absence closes that withdrawal; a failed read is unknown.

Past or identity-backed executed items are left unchanged while other valid
future items continue. An interrupted prior write may finish by read-back only.
If it still cannot be verified, the existing unresolved-effect fence remains and
its existing recovery path applies; the system does not silently abandon an
unknown mutation to start another one.

## Narrow validation boundaries and false-positive controls

- **Exact confirmation:** a missing or mismatched signed effect/plan binding must
  stop persistence or resume, because otherwise a provider write could differ
  from what the athlete approved. A warning cannot authorize different content.
  Plan-only commits, concise apply, restart retries and bookkeeping-only version
  changes remain valid. The cost is a new preview for a changed prescription or
  for exact preview material lost before the first commit.
- **Future ownership:** only the recorded product-owned target may be changed,
  and past or already-executed items may not be written. A warning after mutation
  would already have rewritten history or somebody else's event. Checks occur
  per item; unaffected future workouts remain possible. The cost is leaving a
  newly historical or no-longer-owned event unchanged, with an explicit result.
  Unknown actuals do not imply completion; only attached identity-backed actuals
  or recorded resolved status closes a session.
- **Positive symptoms on resume:** an explicitly true symptom prevents a new
  delivery for that athlete's today and names the need for a human decision.
  A new provider write would otherwise reissue training after the positive signal.
  Withdrawals, other future dates, read-back of an earlier write, and false/null/
  unassessed symptom workflows remain available. The cost is holding even a mild
  reported symptom's same-day delivery; no threshold or training adjustment is
  inferred.

`tests/test_decision_delivery.py` exercises the public MCP normal journey,
replacement/rest/early-roll removal, no-confirm refusal, first publication,
restart and partial retries, signed receipt tampering, changed prescriptions,
settings persistence, expired/identity-backed execution preservation, and
unattached-evidence controls. `tests/test_state_store.py` proves that a missing or
mismatched effect approval cannot commit either a decision or first plan, while
legacy plan-only and exact approved writes remain valid.

Surface cost: one optional prepare boolean, one optional preview/result object,
and tighter descriptions on the existing prepare/apply pair replace a model-
assembled second preview/confirmation for the same coaching act. No new tool or
coaching decision heuristic is introduced.
