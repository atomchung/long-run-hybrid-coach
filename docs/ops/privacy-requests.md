# Handling a privacy request

Most of these no longer reach an operator. An athlete asks the coach for a copy of their
data or asks it to delete their account, and both happen over their own authenticated
connection — which is also the identity check, and a better one than an operator could
perform over email. This page covers what is left.

The public policy is [privacy.html](../privacy.html); what each lifecycle state actually
does is [account-lifecycle.md](../account-lifecycle.md). Neither should be edited to
promise something this page cannot walk through.

## Before anything else: do not collect what you do not need

The initiating channel is the public issue tracker, and it must stay usable without an
athlete disclosing anything. Never ask for, and never repeat back:

- health, training, or plan content;
- an Intervals.icu athlete id, an owner id, an access token, or a token fingerprint;
- an email address, unless the athlete offers one for a private follow-up.

What is enough to identify an account is the **opaque account reference** printed in the
athlete's own data export (`owner_reference`). It is keyed to this deployment, cannot be
turned into an owner id, and is safe in a public thread. If they cannot reach an export,
they cannot prove the account is theirs either — see "Lost access" below.

## Access and export

Self-service. Point the athlete at asking the coach what it holds about them; the archive
arrives in the conversation and states what it deliberately excludes. There is nothing for
an operator to run, and running one would mean handling their data unnecessarily.

## Deletion

Self-service, with one exception below. The preview names what goes and what deletion
cannot reach, and nothing is removed before the athlete confirms it.

**If they report the deletion was refused**, the two documented reasons are:

- An **unfinished delivery** — Intervals may hold a workout the product has not
  reconciled, and the record of it is in the account being deleted. The athlete resolves
  it themselves: retry the same delivery, or check their Intervals calendar and clear the
  attempt. Then delete.
- **The account changed since the preview** — they reported a lift, or a session
  reconciled, between preview and confirmation. Previewing again is the whole fix.

Neither needs an operator, and an operator running `delete-owner` to route around either
one would be deleting a store whose delivery state is unresolved.

### Lost access

The one case that reaches an operator: the athlete has revoked their Intervals.icu
authorization, or lost the account, and can no longer connect to delete anything.

This is a genuine impasse and should be treated as one, because the identity check is gone
with the access. There is no proof of ownership this product can evaluate — every
identifier it holds is either a one-way digest or a value anyone could claim. Say so
plainly rather than deleting an account on an assertion. If the athlete can re-authorize at
Intervals.icu even briefly, that restores the self-service path and resolves it properly.

Where an operator does proceed — a request they can verify by other means, on a deployment
they own — the command is:

```bash
python3 -m garmin_coach_loop.cli delete-owner \
  --identity-db <state root>/identity.db \
  --state-root <state root> \
  --owner-id <owner uuid>
```

It previews without `--confirm`. Read the preview back to the requester before adding it.
The counts it prints are the receipt; do not paste the owner id into a public thread.

## Correction

There is no self-service correction, and that is deliberate: the store is append-only
because a decision that can be rewritten afterwards is a decision nobody can audit. A
wrong number is corrected the way it was recorded — by reporting the right one, which
supersedes it and keeps both in the history.

If an athlete wants a correction that a new record cannot express, the honest answers are
export (so they hold what is there), or deletion (so it is not). Say which one applies
rather than editing a store by hand.

## What an operator can promise

- Export and deletion take effect immediately, because the athlete performs them.
- Deletion reaches the whole account and every snapshot stored beside it, in one
  operation.
- Deletion does **not** reach the athlete's Intervals.icu calendar or authorization.
- No backup-retention window is promised. This product creates no routine backups; if a
  deployment enables its host's volume snapshots, that deployment owes a documented
  rotation and a measured maximum delay before it may be used for anyone's real data.
  Until that measurement exists, do not state a number.
