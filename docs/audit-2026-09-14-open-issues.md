# Audit of five open issues — verdicts and evidence

Re-adjudicated 2026-09-14 against `main` after PRs #445 #446 #447 #448 #449 #450 #451.
Every claim below was checked against running code on that main, not against the issue text.

## #397 — five findings from PR #394's independent reviews

| # | Finding | Verdict | Evidence |
| --- | --- | --- | --- |
| A1 | A retained delivery set can rejoin a same-second reopened reservation | **Superseded** by #443/#444 | See below |
| A2 | A session focus returns other sessions sharing its day; the code comment justifying it is false | **Fixed here** | `context_view.py` `_matches`; `readCoachEvidence` focus description |
| A3 | `training_history`'s kept/of adds `months` and `movement_longevity`, two units | **Fixed** by #437 (`07f26f3`) | `context_view.py` `_SECOND_ROWS`; `test_mixed_focus_counts_every_original_row_of_each_unit`, `test_matching_all_rows_reports_complete_counts_in_both_units` |
| A4 | Uploading recovery readings makes a `records` read plan-bearing | **Still real**, Deferred | Refiled as its own issue with a trigger |
| A5 | The `no_plan_state` guidance digest says "an earlier release" when the account gets its first plan; and `guidance_received: true` with no digest claims "unchanged" unconditionally | First half **fixed** by #436 (`bf67359`); second half **fixed here** | `gateway.py` `_guidance_answer` |

### A1 in full — why no `DELIVERY_ATTEMPT_SCHEMA_VERSION` bump is needed

An `attempt_id` is `canonical_hash({plan_id, plan_version, proposal_hash, session_ids})[:24]`, so
abandoning a delivery and confirming the identical one again opens a second reservation carrying
the same id. PR #394 closed two halves. What was recorded as remaining was *a client that kept
the whole `delivery_set` body* applied to a reservation reopened inside the same second, because
`_utc_stamp()` truncates to seconds and `resumes_opened_at` cannot then separate the two.

That input path no longer exists. On current `main` the public apply tools are:

```
applyWorkoutDelivery  properties ['confirmed','proposal']  required ['proposal','confirmed']  additionalProperties False
applyCoachDecision    properties ['confirmed','proposal']  required ['proposal']              additionalProperties False
```

A client cannot supply a delivery set body at all — it holds an opaque `proposal`, and the set
lives in the server's held record. Three layers now stand where one did:

1. `clearDeliveryAttempt` drops every held set that exists to finish that reservation
   (`gateway.py` `_forget_held_sets_for_attempt`), and clearing *is* the abandonment path;
2. `resumes_opened_at` must equal the reservation's own opening instant (`delivery.py:1987`);
3. there is no client-supplied body left to replay.

The CLI is not a second route: `cli.py:1229-1251` runs `prepare_delivery_set` →
`approve_delivery_set` → `deliver_approved_set` inside one command, constructing the set fresh.

**Honest limit.** `_utc_stamp()`'s second truncation is still true; it is simply no longer
load-bearing for this case, because the guard is now structural rather than temporal.
**Reopen trigger:** any future change that reintroduces a client-supplied delivery set body.

## #280 — first-plan preview/apply `plan_id` handoff

**Fixed** by PR #444 (`54c20a6`). Public JSON-RPC `tools/call` on an anonymous temporary
gateway/store produced:

```json
{"prepare_identifiers":{"plan_id":"ABSENT","plan_version":"ABSENT"},"store_before_apply":false,
 "mechanical_apply_status":"passed","applied_plan_version":1,"stored_version":1,"replay_status":"passed"}
```

Apply was built mechanically from the actual prepare output, copying identity keys only if
returned. No identifier came back for the next call to reject. Inventing them remains
`invalid_request`. Regression: `FirstPlanPublicFlowTests.test_issue_280_first_plan_apply_from_producer_must_persist`.
Local public-MCP evidence with a fake provider, not live client acceptance; #433 stays independent.

## #312 — the session's own fallback is never named in served text

**No `fallback first` instruction approved.** The body retained the withdrawn short-day premise;
it has been replaced with the corrected evidence and an exact reopen trigger. PR #379 (`87c8044`,
corrected asset `ddf37b4`) rebound the case to a genuine 30-minute time-specific reduction and
kept the unchanged-recovery control. No coaching behaviour or served prose changed.

## #25 and #86 — eval coverage

Behaviour cases (PR #311) and the repeated-sample harness are complete; neither issue was
rebuilt. Each is shrunk to one explicitly named missing measurement with an execution trigger.
No stochastic model evaluation is wired into `python3 -m unittest discover -s tests`;
`test_within_session_drift.py` measures physiology with fake streams, not model variance.

## Digests moved by this change

| | before | after |
| --- | --- | --- |
| `tool_catalogue_sha256` | `72af60b009c0932d1e936af568900c099c64c5d09ed1a4279f71ed935e789605` | `136e8d0cdb16f3f7ab7d92d3794dab6fdc020361779d3239705ec4f02d09ceaf` |
| `instructions_sha256` | `de723aea9e88e1e2e766c0242f5e3f16fd4ec8d932e8dc2a2f2e9bc40fd6e2ec` | unchanged |
| `skill_sha256` | `fa8342cf49f9b47f137f223f318d205bc65200798994ab4f25ba3bf52f978c17` | unchanged |

Two tool descriptions moved the catalogue, both because they said something untrue. Scan Tools
and client acceptance are therefore genuinely required before the next submission — the digest
moved, not merely a path rule. Versioning, ceremonies and deployment belong to the release stage.

The A5 replacement costs 250 characters where the full guidance is 10,020, so the truthful
answer is roughly forty times cheaper than re-sending the text (AGENTS.md 13).
