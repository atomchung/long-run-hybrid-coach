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

The complete wellness record for one day (2026-08-11) is: sleep duration,
quality and score; `hrv_rmssd`; `resting_hr`; `weight`; `steps`; `ctl` and
`atl`. Note `temp_weight: true` on that record — the weight is an estimate
carried forward, not a measurement.

CTL and ATL are computed here, but a 42-day weighted average is only meaningful
once roughly six weeks of activity exist. Reading them from a young account
produces a confident number describing nothing.

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
routes `recordAthleteAvailability` and `recordStrengthExecution`, the CLI
`record-availability`, and the days named in an initialization request. Every
record carries `source: "athlete_reported"` and the instant it was recorded.

It holds exactly two things, and both are things neither provider above can
ever answer:

| Field | Why no provider has it |
| --- | --- |
| Which weekdays the athlete can train, as a recurring default plus per-week overrides | Both providers are records of what happened. Neither knows what next Tuesday looks like. |
| Athlete-reported per-set `weight_kg`, `assist_kg`, `reps`, `rpe`, `notes` | Same structural gap as `strength_log` — no provider supplies load — but reachable without a local database. |

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

An unconfigured `--health-db` leaves `recovery_signals` `null` with its own
unknowns note, and leaves `strength_execution` `null` too unless the athlete
reported sets in the window; it never blocks a build. `null` means the reading was
not taken, which the coach must say rather than read either way. A *configured*
path that cannot be read does block, like any other configured-but-broken source.

Two gaps survive this and are worth stating, because reachable evidence is not
the same as corrected state:

- `athlete_baseline.strength_loads` is still hand-written. Nothing derives it
  from `strength_execution`, so it still drifts from what actually happened —
  on 2026-08-12 it claimed a 62.5 kg bench press never completed for five sets.
  The per-set truth is now in front of the coach to judge against; the written
  baseline figure is not corrected by its presence.
- A `recovery_signals` group is present, not complete. Over a window reaching
  back past the start dates in the caveat above, the earlier days are
  unobserved — which is a thinner reading, not a reassuring one.
