# Working rules for Claude

Repository invariants and the verification commands are in [AGENTS.md](AGENTS.md).
The product surface, state mechanics, and delivery boundary are in
[README.md](README.md). Both apply to Claude unchanged. They are not restated
here: a second copy is a second source of truth, and it drifts.

This file covers only what those two do not — how to work in this repository from
one session to the next.

## A bare `issue #NN` inside code is history, not a live link

Most of this code was written in a private repository that is now archived, and its
comments cite that repository's numbers. Those numbers do not resolve here, and a few
collide with real issues in this one. Read them as provenance for work already done —
never follow one, and never cite one in new code. Anything still open was refiled here
and is listed in the migration tracker, issue #37.

## Start from stored state, never from conversation memory

Before deciding anything about the plan:

```bash
python3 -m garmin_coach_loop.cli doctor-store
python3 -m garmin_coach_loop.cli status
```

A plan reconstructed from what an earlier conversation said is a *second* plan.
The store holds the only current one, and reading it is cheap.

Once this machine names a hosted coach (`GARMIN_COACH_LOOP_GATEWAY_URL`), the
canonical plan is the hosted one and the local store is history. Read it the way
every other client does:

```bash
python3 -m garmin_coach_loop.cli hosted-session
```

`doctor-store`/`status` still answer for whatever local store is being worked on;
they just stop being the answer to "what is the athlete's current plan".

## Dogfooding runs on main, and writes in one place

The real store lives at `~/.local/share/garmin-coach-loop`. A git worktree
isolates code, **not** state — so a work-in-progress branch pointed at the
default store writes real state from unreviewed code.

- Dogfood from the main checkout, on `main`.
- Develop in a worktree.
- Anything needing its own state sets `GARMIN_COACH_LOOP_HOME`.
- **One writer per athlete.** ChatGPT, claude.ai, a local MCP client and the CLI
  all reach one hosted owner store; a writable local store beside it is a second
  current plan for one Intervals calendar, which is what issue #40 is about. With
  a hosted coach configured, local writes refuse unless `--offline` says a
  different store is meant, and a migrated store is sealed
  (`hosted-handoff.json`) so no code path writes it at all. Moving an existing
  local store across once: [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md).
- Every refresh passes `--health-db`, or exports `GARMIN_COACH_LOOP_HEALTH_DB`
  once:

  ```bash
  python3 -m garmin_coach_loop.cli refresh-context \
    --health-db ~/Side_project/personal_os/health/data/health.db
  ```

  Without it `strength_execution` is `null` in every context. The loop still
  runs and says so in `unknowns`, but the coach loses the only record of what
  was actually lifted — sets, weights, and the set that got cut short — and is
  left judging strength from duration and average heart rate. The path is the
  athlete's, not the repository's: PersonalOS stays a data source, never an
  import (AGENTS.md 1).

  **This flag remains a local-path flag after the plan moves hosted.** `health.db`
  is a file on this machine, so the gateway cannot read it for anybody — and the
  product does not assume anybody else has one. Hosted recovery evidence comes from
  the athlete in the conversation: numbers read off the watch face, an export they
  paste, whatever they have. Pass those values as
  `startCoachSession.recovery_signals` with a source label saying where they came
  from; do not go reading `health.db` on their behalf, and never send the path,
  database, credential, raw payload, or a figure the model inferred rather than the
  athlete stating what their device showed. Sleep score, sleep duration, last night's
  HRV and resting heart rate are also saved as dated `reported_recovery` records;
  later contexts read the last 28 days where `recovery_signals` does not already
  answer the day. Older records remain stored until account-data deletion. The
  other uploaded recovery figures stay in the current CoachContext and must be
  supplied again when needed on a later turn. See
  [docs/data-sources.md](docs/data-sources.md#consequence-for-the-coach) for correction
  and deletion limits. Hosted
  `strength_execution` still comes from `recordStrengthExecution`, which is the
  athlete stating the sets rather than the database holding them. The history half
  of the same gap is issue #101 and is still open.

This makes schema changes urgent rather than optional: `doctor-store`
revalidates the entire commit history, so once newer code writes a field, older
code cannot open the store **at all** — not the new version, the whole thing.
Land the schema change on `main` before, or immediately after, writing state
that depends on it. A writer-contract version guard (`WRITER_CONTRACT_VERSION`
in `garmin_coach_loop/store.py`) now catches a mismatch before any write,
snapshotting the store first when this checkout is the newer side; `doctor-store`
reports the version and `restore-store` is the recovery path.

## A delivery that did not finish fences the store, including recovery

`delivery-attempt.json` is a journal of what a delivery may already have done to
Intervals. While any operation in it is unreconciled, `apply-decision` is refused —
and so are `snapshot-store`, a confirmed `restore-store`, and `adopt-owner-store
--mode copy`, because each of those would hand out a copy of state that
deliberately omits the reservation. Reach for the retry, not for `restore-store`:
re-running the *same* approved delivery set converges it without a duplicate event.
`clear-delivery-attempt --confirm` is the manual reconciliation, and it prints
exactly which operations it abandoned. A reservation this code cannot parse blocks
`doctor-store` rather than reading as absent.

## Revoking at Intervals signs every entry out, not the one being tested

Intervals authorization is granted per application per athlete, not per connection. So
revoking it at intervals.icu — the obvious way to test the revocation path from one
client — invalidates **every** token issued under that grant at once: claude.ai, ChatGPT,
a local MCP client, whatever else is connected. Re-consenting from the client under test
mints a new token for that client only; every other connection stays dead until it
reconnects on its own.

What those other connections see is a plain `401` challenge and, in the client, "the
connection was invalidated". No reason travels to the client, on purpose — it is written
to the security log instead ([docs/ops/security-events.md](docs/ops/security-events.md)),
which is the only place that says whether a connection was forgotten, refused for the
wrong audience, or presented under a rotated key. The gateway drops the fingerprint on
the first `401` the provider returns (`_forget_connection` in
`garmin_coach_loop/gateway.py`), which is what turns one revocation into a sign-out
everywhere.

Verified 2026-08-18: a revocation test driven from the ChatGPT connector invalidated the
claude.ai connector mid-delivery, and the delivery could not be retried until it was
reconnected.

So before revoking, say so in the tracking issue, and afterwards reconnect every entry
rather than the one under test. **Re-authorizing without revoking is safe** and needs none
of this: earlier fingerprints are kept deliberately (`record_token_fingerprint` in
`garmin_coach_loop/identity.py`), so two clients hold two tokens against one store.

## Verify against the live account, not against the plan

The plan records what the product *intended* to deliver. Whether Intervals holds
it is a separate fact, and only the provider can answer it. When a delivery
matters, read the calendar back rather than trusting `delivery_state` alone.

The same split applies to the hosted gateway itself: a merged release-lane PR, a
green CI run, or someone saying a deploy "should" have gone through are all
still the plan, not the account. `/readyz` on the live domain is the calendar
read-back for deployment — see
[`docs/ops/verify-production-status.md`](docs/ops/verify-production-status.md)
before either trusting or doubting a deploy status secondhand.
