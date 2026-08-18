# Workout delivery compatibility

Last verified: 2026-08-14 (revised the same day by live probing; see the dated sections)

This document records the provider-format research behind Long Run Hybrid Coach's workout delivery boundary. It is development context, not Coach instruction: do not move these provider details into the Skill unless they materially change what the athlete needs to know.

## Decision

`PlanState.session.plan` is the product-owned source of truth for a workout. In particular, `plan.kind == "time_axis"` is the canonical executable representation for step-based workouts.

Do **not** make Intervals.icu workout text, Intervals `workout_doc`, Garmin FIT, or a Garmin API payload the product's canonical model. They are provider representations derived from the plan.

This is already the direction of the current code. The implementation should preserve it rather than introduce a second workout model.

Conceptually:

```text
Coach decision
    -> PlanState session.plan (canonical)
        -> Intervals delivery representation (current adapter)
            -> Intervals.icu calendar WORKOUT
                -> Garmin Connect
                    -> compatible Garmin device
```

A future direct Garmin or FIT path should derive from the same PlanState. It should not require the Coach or PlanState to speak provider-specific syntax.

## What Intervals.icu accepts

Intervals.icu's documented calendar write path is:

```text
POST /api/v1/athlete/{id}/events/bulk?upsert=true
```

A planned workout is a calendar event with `category: "WORKOUT"`. `external_id` can be supplied as the caller's stable identity and is suitable for idempotent upsert.

For workout content, the documented upload interfaces are:

1. native Intervals.icu workout text in `description`; or
2. a workout file (`fit`, `zwo`, `mrc`, `erg`) via filename/file content fields.

Source: https://forum.intervals.icu/t/uploading-planned-workouts-to-intervals-icu/63624

Intervals also returns a structured `workout_doc` when planned workouts are read. Intervals describes this as its native workout format. The returned structure can express nested steps/repeats, time and distance, lap-press termination, intensity, ramp/freeride/max-effort flags, and targets including power, HR, pace and cadence.

Source: https://forum.intervals.icu/t/downloading-planned-workouts-from-the-api/93737

Important boundary: `workout_doc` is useful as provider read-back semantics, but structured `workout_doc` JSON is not the documented general upload contract. The write path is therefore workout text, for every execution model, with no exception. The one narrowly-verified `workout_doc` write this product used to make was removed in issue #22 after the device showed what it actually delivered; a provider representation that read back byte-exact still reached the watch as a target constraining nothing.

## What Garmin exposes

Garmin's Training API allows an approved third-party integration to publish workouts and training plans into Garmin Connect. Garmin Connect then handles synchronization to compatible devices.

Source: https://developer.garmin.com/gc-developer-program/training-api/

Therefore this product cannot infer the actual Intervals -> Garmin wire representation from the fact that Garmin supports FIT. The observable current product path ends at verified Intervals state unless a Garmin-facing API later gives us a further observation.

FIT is nevertheless a useful interoperability target. Garmin's FIT Workout file type represents a structured workout with required File Id, Workout, and ordered Workout Step messages; it supports structured endurance and strength/yoga-style workout instructions.

Sources:

- https://developer.garmin.com/fit/file-types/workout/
- https://developer.garmin.com/fit/cookbook/encoding-workout-files/

FIT encoding details belong in a future FIT/Garmin adapter, not in Coach reasoning or PlanState.

## Current Long Run Hybrid Coach model

The current PlanState already has the right abstraction level:

```text
time_axis
  name
  steps[]

work step
  name
  duration
    time(seconds)
    distance(meters)
  target
    open
    pace(sec_per_km range)
    hr_ceiling(bpm)   # runtime delivery currently supports this

repeat step
  repetitions
  work steps[]
```

Relevant code/contracts:

- `contracts/plan-state.schema.json`
- `garmin_coach_loop/delivery.py`
- `garmin_coach_loop/prescription.py`

`prescription` is a human-readable rendering of the plan. Intervals workout text is another rendering of the same plan for a provider parser. Neither rendering is an independent prescription source.

Do not introduce a second generic `Workout` document just to rename `time_axis`. If future execution models require a reusable type, extract only when there is a second real consumer that justifies it.

## Current Intervals delivery mapping

The existing delivery boundary derives an Intervals event roughly as:

```json
{
  "external_id": "gcl:...",
  "category": "WORKOUT",
  "type": "Run",
  "name": "...",
  "start_date_local": "YYYY-MM-DDT00:00:00",
  "description": "Intervals workout text"
}
```

Running open/pace workouts use the documented workout-text route. Strength currently publishes as a titled `WeightTraining` calendar entry rather than inventing structured strength execution the product cannot verify.

A heart-rate ceiling ships through the same workout-text route, rendered as `{low}-{high}% LTHR`. The plan owns the ceiling in bpm; the percentage is the Intervals rendering of it, resolved against the account's Run `lthr`. There is no longer any second outbound representation -- see the 2026-08-14 device finding and the 2026-08-16 replacement below.

### What a supplied `workout_doc` is, and is not (live-probed 2026-08-14)

Four events written to a live account on an otherwise empty date, then read back and deleted:

| written as | HR target Intervals stored | training load | `zoneTimes` |
| --- | --- | --- | --- |
| `workout_doc`, `hr.units = "bpm"` | stored verbatim | not computed | `null` |
| `workout_doc`, `hr.units = "%hr"` | stored verbatim | not computed | `null` |
| workout text, `70-80% HR` | parsed | 26 | computed |
| workout text, `70-83% LTHR` | parsed | 20 | computed |

The `%hr` row is the control: it removes the unit as an explanation. Intervals runs its own analysis pipeline only over a workout it parsed itself; a supplied `workout_doc` is stored and returned unchanged but is not otherwise processed.

This narrows the earlier finding. "Survives read-back byte-exact" holds. "Enforceable" was never established by that observation and is not established by this one either: whether the ceiling reaches the watch is a separate hop this product cannot observe, and the same forum thread that reports lost pace targets on export is consistent with a supplied document being treated differently on the way out.

This was, at the time, read as a reason to keep the `workout_doc` write: workout text cannot express an absolute BPM ceiling at all, and the alternative looked like delivering no ceiling. The device check below settled it the other way.

### What Garmin Connect on a phone can and cannot settle (observed 2026-08-14)

Three probes on one day — absolute BPM via `workout_doc`, absolute pace via workout text, `% LTHR` via workout text — all reached Garmin Connect, and every event's `push_errors` stayed null. So the Intervals-to-Garmin push works for this account and does not report an error for any of the three.

The phone view cannot separate them further. For all three, Garmin Connect renders the workout's summary and Notes from the Intervals `description` string verbatim (the `- ` line prefixes are visible in the app), and its `Steps` section shows only Garmin's generic storage notice plus "View details and edit on Intervals.icu Training". It renders no per-step target for any of the three, including the one Intervals fully parsed. Uniform output across three deliberately different encodings is no evidence about any of them.

Garmin does receive some structure: the app shows a total time for the two time-only probes and none for the probe containing a distance step, which matches what a duration-bearing workout would produce.

Still unsettled, and only settleable on the device: whether each step carries a target during execution.

### Settled on the device, 2026-08-14: the absolute-BPM ceiling does not arrive

A `workout_doc` carrying `hr: {units: "bpm", start: 0, end: 140}` reaches the watch as a heart-rate target of **1 to 252 bpm** -- Garmin's full range. The ceiling is not merely dropped; it is replaced by a target that permits everything, so the step displays a heart-rate target and constrains nothing. That is worse than no target, because no target is honest about itself.

Every earlier observation was consistent with both outcomes and could not separate them: read-back byte-exact inside Intervals, `push_errors` null, no training load computed, and a phone view that renders all encodings identically. The device separates them.

Consequence, at the time live: every recovery run delivered through this path carried a ceiling the watch never enforced.

Unverified, noticed in passing: the pace probe's description carried `6:05-6:20/km` and the athlete read the watch step as `6:10`. Either Garmin collapses a pace range to one representative value on that screen, or the reading was approximate. If it is the former, the band's edges do not reach the athlete and a threshold session is executed against a single number instead of a range. Worth one deliberate look the next time a pace workout is on a watch (issue #21).

### The replacement, and why it is the only one (issue #22, 2026-08-16)

On the same probe day, `50-86% LTHR` written as workout text arrived on the watch as **81-140 bpm** against a Run threshold HR of 163. That is the whole fix: the one encoding the provider parses is also the one that reaches the device intact.

Re-probed live on 2026-08-16 to establish the read-back shape before writing the verifier against it:

| written as | Intervals stores | training load | `zoneTimes` |
| --- | --- | --- | --- |
| workout text, `50-86% LTHR` | `hr: {units: "%lthr", start: 50, end: 86}` | 18 | computed |

Note what separates this from the row above it: Intervals ran its own analysis, which it never did for a supplied document. The provider parsed the target rather than storing an opaque blob, and a parsed target is the precondition for exporting one.

Consequences taken in code:

- `_hr_ceiling_workout_doc` and every outbound supplied `workout_doc` are gone. `_provider_payload` emits workout text for every execution model, and a regression asserts no payload carries the field.
- The ceiling stays canonical in bpm in PlanState. `hr_ceiling_percent_lthr` converts it, choosing the **largest** whole percent that still resolves at or under the ceiling -- never the nearer one, because rounding up is the silent loosening this replaces.
- The provider's rounding is modelled as round-half-up although it was observed to truncate (50% of 163 arrived as 81, not 82). Modelling it as the looser of the two is what makes the guarantee survive a provider change: an encoding accepted here is safe under either rule.
- The floor is a fixed 50% of threshold, below any running heart rate the athlete produces. The grammar requires a range; the plan gives no floor; so the floor chosen is one that adds no instruction.
- Threshold HR is read from Run sport settings (`lthr`) at **preview**, and a ceiling with no readable threshold blocks there with one actionable message -- never a silent downgrade to an open target, and never a discovery made after a provider write. It is re-read at publish and must still match what the athlete confirmed.
- Read-back checks the unit is `%lthr` (not `bpm`, and not `%hr` -- the 2026-08-12 max-HR denominator failure), that the percentages are the confirmed ones, and that they resolve back to a bpm at or under the plan's ceiling.

Both entry points can make this read. The hosted OAuth authorize request now carries `SETTINGS:WRITE`, which includes read access; the earlier `SETTINGS:READ` connection was confirmed live on 2026-08-15 (issue #41), and issue #179 deliberately upgrades it before public submission.

## Keeping this checked

`scripts/probe_provider_conformance.py` writes one probe per shape the delivery boundary can emit — open/time, open/distance, absolute pace on a distance step, absolute pace inside a repeat, absolute BPM ceiling — to an empty date on a live account, verifies each with the product's own `verify_readback`, reports whether Intervals ran its own analysis over it, and deletes them again. `--keep` leaves them for a device check.

It builds every payload through the real `prepare_delivery_set` and `_provider_payload` rather than restating them, so it cannot drift from what the product actually sends. Adding an execution model means adding a probe, or this stops describing the boundary. It is manual and opt-in: it writes to a real calendar, so it is never run in CI.

Last full run, 2026-08-16: all six shapes exact. Provider analysis present for both pace probes and both heart-rate-ceiling probes, absent for the open probes (correct — no intensity to analyse). The ceiling probes moving from absent to present is the whole of issue #22 in one column: Intervals now parses the target instead of storing an opaque document.

## Compatibility risk: provider acceptance is not device semantic success

An Intervals event being accepted and parsed correctly does not prove Garmin will preserve every target.

A July 2026 real-world report showed an API-created running workout whose Intervals `workout_doc.steps` contained all expected pace ranges while the Garmin watch received the correct distances with `No Target`. The cause was an unset Run `threshold_pace` in the athlete's Intervals sport settings. Once threshold pace was configured, a fresh export preserved the pace targets.

Source: https://forum.intervals.icu/t/pace-targets-lost-in-garmin-export-for-api-created-running-workouts-steps-arrive-on-watch-as-no-target-parsed-correctly-in-workout-doc/130706

Implication: delivery has at least three semantic boundaries:

```text
PlanState semantics
  -> Intervals parse/read-back semantics
    -> Intervals-to-Garmin export prerequisites/semantics
      -> device sync/execution
```

Today Long Run Hybrid Coach can verify the first two. It must not label the latter two as observed success.

For provider-dependent targets such as running pace, add a prerequisite check only when the prerequisite is both reliably observable and necessary to prevent a known silent degradation. Do not build a generic capability-negotiation framework prematurely.

## Concrete issues found in the current repository

### 1. `hr_ceiling` contract drift — resolved 2026-08-14

Runtime delivery and prescription rendering understood `target.kind == "hr_ceiling"` while `contracts/plan-state.schema.json` exposed only `open` and `pace`, so a legal PlanState could not describe a workout the product delivers live. The schema was the stale layer and now mirrors `validate_plan_state` exactly, including the rule that an `hr_ceiling` target may not appear inside a repeat. The one cross-step rule a JSON Schema cannot state — a workout may not mix `pace` and `hr_ceiling` — stays in the validator and is named in the schema's own description.

The drift survived because the schema tests only ever validated example plans, and no example carries an `hr_ceiling`. The regression added asserts both layers against the same fixture, in both directions.

### 2. Intervals-specific write details need a clearer boundary

`delivery.py` currently contains canonical-to-provider rendering, Intervals transport, provider payload quirks, mutation journaling, and read-back verification in one large module. Do not perform a broad architecture rewrite for this research alone, but make it unambiguous in code/tests that:

- PlanState is canonical;
- workout text is an Intervals rendering;
- `workout_doc` is read-back only, never a supplied payload;
- no provider payload may become a second source of workout truth.

A small extraction is justified only if it reduces an actual coupling or makes the above invariant testable; avoid introducing a generic adapter hierarchy with only one provider.

### 3. Pace delivery needs an export prerequisite strategy

Before claiming a pace workout is suitable for Garmin forwarding, determine whether the product can reliably observe the Intervals Run `threshold_pace`/sport-setting prerequisite with the scopes and APIs already available.

If reliably observable, fail before provider mutation with one actionable explanation when a pace target would otherwise be silently stripped on Garmin export. If it is not reliably observable with the current permissions/API, keep the product boundary honest and document the unresolved external risk rather than guessing.

This check is distinct from `PlanState.athlete_baseline.threshold_pace_sec_per_km`: the former is a provider export prerequisite; the latter is coaching evidence supporting the prescribed number.

### Observability, settled (2026-08-14)

The prerequisite lives at `GET /api/v1/athlete/{id}/sport-settings`, on the entry whose `types` contain `Run`, as `threshold_pace` in metres per second.

It is observable on one of the two entry points, not both:

- **CLI, personal API key** -- readable. Confirmed live against a real account.
- **Hosted, OAuth** -- readable, confirmed live 2026-08-15 (issue #41). Intervals defines `SETTINGS` ("Athlete settings") as its own scope alongside `ACTIVITY`, `WELLNESS`, `CALENDAR`, `CHATS` and `LIBRARY`; issue #179 changes the authorize request from `SETTINGS:READ` to `SETTINGS:WRITE`, whose write modifier includes read access. Scopes are chosen in the authorize query, not on the application-registration page.

Source: https://forum.intervals.icu/t/intervals-icu-oauth-support/2759

The implemented **pace** rule now requires an answer: at preview, a missing value becomes a narrow settings change derived from the measured PlanState threshold pace and bound into the same confirmation hash. Apply refuses if the value changed, otherwise writes only the missing field, reads it back, and only then publishes. An unreadable setting blocks rather than guessing.

The **heart-rate ceiling** rule is deliberately stricter, and the difference is not inconsistency. An unset threshold pace degrades a delivery this product can still describe honestly; an unreadable threshold HR leaves no correct number to send at all, because the ceiling has no other encoding that reaches the watch. So a silence blocks there, at preview, before the athlete confirms anything.

## Implementation constraints

- Preserve `PlanState.plan` as the one executable source.
- No FIT-first migration.
- No generic provider/adapter framework unless a second provider is actually being implemented.
- No new provider grammar in the Coach Skill.
- Keep user-visible behavior simple: the athlete approves the same workout preview; provider details stay internal unless they block delivery or make the success claim weaker.
- Continue exact read-back verification at the strongest observable boundary.
- Continue reporting delivery no further than `intervals_accepted` until another external hop is actually observable.
- Add regression tests for every silent semantic degradation that becomes a blocking guard.

## Expected user-facing effect

Before: a correctly planned workout can be accepted by Intervals while provider-specific prerequisites or contract drift still make the delivered target incomplete or impossible to represent.

After: the athlete sees the same simple approve-and-deliver flow, but the internal contract is coherent and known provider prerequisites fail before a misleading delivery claim. No extra workout format or manual setup is exposed unless it is actually required to fix the blocking condition.
