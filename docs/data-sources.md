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
  `segment_execution` for the 14-day window. `health.db` stores one row per
  activity and no breakdown at all, so this source is the only one that has it.
  The provider's grouping does not correspond to the prescribed steps, and
  nothing derives a completion verdict from it.

The complete wellness record for one day (2026-08-11) is: sleep duration,
quality and score; `hrv_rmssd`; `resting_hr`; `weight`; `steps`; `ctl` and
`atl`. Note `temp_weight: true` on that record — the weight is an estimate
carried forward, not a measurement.

One more read rides alongside activities and wellness on this path: the Run
sport settings' own `max_hr`, from the same credential, same GET-only adapter.
It exists for exactly one purpose — a build compares it against
`athlete_baseline.max_hr` (PlanState, the coach's own written figure) and, when
both are present and disagree, states the disagreement as an `unknowns` entry
naming both values and both sources. Neither figure is preferred, averaged, or
written back; a single source present is an ordinary known fact, not a
disagreement, and produces no note. `--source personal-os` never reaches this
endpoint at all, so that path's `athlete_baseline.max_hr` is always read as the
single source it is.

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
as `session_label` instead of asking the athlete for a category.

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
`recordStrengthExecution`, `confirmPrescribedStrength`, `recordBodyMeasurement`
and `recordActivitySummary`, the CLI `record-profile` and `record-availability`,
and the days named in an initialization request. Every record carries the instant
it was recorded and one of two provenances.

Everything in it is something neither provider above can ever answer:

| Field | Why no provider has it |
| --- | --- |
| The athlete's own IANA timezone | Intervals holds dates, not the zone the athlete reads them in. It was a deployment constant, so a second athlete lived in the deployment's day. |
| The language their prescriptions are written in | Nothing measures it, and the sentence reaches their watch, so a wrong guess is a plan they cannot read. |
| Which weekdays the athlete can train, as a recurring default plus per-week overrides | Both providers are records of what happened. Neither knows what next Tuesday looks like. |
| Athlete-reported per-set `weight_kg`, `assist_kg`, `reps`, `rpe`, `notes` | Same structural gap as `strength_log` — no provider supplies load — but reachable without a local database. |
| Athlete-stated `weight_kg` and `body_fat_pct`, one record per day | The Apple body-composition rows above are one machine's `health.db`, so a hosted athlete has no path to them at all. A number read off a scale needs none. |
| A session the athlete trained that no device recorded: sport, duration, optional distance, 1-5 feel, note | Intervals holds what a watch uploaded. A pool without one, a hotel treadmill or a hike is training that no provider will ever have. It stays beside `recent_actuals` and never enters it — see below. |

### Two provenances, because they are two different claims

`source: "athlete_reported"` is the athlete describing what they lifted. `source:
"prescribed_confirmed"` is the athlete confirming they did what the plan said,
with only the parts that differed named (issue #76) — the plan already holds every
set, and asking them to read it back is the friction that let strength evidence
lapse for two and a half weeks and produced a phantom 62.5 kg baseline.

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

The one thing said across that boundary is an observation on the reported row
itself: `provider_actual_same_day` is true when `recent_actuals` also holds an
activity of the same sport on the same date — the late-sync case, where the
athlete reported because the watch failed and the watch then synced after all.
Whether the two are one session is the coach's reading; nothing is merged,
suppressed, or scored.

Both are keyed one record per day (per sport, for a session), and restating
corrects rather than appends — the same rule reported lifts follow. Version 1
therefore holds one summary per sport per day; a write that displaces an earlier
one says so in its response, so two genuinely distinct same-day sessions of one
sport are combined rather than lost quietly.

Missing and unreadable stay distinct here as everywhere else. No file means
nothing was reported, which is an ordinary state and never blocks a build. A file
that exists and cannot be parsed raises, because reading it as "nothing reported"
would silently drop statements the athlete believes are still on record.

## Consequence for the coach

The two sources are selected on separate axes, and they compose. `--source`
picks the one provider supplying activities and recovery — a failure there
blocks the build, and no substitution is ever made. `--health-db` is unrelated
to it: it opts the same build into the two standalone optional evidence groups,
`strength_execution` and `recovery_signals`, whichever provider `--source`
named. So the product path (`--source intervals`) does reach the structural
fields, and gets intervals' subjective feel, trustworthy elevation, and
`training_load` in the same context.

That composition assumes one machine. `--health-db` names a file on the machine
running the build, populated through `garminconnect` with the athlete's own
Garmin username and password. A hosted athlete has neither: the file is not
theirs to point at, and a hosted product cannot ask anyone for those
credentials. Everything above therefore describes the local path. On a hosted
entry `recovery_signals` is permanently `null` — not slow to arrive, not a
configuration step someone forgot — and issue #27 owns what to do about it.
`strength_execution` is the one that stopped being permanently null: the athlete
can report the sets themselves, which is a thinner record than the measured one
and a far better one than nothing.

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

An unconfigured `--health-db` leaves `recovery_signals` `null` with its own
unknowns note, and leaves `strength_execution` `null` too unless the athlete
reported sets in the window; it never blocks a build. `null` means the reading was
not taken, which the coach must say rather than read either way. A *configured*
path that cannot be read does block, like any other configured-but-broken source.

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
