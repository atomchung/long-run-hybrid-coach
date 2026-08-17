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
python3 -m garmin_coach_loop.cli hosted-session --gateway https://<gateway-domain>
```

3. **The Intervals calendar itself.** The plan records what was *meant* to be delivered;
   only the provider says what is on the calendar this week. Read it in the Intervals UI,
   or with the `intervals-icu` MCP tools if they are connected. A session that exists on
   the calendar and in neither store is the case this whole procedure exists to stop
   growing.

Write down, for both stores: `plan_id`, `current_version`, week start, and which sessions
carry an `external_id`. If the two disagree about a delivered session, resolve that before
migrating — after the migration one of the two records is gone from the writing path.

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
summary. An import that fails leaves the destination exactly as empty as it found it.

`--athlete-id` resolves the owner through the identity registry and never creates one: an
athlete id typed at a terminal is not an authorization. If it reports that no owner has
connected, the athlete has not signed in to this deployment yet — do that first.

## Step 5 — verify against the account, not the plan

```bash
railway ssh "python3 -m garmin_coach_loop.cli doctor-store \
  --state-dir \$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT/owners/<owner id>"
```

Then, from the athlete's own machine, through the entry an agent uses:

```bash
python3 -m garmin_coach_loop.cli hosted-session --gateway https://<gateway-domain>
```

The `plan_id` and `plan_version` must match what step 1 exported. Then open claude.ai (or
whichever connector is in routine use) in a **new conversation** and ask what this week
holds: continuity across a fresh conversation is the thing the migration is for, and it is
not proven by a CLI read.

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
