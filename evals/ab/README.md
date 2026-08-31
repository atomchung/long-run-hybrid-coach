# One question, three context builds

`evals/README.md` names the gap this fills:

> A manual run cannot be replayed, so it cannot show whether a change improved anything
> or only moved it, which is what a prompt change most needs to prove.

A context change needs to prove the same thing, and issue #240 §3 is one. A
`cycle_sessions` row from before the previous calendar week stopped carrying what its
session prescribed. Whether that costs the coach anything is a question about answers,
not about characters — so this asks the same coaching question of the build before the
change, the build after it, and whatever this checkout does today, and keeps the three
answers.

It runs on the reads in [`tests/coach_session_scenarios.py`](../../tests/coach_session_scenarios.py)
rather than on reads of its own. Those are already committed, already deterministic, and
already what `startCoachSession` hands back; a second set here would be the copy nobody
edits.

## What is held fixed, and what varies

| | |
| --- | --- |
| held fixed | the question, the served orchestration prompt, the training reference, the model |
| varies | what the tool call handed back — and inside that, two fields |

One model across the arms is not a nicety. Two models would make the comparison a
comparison of models, so every answer records the model that gave it and `report`
refuses to compare a run answered by more than one.

## The arms

| arm | is |
| --- | --- |
| `prose-on-every-row` | `c73b030`, after #246 and before #247: every cycle row carries its prescription |
| `prose-window-two-weeks` | `9a24df8`, after #247: a row from before the previous week carries neither prescription nor activity id |
| `working` | whatever this checkout builds now |

A frozen arm is stored as an **overlay**: the fields it differs in — `cycle_sessions`,
`recent_actuals` and `segment_execution` here — plus a digest of everything else in that
read at the moment it was captured. An arm's response is this checkout's response with
those fields put back. That is only an honest reconstruction while the rest of the read
has not moved, so the digest is checked on every use — a build that changes a third field
stops the comparison with a message instead of quietly becoming a different comparison.
Widening this suite's `overlay_fields` is the fix when the third field is deliberate;
re-capturing is the fix when it is not.

Which fields an arm may overlay is the suite's own choice, not this module's: a suite
names them in a top-level `overlay_fields` list, and a suite silent on the question gets
`cycle_sessions` and `recent_actuals` — the pair every arm before this option existed was
captured against. A suite comparing a different context shape — how a strength label
reads, say — names `strength_execution` instead, and both `capture-arm` and a run built
from that suite overlay exactly that field and nothing else.

`suite.json` is now the worked example of a deliberate widening rather than of the
fallback. #306 gave `segment_execution` a second shape for sessions 14 to 28 days old,
which is a third field these arms would otherwise have had to be identical in forever, so
the suite names all three and both frozen arms were re-captured at their own commits
against the wider list. Nothing the coach reads moved: for the seven reads this suite
uses the field is `null` under every arm, so every packet is byte-for-byte what it was and
the suite version does not turn over. What changed is what a later build is allowed to
move without stopping the comparison.

Whether `working` currently reads byte-identical to a frozen arm is not a claim worth
freezing into this file -- the next builder change would make it stale the same way it
made the arms themselves stale. Read it live, per run, off that run's own manifest under
`arms_identical_to_live`. A run where the instrument reads zero there is worth having: it
says the differences the report shows afterwards are the change and not the harness.

### Re-capturing an arm

`capture-arm` freezes whatever the working tree builds right now, so an arm is captured
from a checkout of the commit it names:

```bash
git worktree add --detach /tmp/gcl-arm c73b030
cp tests/coach_session_scenarios.py /tmp/gcl-arm/tests/
mkdir -p /tmp/gcl-arm/evals/ab && cp evals/__init__.py /tmp/gcl-arm/evals/ && cp evals/ab/*.py evals/ab/suite.json /tmp/gcl-arm/evals/ab/
(cd /tmp/gcl-arm && python3 -m evals.ab.harness capture-arm --arm prose-on-every-row --commit c73b030 --note "…")
cp -R /tmp/gcl-arm/evals/ab/arms/prose-on-every-row evals/ab/arms/
git worktree remove --force /tmp/gcl-arm
```

The scenario definitions travel from this checkout because they are the *inputs* — the
question being asked of the older product code. Everything else is that commit's own.
This is a developer action on a full clone; the test suite only reads the result, so a
shallow CI checkout never needs the history.

The suite copied into `/tmp/gcl-arm/evals/ab/` is what decides which fields get frozen —
`capture-arm` reads that suite's `overlay_fields` the same way `create-run` does. Pass
`--suite <path>` when the arm belongs to a suite other than this directory's own.

An overlay file does not have to come from `capture-arm` at all. Nothing in how a frozen
arm is loaded asks who wrote it, only that its `untouched_sha256` still matches — so a
hand-built hypothetical (what would this read look like with a field written a third way)
is as valid an overlay as a captured commit, as long as it is honest about the digest.

### When a new top-level context key stops every arm

This procedure cannot repair that one, and the reason is worth knowing before reaching
for it: the digest a capture writes is the digest of *that* checkout's response, and a
checkout of the arm's own commit has no such key to hash. Running it reproduces the file
already on disk — verified for #28, where capturing `prose-on-every-row` at `c73b030`
rewrote all seven files byte for byte and left them still failing.

What repairs it is the operation the hand-built arms need anyway: keep the overlay
exactly as it is and recompute `untouched_sha256` against the current build. That is not
a weaker claim than a re-capture. The overlay is still the old commit's answer, the
digest still pins the arm to one build, and the diff still has to show that nothing but
the digest moved — which is the review either way.

Issue #309 turned that from a careful manual diff-check — the shape PR #308 first did by
hand — into a `capture-arm` mode:

```bash
python3 -m evals.ab.harness capture-arm --refresh-digest --arm prose-on-every-row
python3 -m evals.ab.harness capture-arm --refresh-digest
```

The first repairs one arm; the second, with no `--arm`, repairs every frozen arm the
suite declares. Neither takes `--commit` or `--note` — capture provenance is not what
moved, so both are refused rather than silently ignored. Nothing but `untouched_sha256`
can change, so an arm already honest against this build writes back byte-for-byte the
same file it started as — the review is still reading the diff, just with nothing left
to read on the common path. `--suite <path>` picks whose frozen arms and
`overlay_fields` to check against, the same as everywhere else in this file.

## Running one

```bash
python3 -m evals.ab.harness create-run --run-id 2026-08-25-a
```

Packets go to `~/.local/share/garmin-coach-loop/evals/ab/runs/<run-id>/packets/`, one per
arm per turn, outside the repository because they carry model answers next to them. A
packet contains the served texts, the whole tool result, the athlete's question, and
nothing that says which arm it is — the mapping is in the run manifest, which whoever
answers is asked not to open.

`create-run` defaults to `suite.json` beside this file. `--suite <path>` points it at any
other suite JSON instead — the run copies that file in and hashes it into the manifest,
so everything downstream (`record-response`, `report`) reads the run's own copy and never
needs the original path again. This is what lets a later measurement — comparing how a
strength label reads under three context shapes, say — live as its own suite file rather
than editing this one out from under the run it names. Those files are in
[`suites/`](suites), one per question, each naming its own arms, `overlay_fields` and
dimensions; the arms they were captured against sit under `arms/` beside this suite's.

Answer each packet as the coach, out of process, then:

```bash
python3 -m evals.ab.harness record-response --run <run-dir> --packet <packet-id> --answer-file answer.txt --provider anthropic --model claude-opus-5
```

```bash
python3 -m evals.ab.harness report --run <run-dir>
```

Every packet's own `instructions` ask the answer to close with a line of its own —
`packet: <packet_id>`, the id copied from that packet's own `packet_id` field.
`record-response` checks that line against the packet id given on the command line and
strips it from what gets stored, so it never reads as part of the answer or counts toward
`answer_characters`. This is what closes the gap issue #322 found: a blind-answer run
whose packet path did not resolve, so the answerer read a leftover packet from somewhere
else and answered that instead, with nothing to say the answer and the packet it was
filed under were ever the same content. Whether a packet asks for the line is read from
that packet's own file, so a run already in progress when this check landed keeps
accepting the answers its own packets actually asked for.

An answer is written once per **sample**. `--sample <n>` names which attempt at a packet
this is and defaults to `1`, so the common case — one answer per packet, `--sample` never
mentioned — writes and reports exactly as it always has. Recording `--sample 2` and
`--sample 3` against the same packet asks the same model the same question again, to read
how much its own wording moves on repetition alone rather than on anything the arms
changed — the question issue #86 opens. Re-recording a sample already on disk is refused,
same as before, and so is recording against a packet whose bytes no longer match the
manifest. `report` lists every sample of an answered packet side by side, under the same
arm; it does not average them or reduce them to a verdict, for the same reason it does not
average across arms — the spread is what a reviewer is there to read.

## What the report measures, and what it does not

Six counts per answer, and none of them is a score. `evals/README.md` rules out an LLM
judge and a weighted total; this rules out both again.

| | |
| --- | --- |
| `figures_not_in_the_context` | every pace, load, distance, duration or heart rate the answer states that nothing it was handed carries. **Read the list, do not total it** — a figure the model derived lands here too |
| `prescribed_figures_stated / in_this_arm / total` | of what the target session actually asked for: how much the answer said, how much this arm carried anywhere, and how much there was |
| `questions_asked` | an answer that cannot read something asks for it |
| `uncertainty_markers` | or says it cannot |
| `answer_characters` | length, so a longer answer is not mistaken for a better one |
| `context_characters` | what the arm cost, beside what it bought |

The figure check matches on the **number**, not on the number and its unit, and treats the
athlete's own question as carried. Both make it under-report invention rather than
over-report it, which is the only way a check like this stays worth reading. A pace stored
as `average_pace_sec_per_km: 333` supports an answer that says `5:33`, and a distance
stored as `meters: 8000` supports one that says 8 公里; without those derivations every
correctly-cited pace would be flagged.

The verdict is still a person's, against the dimensions in `suite.json` and the two views
`evals/README.md` describes.

## What a turn is chosen for

The suite is half sharp cases and half controls, and the controls are not padding.

A **sharp** case names a session whose specification is stated on its own cycle row and
nowhere else in the read — week one's 12 km long run in a cycle that lengthened it every
week, or a shakeout scheduled once. Dropping the row's text drops those figures outright.

A **control** names a session the read states somewhere else anyway: a lift reported set
by set, so `movement_history` carries what was prescribed; a session the measurement week
repeats by design; a week recent enough that every arm carries it. If a control
moves between arms, the harness is measuring noise.

One turn exists to hold a stated limit visible: `cycle-review-never-reviewed` runs on a
plan whose week was never rolled, so week one is still the stored week and its
prescriptions are in `plan_state` whatever the cycle row says. The arms differ there and
it costs the coach nothing — which is worth seeing beside the cases where it costs
something, and is why the other three late-cycle reads roll the week first.

## Asking the same packet again

`--sample` above records a second and a third answer to a packet already answered. The
arms ask what a context change bought; repeating one packet asks something else — how
much of an answer was the model rather than the context. A difference between two arms is
only worth reading once the same arm is known to say the same thing twice.

This is not owed by every change. Repeat when:

- **the A/B verdict was close, or judgment-heavy** — the arms differ in what the coach
  concluded rather than in whether a figure was there to cite;
- **a suite is running for the first time** — nothing yet says how wide that question's
  own spread is, so a difference between arms has no baseline to stand against;
- **review raised drift** — someone reading the run asked whether an answer would hold on
  a second attempt.

A change whose arms differ by whether the context carries a number at all does not need
repeats. The packet either has it or it does not, and one answer per arm shows that.

**Three samples of every packet a sharp turn produced — each arm, not only the arm
expected to answer. Controls stay at one.** A control is there to show the arms moving
together where the evidence is identical, which is noise *across* arms; answering it three
times measures a within-packet spread it was never chosen to hold, and triples what a
reviewer has to read to learn nothing the control was for. The sharp packets are where the
arms are meant to differ, so they are where it matters whether the difference is stable.

Every sample is answered by the same model as the rest of the run. That is the one-model
rule the arms are already held to, and it needs no amendment to cover samples: `report`
collects the executor of every sample into one set, so a second model recorded against
sample 2 makes the whole run not comparable rather than just that packet.

### Reading the spread

In this order, because an earlier row outranks every row below it:

| read | asking |
| --- | --- |
| the substantive conclusion | did any sample reach a different answer — a different count, a different direction, a refusal where another answered |
| confidence calibration | of the samples that concluded the same thing, did they hold it with the same confidence |
| decision-relevant escalations | did one sample alone invoke something that changes what gets scheduled — the plan's own adjustment trigger, say |
| the counted figures | last, and as lists rather than as totals |

Nothing here is averaged and nothing is scored. `evals/README.md` rules out a weighted
total and an LLM judge, and three samples of one packet are no more summable than two arms
of one turn: a mean over three answers hides the one that differed, which is the only
thing a repeat was run to find.

Whether a decision-relevant escalation deserves a count of its own, the way figures do, is
open on #86. Until that is decided it is found by reading.

### The 2026-08-28 run

`2026-08-28-late-cycle-segment-window`, the #306 acceptance, is the worked example: three
samples of each of the four sharp packets — two turns, two arms — with the control turn
answered once, one model throughout.

- **The conclusion held.** All three compact-arm samples read the same three evidenced
  repetitions out of five, per-repetition times identical across samples, none inventing a
  fourth or fifth, and each saying the count is its own reading of the rows rather than a
  label the provider supplied. All three pre-roll samples decline; none invents a count.
- **Confidence calibration drifted widest**, on byte-identical context: one sample says
  quantifying *how much* progress cannot be done, another says the progress signal is
  clear.
- **One sample in three escalated.** On the progress turn, one compact-arm sample alone
  invokes the plan's own adjustment trigger — two consecutive weeks with the primary
  stimulus missed — which the other two never raise. That is drift that could change what
  gets scheduled, not drift in wording.
- **A minority framing appeared once**: one sample reads the two missing repetitions as
  possibly run but unsegmented, where the other two read the session as having ended after
  three, which the 44-minute accounting supports.
- **One answer was right by guess**: a pre-roll sample states the session probably did not
  run all five, from an arm carrying no segment evidence at all. The compact arm turns the
  same sentence into right by evidence — the arm difference the run existed to measure,
  and readable as a guess only because the repeat put it beside two samples that declined.
- **The counts spread widest where they mattered least**: `uncertainty_markers` reads
  3/1/1 across three pre-roll samples whose hedging reads the same, and
  `figures_not_in_the_context` reads 2/4/5 across three compact samples that agree on the
  answer and differ only in how much arithmetic they show. Honest as counts, misleading as
  scores — which is why they are read last.

### The 2026-08-31 run

`peaks-2026-08-31`, the `peaks-and-breaks` suite's first run and issue #222's first
measurement: three turns, two arms, one model throughout, with the planning turn repeated
three times per arm because the arms disagreed on a fact there and one sample cannot tell
a finding from a coin toss.

The suite asks a narrow question. `training_history`'s monthly buckets already ship
(#101). Does a coach read the athlete's ceiling and their stop out of those buckets on its
own, or does stating them change what it says? The read is
`25_plan_cycle__a_peak_and_a_break_in_the_history`: ten uploaded months whose highest is
February at 157.0 km, whose longest single run is 21.0 km on 2026-02-15, and which hold
a forty-nine-day stop from 2026-03-09 to 2026-04-26 — a stop **no calendar month is empty
for**, because it begins inside March (41.0 km) and ends inside April (21.9 km). The
buckets can only show two light months. The whole arm difference is 625 characters.

- **The planning turn is where it showed, and it showed as spread rather than as a wrong
  answer.** Asked to size the next weeks' volume, the three months-only samples gave three
  different readings: one rejected the stored 32 km baseline as stale, one accepted it, and
  one declined to state a weekly total at all. The three peaks-and-breaks samples all
  landed at 30–32 km and all said why — two of them naming the athlete's own recent
  longest run as the check on the long run's length. Same question, same model, same
  everything but 625 characters.
- **One months-only sample rejected the baseline by inventing a break.** It wrote that
  there was a period with no running record before the week of 8/10 and planned
  conservatively off that. There was not: July holds 14 runs and 118.0 km in the same
  context's own `training_history`. What it read instead was `baseline_evidence`'s
  weekly-volume rows, which report five consecutive weeks at 0 km because the provider's
  account does not reach back that far — **the exact failure #101 was opened on, still
  reachable after the monthly buckets that were meant to answer it.** No peaks-and-breaks
  sample made the claim, and not because the field is cited: none of the three mentions the
  real March–April stop on this turn. Naming the one break that exists is simply what makes
  "there was a recent break" checkably false.
- **The stop only gets described when it is stated.** Asked what this year looked like, the
  months-only answer says the two months dropped and that nothing in the data records why —
  honest, and as far as the buckets go. The peaks-and-breaks answer gives the dates and the
  length: interrupted for forty-nine days, 2026-03-09 to 2026-04-26.
- **Cause restraint held in all eight answers.** Nothing in either arm says why the athlete
  stopped, and no answer asserted a reason. Two asked.
- **The long-run turn reached the same prescription from both arms** — 13–15 km — and
  differed only in what the number was checked against: last week's session in one, the
  athlete's own 14.0 km since returning in the other. An arm difference in the reasoning
  and not in the answer is worth recording as exactly that.
- **Every `figures_not_in_the_context` count on this run is a false positive**, and they are
  a good illustration of why the counts are read last: 99, 29 and 101 are `94-99 分鐘`,
  `27-29 公里` and `96-101 分鐘` — arithmetic on figures the context did state.

**What this does not settle.** Three samples of one turn, one model, one scenario. It says
the months-only arm's weekly-volume reading is unstable on this read and that one instance
of #101's failure survives the buckets; it does not say how often, and it does not say the
two proposed fields are the only fix. Whether either belongs in the context is a budget
decision (AGENTS.md 13) this run informs and does not make.
