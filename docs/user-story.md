# User Story — Long Run Hybrid Coach

## Persona

A runner with a Garmin watch who also lifts regularly. They want structured
progress toward a stated goal without managing training science, spreadsheets,
provider details, or intermediate files.

## The story

> As an athlete, I ask the coach to reassess my goal and plan. It reads the latest
> available evidence and the one current plan, records completed work it can match
> deterministically, then gives me an executable 28-day direction and this week's
> running-plus-strength plan. It tells me what changed and why. The selected plan
> remains current for the next daily revisit or weekly review.

## The first screen

Before implementation detail, the athlete sees:

1. the current 28-day goal and primary／maintenance direction;
2. what to do today and across this week, including executable prescriptions;
3. what materially changed from the previous plan and the two to four reasons
   that actually changed the decision;
4. whether each publishable session is waiting for its exact confirmation
   or has been accepted and read back from Intervals.

Unknowns appear only when they constrain the decision. Validation reports,
provider diagnostics, hashes, and history details come later.

## What the system does

- Reads the latest available activity, recovery, calendar, current PlanState, and
  recent actual-completion evidence.
- Automatically commits only identity-backed, completed planned → actual matches.
  Ambiguous or partial matches remain visible and are not guessed.
- Uses coaching judgment to select or update one 28-day primary adaptation,
  maintenance direction, measurement approach, and stop／adjust conditions.
- Produces one weekly hybrid plan. Running is executable on the watch; strength
  names exercises, sets, reps, and a baseline-supported load or one explicit
  item that still needs confirmation.
- Persists the selected result as the only current state. It never reconstructs a
  parallel plan from conversation memory.

## Reviewing progress

The athlete asks whether they are progressing. The answer comes in one order:
whether they are on track, not yet demonstrated, or the evidence points at a
change; what was actually trained against what was planned; how they responded,
kept separate from what they finished; what the outcome evidence says against the
goal's own measurement protocol; and what happens next, with the evidence behind
it.

Weeks are calendar weeks, Monday to Sunday. Completing the sessions is never by
itself evidence that fitness improved, and one poor wearable value is never a
failed cycle. When the measurement protocol has not been run, progress is unproven
and no wearable number stands in for it. A review that changes nothing still ends
with a conclusion and the next measurement or review condition.

## Interaction cadence

- A weekly reassessment is the normal planning rhythm.
- Daily use is pull-based: normal days follow the current plan; a conversation is
  needed when the athlete asks what to do, reports a material change, or reaches a
  review condition.
- Saying “done” is optional acceleration. Synced actuals are still read and
  reconciled on the next refresh, whichever entry point the athlete started the
  session from.
- The system asks only for information that data cannot provide and that changes
  the decision, such as availability, equipment, an unsupported strength load, or
  a current red flag.

## One delivery confirmation

The athlete confirms one exact preview for the publish set. The approval is bound
to that exact content; changing the workout invalidates it. After confirmation,
deterministic code performs product-owned-event deduplication, writes to
Intervals.icu, reads the event back, verifies the delivered prescription, and
updates state only to the rung actually observed.

Every session in the week is published, so the whole plan travels together rather
than only its running half. What differs is how much structure each sport carries,
and that grows as each sport earns it. A strength session is delivered today as a
titled calendar entry: the title states the day's purpose and the prescription
rides along as text. It carries no executable step structure, and a read-back that
contains one fails closed — the product must not imply it sent a structure it
never built.

## Delivery honesty

The product can observe Intervals accepting and returning an event. It cannot
observe the later per-workout hop to Garmin Connect or the watch, so no state or
first-screen label claims those hops occurred.

## Out of scope

Structured strength delivery to Garmin, publishing without confirmation, direct
Garmin API writes, a generic provider framework, multi-user service, and medical
diagnosis.
