# Moving a local store to the hosted owner store

The product decision is one canonical PlanState per athlete, held by the hosted gateway,
read and written by every entry — claude.ai, ChatGPT, a local MCP client, the CLI
(issue #40). This file is how an athlete who already has a local store gets there once,
and how the local store stops being a second writer afterwards.

It is deliberately an operator procedure rather than a button. Two stores holding two
plans for one Intervals account is not a state any code can resolve on its own: which one
is the athlete's real current plan is a training judgement made by looking at both, and at
the calendar. So every automatic path here **fails closed**, and the only way past a
populated destination is an explicit archive that keeps what it moves.

## Before anything: which store is current

Three facts, read in this order. None of them is inferred from the others.

```bash
# 1. The local store: does it open, what version, is anything mid-delivery?
python3 -m garmin_coach_loop.cli doctor-store --state-dir ~/.local/share/garmin-coach-loop
python3 -m garmin_coach_loop.cli status --state-dir ~/.local/share/garmin-coach-loop
```

```bash
# 2. The hosted store, through the entry every agent uses. Opens a browser once.
# hosted-status is the read: no provider call, no reconciliation, no write -- exactly
# what comparing two stores needs, and not startCoachSession, which can write a
# reconciled version while you are only trying to read one.
python3 -m garmin_coach_loop.cli hosted-status --gateway https://<gateway-domain>
```

3. **The Intervals calendar itself.** The plan records what was *meant* to be delivered;
   only the provider says what is on the calendar this week. Read it in the Intervals UI,
   or with the `intervals-icu` MCP tools if they are connected. A session that exists on
   the calendar and in neither store is the case this whole procedure exists to stop
   growing.

Write down, for both stores: `plan_id`, `current_version`, week start, and which sessions
carry an `external_id`. If the two disagree about a delivered session, resolve that before
migrating — after the migration one of the two records is gone from the writing path.

## The maintenance boundary: what the code holds, and what you still have to hold

Steps 3 and 4 move and replace a whole owner directory while a live gateway may be serving
that same athlete. The code now takes an **owner maintenance fence** across each of them
(issue #128): a file named `<owner id>.maintenance` beside the owner directory — never
inside it, so it is not part of the store, not part of a bundle, and not a schema change.

**What the fence guarantees, wherever the store lives:**

- Nothing may claim the owner directory twice. `archive-store`, `import-store`,
  `init-store` and `adopt-owner-store` all take the same fence, so a destination checked
  free cannot be occupied between the check and the install, and two cutovers cannot both
  believe they hold one owner.
- While it is held, every write to that store is refused: reconciliation, initialization,
  a plan decision, a delivery, a withdrawal, reported evidence, a snapshot, an export, a
  restore. Enforced where the store lock is taken, so a future command inherits it
  without having to remember to.
- It is never granted over a live write. Acquiring it passes through the store's own
  `.lock` — a writer already inside it makes the *cutover* fail, before anything moves —
  and it refuses outright while `delivery-attempt.json` exists. A delivery in flight is
  never separated from the code path that has to finish it.
- A failed or interrupted cutover releases it, and leaves both the store and the bundle
  exactly as they were.

`doctor-store` reports a held fence under `maintenance_fence` without failing the store.
If a process was killed mid-cutover the file can outlive it, exactly as `.lock` can: the
refusal names the operation, the time and the pid, and the recovery is to confirm no
cutover is running and delete that one file.

**What the fence does not cover, and why the gateway still stops.**

The fence is one filesystem's `O_EXCL` create. It is only mutual exclusion where the
cutover and the gateway see the *same* owner directory on the *same* filesystem. On
Railway that is the expectation — one instance, one volume at
`$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT`, `railway ssh` attaching to the running container
— and none of it has been verified end to end on this deployment. A second instance, a
replica with its own volume, or an `ssh` session that lands somewhere other than the
serving container would each leave two writers with two fence files and no relationship
between them.

So until that is verified on a real migration, **stop or drain the gateway around steps 3
and 4 and restart only after step 5 verifies**, and treat the fence as the second line
rather than the first:

```bash
# Before step 3. Whichever of these this deployment uses -- the point is that no request
# reaches the gateway while the owner directory is being moved.
railway down                       # or scale the service to zero replicas
railway status                     # confirm nothing is serving
curl -sS -o /dev/null -w '%{http_code}\n' https://<gateway-domain>/readyz   # expect a failure
```

Restart after step 5's `doctor-store` and `hosted-session` both answer, and re-verify with
[`verify-production-status.md`](verify-production-status.md) — a service that came back is
the plan, and `/readyz` on the live domain is the account.

When the migration is done end to end this way, record here whether `railway ssh` reached
the serving container and its volume. That single fact is what decides whether the stop
step can ever become optional.

## Step 1 — export the local store

```bash
python3 -m garmin_coach_loop.cli export-store \
  --state-dir ~/.local/share/garmin-coach-loop \
  --out ~/coach-store-bundle.json
```

The bundle is the whole append-only chain — every PlanState version, every DecisionEvent,
every receipt — plus `athlete-evidence.json`. It carries no lock, no delivery reservation
and no handoff marker: those describe an operation on *this* machine.

The command refuses a store that does not open, and a store with a delivery in flight
(exporting one would carry a history that omits a provider write still in the air). It
also refuses to write inside this repository, because the file is the athlete's whole
training history in plaintext. Keep it out of any repository, and delete it once step 5
verifies.

Note the `bundle_digest` it prints. It is how both ends of the transfer are compared.

## Step 2 — put the bundle where the deployment can read it

The gateway's owner stores live on the deployment's volume, and no filesystem path joins
it to a laptop. On Railway, the service container is reachable with `railway ssh`:

```bash
railway ssh "cat > /data/coach-store-bundle.json" < ~/coach-store-bundle.json
```

> **Not yet verified on this deployment.** The `railway ssh` roll path in
> [`roll-with-railway-cli.md`](roll-with-railway-cli.md) has been run end to end; piping a
> file into the container this way has not. Verify it on the first real migration —
> `railway ssh "wc -c /data/coach-store-bundle.json"` against the local `wc -c` — and
> record the result here. If the platform refuses stdin to a non-interactive command, the
> fallback is any transfer the operator already trusts onto the same volume; nothing in
> step 3 cares how the file arrived, only that its digest still matches.

## Step 3 — make the destination empty, on purpose

**Stop or drain the gateway first** — see the maintenance boundary above. Steps 3 and 4
run against a store nothing else is serving.

`import-store` refuses a destination that holds anything. That refusal is the design, not
an obstacle to route around: importing is not merging.

If the hosted owner store is empty (the athlete signed in but never planned there), skip
to step 4. If it holds a plan and the local one is the current plan, archive it:

```bash
railway ssh "python3 -m garmin_coach_loop.cli archive-store \
  --athlete-id <intervals athlete id> \
  --state-root \$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT \
  --reason superseded-by-local"
```

Without `--confirm` this prints the exact source, the archive path, and what the store
holds, and moves nothing. Re-run it with `--confirm` to perform the move. The archive is
a rename, not a delete: it still opens under `doctor-store` at the path it reports, and
putting it back is a rename in the other direction.

Both runs take the maintenance fence, so a preview promises exactly what the confirm does.
If either refuses with `state store is locked by another operation` or `a delivery to
Intervals is in flight`, the gateway did not fully drain: something is still writing, or a
delivery is still in the air. Neither is resolved by retrying harder — read the Intervals
calendar, finish or clear that delivery (`clear-delivery-attempt`), and only then move the
store.

## Step 4 — import

```bash
railway ssh "python3 -m garmin_coach_loop.cli import-store \
  --bundle /data/coach-store-bundle.json \
  --athlete-id <intervals athlete id> \
  --state-root \$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT"
```

The preview prints the destination, the plan, the version, the event count and the
digest. Compare the digest with step 1's before adding `--confirm`.

Nothing is written into the owner directory until the whole bundle has been materialized
elsewhere, reopened by `doctor-store` on its own, and checked against the bundle's own
summary. An import that fails leaves the destination exactly as empty as it found it, and
the destination is re-read under the fence immediately before the install — an owner store
that appeared since the preview is never installed over.

`--athlete-id` resolves the owner through the identity registry and never creates one: an
athlete id typed at a terminal is not an authorization. If it reports that no owner has
connected, the athlete has not signed in to this deployment yet — do that first.

## Step 5 — verify against the account, not the plan

```bash
railway ssh "python3 -m garmin_coach_loop.cli doctor-store \
  --state-dir \$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT/owners/<owner id>"
```

Then, from the athlete's own machine, through the read-only entry an agent uses:

```bash
python3 -m garmin_coach_loop.cli hosted-status --gateway https://<gateway-domain>
```

The gateway comes back up before the `hosted-status` read — that read goes through it.
Bring it back, confirm `/readyz` on the live domain, and only then:

The `plan_id` and `plan_version` must match what step 1 exported. `hosted-status` is the
right read for this: it cannot itself move the version it is being used to check, the way
`hosted-session` (`startCoachSession`) could by reconciling on the way. Then open
claude.ai (or whichever connector is in routine use) in a **new conversation** and ask
what this week holds: continuity across a fresh conversation is the thing the migration
is for, and it is not proven by a CLI read.

### The half a read cannot prove: that the new side can be written

Everything above reads. A migrated store that opens, reports the right `plan_id`, and
answers a fresh conversation correctly can still be unable to write anything — and the
failure surfaces at the worst moment, on the first delivery, after the local store has
been sealed and the way back is a fork rather than a retry. Verified live 2026-08-18:
the hosted plan read perfectly through every check in this step while
`publishWorkoutDelivery` failed on the `GET /events` it makes before writing.

The two writes fail for different reasons and neither implies the other, so prove both.

**The store write.** Report something the athlete can state and the plan can hold — a
body measurement is the smallest — through the connector, then read the version back:

```bash
python3 -m garmin_coach_loop.cli hosted-status --gateway https://<gateway-domain>
```

`current_version` must have moved by one. If it did not, the destination is still sealed,
still fenced, or still refusing writes from the writer-contract guard, and `doctor-store`
from step 5 names which.

**The provider write.** A store that accepts versions says nothing about whether the
connected Intervals token can reach the calendar: the two credentials are the same token
but not the same permission, and a token that reads activities and wellness perfectly can
be refused on the calendar. Check what the connection actually holds before spending a
real session on the question:

- `inspectIntervalsPermissions` — the granted scope names, straight from the connection
  the gateway is using. `CALENDAR:WRITE` missing here is the whole answer, and no amount
  of re-authorizing fixes it if the registered application never asked for it.
- Then deliver **one real upcoming session** — `prepareWorkoutDelivery` writes nothing, so
  the pair has to be completed — and read it back off the calendar in the Intervals UI or
  with the `intervals-icu` MCP tools. The plan saying `DELIVERED` is the plan again; the
  event being there is the account.

A `403` on either call is not a token to refresh. It is a permission the authorization was
never granted, so the fix is the registered application's scopes and a fresh consent, not
a retry. Do not seal the local store until this section passes — sealed, the fallback is
`seal-local-store --release`, which forks the two stores rather than undoing anything.

## Step 6 — stop the local store from being a second writer

```bash
python3 -m garmin_coach_loop.cli seal-local-store \
  --state-dir ~/.local/share/garmin-coach-loop \
  --hosted-entry https://<gateway-domain> \
  --confirm
```

Sealing writes `hosted-handoff.json` into the store. From then on every write path refuses
— decisions, delivery, reported evidence, restore, adoption — and every read still works:
`status`, `history`, `doctor-store`, `export-store` and snapshots are unchanged. The
refusal names the hosted entry, so whoever hits it knows where the plan is.

Do this last. A store sealed before the import landed would have fenced the only working
copy.

Finally, tell this machine which coach it belongs to, so a local write has to be said out
loud rather than happening by accident:

```bash
# in ~/.zshrc
export GARMIN_COACH_LOOP_GATEWAY_URL=https://<gateway-domain>
```

With it set, `apply-decision`, `publish-delivery`, `record-profile` and every other local
write refuse unless `--offline` is passed. `--offline` remains the honest way to work on a
development or rehearsal store; what it cannot do is unseal the migrated one.

## Undoing it

- **The local store, back to writable:** `seal-local-store --release --confirm`. It says
  what it costs, because it is a real fork: the hosted store keeps whatever it has, and
  neither side learns about the other.
- **The hosted store, back to what it held:** rename the `<owner id>.archived-*` directory
  from step 3 back to the owner directory, after moving the imported one aside. It sits
  beside the owner directory rather than inside it, and the name is not hidden — the
  archive command prints the exact path it used.
- **Both sides, from a known-good point:** the snapshots `snapshot-store` takes are
  unaffected by any of this; `restore-store` is their path back, and it refuses a sealed
  destination for the same reason everything else does.

## What this procedure does not do

- **It does not carry the local health database.** Training history before the Intervals
  account starts, and per-set strength detail, live in a file on the athlete's machine
  that the hosted path cannot see (issue #101). Migrating the store does not close that
  gap and does not pretend to; `recordStrengthExecution` is the route for reported sets,
  and the history question is still open.
- **It does not merge.** If both sides hold plans that both matter, nothing here combines
  them. Archive one, import the other, and let the coach re-plan from the current week.
- **It does not touch the Intervals calendar.** Whatever was delivered stays delivered.
  After the migration the hosted plan is the one that knows about it, which is why step 1
  reads the calendar before anything moves.
