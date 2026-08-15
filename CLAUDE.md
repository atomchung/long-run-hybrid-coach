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

## Dogfooding runs on main

The real store lives at `~/.local/share/garmin-coach-loop`. A git worktree
isolates code, **not** state — so a work-in-progress branch pointed at the
default store writes real state from unreviewed code.

- Dogfood from the main checkout, on `main`.
- Develop in a worktree.
- Anything needing its own state sets `GARMIN_COACH_LOOP_HOME`.
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

## Verify against the live account, not against the plan

The plan records what the product *intended* to deliver. Whether Intervals holds
it is a separate fact, and only the provider can answer it. When a delivery
matters, read the calendar back rather than trusting `delivery_state` alone.
