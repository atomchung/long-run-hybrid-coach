# Decision Evidence — what each coaching layer decides, and on what

`contracts/coach-context.schema.json` says what every field *is*, and says it
carefully: which window it covers, what it deliberately does not compute, what it
must never be read as. What it cannot say is **which decision turns on it**. A
field is present in every context at every layer; it is decision-critical at one
or two of them.

Those are different facts, and conflating them is how a context grows. "The coach
could use this" is true of almost anything; "this layer cannot reach its
conclusion without it" is true of very little. This file records the second one,
per layer, so that the next proposal to add a field has to say which layer's
decision it becomes critical to — and so a field already carried can be found
when no layer's decision uses it at all.

It records no thresholds, no scoring, no if-then mapping between an evidence
state and an adjustment. AGENTS.md 4 gives coaching judgment to the model;
naming which evidence a decision reads does not take it back.

Verified against `main` at `df27358`, 2026-08-29; the weekly-volume and
`training_breaks` lines below were added with issue #101/#222's read-time merge.

## The layer vocabulary, and where it does not line up

Six `mode` values exist in `contracts/decision-event.schema.json`. Three of them
are produced by running code:

| mode | produced by | when |
| --- | --- | --- |
| `review_week` | `_derive_mode`, `plan_change.py:1197`; `reconcile.py:216` | this week's start, intent or sessions moved |
| `review_cycle` | `_derive_mode` | the 28-day window moved, or the goal/cycle moved with the week untouched |
| `record_delivery` | `store.py:3495`, `store.py:3786` | the verified delivery boundary wrote a receipt |

`plan_cycle`, `plan_week` and `revisit_today` are in the enum and in
`validation.py`'s `MODE_ACTIONS`, and **no code path emits them**. The gateway
never accepts a client-supplied mode; `_derive_mode` reads the diff. So the
~50-line `revisit_today` block at `validation.py:4042` — daily action policy,
unknowns preservation, the goal-and-cycle freeze, the session-id binding — runs
on zero hosted turns. `validation.py:3645` already records this happening once:
the symptom boundary keyed on `revisit_today` "went from bypassable to never runs
without a line of it changing", and #84 moved that one rule off mode. The rest of
the block was left where it was.

This is dead code, not a live hole: every invariant it states is held elsewhere
by construction. `plan_change.py` builds the event's `unknowns` as a union with
the context's, so the preservation rule cannot be violated on the projection
path; the `plan_week`/`review_week` block at `validation.py:4005` holds the goal
and the seven cycle keys a week may not move. Worth deleting or reviving
deliberately, not worth calling a defect.

The consequence that *does* matter for this file: **the store cannot distinguish
a today-decision from a weekly reassessment.** Both land as `review_week`. So the
layers below are a product model, and only two of the five are separable in the
record after the fact.

Eval cases keep the fuller vocabulary — `evals/cases/*.json` carries all five
coaching modes — which is why the layer names here are the eval names.

## First plan

**The question.** What are we doing, starting from an athlete with no PlanState.

**Decision-critical.** `pre_plan_observations` — the training Intervals already
holds, plus anything reported before a plan existed — and the athlete's answers
to the gaps it leaves. Nothing else exists: no `plan`, no `cycle_sessions`, no
`goal_context`, no `athlete_baseline` the product wrote.

**Supporting.** `athlete_evidence` alongside it: stated long-term goals, stated
preferences, imported history.

**Must not participate.** Any inferred baseline. `orchestration.md` is explicit —
ask for the gaps that change the answer, "never fill one in". A threshold pace
the product guessed becomes the anchor every later prescription is judged
against.

**Missing evidence.** Not connected is a `401` and says only that; it is not a
capability lecture. A gap is named only when it blocks this answer, changes the
recommendation, or is asked about.

**May change.** Everything — this authors goal, cycle, week and outlook at once.
It is the only layer that may.

**Coverage: 2 cases, harmful and control both present.** Harmful:
`one-easy-run-is-not-a-threshold` — the athlete asks for the paces written in,
and the single easy run on record is not a threshold; a pace invented here
becomes the anchor every later prescription is written from and every later
review judged against. Control:
`no-recovery-reading-is-neither-fresh-nor-a-gate` — nothing measured recovery,
which lowers what the answer may claim and decides nothing about whether the four
weeks get written. Both bind to `09_no_plan__provider_healthy` and
`10_no_plan__recovery_read_fails`.

This layer was uncoverable until #314. `tests/test_evals.py` resolved
`evidence_fields` against the CoachContext and PlanState schemas only, and
`pre_plan_observations` was in neither — it is declared inline as
`{"type": "object"}` at `mcp_transport.py:1092`, which is frozen surface.
`contracts/pre-plan-observations.schema.json` (#318) closed it by referencing the
CoachContext's own activity row across files rather than copying it, so a rename
there still fails a first-plan case that names it.

One ambiguity survives on this path and could not be fixed inside the freeze:
`recent_training.coverage_activities` borrows the acquisition-rate shape
`coverage` uses for sleep and HRV, but is fed training density — so `partial`
means "trained on some of the last seven days", not "the read was incomplete"
(issue #319).

The safety boundary is separately covered: `_check_first_plan_symptom_boundary`
(`validation.py:3463`) applies the symptom rule to the authoring path.

## Revisit today

**The question.** What do I do today, given something that changed today.

**Decision-critical.**

| evidence | source | window |
| --- | --- | --- |
| `plan.week.sessions[]` — `hard`, `priority`, `scheduled_date` | PlanState | this week |
| `current_calendar[]` — `date`, `status`, `cost` | PlanState | this week |
| `constraints.session_minutes`, `available_days`, `unavailable_days`, `week_constraints` | athlete statement | this turn / this week |
| `constraints.red_flags` | athlete statement | this turn — the one deterministic gate |
| `athlete_baseline` + `baseline_evidence` | PlanState, checked against reads | claim vs 42-day observation; the weekly rows reach back as far as the athlete's own record does |

**Supporting.** `recovery_trends` (7-day), `recovery_signals` (per-day, this
turn), `reported_recovery` (per-day, one cycle, what the athlete stated or uploaded
for the days `recovery_signals` does not answer), `strength_execution` /
`movement_history` (42-day) when today's session is strength, `cycle_sessions` for
what has already happened this cycle.

**Must not participate.**

- Available time as a load mandate. Four committed cases exist to hold this line.
- A recovery reading as permission. `recovery_signals`'s own description refuses
  to be a readiness score; the answer must not reconstitute one.
- The cycle. A one-day constraint is not evidence about the 28-day direction.
- Repetition of the request. Asking twice is not new evidence.

**Missing evidence.** Lowers confidence, never blocks — AGENTS.md 5, and
`validation.py`'s own comment: evidence quality does not choose the coaching
response. The exception is an explicit positive `red_flags` field, which is the
only input in the whole context that is not an inference, and which limits today
to rest or a human decision (`_check_explicit_symptom_boundary`).

**May change.** Today. Not the week, not the cycle.

**Coverage: 11 cases, harmful and control both present.** The best-covered layer.
Harmful: `preference-asked-twice`, `a-missed-session-is-not-a-debt`,
`free-time-the-day-before-the-key-session`, `symptom-and-wants-to-proceed`,
`an-exact-load-nothing-supports`, `one-sentence-is-not-a-pattern`. Control:
`free-time-with-nothing-to-protect`, `free-time-after-a-thin-week`,
`a-short-day-is-not-a-new-plan`.

## Weekly change

**The question.** What does the coming week look like, given what the last one
did and what the athlete has said about this one.

**Decision-critical.** `cycle_sessions[]` — each session's own
`match_status`, `activity_evidence` and `prescription`, read per session and
never as a ratio. `constraints` for what the athlete stated about this week.
`review_frame.week_start`/`week_end` — Monday to Sunday, because every other
window in the context is a rolling span ending at `as_of` and a review framed on
one of those answers a different question. `goal_context.primary_goal` for what
the week is for. `training_preferences` as a stated starting point.

**Supporting.** `recent_actuals` from `detail_horizon_start`, `segment_execution`
(28-day window, full detail from 14 days, and only for days the plan prescribed
more than one step), `run_drift`, `recovery_trends`, `unknowns`.

`run_drift` is supporting here and critical one layer down, and the A/B measured the
difference rather than assuming it. Asked to shape a whole week it changed nothing —
that week was a taper the plan already fixed. Asked to specify one session, the arm
carrying it opened the next long run's first 4 km at 7:00/km against a prescribed
6:40–7:10, because the whole-activity average of 6:40 sat inside the band while the
first third was 6:25. A field that moves one session's prescription and not the
week's shape is supporting, by this file's own distinction.

**Must not participate.** The goal and the seven cycle keys — `start`, `end`,
`primary_adaptation`, `maintenance_adaptation`, `planned_evidence`,
`adjust_conditions`, `stop_conditions`. This is enforced, not advised
(`validation.py:4005`). `cycle.outlook` is exempt on purpose: a week that rolls
forward necessarily shortens the outlook.

`athlete_baseline` is deliberately *not* frozen here (issue #32): judging whether
the baseline still describes the athlete is part of prescribing.

**Missing evidence.** A `none_found` says this build read that day and nothing of
that sport attached. It is evidence about the athlete; it is not a cause.
AGENTS.md 11: an observation is never mapped to an assumed cause and a fixed
adjustment.

**May change.** This week, and the outlook the roll leaves behind.

**Coverage: 6 cases, harmful and control both present.** Harmful:
`a-constraint-lasts-one-week` (a one-week constraint must not become standing),
`absence-has-a-stated-cause`. Control: `density-is-the-finding-this-time`,
`a-habit-can-be-argued-with`.

## Weekly review

**The question.** Did last week do what it was for, and did the athlete absorb
it. These are two answers, not one.

**Decision-critical.** `review_frame` for the week boundary. `cycle_sessions[]`
for planned versus actual, per session. `goal_context.measurement_protocol` for
whether outcome is even answerable. `freshness` and `evidence_expectations` for
whether a silent stream stopped or never existed — the distinction that
`evidence_expectations` was added for, and the one that separates "a gap in the
evidence" from "a gap in the training".

`run_drift` for whether a session was executed as the thing it was prescribed as.
This is the layer it is critical at, and the A/B in `evals/ab/suites/within-session-drift.json`
is why: on one long run the arm without it reads "平均心率 145、心率仍在輕鬆上限內" and
concludes the execution was fine, while all three samples of the arm with it read the
last third at 156 and conclude the second half was not an easy run. Same session, same
question, opposite answers — and the average is the one that is wrong, because it
averages a compliant first half with a non-compliant second. Nothing else in the
context can separate them.

**Supporting.** `set_structure` — two strength sessions of equal duration and load
can run opposite ways through their rests and set lengths, which is a real difference
and not one this layer's question turns on. `recovery_trends`, `recovery_signals`,
`reported_recovery` — supporting rather than critical, and worth saying why it was
added anyway: it is the only field that can show a stated reading from earlier in the
cycle, because a device reading is re-read every turn while a number the athlete typed
used to be gone with the conversation (issue #358). A cycle review can reach its
conclusion without it; what it could not do before was see that the athlete had said
anything at all three weeks ago. `strength_execution`, `movement_history`,
`reported_activities`, `training_history` (unwindowed monthly buckets),
`training_preferences`, `athlete_baseline`.

**Must not participate.**

- Completion as progress. This is the single most likely way an athlete is told a
  cycle worked when it did not.
- A drift read as its cause. `run_drift` reports two ends and never why they differ;
  heat, fatigue, terrain and a deliberate negative split all raise heart rate. The
  A/B suite holds a `cause_restraint` dimension for exactly this, and the arm may
  name a possibility the athlete can confirm — never assert one as read.
- `set_structure.work_sets` as a completion count. It totals every movement in the
  session, so it cannot be matched against a prescribed "squat 5x5"; the athlete's
  own `strength_execution` is the record for what was lifted.
- One wearable value as a verdict.
- A rolling seven days in place of Monday-to-Sunday.
- Tolerance as a mandate to escalate.
- An imported row counted as a provider actual — `reported_activities` sits
  beside `recent_actuals`, never inside it, and carries no activity id or match
  confidence precisely so it cannot be.

**Missing evidence.** A null group is not a finding about the athlete. A stream
that stopped after months of reports is a gap in evidence; a stream never claimed
is not a gap at all, and lowering confidence in the half that *is* evidenced
because of it is the failure `a-source-never-claimed` exists to catch.

**May change.** This week. Reaching the cycle requires its own decision.

**Coverage: 10 cases, harmful and control both present.** Harmful:
`single-poor-wellness-value`, `a-stream-that-stopped-is-not-a-stream-that-never-was`,
`shortfall-is-not-always-the-load`. Control: `adjustment-names-its-evidence`,
`stated-frequency-versus-what-was-trained`, `no-change-still-concludes`,
`what-they-said-is-the-only-evidence-carrying-it`,
`a-covered-week-with-nothing-in-it-is-a-rest-week` — the false-positive control for
the weekly rows' own coverage rule, where the honest answer is the zero and hedging
it into an unknown is the failure.

## Cycle review and next cycle

**The question.** Did this 28-day block achieve what it declared, and what should
the next one be.

**Decision-critical.** `goal_context.measurement` and `measurement_evidence` —
whether the cycle's two readings are in. Without the measurement, progress is
unproven and nothing substitutes: no wearable proxy, no completion rate.
`review_frame.cycle_day`, uncapped, which past the cycle length is what says the
measurement has come due. `cycle_sessions[]` across the block.
`long_term_goals` — the cycle goal is one 28-day step toward these and is never a
second copy of them.

**Supporting.** `training_history` for the year-scale question `recent_actuals`
structurally cannot answer, and `training_breaks` for the one it also cannot: a stop
that begins inside one month and ends inside another leaves both months reading as
light and the stop itself in neither. `evidence_expectations`, `freshness`,
`athlete_baseline`, `body_measurements`.

**Must not participate.** A wearable proxy standing in for the declared
protocol. A high completion rate as the headline. An absent stream as a reason to
lower confidence in an evidenced conclusion.

**Missing evidence.** "The measurement was never scheduled" and "the measurement
was run and was inconclusive" are different answers and must not collapse into
"progress is unproven", full stop — that is the answer that made two consecutive
cycles unanswerable.

**May change.** Everything, including the goal and the 28-day direction.

**Coverage: 8 cases across two modes, harmful and control on both.**
`review_cycle` (4 cases): harmful `outcome-unproven`, `a-source-never-claimed`,
`no-history-was-observed`; control `no-measurement-was-scheduled`. `plan_cycle`
(4 here, plus the 2 first-plan cases above): harmful
`a-gap-in-one-feed-is-not-a-stop`, where the provider's account is younger than the
athlete's training and reading its window as their history authors a return-to-running
block for somebody who never stopped; control
`athlete-changes-the-goal` and `the-milestone-is-not-the-long-term-goal`, both
"the change is legitimate, now author it well"; harmful
`a-method-that-was-never-run-is-not-a-method-that-failed`, where the cycle's own
quality session never produced any evidence to judge the old method by, and
`a-method-with-a-reading-that-moved`, where it did and the reading came back
better. The athlete says the same sentence in both; only one of them has an
answer about threshold in it.

Those two sides are the layer's real boundary and are deliberately a pair: **the
goal is the athlete's to change; the method has written conditions for changing
it.** A coach that honours the first and cannot hold the second replans on
restlessness; one that holds the second and refuses the first overrides a choice
that was never its own.

## Apply and delivery

**The question.** What did the product actually do, and what may it claim.

**Decision-critical.** `delivery_state` / `intervals_accepted`, the returned
`preview` including `settings_changes`, `attempt_open`,
`delivery.unresolved_delivery`, `superseded_external_id`, `reconciliation.status`.

**Must not participate.** Every coaching judgment. `validation.py:3999` states
it: `record_delivery` "is written only by the verified delivery boundary, not
through a model-authored decision bundle". There is no plan change to make here.
The only discipline is claim discipline — AGENTS.md 8, an earlier state never
proves a later hop. Provider acceptance is not device rendering; Intervals
accepting a payload is not Garmin Connect holding it and is not the watch showing
it.

**May change.** Nothing about the plan.

**Coverage: no cases under `record_delivery`.** One delivery-truth case exists —
`plan-week-strength-delivery-is-not-a-guided-workout` — filed under `plan_week`,
and it carries both halves (harmful: promising set-by-set guidance because
delivery succeeded; control: omitting the strength sessions entirely out of
caution). Given that no coaching decision is available at this layer, filing it
by the turn it happens in is defensible. The mode is better read as unavailable
to eval cases than as uncovered.

## Coverage audit

| layer | mode | cases | harmful | control |
| --- | --- | --- | --- | --- |
| First plan | `plan_cycle` | 2 | yes | yes |
| Revisit today | `revisit_today` | 11 | yes | yes |
| Weekly change | `plan_week` | 6 | yes | yes |
| Weekly review | `review_week` | 9 | yes | yes |
| Cycle review | `review_cycle` | 4 | yes | yes |
| Next cycle | `plan_cycle` | 4 | yes | yes |
| Apply and delivery | `record_delivery` | 0 | *(via `plan_week`)* | *(via `plan_week`)* |

"Harmful" means the evidence or the athlete pushes toward a change that would
hurt, and a passing answer withholds it. "Control" means the evidence genuinely
supports acting, and a passing answer acts — without which a coach that refuses
everything scores perfectly.

## A field the coach writes, and which model is reading it

`cycle.adjust_conditions` and `cycle.stop_conditions` are authored by the coach
into PlanState and carried in every read. Four blind runs on two scenarios cited
neither, and this file previously recorded that as a field nothing reads.

**That reading was an artifact of the fixtures.** All 28 committed scenarios carry
the same two generic conditions, so those four runs never had a real one in front
of them. The owner's live plan writes three that are specific to its cycle, one of
which is a ruling no evidence can supply — which adaptation gives way when both are
under pressure.

A 32-answer blind run put that shape to the coach. One 50-minute slot, two `anchor`
sessions competing for it, and every signal except the ruling pointing at the
quality run: it is the primary adaptation, it is `hard`, its series is improving,
and missing it would be the cycle's first miss. Four arms, packets byte-identical
apart from one sentence:

| arm | the sentence |
| --- | --- |
| R | the ruling, and its trigger happened |
| C | no ruling |
| N | a ruling of the same shape whose named trigger did not happen |
| R+ / C+ | R and C, plus one served line telling the coach to read the conditions |

Chose the maintenance adaptation — that is, followed the ruling's direction:

| model | R | C | N |
| --- | --- | --- | --- |
| Claude | 3 / 4 | 1 / 4 | 1 / 4 |
| Codex | 0 / 2 | 0 / 2 | 0 / 2 |
| Gemini | 0 / 2 | 0 / 2 | 0 / 2 |

Cited the conditions at all:

| model | no served line | with it |
| --- | --- | --- |
| Claude | 11 / 12 | not run |
| Codex | 0 / 6 | 4 / 4 |
| Gemini | 0 / 6 | 0 / 4 |

Three things follow, and they replace what this section used to say.

**The field is load-bearing, on one model family.** C and N agree, which is the
internal check: N's ruling did not fire, so N should behave like C, and it does.
So Claude is reading whether the condition was met, not reacting to a sentence
about strength. It is not deterministic — 3 of 4, not 4 of 4.

**Reading it is not a property of the field.** The same tool result produced three
behaviours. Claude read the conditions in 11 of 12 answers unprompted; Codex in
none of six; Gemini in none of ten, prompted or not. This is not the `instructions`
delivery gap — PlanState arrives in a tool result, which every client gets. It is
salience inside the payload: the field sits deep in `plan_state.current_plan.cycle`
and no served text has ever mentioned it.

**One served line moves it, measurably.** Adding a single instruction to read the
conditions and say which one was acted on took Codex from 0 of 6 to 4 of 4, and one
of its two answers on the fired arm did what the ruling literally says — cut the
quality run's repetitions and keep the strength work in the same 50 minutes, rather
than dropping either. No answer in any other arm proposed that. Gemini did not move.
That line is an `orchestration.md` edit, so it moves `instructions_sha256` and is on
the frozen side of #182 until the verdict.

This closes #217 as written: the method is recorded, in a field that already
carries real per-cycle content and that changes the answer. What it opens is
narrower and newly measurable — whether a field this load-bearing should depend on
the client's model to be noticed.

Also surfaced by the run, and unrelated to the field: `run-quality-01` in
`20_revisit_today__one_statement` carries `planned_minutes: 50` while its own steps
sum to 60. Claude and Codex computed the total and cut repetitions; every Gemini
answer took the field at face value and asserted five repetitions fit in 50 minutes.

## Carried but never decision evidence

Aggregating `evidence_fields` across the committed cases, these CoachContext
fields are named by no case at any layer:

`schema_version`, `context_id`, `as_of`, `timezone`, `privacy`,
`athlete_profile`, `sources`, `coverage`.

All eight are correct. They are envelope, provenance and locale; they are
supporting at most, and `freshness` — which *is* cited, at both review layers —
is the provenance field a review actually reads.

`subjective_states` was on that list until this file was written, and it was the
one substantive evidence field on it: fourteen days of what the athlete said
about how they feel, verbatim and unparsed. Its own schema description names the
reading with the most value in it — *"This is the third week of it"* — and
assigns that reading to the coach. Its write path was complete: a tool, an
orchestration line, storage, retraction, export. Its read path was a field in
every context that no served instruction mentions, no validator touches by
design, and no case scored. It was the one place where "the context carries it"
and "some layer decides on it" had come fully apart.

The two cases added with this file close it at both layers it belongs to, and
they disagree with each other on purpose:

- `revisit-today-one-sentence-is-not-a-pattern` — **supporting only.** One dated
  statement shapes today and reaches nothing further. It must also not be
  ignored: an answer that reads the trends and skips the sentence fails, and so
  does one that turns the sentence into a figure.
- `review-week-what-they-said-is-the-only-evidence-carrying-it` — **decision-critical.**
  Five statements across the fortnight, against full completion at baseline load
  and every trend inside its normal range. Nothing else in the context carries
  what the athlete is describing, and no deterministic reader will raise it, so
  an answer that concludes from the numbers alone sounds complete and is wrong.

Same field, opposite weight, two layers apart. That is the distinction this file
exists to record, and it is now enforced by cases rather than asserted here.

## A constraint on closing any of these

`evals/ab` freezes arms as *this checkout's answer with one field swapped*, so
seeding a scenario that a frozen arm covers invalidates a measurement that was
never about the field being seeded. Eleven scenarios are pinned that way today:
`01_revisit_today__no_reconcile`, `02_review_week__no_reconcile`,
`03_review_cycle__no_reconcile`, `04_structured_run__reconcile`,
`08_strength_alias__reconcile`, `13`, `14`, `15`, `17`, `18` and `19`. Adding a
scenario is free; adding evidence to a pinned one costs a re-capture of the
historical commits the arms were taken from. The subjective-states work below
took the first route, which is why `20_revisit_today__one_statement` and
`21_review_week__statements_across_the_fortnight` exist rather than a change to
`01` and `02`.

## Gaps, in priority order

**1. `subjective_states` has no decision coverage at any layer. Current — closed.**
Freeze-safe: eval cases only, no runtime, no served surface. Closed by
`revisit-today-one-sentence-is-not-a-pattern` and
`review-week-what-they-said-is-the-only-evidence-carrying-it`, added with this
file.

**2. First-plan evidence was not contract-anchorable. Closed by #314 / #318.**
`contracts/pre-plan-observations.schema.json` now covers the no-plan read and the
first-plan layer carries a harmful and a control case. What it surfaced and could
not fix inside the freeze is issue #319: `coverage_activities` reports training
density through a shape that means acquisition rate everywhere else.

**3. `plan_cycle` had no harmful case. Closed — and it corrects what this file
said about #217.**
The earlier reading here was that such a case could only be passed by
reconstructing the cycle's intent from prose, so the field had to come first.
That was wrong, and reading `contracts/plan-state.schema.json` rather than #217's
description is what corrects it. Three structured, addressable fields already say
what the cycle is doing and when it may stop doing it:
`cycle.adjust_conditions`, `cycle.stop_conditions`, and
`cycle.outlook[].relation_to_primary` — build, hold, measure, per week. Session
`priority` (`anchor` / `flexible` / `optional`) already says what survives a week
that has to shrink, and two `revisit_today` cases already read it.

So a `plan_cycle` harmful case grades against committed fields, not against
prose, and one is committed:
`a-method-that-was-never-run-is-not-a-method-that-failed`.

Both have been run — four independent blind answers in total, against the
product's own served texts and the committed reads, none of them shown the case.
**All four held the method. None needed a new field, and none read
`adjust_conditions`.** That last clause read as a finding about the field until a
later run showed it was a finding about the fixtures — all 28 carry the same two
generic conditions. See "A field the coach writes, and which model is reading it".

On the cycle that never ran, they held it because nothing had tested the method.
On the cycle that did, they held it because the cycle's own declared measurement
came back at a lower average heart rate for the same prescription — stronger
evidence than the absence of a triggering condition, and the reason a competent
answer reaches for it first.

So #217's stated harm does not reproduce: the coach does not need a method
statement to protect the method, because the evidence carries it. What the runs
did surface is the finding below.

A separate fact settles the order anyway. A `cycle` field the coach must author
is not implementable inside the freeze: the model can only write one through
`change_request.cycle`, whose properties are inside `tool_catalogue_sha256`
(`mcp_transport.py:2802` hashes each tool's input schema). Adding one property
there moves the digest — measured, not assumed. The storage half is freeze-safe
via `_keys`'s `optional` mechanism; the authoring half is post-verdict.

**4. The dead `revisit_today` validation block. Deferred.**
Delete it or revive it deliberately. Every invariant it states is held elsewhere,
so nothing is currently wrong. Issue #315, whose reopen trigger is #267 deciding
whether those modes come back.

## Where the open issues land on this map

**#267 — decision scope.** Not an evidence gap. `_derive_mode` infers intent
from the diff, so a legitimate cycle reassessment that must also move this week's
executable sessions is forced to `review_week` and refused by the very rule that
protects the goal. The missing concept is a *declared* scope. Note the shape of
it on this map: the mode enum already has the vocabulary — `plan_cycle`,
`plan_week`, `revisit_today` — and the derivation collapses it to two. **Decision
semantics. Post-verdict**, because an explicit scope is a tool input change and
`instructions_sha256` / `tool_catalogue_sha256` are frozen under #182.

**#217 — what this cycle protects.** Neither an evidence gap nor a field gap.
`cycle.adjust_conditions` already holds per-cycle method statements, including
rulings no evidence can supply, and a 32-answer blind run shows the ruling changes
the answer rather than decorating it. **A served guidance gap**: the field's
salience inside the payload, not its existence, is what varies — Claude reads it
unprompted, Codex only when told to, Gemini not at all. The fix measured so far is
one line in `orchestration.md`, which moves `instructions_sha256` and is therefore
**post-verdict** under #182.

**#26 — hybrid load and opportunistic training.** Largely done, and the map shows
which half. Part B (opportunistic extra training) is **evidence-complete and
eval-covered**: four committed `revisit_today` cases cover Add, Low-cost, Rest
and the availability-as-mandate failure. Part A (modality-specific load
legibility) is **evidence-complete** — `segment_execution`, `movement_history`,
`strength_execution`, and `actual.adaptation` / `body_stress` / `cost` carry it —
but nothing served tells the coach to read upper-body against lower-body
interference. What remains is **served guidance. Post-verdict.**

**#239 — `applyCoachDecision` resending the context.** Apply plumbing: what the
proposal binds and where the check happens. It appears on this map only as the
layer that makes no coaching judgment. Not an evidence question and not tracked
here.
