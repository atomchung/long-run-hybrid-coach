# Coaching evaluation

How this repository evaluates coaching judgment. Prose in
[SKILL.md](../.agents/skills/garmin-coach-loop/SKILL.md) can be satisfied word by word
while the answer still slides — a review that reports completion as progress, a plan
that changes because the athlete asked twice — so the behaviour is held by cases
rather than by the wording that is supposed to produce it.

This does not call an LLM API ([AGENTS.md](../AGENTS.md)), and it does not grade
product mechanics that code and tests already own.

The case format and the discipline around it are in force now. Suites, recorded
verdicts and run comparison arrive with the harness in #86; this file is the contract
it lands into, so that what already exists here is not rebuilt beside it.

## Where things live

| | holds |
| --- | --- |
| this directory | anonymous cases, shared rubrics, schemas, and the deterministic tooling that runs them |
| [`ab/`](ab) | one fixed turn asked of several context builds at once, to show whether a change to what the coach reads changed what it can say |
| external run store, normally `~/.local/share/garmin-coach-loop/evals/runs/` | real PlanState, private context, executor answers, human reviews, and reports |

Nothing identifying an athlete or reproducing their training history is committed, and
an eval never writes the real PlanState.

## A case

One JSON file per case in [`cases/`](cases), named after its `case_id`. The case is
the unit — a suite selects cases, it does not contain them.

| field | meaning |
| --- | --- |
| `case_id` | Stable identifier, and the file name. Keep it when the product question is unchanged, even if the wording moves. |
| `issues` | The issues this case is evidence for. |
| `mode` | The DecisionEvent mode the turn belongs to. |
| `scenario` | What the athlete asks, and when. |
| `given` | The evidence in play, as facts. Nothing here is a verdict. |
| `evidence_fields` | The contract fields the answer has to actually read. Every path is checked against `contracts/` — a renamed field fails the case, not just the code. |
| `expected.conclusion` | The finding a competent answer reaches. |
| `expected.must_state` | What the answer has to contain to be useful. |
| `expected.must_not_state` | The claims that make the answer wrong even if it sounds right. |
| `fails_if` | How a too-agreeable or over-reacting coach fails this case. |

`expected` and `fails_if` are per case on purpose. A shared rubric catches the
failures that repeat across cases; it cannot catch the one thing only this scenario
knows — that here the evidence supports moving the sets rather than the load.

## A suite

A suite is a named set of case ids plus what is graded the same way everywhere:
scoring dimensions, the communication contract, critical failure tags, capability
gates, and the comparison policy. It carries its own version.

A suite must not restate a case's scenario, evidence, or assertions. Two copies of a
case drift, and the copy inside the suite is the one nobody edits.

## Writing a case

Add or change a case only after real use exposes a concrete uncertainty or failure;
the behaviour case set is tracked in #25. Bump the suite version when the evidence or
the answer contract changes; a new suite version starts a new first run. Never edit an
accepted historical run to match a newer rubric.

**A case can be contaminated by the rule it is supposed to test.** One case here
scored the answer against *"started and ran short means drop the weight, not the
density"* — the mapping under test, written into the ruler, so a coach reasoning by
rule would have passed the case that existed to catch it. Write `expected` and
`fails_if` from what this scenario's evidence supports, never from a general mapping
between a completion state and an adjustment.

Do not add a judge, score, router, readiness model, data field, or product rule to
make a case easier to run or to grade.

## Grading

Two views of the same verbatim answer, and neither overrides the other:

- **The athlete** judges whether the conclusion is clear, reads naturally in the
  language and register they used, and gives an action they would follow. They are
  not expected to know the coaching key.
- **The frozen contract** checks evidence honesty, stated uncertainty, protection of
  the primary adaptation, and critical failures.

Preference cannot pass a technically wrong answer, and technical correctness cannot
pass an unreadable or unactionable one.

Verdicts are `pass`, `partial`, `fail`, or `disputed`, per case. There is no weighted
total: several small wins must not hide one primary-adaptation failure. Retain the
failing case, its tag, and the quoted answer text. There is no LLM judge — add one
only if retained answers prove human review is the bottleneck.

Score the decision and the explanation separately. A right call for an unstated
reason does not pass, because the next turn will not repeat it.

## Running

Running, freezing, recording and comparing runs belong to the harness in #86, together
with the run-store layout above. One thing it owes the rest of this file: a run must
freeze its own suite and the exact Skill and reference text it ran against, or a later
edit rewrites what a historical result meant.

Until it lands, a run is manual and unrecorded — give the case's `scenario` and `given`
to the Coach as a turn and score the answer by hand. That is the gap. A manual run
cannot be replayed, so it cannot show whether a change improved anything or only moved
it, which is what a prompt change most needs to prove.

[`ab/`](ab) closes that gap for one narrower question, and only that one: it asks a fixed
turn of several *context builds* at once, freezes each packet by hash, and records the
answers immutably beside the model that gave them. It does not run the behaviour cases
above and it does not replace the harness in #86 — it borrows the discipline that harness
owes (freeze the run, name the executor, never edit a recorded answer) for the one
comparison a context change needs.

`python3 -m unittest discover -s tests -p 'test_*.py'` checks only that cases are well
formed and still name real contract fields.

## What does not belong here

The rubric says what a good answer reads like — its register, its ordering, whether
it translated field names into words the athlete can act on. That is grading, and it
stays in the suite. It reaches `SKILL.md` only under [AGENTS.md](../AGENTS.md) 12,
which asks for the concrete run where the coach failed without it. The rubric
describing an expectation is not itself that evidence.
