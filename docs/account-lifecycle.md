# Account lifecycle — connect, disconnect, export, delete

Four things get confused with one another constantly: stopping access, losing access,
getting a copy, and removing data. This page says which is which, what each one actually
does, and what none of them reach.

## Connect and reconnect

Authorizing Long Run Hybrid Coach at Intervals.icu is what creates an account here. There
is no sign-up, no password, and no profile: the Intervals.icu athlete you authorize as
*is* the account.

Identity is keyed on that athlete id rather than on the access token, so authorizing again
— from the same client, a second client, or after a revocation — resolves to the same
account and picks the plan back up mid-cycle. It does not start over.

Connecting a second client does not disconnect the first. Two clients hold two tokens and
both open the same plan.

## Disconnect

Revoke Long Run Hybrid Coach from Intervals.icu Settings (Apps you've authorized). That
takes effect immediately, on the provider's side, and needs nothing from this product —
which is deliberate: a "disconnect" button here could only forget a credential, and would
leave the grant you gave Intervals.icu standing.

What happens next:

- **The next authorized call fails**, and that is how this product finds out. Intervals
  answers with an authorization error, and nothing about your token, account id, or the
  failure's details is exposed.
- **The connection is then forgotten.** A credential Intervals refuses as invalid stops
  being one this product recognizes, so the call after that is a plain "not authorized"
  that tells a client to reconnect, rather than an upstream error it can only report. A
  *scope* refusal is not treated as a revocation: the token still works, and the fix is a
  permission the application would have to ask for.
- **Your plan is not touched.** Nothing is deleted, nothing is exported, nothing changes.
  It sits exactly where the last confirmed decision left it, waiting for a reconnect.

Disconnect is reversible, self-service, and instant. It is the right choice whenever the
goal is "stop this from touching my Intervals account right now", including temporarily.

## Two ways to ask

Export and deletion each have two routes, and they reach the same data and the same
erasure.

- **In the conversation.** Ask the coach. Your own connection is the identity check, which
  is a better one than anybody could perform over email, and where it works it is
  immediate.
- **By email**, to `tingcctwai@gmail.com`. Slower, and the one that is guaranteed. Some AI
  clients refuse a tool call at their own approval layer before it ever reaches this
  product; when that happens the conversation cannot complete the request and cannot tell
  you why, because nothing about the refusal reaches the product. The email route does not
  depend on any tool call succeeding.

The email route still has to establish that you control the account, and it does that the
only way this product can: **you re-authorize at Intervals.icu inside a window the operator
names in their reply.** That is the same consent you gave when you first connected. It does
not change your plan and deletes nothing. You will never be asked for a password, an API
key, or an authorization token — an athlete id, a display name, or the address a mail came
from are not proof of anything, and none of them is enough on its own.

Give the athlete id from your own Intervals.icu URL so the account can be found, or the
opaque account reference from a data export if you have one. Neither of them releases
anything by itself.

The operator's side of this is [ops/privacy-requests.md](ops/privacy-requests.md).

## Export

Ask the coach what it holds about you, or for a copy of your data, and it hands you the
whole archive in the conversation: your current plan, every version of it and the decision
behind each one, and everything you reported yourself — availability, strength sets, your
timezone and language. By email, the operator runs that same export and sends you the
archive — only to the address that made the verified request.

The archive names what it deliberately leaves out, and the reasons are worth stating here
too:

- **No credentials.** This product stores no Intervals.icu token. What it keeps is a
  one-way keyed digest that says which account a token opens, and that cannot be turned
  back into a token — not by you, not by an operator, not by whoever steals the file.
- **No raw provider payloads, GPS tracks, or activity files.** They are read to build a
  picture and never written down. Export them from Intervals.icu, which has them.
- **No internal storage identifier.** The archive carries an opaque, deployment-specific
  reference instead, which an operator can use to find your account and which is safe to
  quote in a public issue.
- **No email address or name.** Asking which account is connected, and confirming a
  delivery, both name the Intervals.icu account in the conversation — the address
  Intervals holds first, then your display name — because writing a workout to the wrong
  account of your own is otherwise invisible until afterwards. The address leads because
  your own account and a review account can share a display name. Both are read from
  Intervals at the moment you ask and used for that one answer. Neither is written to your
  plan, the archive, a log line, or the usage rows earlier releases wrote, so there is
  nothing here to export or delete.

Exporting changes nothing and can be done as often as you like.

There is a second half, for the other direction: "stop every client from reaching my plan
through this product," without waiting for the provider hop to notice. An operator runs
`revoke-connections` against the identity registry, which removes the recorded connections
for one owner. Every entry resolves a request through that registry — including the MCP
entry, whose access token carries the provider credential sealed inside it rather than
stored anywhere — so tokens already issued stop working immediately. PlanState is not
touched, and signing in again from any client resolves to the same owner and the same
plan. Doing both is the complete answer: the provider revocation ends this product's
access to Intervals, and this one ends every client's access to the plan.

## Deletion

Deletion removes what this product stores about you: your plan, its whole version history
and the decisions behind it, the evidence you reported yourself, any snapshot of that
history taken beside it, and the identity rows that map your Intervals.icu athlete id and
token to your account.

Ask the coach to delete your data; it shows you exactly what would go and what deletion
cannot reach, and removes nothing until you confirm that preview. The confirmation is bound
to the account as it was previewed — if anything changed in between, it asks again rather
than deleting something you did not see.

By email it is the same two steps and the same binding: once you have re-authorized, the
operator sends you the exact scope, you confirm that scope, and the erasure runs against
it. If the account moved between the two, you are sent the new scope and asked again. The
reply afterwards carries the deletion receipt and the counts proving the account is gone —
no plan content, because an audit record of a deletion should not be the last surviving
copy of what was deleted.

**What deletion does not reach, and cannot:**

- **Workouts already written to your Intervals.icu calendar.** That history belongs to
  your Intervals.icu account, not to this product. Remove those events yourself if you
  want them gone too.
- **Your Intervals.icu authorization.** Deleting data here does not withdraw a grant you
  gave there; revoke it in Intervals.icu Settings.
- **Operational logs**, which carry request paths and refusal reasons and no plan, health,
  or identity content at all.

Deletion is one-way. There is no plan to resume afterward — reconnecting after a deletion
starts a new plan, the same as any first-time sign-in.

**If you revoke your Intervals.icu authorization before deleting, delete first.** That
authorization is the only thing that identifies you here, in the conversation and by email
alike. Without it there is no proof of ownership this product can evaluate — every
identifier it holds is either a one-way digest or a value anyone could claim — and an
account will not be deleted on an assertion. Re-authorizing even briefly resolves it.

The one thing that can delay it: an unfinished delivery. If a publish was interrupted,
Intervals may hold a workout this product has not reconciled, and the record of that is in
the account it would be deleting. The preview says so, and resolving the delivery first is
the fix.

## Retention and backups

- **Plan state, decision history, and reported evidence** are kept until you delete them.
  There is no expiry and no inactivity sweep: an append-only history is what makes a plan
  replayable, and quietly trimming it would make an old decision unauditable.
- **Identity rows** — the athlete-id mapping and the token digests — live for as long as
  the account does. A digest is dropped on its own when Intervals tells this product the
  credential behind it no longer works.
- **Snapshots.** This product writes one only where a store format changes under an
  existing account, beside that account's own directory. Deletion removes them in the same
  operation, so there is no window in which a snapshot outlives the account.
- **Platform backups.** This product creates none. A deployment that enables its host's
  volume snapshots is creating copies this product cannot see or delete, and must document
  the rotation — and the resulting maximum delay before a deletion is complete — before it
  is used for anyone's real data. No backup-retention period is promised here, because
  none has been measured.
- **Logs** rotate with the platform that collects them and hold no plan, health, or
  identity content (`docs/ops/security-events.md`).

## The difference in one line each

Disconnect stops this product from reaching your account and keeps your plan waiting.
Export gives you a copy and changes nothing. Deletion removes the plan itself and severs
the identity mapping that would let a reconnect find it again.
