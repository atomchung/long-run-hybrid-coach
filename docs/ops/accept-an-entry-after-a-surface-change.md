# Accept an entry after the model-facing surface changes

A release that moves `tool_catalogue_sha256`, `instructions_sha256` or `skill_sha256` is
a new reviewed surface. Every connected client re-reads it on its next `initialize` and
`tools/list`, and what a client *does* with the new bytes is not something the test suite
can answer — the suite proves what the gateway serves, not what a model does with it.

Run `python3 scripts/change_gates.py --base origin/main` first. Use this page only when its
output says `client_acceptance: true`; internal code, tests, docs and CI-only changes do not
need a real-client ceremony. A production `/readyz` read-back is still required after every
deployment.

This is the run-once sequence per entry. It is deliberately the same five steps
everywhere, because the coaching capability is entry-agnostic (AGENTS.md 10) and an entry
that needs a different sequence has found a real difference worth recording.

## Before any of it

1. Roll production and read [`verify-production-status.md`](verify-production-status.md).
   `/readyz` has to report the commit you think you deployed, and the three digests have
   to be the ones your checkout computes:

   ```bash
   python3 -c "import hashlib,sys; sys.path.insert(0,'.'); \
     from garmin_coach_loop import mcp_transport as T, orchestration as O; \
     print('tools     ', T.tool_catalogue_sha256()); \
     print('instructions', hashlib.sha256(O.instructions().encode()).hexdigest())"
   ```

2. Re-sync or reconnect the entry, so it is reading the new surface rather than a cached
   catalogue. A client that only refreshed its tool list may still be holding the old
   `instructions`.

## The five steps, per entry

| # | step | what makes it pass |
| --- | --- | --- |
| 1 | Real OAuth | The consent screen completes and the client reports connected. Re-authorizing without revoking is safe and needs no coordination; **revoking is not** — see CLAUDE.md, one revocation signs every entry out. |
| 2 | `startCoachSession` | A plan comes back. The formal account's email is the one displayed. `read` was chosen from the question rather than set to `all`. |
| 3 | `prepareCoachDecision` or `prepareWorkoutDelivery` | The whole preview is shown before any confirmation is asked for, and the Intervals account it would write to is named, email first. |
| 4 | Confirm and apply | One explicit confirmation, then the apply carries the proposal (or `proposal_hash`) and `confirmed: true` and nothing prepare already holds. |
| 5 | Read back from a **new conversation** | The event and its `intervals_accepted` are there, `unresolved_delivery` is null, and the prescription matches. A read-back inside the same conversation proves the client's memory, not the store. |

Step 5 against the provider rather than the plan is the rule that matters: the plan
records what the product intended, and only Intervals can say what it holds.

## Entry status, and what "unverified" means here

[`../../entrypoints/README.md`](../../entrypoints/README.md) is the status table and
[issue #380](https://github.com/atomchung/long-run-hybrid-coach/issues/380) carries the
receipts. Nothing in this document may be marked passed from an adjacent observation:
a client that displays the right name has not thereby applied a workout, and a merged PR
or a green CI run is not a deployment.

**OpenClaw is unverified, not passed.** It has never completed step 1. Its own setup
mechanics are [`../../entrypoints/openclaw/README.md`](../../entrypoints/openclaw/README.md)
and the listing sequence is
[`../distribution/openclaw-clawhub.md`](../distribution/openclaw-clawhub.md); this page
adds only that the five steps above are what would change its row in the status table,
and that four of the five have never been run there at all.
