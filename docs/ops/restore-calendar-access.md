# Restoring calendar access to a connection

You are here because a hosted publish failed with

```
delivery_blocked: Intervals GET failed with HTTP 403: this connection was not granted
the Intervals calendar. Reconnect Intervals and grant calendar access, then retry the
same delivery.
```

or because `inspectIntervalsPermissions` reported `calendar_read: denied`. Both mean one
thing: **this athlete's connection cannot read their calendar**, so nothing can be
delivered to it. Reading Settings, activities and wellness keeps working, which is why
the connection looks healthy everywhere else.

## What this is not

It is not a missing scope in the application registration, and re-registering will not
help. intervals.icu documents six scopes — ACTIVITY, WELLNESS, CALENDAR, CHATS, LIBRARY,
SETTINGS — with one modifier rule: "For each scope specify READ or WRITE (to update,
implies READ access)"
([forum.intervals.icu/t/intervals-icu-oauth-support/2759](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759)).
There is no `CALENDAR:READ` to add; `CALENDAR:WRITE`, which this application already
requests, carries the read.

What the same page also says is where a denial comes from: intervals.icu "will ask the
user to login and display a confirmation dialog with options to choose which scopes to
grant the application" — every permission its own checkbox, and the athlete can leave
one unticked. So the requested scope list is a request, and the granted token can hold
less.

## The checklist

1. **Confirm it with a live read, not with a scope list.**

   Call `inspectIntervalsPermissions`. `calendar_read` is a read performed just now;
   `scopes_recorded_at_authorization` is only what the token said when it was issued and
   proves nothing about what the provider allows today.

2. **Have the athlete reconnect, and say what to tick.**

   Reconnecting is done from their own client — the claude.ai connector settings, the
   ChatGPT connector, or whichever MCP client holds the connection. On the intervals.icu
   consent page, the calendar row must be ticked (both 阅读 and 更新). Leaving it
   unticked is what produced the denial.

   **Do not revoke the authorization at intervals.icu to force this.** Authorization
   there is granted per application per athlete, so revoking signs out every connected
   client at once and each has to reconnect by hand — see the incident recorded in
   `CLAUDE.md`. Re-authorizing without revoking is safe, and it is all that is needed:
   the new grant replaces the old one and earlier fingerprints are kept deliberately.

3. **Verify against the connection, not against the reconnect.**

   Call `inspectIntervalsPermissions` again from the reconnected client and require
   `calendar_read: readable`. A client that held a connection before the reconnect is
   still holding the old token — the new one is bound at connection time — so run this
   from the client that will do the delivery.

4. **Retry the same approved delivery set.**

   Re-run `prepareWorkoutDelivery` → `publishWorkoutDelivery` for the same sessions. If
   an attempt was left open, retry the *same* set rather than preparing a new one; the
   journal converges it without a duplicate event.

5. **Read the calendar back.**

   `delivery_state: intervals_accepted` says the product's own record; only the account
   says what Intervals holds. For a structured run, check the read-back carries
   `training_load` / `intensity_factor` — Intervals computes those only after it has
   parsed the workout into steps.

## When re-authorization is genuinely required, and when it is not

- **A declined or lost calendar permission (this document):** one athlete reconnects.
  Nobody else is affected.
- **A change to the requested scope list:** every existing token would have to be
  re-authorized, because a scope change never applies retroactively to tokens already
  issued. No such change is needed for calendar delivery, and none has been made.
