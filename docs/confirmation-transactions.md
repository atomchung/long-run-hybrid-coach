# Confirmation-bound transactions — issue #435 handoff A

Architecture is settled in issue #435: the model owns coaching judgment; the server owns the prepared transaction; deterministic code must not become a shadow coach. This file is the A contract lock. It is not a runtime receipt. It does not cut the public catalogue.

## Decision

Treat prepare → model-carried intermediary state → apply as one defect class. Keep domain-specific public tools. Every **migrated** confirmation-bound apply accepts **only** `{proposal, confirmed: true}`. There is no `proposal_hash` vs `proposal` choice, and no ignore-or-bind echo compatibility path. First-plan prepare must not expose a non-durable candidate `plan_id` / `plan_version` as authoritative top-level plan identity. Do not resurrect retired public deletion tools. Do not move training judgment into code. One catalogue cut in C; one OpenAI resubmission in D.

## Current state

The public MCP catalogue still splits one user confirmation across two tools. First-plan prepare still returns a derived candidate `plan_id` / `plan_version` that the matching apply refuses if those keys are copied (issue #280). Warm plan change apply can already be `{proposal, confirmed: true}`. Delivery apply still names the opaque token `proposal_hash` and still holds the exact set. Public owner deletion is not an MCP transaction since 1.4.5. Operator deletion is a separate equivalent pair on the CLI path.

## Completed evidence

- Inventory with migrated/excluded reasons (this file).
- Exact shared prepared-record contract (this file): lookup identity, stored exact effect, confirmation binding, staleness, side-effect scope, expiry, idempotency, reuse, commit-without-reauthoring, per-kind validation, duplicate vs stale replay.
- Public MCP producer-to-consumer tests in `tests/test_confirmation_transactions.py` (cold first-plan, warm change, delivery, withdrawal). Apply bodies come from prepare output.
- Issue #280: current-behavior lock (producer still returns candidate ids; echoing them is 400, delete in C) plus `@unittest.expectedFailure` intended repair that copies those keys **only if returned**.
- Operator deletion characterization on an isolated anonymous local owner (preview output → exact digest → commit → absence; scope-changed refusal). Public MCP names stay retired.
- Same-evidence baseline: `evals/baselines/issue-435-a-same-evidence-quality.json` (generic provider/model + anonymous case + original answer + rationale). Runtime receipts live outside the repo. Limited self-score, not independent quality evidence.
- Public/model surface unchanged in A.

## Next handoff

**B — Transaction kernel**, against the prepared-record contract below. No public schema cut. No coaching policy.

Then **C** one catalogue cut; **D** real-client E2E including independent #433 and a real operator deletion transaction.

## Blockers

The contract below preserves commit-time validation, committed-receipt recovery, compound calendar effects, and re-preview provenance. Coordinator acceptance is not claimed here. Remaining limits that are **not** missing-contract work for B:

- No independent coaching-quality scoring exists; the A baseline is a limited self-score.
- #433 remains an independent ChatGPT acceptance question.
- D must still demonstrate live operator deletion, not only the A unit characterization.
- C, not B, makes the public field rename (`proposal_hash` → `proposal`) and stops first-plan prepare from returning candidate ids.

Product/UX tradeoffs that A does **not** reopen:

- Athlete confirmation count stays one per mutation (prepare read-only, apply destructive).
- Public deletion stays off MCP (support page → operator).
- Ephemeral old previews may be invalidated at C; durable PlanState must remain readable.

---

## Inventory

Only workflows that already have a preview/confirmation boundary **and** currently require intermediary transaction state to be carried between two calls.

### Migrated (in scope for B/C public apply)

| Flow | Public tools | Why it is in scope | Code anchors | Today vs target |
| --- | --- | --- | --- | --- |
| First plan (cold) | `prepareCoachDecision` → `applyCoachDecision` | Prepare translates `change_request` to `project_initialization_request`, returns derived candidate `plan_id` / `plan_version`, and a signed `kind: initialization` proposal. Apply through `_first_plan_body` refuses those echoed identifiers (`_refuse_first_plan_fields`). Issue #280. | `gateway.py` `prepare_decision` (no store → `prepare_initialization`); `prepare_initialization` return; `apply_decision_request` empty-store branch; `_refuse_first_plan_fields`; `_initialization_claims`; `mcp_transport.py` schemas | Today: apply can be `{proposal, confirmed}` if the model omits the candidate ids; echoing them is 400. Target: prepare does not return those ids as authoritative identity; apply is `{proposal, confirmed}` only. |
| Warm plan update | same pair | Prepare requires durable `plan_id` / `plan_version` / context from `startCoachSession` (current PlanState). Apply already infers them from the signed `kind: decision` proposal. | `prepare_decision`; `apply_decision_request`; `_decision_claims`; `_hold(HELD_CHANGE_REQUEST)`; `_retain_context` | Today: `{proposal, confirmed}` already works. Target: same, with no extra echoed fields. |
| Calendar delivery | `prepareWorkoutDelivery` → `applyWorkoutDelivery` | Two-phase confirmation. Prepare returns `proposal_hash` plus `delivery_set`. Apply accepts `proposal_hash` + `confirmed` while the set is held. Content hash, not `proposals.py` signed claims. | `prepare_delivery`; `apply_workout_delivery`; `delivery.py` set hash; `_hold(HELD_DELIVERY_SET)` | Today: opaque token is named `proposal_hash`. Target: that token is named `proposal`. No `proposal_hash` alternative. |
| Delivery withdrawal | same pair, `withdraw: true`, **or** bundled in `prepareCoachDecision.preview.calendar_delivery` | Same two-phase handoff; direction is a hashed field of the delivery set. The product's ordinary leftover-calendar case after a delivered session is replaced is the one-confirmation decision preview. | `prepare_delivery` withdraw branch; `prepare_withdrawal_set`; `decision_delivery.py` | Target apply remains `{proposal, confirmed}`. |

### Excluded from public MCP (and from the B/C catalogue cut)

| Flow | Decision | Why | Anchors |
| --- | --- | --- | --- |
| Public `prepareOwnerDeletion` / `applyOwnerDeletion` | **Exclude.** Do not resurrect. | Product boundary since 1.4.5 (issue #417): a client approval layer could refuse the confirming call without the service hearing. Cached names get `account_deletion_moved` and the support URL. | `mcp_transport.py` `RETIRED_TOOLS`; `exportOwnerData` description; `docs/distribution/openai-plugin.md` |
| `confirmActivityMatch`, `confirmPrescribedStrength`, `clearDeliveryAttempt` | **Exclude.** | Single call; no prepare output to carry. | catalogue |
| `record*` / import / retract / reads | **Exclude.** | Not confirmation-bound mutations. | catalogue |

### Operator deletion — equivalent pair, excluded from public MCP, excluded from the B Proposal kernel

**Decision: exclude from the shared MCP Proposal kernel. Keep as an operator confirmation-bound pair. D must demonstrate the actual operator transaction.**

It is an equivalent preview → exact-scope confirmation → commit pair (`privacy_request.deletion_scope` → `apply_deletion`). It is **not** model-carried, has **no TTL** (email is slow), and binds with an unsigned HMAC digest the operator retypes (`_scope_digest` over `{owner: binding(owner_id), preview}`), not a signed token a process holds. Putting it on `issue_proposal` would either expire email confirmations or invent a second token for the operator to paste — an operator-step change this issue does not authorize. Athlete steps stay: support page, then email; the operator runs the CLI.

Reuse that **is** already in place: `proposals.binding` and `canonical_hash`. That is enough. Do not fold operator deletion into B's in-memory Proposal record.

Excluding the retired public tools does **not** satisfy D. D must run `deletion_scope` → confirm the returned `scope_digest` → `apply_deletion` → absence/read-back on a real isolated owner, including a scope-changed refusal. A characterizes that path on an anonymous local owner in `OperatorDeletionCharacterizationTests`.

---

## The single server-owned prepared record (B)

One in-memory record per confirmation. Public apply for every migrated MCP flow:

```text
applyX({ proposal: <opaque token this gateway issued>, confirmed: true })
```

No other apply fields. Extra server-derived keys are `invalid_request`, not ignored and not bound as optional echoes. Routing first-vs-existing from a client `plan_id` stays forbidden. This is the request for a **new commit**; an already committed calendar approval retains its existing proposal-only retry, without asking for another yes. Receipt lookup below owns that distinction, not an alternate coaching-input branch.

### Lookup identity

| Piece | Value |
| --- | --- |
| Client token | the signed `proposal` string from `issue_proposal` |
| In-memory key | `_proposal_key(proposal)` (`sha256` of the token), already used by `_hold` |
| Owner | `claims.owner` = `proposals.binding(owner_id)` via `_owner_binding` |
| Kind | `claims.kind`: `initialization` \| `decision` \| `delivery` \| `withdrawal` |
| Open | Verify signature, owner and kind; check existing committed receipts first. Only an uncommitted proposal then needs `_open_proposal` release/expiry handling and `_held_payload` lookup. |

Missing **uncommitted** ephemeral effect (expired, restart, wrong process): refuse with re-prepare. Cache loss does not erase a **committed** approval: `read_confirmed_delivery(state_dir, approval_key=_proposal_key(proposal), claims=verified_claims)` checks committed sequence membership, receipt/plan hashes and signed effect binding before returning its exact recorded calendar effects. Preserve that lookup before requiring a hold, live proposal clock, retained context or a fresh confirmation. Do not recompute an approved effect from coaching inputs. Durable PlanState remains readable through `startCoachSession` / `getCoachState`.

### Canonical stored exact effect (ephemeral payload)

Hold the **bytes that will be committed**, not the coaching request that produced them.

| Kind | Payload bytes (deepcopy in `_Held.payload`) | Digest |
| --- | --- | --- |
| `initialization` | `{plan, preview, calendar?}` — the candidate PlanState `project_initialization_request` already built | `canonical_hash(plan)` (today `plan_hash`) |
| `decision` | `{after_plan, decision_event, preview, calendar?}` | `after_hash` + `event_hash` |
| `delivery` / `withdrawal` | the delivery/withdrawal set already held as `HELD_DELIVERY_SET` | set `proposal_hash`, renamed `effect_hash` inside signed claims |

**Compound plan+calendar (existing, not a new path).** `prepare_initialization` and `prepare_decision` already call `_prepare_calendar` and `_hold_calendar` (`HELD_DECISION_DELIVERY`). Claims already carry `delivery_hash` and `publish_new_workouts` when calendar effects were previewed. That held calendar payload **is part of the same prepared record** as the PlanState effect, not a second transaction and not only `prepareWorkoutDelivery`. Commit of the plan and attempt of those exact calendar effects stay under one confirmation. Incomplete approved calendar effects retry with the **same** `proposal` (`_resume_confirmed_calendar` / stored delivery receipts via `read_confirmed_delivery` / `open_delivery_attempt`); no second yes; no new durable migration or state machine. Precommit account check (`_require_approved_calendar_account` / target-account binding) stays. Standalone `prepareWorkoutDelivery` remains the leftover/resume path for a set that was not in that confirmation.

**Validation metadata travels beside the effect.** Retain the bound decision context (`context_id`, `context_hash`, evidence provenance/digest), base identity/version/hash, and first-plan safety inputs (`red_flags`, issued instant and the preview's local day) needed by the existing commit checks. Hash-check the retained material against its binding. A frozen plan is not a cached validation verdict: metadata must still reach `_validate_initial_plan` and `apply_confirmed_decision(context=..., after=..., event=...)`, including `validate_bundle` under the store lock. Keep existing fresh-evidence and positive-safety checks; missing optional evidence remains unknown and cannot by itself impose rest or human review.

**Authored inputs are recovery material, never the effect.** Retain the original `change_request` / `initialization_request` and prepare-only publication/safety inputs separately inside the same ephemeral record so existing superseded-preview recovery can reuse the athlete's authored decision. Bind them to that proposal; `publish_new_workouts` must retain its signed value. They may produce a **new preview/token only after refusing the old commit**, never replace the held plan/event/calendar bytes during commit. Today's `HELD_CHANGE_REQUEST` / `HELD_INITIALIZATION` must stop being the source of committed effects. `HELD_DELIVERY_SET` and `HELD_DECISION_DELIVERY` already hold exact side-effect bytes — keep that pattern.

### Exact preview / confirmation binding

Signed claims from `issue_proposal` (via `_issue_proposal`, which stamps `release`):

| Claim | Meaning |
| --- | --- |
| `kind` | route |
| `owner` | keyed owner handle |
| `release` | `_release_binding()` |
| `issued_at` / `expires_at` | stamped by `issue_proposal`; never caller-chosen |
| `effect_hash` | hash of the held exact effect |
| `preview_hash` | `canonical_hash(preview)` — words shown |
| `confirmation_required` | false only when the projection moved nothing |

These common claims do not replace the existing kind-specific bindings: preserve `plan_hash` or `after_hash`/`event_hash`, `plan_id`/`base_version`/`before_hash`, `context_id`/`context_hash`/`evidence_digest`, and compound `delivery_hash`/`publish_new_workouts` as applicable. The shared record must supply the same verified claims and metadata to the current store and delivery checks; no durable `effect_hash` receipt migration is required.

`confirmed: true` is required for an uncommitted effect when `confirmation_required`. The athlete's yes is not stored in the hold; it is the apply argument. For committed calendar effects, the existing durable receipt proves that approval for exact-effect replay; a missing `confirmed` on that retry does not undo it.

### Base revision / evidence staleness

| Kind | Staleness check at commit |
| --- | --- |
| `initialization` | no base; store must still be absent (or identical first version → duplicate success) |
| `decision` | `plan_id`, `base_version`, `before_hash` vs current head under the store lock (`apply_confirmed_decision` already re-reads `before_hash` under the exclusive lock); `evidence_digest` vs a fresh read when evidence can be read (issue #358) |
| `delivery` / `withdrawal` | current `plan_id` / `plan_version`; set hash; account binding (`target_account`) |

Moved head or moved evidence: refuse the old commit. If bound recovery inputs remain, use them to prepare a new preview against current evidence, preserving the original publication intent and requiring confirmation of the new preview. Apply carries only proposal/confirmed: current context cannot reconstruct an authored decision, and there is no unstated or resent request to recover from. If the recovery inputs are gone, return reprepare-required; the model must supply authored inputs to **prepare**, not to an apply compatibility branch. A different plan restored at the same version still receives the existing store refusal, not an automatic rebase. First-plan expiry stays clock-bound (`PROPOSAL_TTL_SECONDS`), as in `apply_initialization`.

### Side-effect scope

`none` | `calendar_effects` | `settings_and_calendar`. Frozen into the held calendar payload (`HELD_DECISION_DELIVERY` today) and into delivery-set `settings_changes`. Commit attempts only those exact effects.

### Expiry

For **uncommitted** effects: `issue_proposal` TTL (`PROPOSAL_TTL_SECONDS`) plus hold `until` (`CONTEXT_RETENTION_SECONDS` today). First-plan commit stays fail-closed on expiry. Warm decisions that can re-read evidence use `evidence_digest` as the real bound; the clock is the fallback when evidence cannot be read. Missing effect after restart requires reprepare. Already committed calendar approvals follow durable replay rules below, not ephemeral TTL. Operator deletion is outside this kernel and has no TTL.

### Idempotency vs stale replay

| Outcome | When |
| --- | --- |
| Duplicate success (`idempotent_replay: true`) | verified existing receipt matches the exact approved effect using existing hashes (first-plan `plan_hash` at version 1; decision `plan_hash`/`event_hash`/`context_hash`); preserve current replay rules |
| Committed compound calendar retry | `read_confirmed_delivery` finds the exact approved receipt by `approval_key`; finish/replay only its recorded effects through `_resume_confirmed_calendar`, even after cache loss or later delivery commits; no new yes |
| Uncommitted proposal refused | head/evidence moved, missing/expired effect, missing confirmation, or integrity/owner/kind mismatch, subject to the per-kind expiry rules |
| Missing ephemeral record and no matching committed receipt | re-prepare; no authority to commit or reconstruct an effect |

Committed replay preserves `_replayed_calendar_account`, current-prescription/identity checks, positive-symptom handling, delivery reservations and read-back rules. It does not authorize re-publication of changed content, invent a new receipt schema, or bypass a delivery lock.

### Commit without re-authoring

Apply:

1. Validate the allowed input keys and authenticate token signature, owner and kind. Preserve the existing committed-receipt path before ephemeral release/expiry handling.
2. Look for the exact committed approval using verified claims and existing receipt checks. If found, return duplicate success under that kind's existing conditions or resume its recorded calendar effects. Committed calendar replay requires neither a held payload nor another yes; retain the existing confirmation conditions for other duplicate paths.
3. Only for an uncommitted proposal, enforce `_open_proposal` release/expiry rules, load the exact effect and bound validation metadata, and verify their hashes. If absent, refuse with reprepare-required. Require `confirmed is True` when claims say so.
4. Run the existing staleness, structural, positive-safety and identity checks against the frozen effect and bound metadata. Refusal may generate a separately confirmed replacement preview from retained recovery inputs; it must not substitute a new plan into this commit. Do **not** call `project_initialization_request` / `project_change_request` to build the effect being committed.
5. Commit the held bytes through the existing writers and locks:
   - initialization → `_validate_initial_plan(held.plan, red_flags=..., today=...)` then `init_store(state_dir, plan=held.plan, proposal_claims=claims, confirmed_delivery=...)`
   - decision → `apply_confirmed_decision(..., context=held.context, after=held.after_plan, event=held.decision_event, proposal_claims=claims, confirmed_delivery=...)`; `_apply_decision` re-reads the head, checks `before_hash` and validates the bundle under the exclusive lock
   - delivery/withdrawal → existing apply of the held set

For compound effects, `confirmed_delivery` retains the existing `{approval_key: _proposal_key(proposal), prepared: held.calendar}` envelope. `_calendar_account_for_apply` / `_require_approved_calendar_account` run before the plan commit, and the plan writer persists the approved calendar effects with it. Prepare-time validation never substitutes for commit-time checks: revalidation is deterministic integrity, not coaching. Hash mismatch between held effect and claims is `proposal_mismatch` (corruption), not a prompt to re-project.

### Per-kind validation (prepare and commit)

| Kind | Validator | Harmful case | Valid-workflow control |
| --- | --- | --- | --- |
| `initialization` | `_validate_initial_plan` (structure and adopted-plan safety) at prepare **and apply**, plus `init_store` integrity/locking | first week trains today after an explicit true symptom, including one reported since preview | rest today, train later; unstated symptoms remain unknown (`FirstPlanSymptomBoundaryTests`) |
| `decision` | `validate_bundle` at prepare/apply and again in `store._apply_decision` under lock; bound context, head hash/version and delivery fence | structurally invalid change; changed identity/head; positive safety conflict | one session moves, week otherwise unchanged |
| `delivery` | `prepare_delivery_set` plus existing apply set/account/current-plan/ownership/reservation checks | flipped direction; wrong account | retry identical approved set to convergence |
| `withdrawal` | `prepare_withdrawal_set` plus existing apply identity/ownership/read-back checks; only product-owned events | deleting a non-owned event | remove the superseded event named in preview |
| operator deletion (not this kernel) | `deletion_preview` / fence | digest for another owner; scope moved | isolated owner erase + bystander untouched |

No missed-session, recovery, or progression policy enters these validators (AGENTS.md 5–6, issue #82).

Regression anchors that B must preserve:

- `tests/test_gateway.py::FirstPlanSymptomBoundaryTests`: `test_the_confirmation_is_judged_on_what_the_athlete_has_said_by_then`, `test_the_same_symptom_leaves_a_first_plan_that_rests_today_open`, and `test_an_athlete_who_stated_nothing_authors_the_identical_first_plan`. Preserve their safety/control outcomes when C cuts input fields; do not silently drop a newly observed positive signal or add compatibility inputs.
- `tests/test_decision_delivery.py::CombinedDecisionJourneyTests`: `test_partial_failure_resumes_the_saved_approval_after_cache_loss_without_another_yes`, `test_a_rehashed_receipt_cannot_change_what_the_signature_approved`, `test_a_different_current_prescription_cannot_reuse_a_saved_calendar_approval`, and `test_a_new_positive_symptom_holds_only_todays_delivery_for_a_human_decision`.
- The same class's `test_moved_evidence_repreviews_the_original_signed_publication_request` and `test_moved_evidence_does_not_add_publication_to_a_plan_only_preview` bind recovery to the authored intent; `FirstPlanCombinedJourneyTests.test_one_first_plan_preview_can_include_first_delivery_and_survive_replay` binds cold-start durable replay. First-plan recovery after **total** cache loss cannot use the legacy test's resent apply `change_request` after C; it must accurately require a new prepare.

### Reuse — no parallel framework

| Primitive | Role |
| --- | --- |
| `proposals.issue_proposal` / `open_proposal` / `binding` | signed token, owner handle, TTL stamp |
| `CoachGateway._issue_proposal` / `_open_proposal` | release stamp; owner/kind/release check |
| `_hold` / `_held_payload` / `_Held` | ephemeral exact-effect store; key = `_proposal_key(proposal)` |
| `_initialization_claims` / `_decision_claims` | fold into one claim builder; keep kind-specific hashes |
| `canonical_hash` | effect and preview hashes |
| `store.init_store` / `apply_confirmed_decision` | durable PlanState commit of **held** plan/event bytes |
| `HELD_DELIVERY_SET` + delivery apply | exact set commit; wrap with signed `proposal` in C |
| `HELD_DECISION_DELIVERY` / `_hold_calendar` / `delivery_hash` / `publish_new_workouts` | exact calendar side effects on the same confirmation as the plan |
| `_resume_confirmed_calendar` / `read_confirmed_delivery` | retry incomplete approved calendar effects on the same proposal |
| `_require_approved_calendar_account` / `_calendar_account_for_apply` | compound precommit account checks; keep (`_require_approved_account` is the standalone delivery check) |
| `privacy_request._scope_digest` | operator pair only; not this kernel |

Drop as the source of the committed effect: re-projection from `HELD_CHANGE_REQUEST` / `HELD_INITIALIZATION` request bodies.

### First-plan prepare identity (C)

Prepare may derive an internal plan id for the candidate. It must not return that id as top-level authoritative `plan_id` / `plan_version` before apply succeeds. After apply, those fields are the durable store identity. A current-behavior lock in A records that today they are still returned; delete that lock in C.

### Ephemeral vs durable

| Material | After C |
| --- | --- |
| In-memory uncommitted effect / context / recovery inputs | May be invalidated. Restart already forgets them. Missing hold and no committed receipt → re-prepare. No shim for pre-migration previews. |
| Signed proposal | Uncommitted tokens from before C may `proposal_mismatch`; prepare again. Existing committed calendar approvals still use authenticated receipt lookup before ephemeral release/TTL rules. |
| Durable PlanState + history | **Remain readable.** Frozen stores in `tests/fixtures/frozen_stores/` must still `doctor_store`. No PlanState schema migration for this architecture. |
| Existing committed calendar approval | Remains exact-effect retry authority through `approval_key` / `read_confirmed_delivery`; no new receipt schema or second confirmation. |

---

## Issue #280 repair criterion

**Producer (today):** public `prepareCoachDecision` with no PlanState returns non-null candidate `plan_id` and `plan_version`.

**Broken consumer (today):** copying those keys, when present, into `applyCoachDecision` with the same `proposal` and `confirmed: true` is `invalid_request`. Current-behavior lock: `test_issue_280_echoed_ids_are_refused_on_public_mcp_today` (delete in C).

**Repair (C):** first-plan prepare omits those non-durable ids as authoritative identity. Apply is `{proposal, confirmed: true}` only. The intended test (`test_issue_280_first_plan_apply_from_producer_must_persist`) requires `proposal`, copies `plan_id` / `plan_version` **only if the producer returned them**, and asserts the previewed week persists. It must not invent those keys, and C must not accept them as a compatibility echo. After C omits them, the body is proposal plus confirmed and the test turns green; remove `@unittest.expectedFailure`. Unwrap today with `ISSUE_435_UNWRAP_280=1`.

Duplicate identical first apply still converges. Existing-plan prepare still names the **current** durable plan on prepare, not on apply.

---

## Public-flow characterization (A tests)

`tests/test_confirmation_transactions.py` uses JSON-RPC `tools/call`. Helpers are not a subclass of `McpJourneyTests`.

| Path | Producer | Apply body | Assertion |
| --- | --- | --- | --- |
| Cold first plan, valid | `prepareCoachDecision` `{change_request}` | `{proposal, confirmed: true}` | stored week matches preview goal, dates, prescriptions, minutes |
| Cold first plan, #280 | same | proposal + confirmed + identity keys **if returned** | today 400; expectedFailure wants persist of previewed week |
| Warm update | `prepareCoachDecision` | `{proposal, confirmed: true}` | stored replaced session matches preview `after` |
| Delivery (current field name) | `prepareWorkoutDelivery` | `{proposal_hash, confirmed: true}` — today's producer token name | previewed `session_id` recorded on PlanState execution; FakeIntervals is a test double |
| Withdrawal / compound plan+calendar | deliver, then `prepareCoachDecision` rest | `{proposal, confirmed: true}` | stored session is rest as previewed; fake calendar empty; same proposal retries without a second yes |
| Operator deletion | `deletion_scope` | `apply_deletion(scope_digest=preview.scope_digest, confirmed=true)` | directory absent; tombstone; moved-scope refused |

Durable retry of a partial calendar after a committed plan is already covered by `tests/test_decision_delivery.py` (`CombinedDecisionJourneyTests.test_partial_failure_resumes_the_saved_approval_after_cache_loss_without_another_yes` and `test_one_first_plan_preview_can_include_first_delivery_and_survive_replay`). A does not add a second state machine for it. The withdrawal characterization retries the same proposal after a successful compound commit.

No apply body is synthesized from the consumer schema. Delivery's current `proposal_hash` is characterized as current, not as an allowed target alternative.

---

## Stale / wrong-owner / replay / duplicate / mismatched confirmation

Keep existing gateway.route coverage. A adds public-MCP first-plan wrong-owner, unconfirmed, and duplicate, plus operator scope-changed refusal.

| Case | Current behavior | C must keep |
| --- | --- | --- |
| Wrong owner | `proposal_mismatch`; nothing written | yes |
| Unconfirmed | `confirmation_required` | yes |
| Expired uncommitted first plan | `proposal_superseded` + fresh prepare when authored inputs remain | preserve recovery when retained; otherwise require prepare, never infer a decision |
| Duplicate identical first apply | `idempotent_replay`; store unchanged | yes |
| Replay onto a moved plan | `plan_state_exists` | yes |
| Edited request vs proposal | `proposal_mismatch` | frozen-effect/binding integrity stays; extra apply request fields are `invalid_request` after C |
| Warm evidence moved | `proposal_superseded` | yes |
| Uncommitted delivery set no longer held | re-prepare | yes; committed compound receipt recovery is separate |
| Operator scope moved | `PrivacyRequestError`; store remains | yes |

C must **not** add blocks for suboptimal coaching.

---

## Durable PlanState read compatibility

A does not change `contracts/plan-state.schema.json` or the writer contract. Frozen `tests/fixtures/frozen_stores/current_contract` must still open. B/C should not require a PlanState migration.

---

## Coaching quality characterization

Deterministic validity tests are not live model quality proof. Absent evidence stays absent. The A baseline is a **limited worker self-score**, not independent quality evidence.

Repo holds: `evals/baselines/issue-435-a-same-evidence-quality.json` bound to `evals/cases/revisit-today-a-missed-session-is-not-a-debt.json` (same scenario and given), generic `xai` / `grok-4.6`, original answer and rationale. Structural binding is in `tests/test_evals.py`. Runtime session/launch/time receipts are not in the repo.

Unavailable: no #86 harness run; no Claude/ChatGPT live coaching comparison; no D acceptance.

---

## Client before/after (athlete actions)

A and C must not change the number or type of athlete actions.

| Mutation | Before | After C |
| --- | --- | --- |
| First plan | One athlete yes. Client may approve `applyCoachDecision`. | Same one yes. Apply arguments are proposal + confirmed only. |
| Warm plan change | Same one yes. | Same. |
| Delivery / withdrawal | Same one yes. | Same. Apply token renamed to `proposal` in C. |
| Account deletion | No MCP tool. Support page; operator erases. | **Unchanged.** |

Do not add a second confirmation or a new OAuth prompt. Do not remove the athlete's yes.

---

## Model-readable surface budget

A pays **zero** catalogue/instruction/Skill bytes.

C pays **one** cut:

- Replace `applyCoachDecision`'s "For a first plan, the proposal alone -- still no plan_id" and `prepareCoachDecision`'s "Omit for a first plan" with: apply sends the opaque `proposal` plus `confirmed: true`; prepare does not return a candidate plan identity.
- Replace `applyWorkoutDelivery`'s `proposal_hash` / optional `delivery_set` with the same `{proposal, confirmed: true}` rule. One name: `proposal`.
- Do not add a tool. Do not add a second apply generation.

`tool_catalogue_sha256` moves once, in C. A leaves it equal to `origin/main`.

---

## Evidence D still owes

1. Fresh isolated owner → first-plan preview → confirmation → persisted plan read-back, on claude.ai connector and ChatGPT custom app.
2. Existing plan → change → confirmation → persisted update, **same ChatGPT app and same session**.
3. Delivery preview → confirmation → Intervals write/read-back.
4. **Operator deletion transaction** (`deletion_scope` → confirm returned digest → `apply_deletion` → absence/read-back), not merely showing the support URL. Public MCP deletion tools stay retired.
5. Stale / wrong-owner / replay / duplicate / mismatched confirmation on the live gateway.
6. Same-evidence coaching comparison vs the committed baseline; independent scoring. A self-score is not that.
7. **Issue #433 independent.** Do not assume #435 fixes it. A prior ChatGPT first-plan success on 2026-09-11 does not close later nondispatch. D must record the exact app id, run first-plan **and** update in that same app/session, and archive the access log the same day.
8. One production release; one new OpenAI submission against the final catalogue.

---

## Intelligence guardrail (do not regress #82)

The server may freeze, validate, authorize, and commit. It may not decide missed session → reduce density; poor recovery → remove intervals; stalled strength → change rep range; or fixed progression percentages. `none_found` means no matching evidence was observed, never that the week was too full.
