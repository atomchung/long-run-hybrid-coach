# What a scope change costs

Scopes are chosen in each authorize query, not fixed at registration — the Intervals.icu
application-registration page has no scope field. So changing the requested set is one line
of code and a deploy. The cost is everything that follows the deploy, and it is paid by
people, not by the pipeline. Read this before deciding to add, drop, or widen a scope;
issue #179 is the first crossing and the record of why the timing mattered more than the
feature.

## Every connected grant re-authorizes by hand

A token issued under the old authorize set keeps working until it expires or is refused,
but a *reconnect* — and any newly connecting client — sees the new consent screen. There is
no server-side migration: the owner's claude.ai connection, every ChatGPT connection, every
local MCP client, and the reviewer test account each complete the new consent themselves,
one athlete at a time. Plan for the reconnects in the same change that lands the scope,
and name them in the PR.

## The consent page is a set of independent checkboxes

Intervals presents each permission as its own tickable box, and a grant completed with one
box unticked is not an error — it is a working token missing one capability. That is
exactly the 2026-08-18 incident: calendar left unticked, Settings reads returning `200`,
and every delivery failing with a `403` that had to be traced back to a consent screen
nobody could see anymore. After every reconnect, run the per-permission diagnostic
(`inspectIntervalsPermissions`) and check each item, not just that the connection "works".

## Timing decides the price

Before a public listing exists, the reconnect set is the handful of connections this
project controls — each one reachable, each one a click. After a listing exists, it is
every installer, including a reviewer mid-review whose connection dying under them reads
as a broken product, not as a scope migration. That asymmetry, not the feature itself, is
why issue #179 took `SETTINGS:WRITE` before submission rather than after: a scope the
product will foreseeably need is cheapest to request while the audience is still small.

The other side of the same coin: a scope the product does not exercise is a claim a
directory reviewer can falsify by reading the tool catalogue. Request a scope in the same
change that ships the code using it, never speculatively —
`docs/distribution/README.md` states the justification a reviewer checks each scope
against.
