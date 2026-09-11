# Handling a privacy request

An athlete can ask for a copy of their data in the conversation or by writing to the
support address. **A deletion is this page only.** The conversation has no deletion tool
since 1.4.5: asked to delete everything, the coach hands over
`https://paceandstaystrong.com/support.html#data-by-email` and says that nothing has been
submitted and nothing removed.

The reason is issue #417. A deletion confirmed in the conversation did not reach the
service: the model emitted the call, the client's own approval layer refused it before it
left the client, and the athlete was left holding a preview and no erasure. Nothing about
that failure reached the product, so the product could not tell them. Rather than keep a
route that had been observed to stop short, the erasure moved entirely to the one an
operator can see finish -- this one.

The public policy is [privacy.html][privacy] in the website repository; what each lifecycle
state actually does is [account-lifecycle.md](../account-lifecycle.md). Neither should be
edited to promise something this page cannot walk through.

[privacy]: https://paceandstaystrong.com/privacy.html

## The channel

Requests arrive at **tingcctwai@gmail.com**. It is private, which is why it is the channel
for anything about an account or health data. The public issue tracker stays open for bugs
and feature requests and is never where a data request is worked: the thread below carries
an athlete id and eventually an archive.

## Where these commands run

**On the deployment, not on a laptop.** The state root a hosted athlete's account lives in
is the Railway volume at `/data`, so every command below is a `railway ssh` away, the same
as `read-usage-stats.md`. With the CLI linked to the production environment
(`roll-with-railway-cli.md` for the one-time `railway link`):

```bash
railway ssh "python3 -m garmin_coach_loop.cli privacy-request-open --athlete-id <id>"
```

`--state-root` is omitted on purpose: `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` is already a
plain variable on the service (`/data`), and `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY` is already
a secret on it, so a session inside the container has both. **Running these on a laptop is
the dangerous mistake**, because `--state-root` falls back to whatever that machine's
environment names — which may be a local test store, and `privacy-request-delete --confirm`
against the wrong store is irreversible. Every report now prints the `state_root` it acted
on; read it before replying.

The export writes its file inside the container, so it takes one more step to reach the
athlete:

```bash
railway ssh "python3 -m garmin_coach_loop.cli privacy-request-export --athlete-id <id> \
  --identity-evidence settings-screenshot --out /tmp/archive.json"
railway ssh "cat /tmp/archive.json" > ~/privacy-requests/<something>.json
railway ssh "rm /tmp/archive.json"
```

> **Not yet verified on this deployment.** The commands themselves have been walked end to
> end against a synthetic state root; pulling a file back out through `railway ssh "cat"`
> has not been run against the live service — the same caveat
> `migrate-local-store-to-hosted.md` carries for the other direction. Check the file you
> receive opens as JSON and carries the expected `owner_reference` before sending it.

Everything below states the commands without the `railway ssh` wrapper, to keep the
arguments readable. Add it.

## Before anything else: do not collect what you do not need

Never ask for, and never repeat back:

- health, training, or plan content;
- a password, an Intervals.icu API key, or an OAuth token — none of them is ever needed
  here, and asking for one teaches an athlete a habit that will be used against them;
- an owner id or a token fingerprint.

What a request needs is the **Intervals.icu athlete id** — the number in the athlete's own
Intervals.icu URL. The **opaque account reference** (`owner_reference`) printed in an
athlete's own data export also locates the account and is the one identifier safe to quote
in a public thread.

## The identity check is yours, not the code's

**This is a person's judgement, by the owner's decision of 2026-09-11.** Nothing in the
tooling will refuse a request because the requester might not be the athlete. Do not read
a successful command as the product having verified anybody.

Ask for **a screenshot of their Intervals.icu Settings page**, showing their athlete id
and Long Run Hybrid Coach among the applications they have authorized. One message, no
round trip, and only somebody signed in to that account can produce it. Check that the id
in the screenshot is the id they gave you.

**If they cannot reach that page, the athlete id alone is enough.** Impersonation is out of
scope by decision: an earlier design made the check mechanical — re-authorize inside a
window named in a private reply, everything else refused — and it was rejected for needing
two reliable rounds of email in both directions before anybody got anything. A mail thread
with one maintainer is not a protocol.

What the tooling does instead is record the basis. Every command takes
`--identity-evidence`, with two accepted answers:

| Value | Means |
| --- | --- |
| `settings-screenshot` | they sent the Settings page showing this athlete id and the authorized app |
| `athlete-id-only` | they gave the id and nothing further |

It is required and has no default, and it is carried into the export report and into the
deletion receipt. An erasure cannot be taken back, so "what did we look at before running
it" should be answerable from the receipt rather than from memory.

### Open the request

```bash
python3 -m garmin_coach_loop.cli privacy-request-open \
  --state-root <gateway state root> \
  --athlete-id <the athlete id they gave>
```

It reads no store. It prints two things:

- `reply` — send this back, unchanged. It asks for the screenshot and says plainly that no
  password, API key or token will ever be read. **It is deliberately the same text whether
  or not the account exists**: replying "no such account" to somebody who has shown nothing
  tells a stranger which athlete ids are registered here.
- `operator_only` — whether the account exists, and its owner id, for you. Do not quote it.

## Export

```bash
python3 -m garmin_coach_loop.cli privacy-request-export \
  --state-root <gateway state root> \
  --athlete-id <athlete id> \
  --identity-evidence settings-screenshot \
  --out ~/privacy-requests/<something>.json
```

The archive is the same one `exportOwnerData` builds, `excluded` list included. The file is
written 0600 and must be outside this repository. Send it **only to the address that made
the request**, and delete it once it is sent.

An athlete id that has never connected fails the command; there is nothing to export.

## Deletion

Two operator commands bind execution to the current account and scope. The written
request can already authorize whole-account deletion; sending the scope back and waiting
for a second confirmation is not a mandatory email sequence. Ask for clarification only
when the request does not clearly authorize the scope.

```bash
# 1. the scope, and the digest that binds it
python3 -m garmin_coach_loop.cli privacy-request-delete \
  --state-root <gateway state root> --athlete-id <athlete id> \
  --identity-evidence settings-screenshot
```

Inspect `removes` and `not_removed` and check that the written request authorizes that
scope. If it does, proceed; otherwise clarify the missing authorization first:

```bash
# 2. the erasure, against the inspected and authorized scope
python3 -m garmin_coach_loop.cli privacy-request-delete \
  --state-root <gateway state root> --athlete-id <athlete id> \
  --identity-evidence settings-screenshot \
  --scope-digest <scope_digest from step 1> --confirm
```

If the account moved in between — they reported a lift, a session reconciled — the digest
no longer matches and the command refuses. Preview again and check the new scope against the written authorization. Ask again only
if that authorization no longer covers it. Nothing is deleted by a refusal.

This is `owner_data.delete_owner`, the same erasure the athlete's own confirmation runs:
one owner maintenance fence, the store and then the identity rows, and a tombstone left
behind. It is not the older `delete-owner` command, which takes no fence — see "Lost
access".

The result is the receipt. Read it before replying:

- `verified_after_deletion.state_directory_absent` and `identity_rows_remaining` all zero —
  the account is gone;
- `deletion_tombstone` — the fence that refuses a request which authenticated before the
  deletion and arrives after it;
- `accounts_before` / `accounts_after` and `other_accounts_unchanged` — exactly one account
  went. This is the only check that would notice a deletion that also took somebody else's,
  and a mail thread offers no other way to see it.

`receipt_id` and the counts are what a reply may quote. The receipt deliberately carries no
owner id, no plan content and nothing about their training: an audit record of a deletion
should not be the last surviving copy of what was deleted.

### What deletion reaches, and what it does not

It removes what this product stores: the plan, its whole version history and the decisions
behind it, everything the athlete reported themselves, any snapshot stored beside it, and
the identity rows mapping their athlete id and token digest to the account.

It does **not** reach three things, and every preview and receipt says so:

- **workouts already written to their Intervals.icu calendar** — those events belong to
  their Intervals.icu account;
- **their Intervals.icu authorization** — revoked at Intervals.icu Settings, never here;
- **operational logs**, which carry request paths and refusal reasons and no plan, health,
  or identity content at all.

Their activities, wellness readings, GPS and FIT files were never stored here in the first
place: they are read to build a context and never written down. The product only ever
writes its own planned workouts to that calendar, never a completed activity or a wellness
record.

**One thing the receipt cannot speak for: the running gateway's own memory.** This command
runs in its own process, so the live gateway may still be holding that athlete's
CoachContext and any prepared proposal — up to an hour each, in memory only
(`CONTEXT_RETENTION_SECONDS`). Nothing can reach them: every request re-reads the identity
registry, whose rows are gone, so no bearer resolves that owner any more, and the holds
touch no disk, no log and no export. They expire on their own, and a redeploy drops them
immediately. Say that plainly if an athlete asks whether anything survives the receipt; do
not claim the process forgot at the moment the receipt was written.

**This route creates a fourth copy that a product deletion cannot reach: the mail thread
itself.** It holds their email address, their athlete id, whatever screenshot they sent,
and — for an export — the archive you attached. None of it is in the store, so
`privacy-request-delete` does not touch it and no receipt can claim it did. Delete the
thread and the local copy of the archive by hand once the request is closed, and say so
plainly if they ask what is left.

### If deletion is refused

Three documented reasons, and none of them is answered by deleting harder:

- An **unfinished delivery** — Intervals may hold a workout the product has not reconciled,
  and the record of it is in the account being deleted. The athlete resolves it: retry the
  same delivery, or check their Intervals calendar and clear the attempt. Then delete.
- **The account changed since the preview** — previewing again is the whole fix.
- **A store cutover is in progress** — an operator is moving that owner's store
  ([migrate-local-store-to-hosted.md](migrate-local-store-to-hosted.md)), and the deletion
  says so rather than queueing behind it invisibly. This one is the operator's to end.

Reaching for the older `delete-owner` to route around the first would be deleting a store
whose delivery state is unresolved.

### What a completed deletion leaves behind, deliberately

One file beside the (now absent) owner directory, `<owner id>.maintenance`, carrying
`"tombstone": true`. It holds no plan, health, or identity content — it is the record that
this owner id is deleted, and it is what refuses a request that authenticated before the
deletion and arrives after it. `doctor-store` reports it as `deletion_tombstone`. Leave it:
removing it re-opens the one window a deletion cannot otherwise close, and owner ids are
never reused, so it can never be in a returning athlete's way.

## Lost access

An athlete who revoked their Intervals.icu authorization, or lost that account, can still
be served here: the athlete id is enough, and a Settings screenshot is better where they
can still sign in. That is what the owner's decision buys — the earlier design turned this
case into an impasse, because re-authorizing *was* the check.

The older operator command is still there, for a deployment where the whole account has to
go by hand:

```bash
python3 -m garmin_coach_loop.cli delete-owner \
  --identity-db <state root>/identity.db \
  --state-root <state root> \
  --owner-id <owner uuid>
```

It previews without `--confirm`. It takes no owner maintenance fence and leaves no
tombstone, so it does not belong in an ordinary request: `privacy-request-delete` is fenced
end to end, checks the scope against what the athlete confirmed, and reads the result back.
Use this one only where that one cannot run.

## Correction

There is no self-service correction, and that is deliberate: the store is append-only
because a decision that can be rewritten afterwards is a decision nobody can audit. A
wrong number is corrected the way it was recorded — by reporting the right one, which
supersedes it and keeps both in the history.

If an athlete wants a correction that a new record cannot express, the honest answers are
export (so they hold what is there), or deletion (so it is not). Say which one applies
rather than editing a store by hand.

## What an operator can promise

- **Both routes reach the same data and the same erasure.** The email route runs the
  product's own export and deletion, not a second implementation of either.
- **Deletion removes only what this product stores.** It never touches the athlete's
  Intervals.icu activities, calendar entries, or authorization.
- **Nothing secret is ever requested.** No password, API key or token, on either route.
- **No response time is promised.** This is one maintainer and a mailbox. Where the
  in-conversation route works, it is immediate and needs none of this.
- **No email address or name is stored by the product.** That is a claim about the store,
  not about your mailbox — see the paragraph on the mail thread above, and do not quote
  this line at somebody who just emailed you. Both are read from Intervals.icu on demand — when
  an athlete asks which account is connected, and in any preview that would write to the
  calendar, so a person with two Intervals accounts can see which one is about to be
  written — and used for that one answer. The address is stated first, because two accounts
  of one person often share a display name. Neither reaches PlanState, the archive, the
  usage rows earlier releases wrote, or a log line, so a request to delete an email address
  has nothing to act on and the honest answer says so. What is stored about identity
  remains the athlete id and the keyed token digest.
- **Deletion reaches the whole account and every snapshot stored beside it, in one
  operation.**
- **No backup-retention window is promised.** This product creates no routine backups; if a
  deployment enables its host's volume snapshots, that deployment owes a documented
  rotation and a measured maximum delay before it may be used for anyone's real data. Until
  that measurement exists, do not state a number.
