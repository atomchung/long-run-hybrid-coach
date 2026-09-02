# Data Sources — what each one can and cannot answer

Two sources back this product, and they are not substitutes. Choosing between
them is a per-field question, not a per-provider one. This file records which
fields each source can carry at all, so a future decision does not have to
rediscover it by inspecting live accounts.

Verified against the live account on 2026-08-12.

## The distinction that matters: structural gap vs. accumulation

A field missing from intervals.icu today is one of two very different things.

**Accumulation.** Activities, HRV, resting heart rate, and sleep arrive through
the normal Garmin sync. A short history means the account is young, not that the
field is unavailable. Waiting fixes it.

**Structural.** Garmin's Firstbeat-derived metrics are not released through the
official sync path intervals.icu uses. Waiting never fixes these — they will
read empty indefinitely.

Reaching for a second source to solve an accumulation problem is wasted work.
Reaching for one to solve a structural gap is the only option there is.

## intervals.icu — the product path

Read through the direct read-only REST adapter (`--source intervals`).

Carries, and is the only source that carries:

- **Subjective feel** on an activity. `health.db` has no column for it.
- **Trustworthy elevation gain.** The same 2026-08-10 activity reads 145 m here
  and 8 m in `health.db` — an order of magnitude apart, so the local value is not
  read at all.
- **`training_load` / TRIMP** per activity, including strength activities.
- **Per-segment execution** for a run — each segment's distance, moving time,
  pace, and average/max/min heart rate, read per activity and carried as
  `segment_execution`. `health.db` stores one row per activity and no breakdown
  at all, so this source is the only one that has it. The provider's grouping
  does not correspond to the prescribed steps, and nothing derives a completion
  verdict from it. It is read for the 14-day window *and* only for days the plan
  prescribed more than one step on: an easy run planned as one continuous effort
  is reported completely by its average pace and average heart rate in
  `recent_actuals`, and reading its auto-laps costs one provider request per
  activity for nothing further (issue #233).

The complete wellness record for one day (2026-08-11) is: sleep duration,
quality and score; `hrv_rmssd`; `resting_hr`; `weight`; `steps`; `ctl` and
`atl`. Note `temp_weight: true` on that record — the weight is an estimate
carried forward, not a measurement.

One more read can ride alongside activities and wellness on this path: the Run
sport settings' own `max_hr`, from the same credential, same GET-only adapter.
It exists for exactly one purpose — a build compares it against
`athlete_baseline.max_hr` (PlanState, the coach's own written figure) and, when
both are present and disagree, states the disagreement as an `unknowns` entry
naming both values and both sources. Neither figure is preferred, averaged, or
written back; a single source present is an ordinary known fact, not a
disagreement, and produces no note. That single purpose is also the condition on
the read: a plan carrying no measured `max_hr` of its own leaves nothing for this
value to disagree with, so the request is not made and the field stays `null` —
the same value it already holds whenever the endpoint has no Run entry or cannot
be reached. `--source personal-os` never reaches this endpoint at all, so that
path's `athlete_baseline.max_hr` is always read as the single source it is.

CTL and ATL are computed here, but a 42-day weighted average is only meaningful
once roughly six weeks of activity exist. Reading them from a young account
produces a confident number describing nothing.

### What a WeightTraining activity actually carries

The bullets above were verified against activities generally, never specifically
against a strength session. Probed live against the real account on 2026-08-15,
across all 5 of its WeightTraining activities:

Reliably present: `name` (the athlete's own session label, e.g. 胸日/背日/腿日),
`type` (always `"WeightTraining"`), `start_date_local`, `moving_time` /
`elapsed_time`, `average_heartrate`, `max_heartrate`, `icu_training_load`, `trimp`,
`icu_intensity`, `calories`, `icu_warmup_time`, `icu_cooldown_time`, `device_name`,
and `source` (always `"GARMIN_CONNECT"`; the device behind it is a Garmin
Forerunner 570).

Structurally absent, on every one of the five:

| Field | What that means |
| --- | --- |
| `kg_lifted` | Exists in the Intervals schema; null on all five activities. |
| `icu_lap_count` | 0 on all five. The intervals endpoint returns a single synthetic RECOVERY lap — there are no per-set laps to read. |
| `stream_types` | `["time","heartrate"]` only. |

So no exercise name, no set count, no reps and no load ever arrive from Garmin
through Intervals for a strength session — only that it happened, when, for how
long, and at what heart rate and load. Per-set truth can only come from the local
strength log or from what the athlete says (see the athlete-reported evidence
section below); the session's own `name` is what the product now carries through
as `session_label` instead of asking the athlete for a category. It travels on
the `recent_actuals` row -- a reduced row keeps it too (issue #240 §1) -- and on
an attached `cycle_sessions[].activity`.

A consequence worth stating plainly: when nothing at all reaches Intervals for a
session, the athlete's own account is the only record it has. That account is
believed. A day they say they trained is taken as fact, and a
scheduled session on that day reads as `activity_evidence: athlete_reported`
rather than `none_found` — a watch that was off, flat, or failed to sync is the
ordinary reason a session is missing, and reading it as "never trained" feeds the
coach a false signal it acts on by easing the load of somebody who is training.
Believing the athlete is not the same as inventing a provider record for them: no
`activity_id` appears, nothing enters `recent_actuals`, and automatic
reconciliation still sees only what the provider holds.

## personal_os `health.db` — the local second source

A locally owned database (`personal_os/health/data/health.db`), populated by
`personal_os/health/sync_garmin.py` through `garminconnect`, an unofficial
library that calls Garmin Connect's internal endpoints
(`get_training_readiness`, `get_hrv_data`, …). That access path is why the
fields below exist here and nowhere else.

Structurally unavailable from intervals.icu:

| Field | Table |
| --- | --- |
| `readiness_score`, `readiness_level`, `readiness_factors_json` | `recovery_daily` |
| `hrv_status`, `hrv_7d_avg_ms`, `hrv_baseline_json` (intervals carries raw rmssd only) | `recovery_daily` |
| `acute_load`, `recovery_time_sec`, `training_status` | `recovery_daily` |
| Body Battery high/low, `avg_stress` | `daily_metrics` (garmin) |
| Sleep stages (deep/rem/light/awake), respiration, SpO2 | `daily_metrics`, `recovery_daily` |
| Measured weight, `body_fat_pct`, `lean_mass_kg` (Apple body composition) | `daily_metrics` (apple) |
| Per-set `weight_kg`, `reps`, `notes` for strength work | `strength_log` |
| HR zone 1–5 seconds per activity | `workouts` |

`strength_log` is not a Garmin field at all — it is written by the
`/log-strength` skill. Garmin records sets and reps for strength work but not
load: `strength_auto.max_weight_kg` is zero throughout. No provider will ever
supply it. Its per-set execution is reachable from the product path as the
optional `strength_execution` CoachContext group (`--health-db`, or the same
env vars as `--source personal-os`), added 2026-08-12.

The `readiness_score`/`readiness_level`, `hrv_status`, `acute_load`/
`recovery_time_sec`, and Body Battery/`avg_stress` rows in the table above are
reachable the same way, as the optional `recovery_signals` CoachContext group
(same `--health-db` flag and env vars), added 2026-08-12.

Two caveats belong with the table above. `acute_load` and `readiness` only begin
on 2026-08-07, far shorter than the Body Battery and stress series that run from
2025-10-21. And `health.db` remains a private patch on the activity side, for the
feel and elevation reasons above — that limitation is about activity records, not
about the recovery, load, body-composition, and strength-execution fields listed
here.

## Athlete-reported evidence — the third source

A local JSON file, `athlete-evidence.json`, beside the owner's `store.json`.
Written by the athlete's own statements rather than by any sync: the hosted
routes `recordAthleteProfile`, `recordAthleteAvailability`,
`recordLongTermGoal`, `recordTrainingPreference`, `recordStrengthExecution`,
`confirmPrescribedStrength`, `recordBodyMeasurement`, `recordActivitySummary`,
`recordSubjectiveState` and `importAthleteHistory`, the CLI `record-profile` and
`record-availability`,
and the days named in an initialization request. Every record carries the instant
it was recorded and one of three provenances.

Everything in it is something neither provider above can ever answer:

| Field | Why no provider has it |
| --- | --- |
| The athlete's own IANA timezone | Intervals holds dates, not the zone the athlete reads them in. It was a deployment constant, so a second athlete lived in the deployment's day. |
| The language their prescriptions are written in | Nothing measures it, and the sentence reaches their watch, so a wrong guess is a plan they cannot read. |
| Which weekdays the athlete can train, as a recurring default plus per-week overrides | Both providers are records of what happened. Neither knows what next Tuesday looks like. |
| Athlete-reported per-set `weight_kg`, `assist_kg`, `reps`, `rpe`, `notes` | Same structural gap as `strength_log` — no provider supplies load — but reachable without a local database. |
| Athlete-stated `weight_kg` and `body_fat_pct`, one record per day | The Apple body-composition rows above are one machine's `health.db`, so a hosted athlete has no path to them at all. A number read off a scale needs none. |
| A session the athlete trained that no device recorded: sport, duration, optional distance, 1-5 feel, note | Intervals holds what a watch uploaded. A pool without one, a hotel treadmill or a hike is training that no provider will ever have. It stays beside `recent_actuals` and never enters it — see below. |
| Training that predates the Intervals connection, out of a file the athlete uploads | Intervals holds one account's history from the day it was connected. Everything before that lives in a Garmin, Strava or Apple export the athlete still has, and no provider read will ever reach it (issue #101). |
| What the athlete is training for beyond this cycle: `metric`, `target`, optional `target_date` | An aim is not an observation, so no provider records one. It also outlives the 28-day cycle, which is why it is not in PlanState: the cycle's own `goal` is a milestone toward it, and would take the target with it when the cycle closed. |
| How the athlete says they felt on a day: the sentence and its date, last two weeks | A wearable reports a readiness figure; nothing measures "我覺得很累". It used to live only inside the conversation it was said in, so three consecutive weeks of it read exactly like a first (issue #188). Stored as the words rather than a score — a subjective feeling translated into a number is what `recovery_signals` refuses, and this is the sentence that ban was protecting. Symptoms are not here: those are `red_flags`, which limit the day deterministically. |
| How the athlete says they like to train: `topic`, `statement` | Both providers show what was trained, never that it was meant. A Sunday long run read out of history is an inference the coach may weigh; that the athlete *asked* for Sunday is a fact only they can supply — and only their own statement writes it here. |
| What this week is, beyond which days: the `note` on an availability week statement | Travel, a hotel gym, a work week that will run late. Scoped to one week and gone with it, which is what keeps a temporary constraint from silently becoming a standing habit. |

### An upload is a fourth reader, not a fourth store

`importAthleteHistory` takes a CSV export (Strava, Intervals.icu and Garmin
Connect are recognised by their own headers; anything else through a caller-supplied
`column_mapping`), an Apple Health export or fragment of one, a base64 `.fit` file,
or rows the caller read out of something no parser here can open. All four land in
the two groups above — `reported_activities` and `body_measurements` — under the same
identity rules, in the same file, read by the same context build, carried by the same
export and removed by the same deletion. There is no import store and no per-format
path below `garmin_coach_loop/evidence_import.py`, which is a reader and nothing else.

The file is parsed and dropped. What survives is a per-session summary plus a
provenance label; no GPS track, no stream, no file (AGENTS.md 2). Units are never
guessed — a recognised export declares its own, an unrecognised one declares them in
the mapping, and Garmin Connect's unit-less `Distance` column is dropped with a named
reason rather than read as kilometres. Sports are mapped from a table of spellings,
never inferred: a name the table does not hold comes back unmapped with what the file
actually said.

Dedup is deterministic wherever code can answer it. The upload's own digest
recognises the same file sent twice; a per-session key recognises the same session in
a different export; and two records are one session when they share a day and a sport
and agree on duration within three minutes (and, when both state one, distance within
a kilometre). That one predicate is asked from both directions — an uploaded row asks
it of what is stored, and a spoken summary asks it of a day an upload already covers —
so a session cannot be held twice by arriving two ways. Only a genuine ambiguity, a
same-day same-sport record that does *not* agree, is handed back as a question; nothing
is written for that row until the athlete answers, and answering is re-sending the same
payload with a `resolutions` entry.

An upload can leave two sessions of one sport on one day, which a conversation never
could. That is the one place retraction changes: `sport` plus `date` may name more than
one record, and then nothing is removed until a `started_at` from the returned
`candidates` says which.

### Three provenances, because they are three different claims

`source: "athlete_reported"` is the athlete describing what they lifted. `source:
"prescribed_confirmed"` is the athlete confirming they did what the plan said,
with only the parts that differed named (issue #76) — the plan already holds every
set, and asking them to read it back is the friction that let strength evidence
lapse for two and a half weeks and produced a phantom 62.5 kg baseline.

`source: "athlete_imported"` is the third: a session read out of a file the athlete
uploaded. Still their own evidence and still not a provider actual — this product
observed none of it — but a coach weighing a progression needs to know whether the
numbers came from a device's export or from somebody's memory of the session. The
row also carries `imported_from`, the athlete's own name for the upload.

The distinction is not bookkeeping. A confirmed prescription tells a coach reading
a progression nothing the plan did not already say, while a described set does. So
each record names which it is, and so does the group above them: a build carrying
both reads `athlete_reported+prescribed_confirmed`.

Neither is a measurement, and neither displaces one. Where `strength_log` holds the
same `(date, exercise)`, the local row stands alone and the statement is dropped —
never merged, never averaged. That rule is unchanged by there being two kinds of
statement; the cheaper one to produce is exactly the one that must not be able to
overwrite a measured record.

Availability was not previously stored at all. It arrived inside one request
(`constraints.available_days`) and died with the conversation that carried it, so
every conversation asked again. Stored, it reaches `constraints` on every later
build, and `constraints.availability_source` names which of the two authors spoke:
`request` for this turn's statement, `athlete_evidence` for a standing one, `null`
when neither. `unavailable_days` is a separate statement rather than the
complement of `available_days` — naming a lost day confirms nothing about the
others, so `available_days_not_confirmed` survives a week that only names what is
gone.

Two precedence rules, and they point in opposite directions on purpose:

- **Availability:** the request wins. It is what the athlete is saying now; the
  stored value is what they said earlier.
- **Strength:** `health.db` wins, absolutely. Reported sets fill
  `strength_execution` only where no local strength log resolved at all — which
  is every hosted build. A measured per-set record is never displaced by a
  recollection, and the two never merge.

### A reported session is beside the provider's, never inside it

`body_measurements` and `reported_activities` reach the coach as their own
CoachContext groups, each row labelled `athlete_reported`. A reported session in
particular has no precedence rule at all, because it never meets a provider
activity: it carries no activity id, no match confidence and no completion, so
nothing can attach it to a planned session, it never enters `recent_actuals`, it
moves no coverage or freshness row, and reconciliation does not read it. A session
counted as both the athlete's word and the provider's record would be one week of
training read as two, and the loop's claim about what came back would stop being
about what Intervals actually holds.

One reader outside `recent_actuals` does look at it: a cycle session's own
`activity_evidence` (issue #30), the same review field a reported lift already
turns from `none_found` into `athlete_reported`. A same-day, same-sport reported
session does that and nothing else — attachment, completion, coverage and
freshness stay exactly as untouched as the paragraph above says.

The one thing said across that boundary is an observation on the reported row
itself: `provider_actual_same_day` is true when `recent_actuals` also holds an
activity of the same sport on the same date — the late-sync case, where the
athlete reported because the watch failed and the watch then synced after all.
Whether the two are one session is the coach's reading; nothing is merged,
suppressed, or scored.

Both are keyed one record per day (per sport, for a session), and restating
corrects rather than appends — the same rule reported lifts follow. A *spoken*
summary therefore holds one record per sport per day, and a write that displaces an
earlier one says so in its response, so two genuinely distinct same-day sessions of
one sport are combined rather than lost quietly. An upload is the exception, and the
reason is that it can be one: it carries start times, so a morning run and an evening
run arrive as two rows that are provably not one row restated.

Correcting is not the only way to unwind a statement. One route,
`retractAthleteRecord`, removes a record outright instead of replacing it: `kind`
picks the family (`strength_execution`, `body_measurement`, `activity_summary`) and,
where the record needs a second name, that name — the exercise, or the sport, with
just the date for a measurement, plus `started_at` when an upload has left two
sessions of that sport on that day. For strength, this holds regardless of whether the
record was `athlete_reported` or `prescribed_confirmed`. A retraction that finds
nothing on record is a no-op, not an error. The two keyed kinds — strength and
activity — also name what is on record for that day, so the athlete can retry
with the right name; a measurement is keyed by date alone, with no name to get
wrong.

Missing and unreadable stay distinct here as everywhere else. No file means
nothing was reported, which is an ordinary state and never blocks a build. A file
that exists and cannot be parsed raises, because reading it as "nothing reported"
would silently drop statements the athlete believes are still on record.

## Consequence for the coach

The two sources are selected on separate axes, and they compose. `--source`
picks the one provider supplying activities and recovery, and no substitution is
ever made for either. A failed *activity* read blocks the build: matching, the
cycle record and baseline evidence all run on it, so a context without it has
nothing to reconcile against.

The *recovery* read, on the intervals path where it is its own request, is graded
by how it ended, because the repairs are different and only one of them is
waiting:

| how the wellness read ended | the build | what the recovery half says |
| --- | --- | --- |
| answered, values in the window | continues | `fresh` or `stale` |
| answered, no value anywhere in it | continues | `failed` — looked, nothing there |
| network error or 5xx after the retry | continues | `unknown`, `intervals_wellness_read_failed` |
| 403, `WELLNESS:READ` not granted | continues | `unknown`, `intervals_wellness_permission_denied` |
| 401, the credential itself refused | blocks | — the connection is forgotten |
| a body the product cannot parse | blocks | — provider-contract drift |

None of the first four is evidence of recovery, and the two that block are the two
that do not clear on their own: a refused credential needs authorizing again, and a
response whose JSON or root shape is undocumented means this code no longer
understands the provider. `--health-db` is unrelated
to it: it opts the same build into the two standalone optional evidence groups,
`strength_execution` and `recovery_signals`, whichever provider `--source`
named. So the product path (`--source intervals`) does reach the structural
fields, and gets intervals' subjective feel, trustworthy elevation, and
`training_load` in the same context.

`recovery_signals` now has a third origin, and it is the one every hosted athlete
already has. The intervals wellness read is made over the full 42-day cycle window and
its daily rows fill the same per-day container — `sleep_score`, `sleep_duration_sec`,
`hrv_last_night_ms` (intervals' raw RMSSD) and `resting_hr_bpm`, the four of that shape
the provider can answer. Before this, that container was null for anyone without a local
database or an upload, and the only recovery evidence in the context was `recovery_trends`
— a label computed from values the coach never saw, over a week that could end before the
nights worth reading (issue #358). The trend and the three coverage entries are unchanged
and still computed over the 7-day window; what widened is the read and what the coach is
handed. A local database or a client upload still wins where one exists.

That composition assumes one machine, and most athletes do not have one. `--health-db`
names a file on the machine running the build, populated through `garminconnect`
with the athlete's own Garmin username and password. A hosted product cannot ask
for those credentials or open that file, and assumes nobody has such a database at
all. Recovery evidence the athlete states arrives as values instead: `{source, days}` in
`startCoachSession.recovery_signals`, however the client came by them — numbers the
athlete read off their watch face, an export they pasted, or a local source the
client read for itself. The route in is not asked about; the values and a short
`source` label are, and the coach weighs that provenance itself. Only `date` is
required per day, so a client sends the readings it has. The gateway derives the
window, fills the unsent readings with `null`, rejects duplicates,
out-of-window/all-null rows and impossible numeric values, prefixes provenance with
`client-uploaded:`, and puts the group in that response's CoachContext. It never
receives a path, credential or raw provider payload, never accepts a figure the
model invented rather than observed, and never writes the upload down: the
CoachContext carrying it is held in the gateway's process memory for 60 minutes,
so the client can name that context by `context_id` on the two decision calls
instead of resending it (issue #355), and it reaches neither the store nor an
export; a confirmed decision may retain the context hash and the model's evidence
summary, not the uploaded days themselves. A later session that does not send it
reads `null`.

`strength_execution` follows a different boundary: the athlete can report the
sets themselves, which is a thinner record than the measured one and a far better
one than nothing.

`movement_history` is not a fourth source. It is `strength_execution` — from
whichever of the two paths supplied it, measured file or athlete report — grouped
by movement instead of by date, with the prescription for the same date beside
each occurrence. It inherits that group's availability exactly: `null` wherever
`strength_execution` is `null`, and no wider window than the one it came from.

`baseline_evidence` is not a fifth source either. It reads groups already in the
context — `recent_actuals` for the running fields, `movement_history` for the
strength ones — and states, per `athlete_baseline` field, what the baseline
claims beside what was observed and how many observations back it. Nothing in it
is a verdict, and a field the window holds nothing for says so rather than being
altered.

Its weekly running rows are the one claim read from *both* kinds of evidence: the
provider's actuals and the athlete's own dated records, merged into one list of
sessions and counted once each. A stored row whose day and sport the provider also
holds is dropped, the same same-day-same-sport reading `reported_activities`'
`provider_actual_same_day` flag already states, so a session the device recorded
late is never counted twice. Every week names the sources that cover it, and that
is what separates the two answers a blank week can have: the provider covers every
week inside the span it was read on, so a week with nothing in it there is a
zero; a stored record covers only the days it holds, so it can add a week and can
never empty one; and a week no source covers is left out rather than reported as
zero kilometres. `km` sums the runs that stated a distance and
`runs_missing_distance` says how many did not, which is `training_history`'s own
partial-sum rule — the two are the same evidence at two grains and must not sum it
two different ways.

Volume is the only claim read this way, and deliberately: the other rows are about
how a session was *executed* — what pace, what heart rate, how long — and a stored
row carries the athlete's word for a session rather than a measurement of it. A
week's kilometres are a count of sessions and their distances, which the athlete's
own record states as well as a device does.

`training_history` is not a sixth source either, and it is not windowed at all —
the other five groups above all read a 42-day-or-shorter span, and this is the
one place in the context that deliberately does not (issue #101). It is also
where the evidence the per-session groups stop at now goes: `recent_actuals` and
`reported_activities` report rows from `review_frame.detail_horizon_start` — the
earliest of the previous Monday, the cycle start, and the plan's own week start —
and everything before that is here, by month (issue #233). The provider read
itself is unchanged at 42 days: matching, the cycle record and
`baseline_evidence` all still run against the full span, and `baseline_evidence`
states it per claim. It rolls up
the athlete's complete `reported_activities` and strength-report history — every
row either has ever held, not the recent slice `strength_execution` and
`reported_activities` read — into one bucket per calendar month per sport:
session count, total minutes and total km among the rows that actually stated
one, and how many of the bucket's rows carry each provenance. Strength counts
distinct training days across its two containers (a coarse activity summary and
a per-exercise report can describe the same gym visit) rather than rows, so one
workout is never counted twice. Only months holding at least one row appear, at
most the most recent 24 — `truncated` and `earliest_observed_month` say plainly
when older evidence exists beyond what is shown, rather than letting a dropped
month read as a month with nothing in it. A separate `movement_longevity` list
carries each movement's earliest and heaviest observation across the same
unwindowed history, reusing `movement_history`'s own top-load reading so the two
never disagree about what counts as heavier within one day.

This exists because a 42-day window cannot answer "how has my volume changed
this year" — reading the account's oldest evidence as the athlete's oldest
training is exactly the mistake issue #101 opened on. `null`, with its own
unknowns note, means the athlete has reported nothing long-range yet; it must
never be read as "never trained", the same rule every other evidence gap in this
file already follows. What this group does not reach: a Garmin-connected
provider's own pre-connection activity history is a different, still-open half
of issue #101, structurally unavailable to a hosted build with no local
database to read it from.

`training_breaks` is not a seventh source, and it is the second thing derived from
that merged list (issue #222). A calendar month cannot show a stop that begins
inside one month and ends inside another: an athlete who stopped on 9 March and
came back on 26 April leaves a March holding eight days of training and an April
holding four, so the buckets read as two light months and the seven weeks off are
in neither. Each row is a sport, the blank's start, end and length in days, the
last observation before it and the first one back, and the nearest monthly volume
either side that the break did not cut through. Dates and observations only:
injury, travel, illness and a change of mind all leave exactly these rows, so
nothing here says why, nothing concludes anything about what the athlete came back
able to do, and nothing is scored. `null` means no blank of at least 28 days runs
inside the record — which `training_history.earliest_observed_month` says the reach
of, so it is never a claim that no stop ever happened.

`evidence_expectations` is not an eighth source either. It reads evidence the
groups above already carry and reports one dated row per *stream* that has ever
produced an observation: when it first arrived, when it last did, how many
observations there were, and how many days of silence have run since (issue
#28). It exists because a stream that supplied evidence for months and then
stopped currently looks exactly like one that was never there — both are a null
group beside an `unknowns` line that read the same on the first day the product
was ever run, so nothing separates a supply that broke from a supply nobody has.

**`freshness` is about the read; this is about the record.** They are separate
questions and nothing joins them: a wellness read that failed this morning is
this turn's news and the freshness table already grades it, while a strength log
that went quiet in June is a fact no single read can see. Nothing in this group
looks at `freshness`, `coverage` or any `unknowns` string, and nothing outside it
moves because of it — no validator branches on it, no `unknowns` entry comes from
it, and no `activity_evidence` value changes.

A stream that has never produced anything has **no row**, and that is the whole
false-positive control: never seen is not expected, and not expected is not
reported, so an athlete who never claimed a recovery device is never told one is
missing. There is no list anywhere of streams an athlete ought to have. Nor is
there any verdict on a row — no status, no expected flag, no severity, no score
— because how long a silence has to run before it means something depends on the
athlete and the season, and a threshold here would be that judgment made in the
wrong layer. Whether the record went quiet because the athlete stopped training
or because they stopped saying so is exactly the question the row does not
answer.

Four streams, and what is left out is left out for reasons rather than for now:

| stream | reads |
| --- | --- |
| `provider_activities` | the activity read's own rows, over the window it named — the one row whose `basis` is `read_window` rather than `stored_record`, because its first observation is bounded by the span this build asked for |
| `athlete_reported_activities` | spoken sessions only |
| `athlete_reported_strength` | described sets and confirmed prescriptions together — two different claims, but one supply |
| `athlete_body_measurements` | stated weigh-ins only, over the athlete's whole history rather than the 42-day slice `body_measurements` carries |

Both athlete-written streams take spoken records only, and the imported half of
each container is not a stream of its own either. An upload is one event, not a
supply: a file holding a year of sessions, or a year of weights out of an Apple
Health export, arrives on one day. Letting its rows set a stream's dates would
report a supply that ran for a year and then stopped, when nothing about what
the athlete does changed at all and one file simply arrived. An upload's own
rows are read by `training_history`, at the grain that question needs.

Provider wellness and recovery are deliberately not among them. Neither leaves a
durable dated trace to build a row from: the wellness read is a live seven-day
window this product does not keep, and a hosted `recovery_signals` upload is
request-scoped by design — consumed for one context, held in memory only for as
long as that context can be named on a decision call, and never written down. Dating
that stream needs a record that does not exist yet, which is a change to what is
stored rather than a fifth entry in the table above. `strength_execution` from a
local `health.db` is out for the same reason, and `segment_execution` is out for
a different one: its rows are the provider's own activities read a second way, so
a stream for it would count `provider_activities` twice.

An unconfigured local `--health-db`, or a hosted session with no client upload,
leaves `recovery_signals` `null` with its own unknowns note, and leaves
`strength_execution` `null` too unless the athlete reported sets in the window;
it never blocks a build. `null` means the reading was not taken, which the coach
must not read either way. A *configured* local path that cannot be read does block,
like any other configured-but-broken source; absence of optional hosted upload does not.

Two gaps survive this and are worth stating, because reachable evidence is not
the same as corrected state:

- `athlete_baseline` is still written by judgment, deliberately: nothing derives
  it from the evidence, and no threshold promotes an observation into the
  written figure. What changed with issue #32 is that the drift is stated
  instead of discovered — `baseline_evidence` puts each written figure beside
  what was observed and its dated counts, and a baseline found wanting is
  updated as an ordinary recorded decision from whichever mode the coach is
  already in. The comparison no longer has to be recomputed to be seen; which
  side of it is right still does.
- A `recovery_signals` group is present, not complete. Over a window reaching
  back past the start dates in the caveat above, the earlier days are
  unobserved — which is a thinner reading, not a reassuring one.
