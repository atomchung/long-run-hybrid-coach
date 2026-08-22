# Reading how much the service is used

`verify-production-status.md` answers whether the service is healthy and
`security-events.md` answers what happened at its trust boundary. This one answers the
question that comes before either: **how many people use this, and how often.**

There is no analytics service behind it, and deliberately so. The gateway counts one row
per account per UTC day per tool in its own identity registry, and this page is how that
count is read back.

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
      "tools": {"session": 38, "delivery_apply": 6}
    }
  ]
}
```

`registered` is every account that exists **now** -- not every account that ever
authorized. Deletion removes an owner's row along with their counters, so a deleted
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

**`active_days`, not `calls`.** A day is counted once no matter how many times a client
calls, so a retry loop or a chatty conversation moves `calls` and cannot move
`active_days`. Read `calls` as intensity within a day, never as a population figure.

A call is counted when it is dispatched, so a refused one counts too. That is intentional:
an athlete whose every session is being blocked is using the product, and a report that
showed them as inactive would point at the wrong problem.

## What it cannot tell you

By construction, not by omission. The table holds an owner id, a date, a tool name and a
count — so there is no way to ask it:

- **who anybody is.** No email address is ever collected: Intervals returns an athlete id
  and a token at exchange, there is no UserInfo endpoint, and no `openid`/`email` scope
  (`docs/distribution/openai-plugin.md`).
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

The counters are the athlete's rows. `delete_owner_identity` removes them in the same
transaction as the identity rows, so a deletion cannot leave them behind, and there is no
second sweep to remember. The deletion preview states that they go (`usage_counters`), and
a data export states that they are held (`identity.usage_days`).

The preview states rather than counts them on purpose: a deletion proposal binds the hash
of its own preview, and a usage count would change on the very calls that confirm it.

## When to replace this

When there are enough accounts that a number stops being enough and a trend is what is
wanted, or when the questions start needing time-of-day or per-client answers. The move
then is a log drain to a long-lived sink, chosen against real traffic — not a client-side
analytics SDK, which this service has no browser to run in and no page view to report.
