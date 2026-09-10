# Reading how much the service is used

`verify-production-status.md` answers whether the service is healthy and
`security-events.md` answers what happened at its trust boundary. This one answers the
question that comes before either: **how many people use this, and how often.**

There is no analytics service behind it, and since 1.4.3 there is no counter either.

**The counters stopped on 1.4.3.** Every dispatched call used to write one row per account
per UTC day per tool, plus one per outcome. Those writes are why a plain read could not be
annotated `readOnlyHint: true`, and a client that cannot tell a read from a write asks the
athlete to approve both — so the owner dropped the report rather than the permission
(issue #408). Nothing replaced them: no queue, no deferred flush, no second telemetry path.

What that leaves is this page, honestly narrower than it was:

- **`registered`, `created_at` and `entries` still answer who is here.** They are written
  once, at authorization, and are the numbers that actually told an operator something new.
- **`active_days`, `calls`, `tools`, `accepted` and `refused` are frozen history.** Rows
  written before 1.4.3 are still readable and still an athlete's own data — deleting an
  account still clears them — but nothing adds to them. An account that arrived after
  1.4.3 reads `active_days: 0` forever, and that is not a dormant athlete.
- **For anything about live traffic, read the security log** (`security-events.md`). It is
  the same stream `mcp_authentication` events go to, and its retention is Railway's.

## The report

With the Railway CLI linked to the production environment (`roll-with-railway-cli.md` for
the one-time `railway link`):

```bash
railway ssh "python3 -m garmin_coach_loop.cli usage-report --identity-db /data/identity.db"
```

```json
{
  "status": "passed",
  "registered": 2,
  "active": 2,
  "since": null,
  "owners": [
    {
      "owner_id": "…",
      "registered_at": "2026-08-15T14:03:25Z",
      "active_days": 4,
      "calls": 61,
      "first_active_day": "2026-08-15",
      "last_active_day": "2026-08-20",
      "tools": {"session": 38, "delivery_apply": 6},
      "accepted": 41,
      "refused": {"plan_state_exists": 3},
      "entries": ["https://claude.ai"]
    }
  ]
}
```

In a report read after 1.4.3, the counter fields above are pre-1.4.3 history and the
`entries`/`registered` fields are current. `registered` is every account that exists
**now** -- not every account that ever authorized. Deletion removes an owner's row along with their counters, so a deleted
account leaves no trace in this report at all, which is what erasure is supposed to mean.
Read the number as a current population, never as a lifetime signup total. `active` is how
many of them have any counted activity. An account that connected once and never came back appears with
`active_days: 0` and a null `last_active_day` — that row is the one worth looking for.

Bound the window with a UTC date to get a monthly or weekly active count. `registered`
deliberately ignores it, so the two numbers read as "how many exist" beside "how many were
active":

```bash
railway ssh "python3 -m garmin_coach_loop.cli usage-report --identity-db /data/identity.db --since 2026-08-01"
```

## Which number to trust

**`registered` and `entries`, which are the two still being written.** Of the frozen
fields, `active_days` was always the one to read rather than `calls`: a day counted once
however many times a client called, so a retry loop moved `calls` and could not move it.

A call was counted when it was dispatched, so a refused one counted too -- deliberately,
because an athlete whose every session is blocked is using the product. That question now
has one answer only: the security log, which records the refusal at the trust boundary.

## `accepted` / `refused`, and why zero calls is not zero information

`active_days` and `calls` say how often an account calls. They cannot say what happened
when it did, and without that an account with an empty store reads the same for three
opposite reasons: it authorized and never called anything, it spent its whole session on
read-only tools, or it called something and was turned away. Those are a distribution
question, a coaching-quality question and a bug (issue #275).

`accepted` counted calls this gateway answered. `refused` breaks the rest down by this
gateway's **own** refusal code -- `plan_state_exists`, `proposal_expired`,
`provider_error` and the rest. Never an exception message, never a provider body, never
anything the caller sent: a code the writer did not recognise was filed as `other`. The
bounded set the writer checked against went with the writer in 1.4.3, so `other` in a
pre-1.4.3 row means "a code nobody had added yet", and no new row can appear under any of
them.

Before 1.4.3, an account with `active_days: 0` and no refusals never dispatched a tool at
all, and one with refusals and nothing accepted was the bug case. After it, `active_days:
0` means only that the account arrived after the counters were removed. **The account
worth looking for is now the newest row in `registered` that you do not recognise**, and
the bug case is read out of the security log instead.

## `entries` -- which platform carried somebody in

Recorded once, at the provider callback, which is the only point in the flow holding
both an owner and an origin: a client registers before anybody has consented, and every
request afterwards carries a token rather than a redirect URI (issue #209). The value is
`local` for a loopback MCP client, a verified origin this gateway already accepts --
`https://claude.ai`, `https://chatgpt.com` -- or, since 1.4.1, the fixed word
`unverified` for anything else: an anonymous registration must not be able to choose
what a bounded column stores.

**Which platform, never which channel.** No referrer survives an OAuth callback, so this
cannot say whether somebody came from a forum post, a registry listing, or a link a friend
sent. Distinguishing those needs a distinct URL per channel, which is a product decision
and not a column.

It is deliberately **not** bounded by `--since`. Arrival happens once; a window asking who
was active last week would otherwise erase the answer for everybody who arrived before it.

## What it cannot tell you

By construction, not by omission. The table holds an owner id, a date, a tool name and a
count — so there is no way to ask it:

- **who anybody is.** No email address is ever stored: Intervals returns an athlete id and
  a token at exchange, there is no UserInfo endpoint, and no `openid`/`email` scope
  (`docs/distribution/openai-plugin.md`). A tool result that has to name the connected
  account reads the athlete's Intervals profile at that moment and keeps nothing, so it
  never reaches this table — these rows hold an owner id, a date, a tool name and a count,
  and there is no column an address could arrive in.
- **where they are.** No IP address, no user agent, no referrer is recorded anywhere.
- **which client they used.** claude.ai and ChatGPT are indistinguishable here. The
  security log's `client` handle separates *connections*, not people, and cannot be
  counted as either.
- **what they did.** No request body, no plan content, no argument value.
- **when within a day.** The finest timestamp is a date.
- **anything about an account that was deleted.** Its rows went with it.

For anything time-of-day or client-shaped, `security-events.md` is the stream to read, and
its retention is Railway's log retention — which is why this counter exists separately.

## Deletion

The counters are the athlete's rows, frozen or not. `delete_owner_identity` removes them
in the same transaction as the identity rows, so a deletion cannot leave them behind, and
there is no second sweep to remember. The deletion preview states that they go
(`usage_counters_removed`, `call_outcomes_removed`, `entry_origins_removed`), and a data
export states what is held: `identity.usage_days` counts the days on record,
`identity.call_outcomes_recorded` is read from the rows rather than asserted, and
`identity.entry_origins` names the platforms.

The preview states rather than counts them on purpose: a deletion proposal binds the hash
of its own preview, and a number that moved between the preview and the confirmation would
refuse the erasure. That was a live hazard while the counters were running; it is now a
property the preview keeps rather than one it needs.

## When to replace this

When there are enough accounts that "who is here" stops being enough. Whatever answers it
then has to be built without making a read look like a write to a client: the counters
were removed because a per-call write inside a tool is charged to the athlete as an
approval prompt. A log drain to a long-lived sink, chosen against real traffic, is the
move that does not have that cost -- not a per-call row in the identity registry, and not
a client-side analytics SDK, which this service has no browser to run in and no page view
to report.
