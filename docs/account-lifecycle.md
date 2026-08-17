# Account lifecycle — disconnect vs. deletion

Two different requests get confused with each other constantly: stopping access, and
removing data. This page says which is which, and what each one actually does.

## Disconnect

Revoke Long Run Hybrid Coach from Intervals.icu Settings (Apps you've authorized). That
takes effect immediately, on the provider's side, and needs nothing from this product.

What happens next:

- The next authorized call this product makes on your behalf fails. Intervals answers
  with an authorization error, this product reports it as a generic provider error, and
  nothing about your token, owner id, or the failure's details is exposed.
- Your PlanState is not touched. Nothing is deleted, nothing is exported, nothing changes.
  It sits exactly where the last confirmed decision left it.
- Reconnecting resumes the same plan. Identity here is keyed on your Intervals.icu
  athlete id, not on the access token, so signing in again resolves to the same owner and
  picks the plan back up mid-cycle — it does not start over.

Disconnect is reversible, self-service, and instant. It is the right choice whenever the
goal is "stop this from touching my Intervals account right now," including temporarily.

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

Deletion removes what this product stores about you: your PlanState (goals, plan
history, decisions), the athlete-reported evidence you gave it (availability, self-reported
strength sets), and the identity rows that map your Intervals athlete id and access-token
fingerprint to your owner record.

Deletion is not currently self-service. Request it the way the
[privacy policy](privacy.html) already describes — during development, access, export,
correction, and deletion requests are handled manually. An operator runs one command
(`delete-owner`) against the identity registry and your owner directory, and confirms
back to you what was removed.

What deletion does not reach, and cannot: any workout this product already wrote to your
Intervals.icu calendar. That history belongs to your Intervals.icu account, not to this
product, and deleting local state cannot and does not claim to delete it. Remove those
events from Intervals.icu yourself if you want them gone too.

Deletion is one-way. There is no PlanState to resume afterward — reconnecting after a
deletion starts a new plan, the same as any first-time sign-in.

## The difference in one line

Disconnect stops this product from reaching your account and keeps your plan waiting for
you. Deletion removes the plan itself and severs the identity mapping that would let a
reconnect find it again.
