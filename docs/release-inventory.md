# Release inventory — what this round added, moved, derived, and deliberately did not keep

The item-by-item backing for the counts README publishes. README carries the journey and
the counts; this file carries the enumeration, so a reader who wants to check a claim can
find the exact file, record shape, and lifecycle without reading source.

Audited against `main` at the commit production reports under `/readyz`
(`source_git_commit`), not against any open branch. The working count in the issue that
asked for this document was **5 / 2 / 1 / 12**; four things merged after it was written,
and the corrected count is **8 / 5 / 1 / 14**. Every difference is named below.

Interface scale, all of it derived from code by tests rather than written down here:
**22 MCP tools** (`garmin_coach_loop.mcp_transport.TOOLS`), **2 prompts**
(`coach_orchestration` and `coach_training_judgment`), **31 CLI commands**, **4 JSON Schema contracts** under
`contracts/`, **9 identity tables**.

---

## 1. New persisted data shapes (8)

| # | Shape | Where it lives | Lifetime | In an export? | Removed by owner deletion? |
| --- | --- | --- | --- | --- | --- |
| 1 | Local hosted-handoff seal (`hosted-handoff.json`, schema 1.0) | inside the **local** store directory | until the athlete removes that directory | no — never enters a bundle | n/a, it is not in the hosted store |
| 2 | Per-owner revocation boundary (`owner_revocations.revoked_after`) | identity registry | until deletion | count, plus the instant as ISO-8601 | yes |
| 3 | Cycle outlook (`cycle.outlook`) | PlanState, inside the commit chain | with the plan version that holds it | yes, inside `plan_state` / `decision_history` | yes |
| 4 | Goal measurement protocol (`goal.measurement`) | PlanState | same | yes | yes |
| 5 | Per-session measurement marker (`session.measures`) | PlanState session | same | yes | yes |
| 6 | Owner maintenance fence / deletion tombstone (`<owner>.maintenance`, schema 1.0) | **beside** the owner directory, never inside it | a cutover holds it for the operation; a deletion seals it and it is kept permanently | no | deliberately left standing — it *is* the deletion's record |
| 7 | Body measurements (`athlete_evidence.body_measurements`) | `athlete-evidence.json` | until deletion | yes, inside `athlete_evidence` | yes |
| 8 | Reported activity summaries (`athlete_evidence.reported_activities`) | `athlete-evidence.json` | until deletion | yes, inside `athlete_evidence` | yes |

Items 1–5 are the ones the issue listed. Items 6–8 merged afterwards:

- **6** arrived as the cutover fence and was then reused, unchanged in shape, as the
  deletion tombstone (`tombstone: true` plus `deleted_at`). It is a sibling file rather
  than a store member on purpose: it does not enter the commit chain `doctor-store`
  revalidates, does not enter a bundle, and does not move `WRITER_CONTRACT_VERSION`. An
  older checkout opens the store exactly as before, and refuses writes against a fence it
  does not recognise — which is the safe reading.
- **7 and 8** are two new containers in the existing athlete-evidence file. The file's
  version did **not** move: an absent container reads as empty, so a file written before
  these groups existed opens unchanged rather than being refused whole.

Two properties worth stating for 7 and 8, because they are what keeps a second data source
from becoming a second truth:

- **A reported activity is not an actual.** It carries no activity id, no match confidence
  and no completion. It never enters `recent_actuals`, moves no coverage row, and
  reconciliation does not read it. It can therefore never complete a planned session.
- **Nothing is derived from either.** A weight series is handed to the coach raw — no
  trend, no rate of change, no comparison against a target.

## 2. Existing data moved or copied, not newly invented

- The canonical PlanState append-only chain (`store.json`, `commits/*/plan.json`).
- Decision events and receipts (`commits/*/event.json`, `receipt.json`).
- Athlete evidence: profile, availability, strength reports (`athlete-evidence.json`).
- The portable store bundle carrying those records.
- The existing local `health.db`, which stays local and is **not** silently migrated.

## 3. Derived or response-only views (5)

Computed per request, never written to disk.

| # | View | Produced by | Notes |
| --- | --- | --- | --- |
| 1 | `measurement_evidence` | CoachContext build | Says whether each of the two readings is in. Computes no verdict. `null` when the cycle declared no measurement — a real state, not an unproven one. |
| 2 | Owner data export archive | `exportOwnerData` | `archive_version`, `owner_reference` (a keyed handle, not the owner id), `identity` (five row counts + `revoked_after` + `token_scope_names`), `plan_state`, `decision_history`, `athlete_evidence`, `unresolved_delivery`, `excluded`, `unknowns`. |
| 3 | Deletion preview | `prepareOwnerDeletion` | `removes` (plan id, plan versions, each evidence group's count, `identity_rows`, `stored_snapshots`), `not_removed`, `reversible: false`. Computed by the same code path that performs the removal, so the two cannot disagree. |
| 4 | Deletion receipt | `applyOwnerDeletion` | `deleted`, `receipt_id` (`gcd-…`), `removed`, `not_removed`. Carries no owner id, no fingerprint, no plan content. |
| 5 | Stored-plan summary | `getCoachState` | plan id/version, `cycle.outlook_weeks`, `week.session_count`, `goal`, `delivery`, `pending_delivery_attempt_id`. Deliberately thinner than a session's full PlanState. |

The issue listed 1 and 2. Items 3 and 4 are the deletion pair, counted separately because
they are distinct shapes with distinct guarantees; item 5 merged afterwards with
`getCoachState`.

## 4. Portable copy format (1)

`garmin-coach-loop-store-bundle`, schema 1.0 — the append-only chain and nothing else:
no lock, no delivery reservation, no snapshots, no handoff marker.

Unchanged in count, changed in how it is written. `export-store` now opens a
same-directory temporary file with `O_EXCL` at mode `0600` **before any byte is written**,
so the mode is exact from creation regardless of umask; the payload is written, flushed and
fsynced, then installed with one atomic `os.replace`. The destination is refused — as a
symlink or as an existing path — both before the temporary file is opened and again
immediately before the install. Any failure removes the temporary file, so nothing partial
is ever left under the destination's name.

## 5. Ephemeral or explicitly excluded material

Never written to disk by this product, and never in an export:

| Material | Lifetime | Where it actually lives |
| --- | --- | --- |
| OAuth `state` | one authorization | sealed into the value sent to the provider consent page |
| Gateway authorization code | 60 seconds | not stored; single use |
| Gateway access token | no stated expiry (the provider issues none) | the token **is** the storage — nothing server-side, no session, no SSE stream |
| Intervals access token | the provider's own lifetime | encrypted inside the gateway's token, under a key only the gateway holds |
| Token fingerprint | until deletion | a keyed one-way HMAC in the identity registry; it cannot be turned back into a token |
| Confirmation proposals (decision, delivery, deletion) | 900 seconds | signed, not stored — there is no proposal database, so a restart cannot forget an approval and an approval cannot outlive its claims |
| Returned CoachContext (`startCoachSession`) | 3,600 seconds, extended to the life of any proposal prepared against it | gateway process memory, per owner, newest four — so `prepareCoachDecision` / `applyCoachDecision` may name it by `context_id` instead of resending it (issue #355); never in the store or an export, forgotten by a restart and by account deletion |
| Previewed change request and prepared delivery set | the proposal's life plus a minute; 3,600 seconds | the same memory, keyed by the proposal / the set's own `proposal_hash`, so a confirmation carries the proposal or the hash alone; the after/event hashes and the set's own hash verify the held copy exactly as they verify a resent one |
| Delivery locks | the operation | the store's `.lock`; the reservation journal (`delivery-attempt.json`) is the part that survives, by design |
| Raw Intervals payloads, GPS tracks, activity files | the request | read to build a context, never written down |

Product boundary, stated once so it is not implied away:

- **No generic historical FIT or Apple Health upload.** Phase 1 of the evidence design is
  the conversational groups above; bulk import is phase 2, tracked at issue #140.
- **No promise to preserve raw provider payloads or GPS streams.** Export them from
  Intervals.icu itself.
- **No deletion of provider-side calendars or provider authorization**, unless the specific
  action says so — and none of them do.

## 6. User and operator workflow groups (14)

The twelve the issue named, then the two that merged after it.

| # | Group | Reached through |
| --- | --- | --- |
| 1 | Connect a hosted MCP client through OAuth | discovery → dynamic client registration → PKCE |
| 2 | Use several clients against one canonical owner store | `(provider, provider_athlete_id) -> owner -> one PlanState` |
| 3 | First use: minimal questions, one 28-day preview, one confirmation | `startCoachSession` → `prepareCoachDecision` → `applyCoachDecision`, with no `plan_id` |
| 4 | Start a hosted coaching session, including the reconciliation write it can perform | `startCoachSession` / CLI `hosted-session` |
| 5 | Review, change and confirm a plan; read delivery state | `prepareCoachDecision` → `applyCoachDecision`; delivery and withdrawal pairs; `clearDeliveryAttempt` |
| 6 | Migrate local state | `export-store` → `import-store` → `archive-store` → `seal-local-store`, all under the owner maintenance fence |
| 7 | Explicit offline-local mode, and why local + hosted are not two canonical stores | `--offline`, `GARMIN_COACH_LOOP_MODE=offline`, the handoff seal |
| 8 | Revoke connections and reconnect | `revoke-connections`; a reconnect in the same second as its own revocation now works |
| 9 | Export the authenticated owner's data in conversation | `exportOwnerData` |
| 10 | Delete owner data through preview and one explicit confirmation | `prepareOwnerDeletion` → `applyOwnerDeletion`, fenced and tombstoned |
| 11 | Read the current week plus the three-week outlook | `plan.week` + `cycle.outlook` |
| 12 | Configure and review an ordinary session as the cycle's measurement | `goal.measurement`, `session.measures`, `measurement_evidence` |
| 13 | **New** — read the plan with no write at all | `getCoachState` / CLI `hosted-status` |
| 14 | **New** — report a weight and a session no device recorded | `recordBodyMeasurement`, `recordActivitySummary` |
| 15 | **New** — say how you feel in your own words, and read it back as a pattern | `recordSubjectiveState`, `context.subjective_states` |
| 16 | **New** — one coaching sentence that travels with the workout to the calendar | `session.coach_note` |

Two mechanics changed inside groups above rather than adding a group of their own:

- The local hosted client now completes the MCP 2025-06-18 lifecycle — `initialize`,
  protocol-version and `tools` capability verification, `notifications/initialized`, and
  only then `tools/call` — on both authentication paths, so no caller can reach a tool
  call without it (groups 4 and 13).
- `archive-store`, `import-store`, `init-store` and `adopt-owner-store` now hold the owner
  maintenance fence end to end, and the deletion path holds the same fence and seals it
  (groups 6 and 10).

## 7. What holds these claims

- Tool count: derived from `TOOLS` in `tests/test_distribution_surface.py`; any prose
  count in README or an entrypoint README that disagrees fails the build.
- Export and deletion prose: `tests/test_owner_lifecycle.py` asserts the response payloads
  against `owner_data.EXCLUDED` and `owner_data.NOT_REMOVED` themselves, not against
  literals, and asserts the export's identity coverage equals the deletion preview's.
- Fence and tombstone behaviour: `tests/test_store_cutover_fence.py`,
  `tests/test_owner_deletion_fence.py`.
- Outlook and measurement: `tests/test_four_week_view.py`,
  `tests/test_measurement_protocol.py`.
- Reported evidence: `tests/test_athlete_evidence.py`, plus the gateway test that proves a
  reported activity never leaks into `context.recent_actuals`.
