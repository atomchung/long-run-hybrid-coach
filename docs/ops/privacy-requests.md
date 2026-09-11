# Handling a privacy request

An athlete can ask for a copy of their data, or for it to be deleted, in two places: in the
conversation, through their own connection, or by writing to the support address. The first
is faster and is a better identity check than an operator can perform. The second is the
one this release can promise, and it is what this page is mostly about.

The reason for the second route is issue #417. A deletion confirmed in the conversation
did not reach the service: the model emitted the call, the client's own approval layer
refused it before it left the client, and the athlete was left holding a preview and no
erasure. Nothing about that failure reached the product, so the product could not tell
them. Until that path is verified in each client, "ask the coach and it is done" is a claim
about a route that has been observed to stop short, and the published pages say so.

The public policy is [privacy.html][privacy] in the website repository; what each lifecycle
state actually does is [account-lifecycle.md](../account-lifecycle.md). Neither should be
edited to promise something this page cannot walk through.

[privacy]: https://paceandstaystrong.com/privacy.html

## The channel

Requests arrive at **tingcctwai@gmail.com**. It is private, which is why it is the channel
for anything about an account or health data. The public issue tracker stays open for bugs
and feature requests and is never where a data request is worked: the thread below carries
an athlete id and eventually an archive.

## Before anything else: do not collect what you do not need

Never ask for, and never repeat back:

- health, training, or plan content;
- a password, an Intervals.icu API key, or an OAuth token — none of them is ever needed
  here, and asking for one teaches an athlete a habit that will be used against them;
- an owner id or a token fingerprint.

What a request needs is the **Intervals.icu athlete id** — the number in the athlete's own
Intervals.icu URL. It locates the account and nothing else: on its own it is not a reason
to release anything, which is the next section.

The **opaque account reference** (`owner_reference`) printed in an athlete's own data
export still works as a locator too, and is the one identifier safe to quote in a public
thread. It is not proof of ownership either.

## The identity check

Three things an athlete can put in an email look like proof and are not:

- **The athlete id.** It is in a URL.
- **The display name.** Same, and two accounts of one person routinely share one.
- **The address the mail came from.** This product stores no email address at all (see
  [account-lifecycle.md](../account-lifecycle.md)), so there is nothing to compare it
  against. An address that really is the athlete's Intervals.icu address still only proves
  the sender knew it.

What is proof is **a fresh Intervals.icu authorization inside a window you named
privately**. Completing that consent requires signing in as that athlete; this deployment
records the instant it happened. An impersonator can produce an id, a name and an address,
and cannot produce that row.

It works for the request that made this route necessary, because re-authorizing is the
OAuth hop rather than an MCP call: an athlete whose client refuses the deletion tool can
still reconnect. And it asks for nothing secret — it is the same consent they gave when
they first connected.

### 1. Open the request

```bash
python3 -m garmin_coach_loop.cli privacy-request-open \
  --state-root <gateway state root> \
  --athlete-id <the athlete id they gave>
```

It reads no store. It prints two things:

- `reply` — send this back, unchanged. It asks them to re-authorize and to reply when they
  have. **It is deliberately the same text whether or not the account exists**: replying
  "no such account" to somebody who has proved nothing tells a stranger which athlete ids
  are registered here.
- `operator_only` — whether the account exists, for you. Do not quote it.

Note the time you sent the reply. That is the window's opening bound.

### 2. Wait for them to reconnect, then close the window

When their "I have reconnected" reply arrives, note that time too. The two together are
`--authorized-after` and `--authorized-before`, both ISO-8601 **with a timezone**
(`2026-09-11T14:00:00Z`). The commands refuse a naive timestamp rather than assuming UTC,
refuse a window still running, and refuse a window longer than 24 hours — a window wide
enough to catch a reconnect the athlete made for their own reasons is a window that
verifies an impersonator by coincidence.

Every command re-checks the window. There is no verified state kept anywhere; the record of
the request is the mail thread.

## Export

```bash
python3 -m garmin_coach_loop.cli privacy-request-export \
  --state-root <gateway state root> \
  --athlete-id <athlete id> \
  --authorized-after <when you sent the challenge> \
  --authorized-before <when they replied> \
  --out ~/privacy-requests/<something>.json
```

The archive is the same one `exportOwnerData` builds, `excluded` list included. The file is
written 0600 and must be outside this repository. Send it **only to the address that made
the verified request**, and delete it once it is sent.

Check `verification.authorization_instants` against what they said they did before you
attach anything. More than one instant is not a failure — a client can mint two tokens —
but it is worth a second look.

If verification fails, the whole command fails and nothing is read. The refusal wording is
the same for "no such account" and "they did not reconnect", so it is safe to paste.

## Deletion

Two steps, because the athlete has to confirm the scope they are actually losing.

```bash
# 1. the scope, and the digest that binds it
python3 -m garmin_coach_loop.cli privacy-request-delete \
  --state-root <gateway state root> --athlete-id <athlete id> \
  --authorized-after <...> --authorized-before <...>
```

Send them `removes` and `not_removed` and ask them to confirm that exact scope. Then:

```bash
# 2. the erasure, against the scope they confirmed
python3 -m garmin_coach_loop.cli privacy-request-delete \
  --state-root <gateway state root> --athlete-id <athlete id> \
  --authorized-after <...> --authorized-before <...> \
  --scope-digest <scope_digest from step 1> --confirm
```

If the account moved in between — they reported a lift, a session reconciled — the digest
no longer matches and the command refuses. Preview again, send the new scope, take a fresh
confirmation. Nothing is deleted by a refusal.

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

The athlete revoked their Intervals.icu authorization, or lost the account, and cannot
re-authorize at all.

This route does not solve that, and should not pretend to: re-authorizing *is* the identity
check, so an athlete who cannot perform it has no proof of ownership this product can
evaluate. Every identifier it holds is either a one-way digest or a value anyone could
claim. Say so plainly rather than deleting an account on an assertion. If they can
re-authorize at Intervals.icu even briefly, the request proceeds normally — that is the
whole of what is needed.

Where an operator does proceed on a request they can verify by other means, on a deployment
they own, the older command is still there:

```bash
python3 -m garmin_coach_loop.cli delete-owner \
  --identity-db <state root>/identity.db \
  --state-root <state root> \
  --owner-id <owner uuid>
```

It previews without `--confirm`. It takes no owner maintenance fence and leaves no
tombstone, which is sound only because of the precondition above: this is for an account
whose credential no longer works, so there is no in-flight request of theirs left to race.
Do not reach for it while an athlete can still authorize — the verified route above is
fenced end to end and is the one to use.

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
- **A request is served only after the athlete re-authorizes.** No password, API key or
  token is ever requested, and a request that cannot be verified is refused rather than
  served cautiously.
- **No response time is promised.** This is one maintainer and a mailbox. Where the
  in-conversation route works, it is immediate and needs none of this.
- **No email address or name is stored.** Both are read from Intervals.icu on demand — when
  an athlete asks which account is connected, and in any preview that would write to the
  calendar, so a person with two Intervals accounts can see which one is about to be
  written — and used for that one answer. The address is stated first, because two accounts
  of one person often share a display name. Neither reaches PlanState, the archive, the
  usage rows earlier releases wrote, or a log line, so a request to delete an email address
  has nothing to act on and the honest answer says so. What is stored about identity
  remains the athlete id and the keyed token digest.
- **Deletion reaches the whole account and every snapshot stored beside it, in one
  operation.**
- **Deletion does not reach the athlete's Intervals.icu calendar or authorization.**
- **No backup-retention window is promised.** This product creates no routine backups; if a
  deployment enables its host's volume snapshots, that deployment owes a documented
  rotation and a measured maximum delay before it may be used for anyone's real data. Until
  that measurement exists, do not state a number.
