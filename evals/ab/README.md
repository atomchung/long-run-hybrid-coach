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
